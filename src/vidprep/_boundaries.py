"""The waveform stills and the boundary digest video (design.md §5.6).

Both artifacts answer the same question — "what disappears here?" — from the
original material rather than from the rendered output, which is why the digest
is cut out of the source video: a boundary is only reviewable while the part
about to be removed is still in front of you.

One window is produced per cut, spanning the cut plus :data:`BOUNDARY_MARGIN`
seconds of context on each side and clamped to the material. Windows are cut
out independently, so two cuts closer together than the margin simply show
overlapping context: repeating a second of video is cheaper than hiding one.

Nothing here raises for a boundary it could not build. A single failed ffmpeg
invocation costs that one window and is reported as a warning (REQ-032), since
a report that refuses to exist is worth less than a report with a hole in it.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import _ffmpeg
from . import project as project_module
from .errors import ExecutionFailedError, UsageError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .models import Cut, VideoStream

#: Context shown on each side of a cut, in seconds (design.md §5.6).
BOUNDARY_MARGIN = 2.0

#: Silent black frames separating two windows in the digest (design.md §5.6).
SEPARATOR_SECONDS = 0.5

#: Waveform still geometry, wide enough to read a 4+ second window.
WAVEFORM_SIZE = "1200x240"

BOUNDARIES_DIR = Path("report") / "boundaries"
DIGEST_NAME = Path("report") / "boundary_digest.mp4"

WORKSPACE_PREFIX = ".report-"
STAGED_DIR = "boundaries"
DIGEST_FILE = "digest.mp4"
SEPARATOR_FILE = "separator.mp4"
CONCAT_FILE = "concat.txt"
CLIP_FORMAT = "clip-{index:04d}.mp4"

#: Every piece of the digest is encoded the same way, because the concat
#: demuxer stitches streams together without re-encoding them.
DIGEST_VIDEO_CODEC = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "23")
DIGEST_PIXEL_FORMAT = ("-pix_fmt", "yuv420p")
DIGEST_AUDIO_RATE = 48000
DIGEST_AUDIO_CODEC = ("-c:a", "aac", "-ar", str(DIGEST_AUDIO_RATE), "-ac", "2")

#: Failures that cost one artifact rather than the whole report: a missing
#: executable and an ffmpeg that exited non-zero are both survivable here.
_RECOVERABLE = (ExecutionFailedError, UsageError)


@dataclass(frozen=True, slots=True)
class Boundary:
    """The window shown around one cut, already clamped to the material."""

    cut_id: str
    start: float
    end: float

    @property
    def seconds(self) -> float:
        """Length of the window, in seconds."""
        return self.end - self.start

    @property
    def image_name(self) -> str:
        """File name of this window's waveform, carrying the cut id (REQ-013)."""
        return f"{self.cut_id}.png"


@dataclass(frozen=True, slots=True)
class Material:
    """The files the boundary artifacts are cut out of.

    Attributes:
        root: Project directory the artifacts are published into.
        source: Source video, read for the digest.
        audio: ``audio/processed.wav``, read for the waveforms, or ``None``
            when ``audio-fix`` has not produced it yet.
        video: Geometry and frame rate every digest piece is encoded to.
    """

    root: Path
    source: Path
    audio: Path | None
    video: VideoStream


@dataclass(frozen=True, slots=True)
class Artifacts:
    """What one generation pass published.

    Attributes:
        waveforms: Cut identifiers that produced a still.
        digest: Where the digest was written, relative to the project.
        digest_seconds: How long the digest turned out, measured with ffprobe.
        expected_seconds: How long it should be — every window plus one
            separator each. Encoding rounds each piece up to whole video and
            audio frames, so the two differ by a few frames per boundary;
            anything larger means a window is missing from the digest.
        warnings: What could not be built, one line per failure.
    """

    waveforms: tuple[str, ...] = ()
    digest: str | None = None
    digest_seconds: float | None = None
    expected_seconds: float | None = None
    warnings: tuple[str, ...] = ()


