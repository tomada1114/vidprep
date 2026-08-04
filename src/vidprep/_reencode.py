"""How a cut plan becomes a file: the renderer protocol and the v1 renderer.

v1 re-encodes everything (design.md §1, decision 2): the kept intervals are cut
out with ``trim``/``atrim`` and joined with ``concat``, the video through
libx264 at the profile's CRF and preset, the audio from ``audio/processed.wav``
rather than from the container, because the processed audio is what every other
stage was built on (design.md §2.1).

Boundaries get a fade in and a fade out, not a crossfade (design.md §1,
decision 9). A crossfade overlaps its two sides and so shortens the result by a
fade at every boundary, which would leave the timeline mapping — and every
subtitle timed by it — wrong by an amount that grows with the number of cuts.
``afade`` changes no length at all, which is why the output length can be
checked against arithmetic afterwards.

Rendering goes through :class:`Renderer` so the cut-without-re-encoding
implementation planned for later (design.md §8) can take its place without the
stage noticing.
"""

from __future__ import annotations

import math
import shutil
import tempfile
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

from . import _ffmpeg
from . import audio as audio_module
from . import project as project_module
from .errors import InvariantViolationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import Profile

#: An interval of the original timeline, in seconds.
type Interval = tuple[float, float]

VIDEO_CODEC = "libx264"
PIXEL_FORMAT = "yuv420p"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "320k"

VIDEO_STREAM = "v"
AUDIO_STREAM = "a"

WORKSPACE_PREFIX = ".render-"
MS_PER_SECOND = 1000.0

#: Tolerances of verification-plan.md §8. The length may be off by one frame,
#: which is the smallest unit the container can express.
MAX_AV_DELTA_MS = 50.0
LOUDNESS_TOLERANCE_LUFS = 0.5
DELTA_DECIMALS = 3
LUFS_DECIMALS = 2

#: Frame length assumed when the container reports no usable frame rate.
DEFAULT_FRAME_MS = 40.0

#: Cut boundaries are recorded to milliseconds, so a frame index computed from
#: one is a whole number well before this many decimals; rounding to it keeps
#: float arithmetic from turning 51.99999999 into a frame of its own.
ALIGNMENT_DECIMALS = 6


@dataclass(frozen=True, slots=True)
class RenderJob:
    """One rendering: what to keep from where, and where to put it."""

    source: Path
    keep: tuple[Interval, ...]
    audio: Path
    profile: Profile
    out: Path


@dataclass(frozen=True, slots=True)
class RenderResult:
    """What a renderer produced, measured on the file it wrote."""

    renderer: str
    output: Path
    target_lufs: float
    expected_duration: float
    duration: float
    video_duration: float
    audio_duration: float
    integrated_lufs: float
    true_peak_dbtp: float

    @property
    def delta_ms(self) -> float:
        """How far the output length is from the arithmetic, in milliseconds."""
        return abs(self.duration - self.expected_duration) * MS_PER_SECOND

    @property
    def av_delta_ms(self) -> float:
        """How far the two streams' lengths are apart, in milliseconds."""
        return abs(self.video_duration - self.audio_duration) * MS_PER_SECOND


class Renderer(Protocol):
    """How the kept intervals of a source become a file (design.md §5.5)."""

    def render(
        self,
        source: Path,
        keep: Sequence[Interval],
        audio: Path,
        profile: Profile,
        out: Path,
    ) -> RenderResult:
        """Write the *keep* intervals of *source* to *out*, and measure them.

        The implementation is expected to publish *out* atomically and to leave
        any previous version of it untouched when anything goes wrong.
        """
        ...


def _seconds(value: float) -> str:
    """Format a number of seconds for an ffmpeg filter argument."""
    return f"{value:.6f}"


