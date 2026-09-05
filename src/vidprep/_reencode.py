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

The one thing here that does change a length is the closing fade to black
(:class:`Closing`), and it changes it by a number the stage states in advance,
so the check survives.

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

#: Filtergraph labels. ``concat`` writes the finished streams under ``FINAL``
#: unless a :class:`Closing` follows it, in which case it hands them over under
#: ``JOINED`` and the closing writes ``FINAL`` — so what is mapped to the file
#: is named the same either way.
FINAL = "out"
JOINED = "join"

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


#: Holds the last frame instead of inserting a black one, so a padded output
#: darkens from the picture it ended on rather than cutting to black.
FREEZE_MODE = "clone"

#: ``tpad`` works out how many frames to add from the frame duration of what it
#: is given, and ``concat`` hands it a stream it cannot read one from: measured
#: against ffmpeg 7.1.1, ``tpad`` behind a ``concat`` adds nothing at all and
#: says nothing about it, leaving the audio padded and the video not. Pinning
#: the rate first — to the same one the encoder is told to write — makes the
#: hold happen.
RATE_FILTER = "fps"


@dataclass(frozen=True, slots=True)
class Closing:
    """The fade to black an output ends on, and the material it needs.

    The fade may not start while somebody is still talking, so it is measured
    back from the end of a tail the caller has already checked is silent: the
    ``tail`` given to :meth:`plan` is how long the kept material runs past the
    last word. Where that is shorter than the fade — a recording stopped the
    moment the sentence did — the difference is made up by holding the last
    frame, because a recording cannot be asked for footage it does not have
    and a hard cut to black is the thing the fade exists to avoid.

    Attributes:
        fade: How long the fade lasts, in seconds; ``0`` disables both.
        pad: How much material to hold on after the last kept interval, in
            seconds, so the fade has somewhere to happen.
    """

    fade: float = 0.0
    pad: float = 0.0

    @classmethod
    def plan(cls, fade_out: float, tail: float) -> Closing:
        """Return the closing for material running *tail* seconds past the last word."""
        if fade_out <= 0:
            return cls()
        return cls(fade=fade_out, pad=max(0.0, fade_out - max(tail, 0.0)))

    @property
    def is_empty(self) -> bool:
        """Whether this closing leaves the joined streams alone."""
        return self.fade <= 0 and self.pad <= 0

    def chains(self, total: float, fps: str) -> list[str]:
        """Return the filter chains that close an output *total* seconds long.

        Empty when there is nothing to do, which is what keeps the graph of a
        project with ``fade_out`` at ``0`` exactly what it was before.

        Args:
            total: How long the output is once this closing is applied.
            fps: Frame rate to normalise to before holding a frame; see
                :data:`RATE_FILTER`.
        """
        if self.is_empty:
            return []
        span = min(self.fade, total)
        start = max(0.0, total - span)
        video: list[str] = []
        audio: list[str] = []
        if self.pad > 0:
            video.append(f"{RATE_FILTER}={fps}")
            video.append(
                f"tpad=stop_mode={FREEZE_MODE}:stop_duration={_seconds(self.pad)}"
            )
            audio.append(f"apad=pad_dur={_seconds(self.pad)}")
        if span > 0:
            video.append(f"fade=t=out:st={_seconds(start)}:d={_seconds(span)}")
            audio.append(f"afade=t=out:st={_seconds(start)}:d={_seconds(span)}")
        return [
            f"[{JOINED}{VIDEO_STREAM}]{','.join(video)}[{FINAL}{VIDEO_STREAM}]",
            f"[{JOINED}{AUDIO_STREAM}]{','.join(audio)}[{FINAL}{AUDIO_STREAM}]",
        ]


#: A closing that does nothing, so ``filtergraph`` can default to one.
NO_CLOSING = Closing()


@dataclass(frozen=True, slots=True)
class RenderJob:
    """One rendering: what to keep from where, and where to put it.

    ``tail`` is how long the kept material runs past the last spoken word, in
    seconds; it decides how much of the closing fade has to be manufactured.
    """

    source: Path
    keep: tuple[Interval, ...]
    audio: Path
    profile: Profile
    out: Path
    tail: float = 0.0