def windows(
    cuts: Iterable[Cut], duration: float, margin: float = BOUNDARY_MARGIN
) -> list[Boundary]:
    """Return the review window of every cut, in timeline order.

    Args:
        cuts: The cut candidates to show, in any order.
        duration: Duration of the source material, in seconds.
        margin: Context kept on each side of the cut, in seconds.

    Returns:
        One window per cut, ordered by start then by cut id so that a
        re-ordered ``cuts.json`` still produces the same digest (REQ-041).
    """
    ordered = sorted(cuts, key=lambda cut: (cut.start, cut.end, cut.id))
    return [
        Boundary(
            cut_id=cut.id,
            start=max(0.0, cut.start - margin),
            end=min(duration, cut.end + margin),
        )
        for cut in ordered
    ]


def waveform_command(audio: Path, boundary: Boundary, target: Path) -> list[str]:
    """Return the ``showwavespic`` command that draws one window (REQ-010)."""
    return [
        _ffmpeg.FFMPEG,
        *_ffmpeg.WRITING,
        "-ss",
        f"{boundary.start:.3f}",
        "-t",
        f"{boundary.seconds:.3f}",
        "-i",
        str(audio),
        "-filter_complex",
        f"showwavespic=s={WAVEFORM_SIZE}",
        "-frames:v",
        "1",
        str(target),
    ]


def clip_command(material: Material, boundary: Boundary, target: Path) -> list[str]:
    """Return the command that cuts one window out of the source video."""
    stream = material.video
    return [
        _ffmpeg.FFMPEG,
        *_ffmpeg.WRITING,
        "-ss",
        f"{boundary.start:.3f}",
        "-i",
        str(material.source),
        "-t",
        f"{boundary.seconds:.3f}",
        "-vf",
        f"scale={stream.width}:{stream.height},fps={stream.fps}",
        *DIGEST_VIDEO_CODEC,
        *DIGEST_PIXEL_FORMAT,
        *DIGEST_AUDIO_CODEC,
        str(target),
    ]


def separator_command(material: Material, target: Path) -> list[str]:
    """Return the command that renders the silent black frames (REQ-012)."""
    stream = material.video
    return [
        _ffmpeg.FFMPEG,
        *_ffmpeg.WRITING,
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={stream.width}x{stream.height}:r={stream.fps}",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={DIGEST_AUDIO_RATE}:cl=stereo",
        "-t",
        f"{SEPARATOR_SECONDS:.3f}",
        *DIGEST_VIDEO_CODEC,
        *DIGEST_PIXEL_FORMAT,
        *DIGEST_AUDIO_CODEC,
        str(target),
    ]


def concat_command(listing: Path, target: Path) -> list[str]:
    """Return the command that stitches the digest together without re-encoding."""
    return [
        _ffmpeg.FFMPEG,
        *_ffmpeg.WRITING,
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        str(target),
    ]


def concat_listing(pieces: Sequence[Path]) -> str:
    """Return the concat demuxer document naming *pieces*, in order.

    Paths are single-quoted the way the demuxer expects, with any quote in the
    project's own path closed, escaped and reopened, so a directory name is
    never read as part of the syntax.
    """
    quoted = (str(path).replace("'", "'\\''") for path in pieces)
    return "".join(f"file '{path}'\n" for path in quoted)


def _waveforms(
    material: Material, boundaries: Sequence[Boundary], staged: Path
) -> tuple[list[str], list[str]]:
    """Draw every window's waveform into *staged*.

    Returns:
        The cut ids that produced an image, and the warnings for the ones that
        did not (REQ-032).
    """
    audio = material.audio
    if audio is None:
        return [], ["audio/processed.wav not found; no boundary waveforms were drawn"]
    drawn: list[str] = []
    warnings: list[str] = []
    for boundary in boundaries:
        command = waveform_command(audio, boundary, staged / boundary.image_name)
        try:
            _ffmpeg.run(command)
        except _RECOVERABLE as exc:
            warnings.append(f"{boundary.cut_id}: waveform not drawn ({exc})")
            continue
        drawn.append(boundary.cut_id)
    return drawn, warnings


