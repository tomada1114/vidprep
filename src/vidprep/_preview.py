"""Burning the telop track into a preview video (design.md §5.5).

The preview is not a second render: it is ``out/output.mp4`` with the ASS
document drawn on top by libass, so what it shows is the cut the editor will
receive. The audio is copied rather than re-encoded, which is also why the two
files can be required to last the same — a preview whose length drifted from
the render would be showing telops at the wrong moments.

``out/output.mp4`` is only ever read here (REQ-040), and the preview is built
in a working directory and moved into place, so a failed burn leaves whatever
was there before untouched.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from . import _ffmpeg
from . import doctor as doctor_module
from . import project as project_module
from ._reencode import DELTA_DECIMALS, PIXEL_FORMAT, VIDEO_CODEC
from .errors import InvariantViolationError, UsageError

if TYPE_CHECKING:
    from .models import RenderProfile

WORKSPACE_PREFIX = ".preview-"
MS_PER_SECOND = 1000.0

#: The filter that hands a subtitle file to libass.
SUBTITLES_FILTER = "subtitles"

#: Characters an ffmpeg filter argument escapes, and the ones the filtergraph
#: around it escapes in turn. A path goes through both, innermost first
#: (ffmpeg-filters(1), "Notes on filtergraph escaping").
_ARGUMENT_ESCAPES = str.maketrans({"\\": "\\\\", ":": "\\:", "'": "\\'"})
_GRAPH_ESCAPES = str.maketrans(
    {"\\": "\\\\", "'": "\\'", "[": "\\[", "]": "\\]", ",": "\\,", ";": "\\;"}
)


def escape_filter_path(path: Path) -> str:
    """Return *path* as an ffmpeg filter argument.

    A project directory is a place the user chose, so it may hold a colon or a
    comma — both of which end an argument, and one of which ends a filter.
    Escaping happens in the order ffmpeg unescapes it: what the filtergraph
    parser hands the filter must still be the escaped argument.
    """
    return str(path).translate(_ARGUMENT_ESCAPES).translate(_GRAPH_ESCAPES)


def require_libass() -> None:
    """Check that this ffmpeg can draw subtitles at all (REQ-023).

    Raises:
        UsageError: If ffmpeg is missing or was built without libass. The
            preview is the only stage that needs it, so the check is made here
            rather than turning every other command into a dependency check.
    """
    check = doctor_module.check_ffmpeg()
    if not check.get("libass", False):
        msg = (
            "ffmpeg has no subtitles filter (libass), which `--preview` burns "
            "the telops in with — run `vidprep doctor` to see what is missing"
        )
        raise UsageError(msg)


def burn_command(
    video: Path, telops: Path, out: Path, profile: RenderProfile
) -> list[str]:
    """Return the ffmpeg invocation that draws *telops* over *video*."""
    return [
        _ffmpeg.FFMPEG,
        *_ffmpeg.WRITING,
        "-i",
        str(video),
        "-vf",
        f"{SUBTITLES_FILTER}={escape_filter_path(telops)}",
        "-c:v",
        VIDEO_CODEC,
        "-crf",
        str(profile.crf),
        "-preset",
        profile.preset,
        "-pix_fmt",
        PIXEL_FORMAT,
        # Copied, not re-encoded: nothing about the audio changes, and a copy
        # cannot move the length the telops were timed against.
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(out),
    ]


def commands(
    video: Path, telops: Path, out: Path, profile: RenderProfile
) -> list[list[str]]:
    """Return every external command a preview burn executes."""
    return [burn_command(video, telops, out, profile), _ffmpeg.duration_command(out)]


def burn(
    video: Path, telops: Path, out: Path, profile: RenderProfile, frame_ms: float
) -> float:
    """Draw *telops* over *video* into *out* and return the length that came out.

    Args:
        video: The rendered output the telops belong to; only read.
        telops: The ASS document to burn in.
        out: Where the preview goes, replaced in one step once it verifies.
        profile: The ``render`` section, so the preview is encoded like the
            output it previews.
        frame_ms: One frame of the material, the tolerance of the length check.

    Returns:
        The duration of the preview, in seconds.

    Raises:
        InvariantViolationError: If the preview does not last as long as the
            render it was drawn over (REQ-013); *out* keeps whatever it held.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(dir=out.parent, prefix=WORKSPACE_PREFIX))
    try:
        produced = workspace / out.name
        _ffmpeg.run(burn_command(video, telops, produced, profile))
        duration = _ffmpeg.duration(produced)
        _verify(duration, _ffmpeg.duration(video), frame_ms, out.name)
        project_module.atomic_replace(produced, out)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return duration


def _verify(duration: float, expected: float, frame_ms: float, name: str) -> None:
    """Check that the preview lasts exactly as long as the render (REQ-013).

    Raises:
        InvariantViolationError: If the two are more than one frame apart.
    """
    delta_ms = abs(duration - expected) * MS_PER_SECOND
    if round(delta_ms, DELTA_DECIMALS) > round(frame_ms, DELTA_DECIMALS):
        msg = (
            f"the preview lasts {duration:.3f}s where the render lasts "
            f"{expected:.3f}s (delta {delta_ms:.1f}ms > {frame_ms:.1f}ms, one "
            f"frame); {name} was left untouched"
        )
        raise InvariantViolationError(msg)