def align_to_frames(cuts: Sequence[Interval], fps: str) -> list[Interval]:
    """Pull every cut in onto the frame grid of a *fps* video.

    A frame is either kept whole or dropped whole, so a cut that begins in the
    middle of one is rounded by ffmpeg — and the rounding of each cut adds to
    the next, until an output with fifty boundaries is several frames away from
    the length its cut list says it should have. Rounding here instead makes
    every kept interval a whole number of frames long, so the arithmetic and
    the file agree however many cuts there are.

    Rounding goes inwards, never outwards: a cut can only get shorter, so a
    boundary that detection cleared of speech (verification-plan.md §7) cannot
    grow into a word here. Cuts shorter than a frame disappear, which is the
    same statement — there is no frame they could remove.

    Args:
        cuts: The approved cuts, in original-timeline seconds.
        fps: Frame rate as the ``num/den`` string the manifest records.

    Returns:
        The cuts that survive, snapped to frame boundaries.
    """
    rate = Fraction(fps)
    if rate <= 0:
        return list(cuts)
    aligned: list[Interval] = []
    for start, end in cuts:
        first = math.ceil(round(start * rate, ALIGNMENT_DECIMALS))
        last = math.floor(round(end * rate, ALIGNMENT_DECIMALS))
        if first < last:
            aligned.append((float(first / rate), float(last / rate)))
    return aligned


@dataclass(frozen=True, slots=True)
class ReencodeRenderer:
    """Re-encodes the kept intervals into a new file (design.md §1, decision 2).

    Attributes:
        fps: Frame rate of the source, as the ``num/den`` string the manifest
            records. It is forced on the output so cutting cannot turn a
            constant frame rate into a variable one, and it sets the tolerance
            the output length is checked against.
    """

    NAME: ClassVar[str] = "ReencodeRenderer"

    fps: str = "25/1"

    @property
    def frame_ms(self) -> float:
        """One frame, in milliseconds — the tolerance of the length check."""
        rate = float(Fraction(self.fps))
        return MS_PER_SECOND / rate if rate > 0 else DEFAULT_FRAME_MS

    def filtergraph(self, keep: Sequence[Interval], fade: float) -> str:
        """Return the ``trim``/``atrim`` + ``concat`` graph for *keep*.

        Each kept interval is faded in and out over *fade* seconds without
        changing its length; an interval too short to hold two fades gets
        shorter ones rather than overlapping ones.
        """
        chains: list[str] = []
        labels: list[str] = []
        for index, (start, end) in enumerate(keep):
            length = end - start
            span = min(fade, length / 2)
            chains.append(
                f"[0:{VIDEO_STREAM}]trim=start={_seconds(start)}:end={_seconds(end)},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
            chains.append(
                f"[1:{AUDIO_STREAM}]atrim=start={_seconds(start)}:end={_seconds(end)},"
                "asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d={_seconds(span)},"
                f"afade=t=out:st={_seconds(length - span)}:d={_seconds(span)}"
                f"[a{index}]"
            )
            labels.append(f"[v{index}][a{index}]")
        chains.append(
            f"{''.join(labels)}concat=n={len(keep)}:v=1:a=1"
            f"[out{VIDEO_STREAM}][out{AUDIO_STREAM}]"
        )
        return ";".join(chains)

    def encode_command(self, job: RenderJob) -> list[str]:
        """Return the single ffmpeg invocation that produces ``job.out``."""
        render = job.profile.render
        return [
            _ffmpeg.FFMPEG,
            *_ffmpeg.WRITING,
            "-i",
            str(job.source),
            "-i",
            str(job.audio),
            "-filter_complex",
            self.filtergraph(job.keep, render.boundary_fade),
            "-map",
            f"[out{VIDEO_STREAM}]",
            "-map",
            f"[out{AUDIO_STREAM}]",
            "-c:v",
            VIDEO_CODEC,
            "-crf",
            str(render.crf),
            "-preset",
            render.preset,
            "-pix_fmt",
            PIXEL_FORMAT,
            "-r",
            self.fps,
            "-c:a",
            AUDIO_CODEC,
            "-b:a",
            AUDIO_BITRATE,
            "-movflags",
            "+faststart",
            str(job.out),
        ]

    def commands(self, job: RenderJob) -> list[list[str]]:
        """Return every external command a run of *job* executes."""
        return [
            self.encode_command(job),
            _ffmpeg.duration_command(job.out),
            _ffmpeg.stream_duration_command(job.out, VIDEO_STREAM),
            _ffmpeg.stream_duration_command(job.out, AUDIO_STREAM),
            audio_module.measurement_command(job.out, job.profile.audio.loudnorm),
        ]

    def render(
        self,
        source: Path,
        keep: Sequence[Interval],
        audio: Path,
        profile: Profile,
        out: Path,
    ) -> RenderResult:
        """Re-encode *keep* into *out*, verified before it replaces anything.

        Raises:
            InvariantViolationError: If the file that came out does not match
                the arithmetic (verification-plan.md §8); *out* keeps whatever
                it held before in that case.
        """
        return self.run(RenderJob(source, tuple(keep), audio, profile, out))

    def run(self, job: RenderJob) -> RenderResult:
        """Encode *job* inside a workspace, verify it, then publish it.

        The file is built next to its destination and moved there in one step,
        so an ffmpeg failure — or a result that fails verification — leaves the
        previous render in place (design.md §6).

        Raises:
            InvariantViolationError: If the result fails verification.
        """
        job.out.parent.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(dir=job.out.parent, prefix=WORKSPACE_PREFIX))
        try:
            produced = replace(job, out=workspace / job.out.name)
            _ffmpeg.run(self.encode_command(produced))
            result = self._measure(produced)
            _verify(result, self.frame_ms)
            project_module.atomic_replace(produced.out, job.out)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        return replace(result, output=job.out)

    def _measure(self, job: RenderJob) -> RenderResult:
        """Read back what was written: lengths, and the loudness it kept."""
        targets = job.profile.audio.loudnorm
        measured = audio_module.measure(job.out, targets)
        return RenderResult(
            renderer=self.NAME,
            output=job.out,
            target_lufs=targets.i,
            expected_duration=sum(end - start for start, end in job.keep),
            duration=_ffmpeg.duration(job.out),
            video_duration=_ffmpeg.stream_duration(job.out, VIDEO_STREAM),
            audio_duration=_ffmpeg.stream_duration(job.out, AUDIO_STREAM),
            integrated_lufs=measured.integrated_lufs,
            true_peak_dbtp=measured.true_peak_dbtp,
        )