def _clips(
    material: Material, boundaries: Sequence[Boundary], workspace: Path
) -> tuple[list[tuple[Path, float]], list[str]]:
    """Cut every window out of the source video.

    Returns:
        The clips that were produced with their lengths, and the warnings for
        the windows that failed.
    """
    produced: list[tuple[Path, float]] = []
    warnings: list[str] = []
    for index, boundary in enumerate(boundaries):
        target = workspace / CLIP_FORMAT.format(index=index)
        try:
            _ffmpeg.run(clip_command(material, boundary, target))
        except _RECOVERABLE as exc:
            warnings.append(f"{boundary.cut_id}: digest clip not cut ({exc})")
            continue
        produced.append((target, boundary.seconds))
    return produced, warnings


def _digest(
    material: Material, boundaries: Sequence[Boundary], workspace: Path
) -> Artifacts:
    """Build ``report/boundary_digest.mp4`` and publish it.

    Returns:
        The digest half of the artifacts: where it was written, how long it
        turned out and how long it should be, all ``None`` when nothing could
        be cut out of the source at all.
    """
    clips, warnings = _clips(material, boundaries, workspace)
    if not clips:
        warnings.append("no boundary clip could be cut; the digest was not built")
        return Artifacts(warnings=tuple(warnings))
    separator = workspace / SEPARATOR_FILE
    try:
        _ffmpeg.run(separator_command(material, separator))
        pieces = [piece for clip, _ in clips for piece in (clip, separator)]
        listing = workspace / CONCAT_FILE
        listing.write_text(concat_listing(pieces), encoding="utf-8")
        built = workspace / DIGEST_FILE
        _ffmpeg.run(concat_command(listing, built))
    except _RECOVERABLE as exc:
        warnings.append(f"the boundary digest was not built ({exc})")
        return Artifacts(warnings=tuple(warnings))
    published = material.root / DIGEST_NAME
    project_module.atomic_replace(built, published)
    expected = sum(length for _, length in clips) + len(clips) * SEPARATOR_SECONDS
    measured = None
    try:
        measured = _ffmpeg.duration(published)
    except _RECOVERABLE as exc:
        warnings.append(f"the length of {DIGEST_NAME} could not be read ({exc})")
    return Artifacts(
        digest=str(DIGEST_NAME),
        digest_seconds=measured,
        expected_seconds=expected,
        warnings=tuple(warnings),
    )


def _publish_directory(staged: Path, target: Path) -> None:
    """Move *staged* onto *target*, replacing whatever was there before.

    The waveforms of a previous run belong to a cut set that no longer exists,
    so the directory is swapped rather than written into.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(target, ignore_errors=True)
    project_module.atomic_replace(staged, target)


def generate(material: Material, boundaries: Sequence[Boundary]) -> Artifacts:
    """Draw the waveforms and build the digest for *boundaries*.

    Args:
        material: Where the pictures and the video are cut out of.
        boundaries: The windows to show, from :func:`windows`.

    Returns:
        What was published, with a warning for every window that failed and
        for the case where there was nothing to show at all.
    """
    if not boundaries:
        _publish_directory_empty(material.root)
        return Artifacts(
            warnings=("no cuts to review; no waveform or digest was generated",)
        )
    workspace = Path(tempfile.mkdtemp(dir=material.root, prefix=WORKSPACE_PREFIX))
    try:
        staged = workspace / STAGED_DIR
        staged.mkdir()
        drawn, warnings = _waveforms(material, boundaries, staged)
        _publish_directory(staged, material.root / BOUNDARIES_DIR)
        digest = _digest(material, boundaries, workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return Artifacts(
        waveforms=tuple(drawn),
        digest=digest.digest,
        digest_seconds=digest.digest_seconds,
        expected_seconds=digest.expected_seconds,
        warnings=(*warnings, *digest.warnings),
    )


def _publish_directory_empty(root: Path) -> None:
    """Leave an empty ``report/boundaries/`` behind when there is no cut left."""
    target = root / BOUNDARIES_DIR
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    (root / DIGEST_NAME).unlink(missing_ok=True)