@dataclass(frozen=True, slots=True)
class RenderResult:
    """What a renderer produced, measured on the file it wrote."""

    renderer: str
    output: Path
    target_lufs: float
    target_tp: float
    expected_duration: float
    duration: float
    video_duration: float
    audio_duration: float
    integrated_lufs: float
    true_peak_dbtp: float
    closing: Closing = NO_CLOSING

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

    def render(self, job: RenderJob) -> RenderResult:
        """Write the kept intervals of *job* to its output, and measure them.

        The implementation is expected to publish the output atomically and to
        leave any previous version of it untouched when anything goes wrong.
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

    def closing(self, job: RenderJob) -> Closing:
        """Return how *job* ends, held frames snapped onto the frame grid.

        ``tpad`` can only add whole frames, so it rounds a duration up on its
        own; rounding it here instead means the arithmetic the output is
        checked against is the length the file really gets, rather than one up
        to a frame short of it — and the picture and the sound are padded by
        the same number rather than drifting apart by the rounding.
        """
        closing = Closing.plan(job.profile.render.fade_out, job.tail)
        rate = Fraction(self.fps)
        if closing.pad <= 0 or rate <= 0:
            return closing
        frames = math.ceil(round(closing.pad * rate, ALIGNMENT_DECIMALS))
        return replace(closing, pad=float(frames / rate))

    def filtergraph(
        self, keep: Sequence[Interval], fade: float, closing: Closing = NO_CLOSING
    ) -> str:
        """Return the ``trim``/``atrim`` + ``concat`` graph for *keep*.

        Each kept interval is faded in and out over *fade* seconds without
        changing its length; an interval too short to hold two fades gets
        shorter ones rather than overlapping ones. *closing* is appended to the
        joined streams and is the only thing here that changes a length.
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
        total = sum(end - start for start, end in keep) + closing.pad
        tail = closing.chains(total, self.fps)
        joined = JOINED if tail else FINAL
        chains.append(
            f"{''.join(labels)}concat=n={len(keep)}:v=1:a=1"
            f"[{joined}{VIDEO_STREAM}][{joined}{AUDIO_STREAM}]"
        )
        return ";".join([*chains, *tail])

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
            self.filtergraph(job.keep, render.boundary_fade, self.closing(job)),
            "-map",
            f"[{FINAL}{VIDEO_STREAM}]",
            "-map",
            f"[{FINAL}{AUDIO_STREAM}]",
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

    def render(self, job: RenderJob) -> RenderResult:
        """Encode *job* inside a workspace, verify it, then publish it.

        The file is built next to its destination and moved there in one step,
        so an ffmpeg failure — or a result that fails verification — leaves the
        previous render in place (design.md §6).

        Raises:
            InvariantViolationError: If the result fails verification, or if the
                file that came out does not match the arithmetic
                (verification-plan.md §8); the output keeps whatever it held
                before in that case.
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
        closing = self.closing(job)
        return RenderResult(
            renderer=self.NAME,
            output=job.out,
            target_lufs=targets.i,
            target_tp=targets.tp,
            expected_duration=sum(end - start for start, end in job.keep) + closing.pad,
            duration=_ffmpeg.duration(job.out),
            video_duration=_ffmpeg.stream_duration(job.out, VIDEO_STREAM),
            audio_duration=_ffmpeg.stream_duration(job.out, AUDIO_STREAM),
            integrated_lufs=measured.integrated_lufs,
            true_peak_dbtp=measured.true_peak_dbtp,
            closing=closing,
        )


def _verify(result: RenderResult, frame_ms: float) -> None:
    """Check the completion conditions of verification-plan.md §8.

    All checks are performed before any of them is reported, because a render is
    expensive enough that "and this is also wrong" is worth knowing in one go.

    Raises:
        InvariantViolationError: If the length drifted by more than a frame,
            the two streams disagree by more than
            :data:`MAX_AV_DELTA_MS`, loudness normalisation did not survive the
            cuts, or the true peak exceeds its target. The work is discarded
            rather than published.
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
    if round(result.true_peak_dbtp, LUFS_DECIMALS) > round(
        result.target_tp, LUFS_DECIMALS
    ):
        problems.append(
            f"the output true peak is {result.true_peak_dbtp:.2f} dBTP against "
            f"a maximum of {result.target_tp:.1f} dBTP"
        )
    if problems:
        msg = f"{'; '.join(problems)}; {result.output.name} was left untouched"
        raise InvariantViolationError(msg)