def _verify(result: RenderResult, frame_ms: float) -> None:
    """Check the completion conditions of verification-plan.md §8.

    All three are checked before any of them is reported, because a render is
    expensive enough that "and this is also wrong" is worth knowing in one go.

    Raises:
        InvariantViolationError: If the length drifted by more than a frame,
            the two streams disagree by more than
            :data:`MAX_AV_DELTA_MS`, or loudness normalisation did not survive
            the cuts. The work is discarded rather than published.
    """
    problems: list[str] = []
    if round(result.delta_ms, DELTA_DECIMALS) > round(frame_ms, DELTA_DECIMALS):
        problems.append(
            f"the output is {result.duration:.3f}s where the kept intervals add "
            f"up to {result.expected_duration:.3f}s "
            f"(delta {result.delta_ms:.1f}ms > {frame_ms:.1f}ms, one frame)"
        )
    if round(result.av_delta_ms, DELTA_DECIMALS) > MAX_AV_DELTA_MS:
        problems.append(
            f"the video stream lasts {result.video_duration:.3f}s and the audio "
            f"stream {result.audio_duration:.3f}s "
            f"(delta {result.av_delta_ms:.1f}ms > {MAX_AV_DELTA_MS:g}ms)"
        )
    drift = abs(result.integrated_lufs - result.target_lufs)
    if round(drift, LUFS_DECIMALS) > LOUDNESS_TOLERANCE_LUFS:
        problems.append(
            f"the output measures {result.integrated_lufs:.2f} LUFS against a "
            f"target of {result.target_lufs:.1f} "
            f"(off by {drift:.2f} > {LOUDNESS_TOLERANCE_LUFS:g})"
        )
    if problems:
        msg = f"{'; '.join(problems)}; {result.output.name} was left untouched"
        raise InvariantViolationError(msg)
