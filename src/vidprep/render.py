"""The ``render`` stage: apply the approved cuts, write the video and the SRT.

This is where the pipeline finally produces something to hand to an editor, and
it does two things from one decision. The approved cuts of ``cuts.json`` build
a :class:`~vidprep.timeline.Timeline`; the renderer keeps the intervals that
timeline leaves, and the subtitles are mapped by that same timeline. Neither
computes its own idea of where a cut is, so the video and the subtitles cannot
drift apart (design.md §4).

Only ``approved`` cuts are applied. A ``proposed`` candidate nobody has looked
at, and a ``rejected`` one somebody has, both stay in the recording
(design.md §3.4) — the review is the gate, and rendering is not allowed to be
a second, quieter one.

What is written: ``out/output.mp4``, ``out/subtitles.srt`` and, with
``--no-wrap``, ``out/subtitles.nowrap.srt``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _reencode, _subtitles
from . import audio as audio_module
from . import doctor as doctor_module
from . import project as project_module
from . import transcribe as transcribe_module
from ._reencode import ReencodeRenderer, Renderer, RenderResult
from .detect import CUTS_NAME
from .errors import InvariantViolationError, UsageError
from .models import Cuts, Transcript
from .timeline import Timeline

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._subtitles import Subtitles
    from .models import Cut
    from .project import Project

__all__ = [
    "ReencodeRenderer",
    "RenderResult",
    "Renderer",
    "Result",
    "plan",
    "run_render",
]

STAGE = "render"

OUT_DIR = Path("out")
VIDEO_NAME = OUT_DIR / "output.mp4"
SUBTITLES_NAME = OUT_DIR / "subtitles.srt"
NOWRAP_NAME = OUT_DIR / "subtitles.nowrap.srt"

SECONDS_DECIMALS = 3
LUFS_DECIMALS = 2


@dataclass(frozen=True, slots=True)
class Result:
    """What one ``render`` run applied, produced and measured."""

    rendered: RenderResult
    approved: int
    skipped_proposed: int
    skipped_rejected: int
    source_duration: float
    removed_duration: float
    subtitles: Subtitles
    outputs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Render the result as the JSON document ``--json`` prints."""
        produced = self.rendered
        subtitles = self.subtitles
        return {
            "renderer": produced.renderer,
            "cuts_applied": {
                "approved": self.approved,
                "skipped_proposed": self.skipped_proposed,
                "skipped_rejected": self.skipped_rejected,
            },
            "duration": {
                "source": round(self.source_duration, SECONDS_DECIMALS),
                "removed": round(self.removed_duration, SECONDS_DECIMALS),
                "expected": round(produced.expected_duration, SECONDS_DECIMALS),
                "actual": round(produced.duration, SECONDS_DECIMALS),
                "delta_ms": round(produced.delta_ms, SECONDS_DECIMALS),
            },
            "streams": {
                "video_sec": round(produced.video_duration, SECONDS_DECIMALS),
                "audio_sec": round(produced.audio_duration, SECONDS_DECIMALS),
                "av_delta_ms": round(produced.av_delta_ms, SECONDS_DECIMALS),
            },
            "loudness": {
                "integrated_lufs": round(produced.integrated_lufs, LUFS_DECIMALS),
                "true_peak_dbtp": round(produced.true_peak_dbtp, LUFS_DECIMALS),
            },
            "subtitles": {
                "entries": len(subtitles.entries),
                "dropped_by_cut": subtitles.count("dropped_by_cut"),
                "warn_min_display": subtitles.count("min_display"),
                "warn_max_cps": subtitles.count("max_cps"),
                "warn_line_overflow": subtitles.count("line_overflow"),
            },
            "outputs": list(self.outputs),
        }

    def lines(self) -> list[str]:
        """Render the result for a human."""
        produced = self.rendered
        subtitles = self.subtitles
        return [
            f"✔ {VIDEO_NAME} ({self.approved} approved cuts, "
            f"-{self.removed_duration:.1f}s → {produced.duration:.2f}s, "
            f"delta {produced.delta_ms:.1f}ms, "
            f"{produced.integrated_lufs:.2f} LUFS)",
            f"✔ {SUBTITLES_NAME} ({len(subtitles.entries)} entries; "
            f"{subtitles.count('dropped_by_cut')} dropped by a cut, "
            f"{subtitles.count('min_display')} under min_display, "
            f"{subtitles.count('max_cps')} over max_cps, "
            f"{subtitles.count('line_overflow')} over max_chars_per_line)",
        ]


def _audio_path(loaded: Project) -> Path:
    """Return the processed audio the output is built from.

    The container's own audio is never used: everything downstream of
    ``audio-fix`` is timed against ``audio/processed.wav`` (design.md §2.1).

    Raises:
        UsageError: If ``audio-fix`` has not produced it yet.
    """
    path = loaded.root / audio_module.OUTPUT_NAME
    if not path.is_file():
        msg = f"{audio_module.OUTPUT_NAME} not found — run `vidprep audio-fix` first"
        raise UsageError(msg)
    return path


def _load_cuts(loaded: Project) -> list[Cut]:
    """Return the reviewed cuts, checked against every invariant.

    Raises:
        UsageError: If ``detect`` has not produced ``cuts.json`` yet.
        SchemaInvalidError: If the file breaks its schema — an interval past
            the end of the material, or two ``approved`` cuts overlapping.
    """
    path = loaded.root / CUTS_NAME
    if not path.is_file():
        msg = f"{CUTS_NAME} not found — run `vidprep detect` first"
        raise UsageError(msg)
    duration = loaded.manifest.source.duration
    return list(project_module.load_artifact(path, Cuts, duration).cuts)


def _transcript_path(loaded: Project) -> Path:
    """Return the transcript the subtitles are built from.

    Raises:
        UsageError: If ``transcribe`` has not produced it yet.
    """
    name = transcribe_module.TRANSCRIPT_NAME
    path = loaded.root / name
    if not path.is_file():
        msg = f"{name} not found — run `vidprep transcribe` first"
        raise UsageError(msg)
    return path


def _load_transcript(loaded: Project) -> Transcript:
    """Parse the transcript.

    Raises:
        UsageError: If ``transcribe`` has not produced it yet.
        SchemaInvalidError: If the file breaks its schema.
    """
    duration = loaded.manifest.source.duration
    return project_module.load_artifact(_transcript_path(loaded), Transcript, duration)


def _timeline(loaded: Project, cuts: Sequence[Cut]) -> Timeline:
    """Build the cut plan from the approved cuts only (design.md §3.4).

    The cuts are snapped to the frame grid before the timeline is built, not
    after: a video can only be cut at a frame boundary, and the subtitles have
    to be timed against the boundaries the video actually gets.

    Raises:
        InvariantViolationError: If the approved cuts would remove the whole
            recording, leaving nothing to render.
    """
    approved = [(cut.start, cut.end) for cut in cuts if cut.status == "approved"]
    timeline = Timeline(
        _reencode.align_to_frames(approved, loaded.manifest.source.video.fps),
        loaded.manifest.source.duration,
    )
    if not timeline.keeps:
        msg = (
            f"the approved cuts in {CUTS_NAME} would remove the whole recording; "
            f"{VIDEO_NAME} was left untouched"
        )
        raise InvariantViolationError(msg)
    return timeline


def _renderer(loaded: Project) -> ReencodeRenderer:
    """Return the renderer for *loaded* (design.md §5.5 knows only one so far)."""
    return ReencodeRenderer(fps=loaded.manifest.source.video.fps)


def _job(loaded: Project, timeline: Timeline, audio: Path) -> _reencode.RenderJob:
    """Describe the rendering of *timeline* for ``--dry-run`` to read back."""
    return _reencode.RenderJob(
        source=loaded.source_path,
        keep=timeline.keeps,
        audio=audio,
        profile=loaded.profile,
        out=loaded.root / VIDEO_NAME,
    )


def _build_subtitles(loaded: Project, timeline: Timeline) -> Subtitles:
    """Map the transcript onto the cut timeline and dress it as subtitles."""
    transcript = _load_transcript(loaded)
    settings = loaded.profile.subtitle
    mapped, warnings = timeline.map_segments(
        [(segment.id, segment.start, segment.end) for segment in transcript.segments],
        min_display=settings.min_display,
    )
    texts = {segment.id: segment.text for segment in transcript.segments}
    return _subtitles.build(mapped, texts, warnings, settings)


def _write_subtitles(
    loaded: Project, subtitles: Subtitles, *, no_wrap: bool
) -> list[str]:
    """Write the SRT files and return the names of the ones written."""
    written = [SUBTITLES_NAME]
    if no_wrap:
        written.append(NOWRAP_NAME)
    for name in written:
        path = loaded.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        project_module.atomic_write_text(
            path, subtitles.to_srt(wrapped=name != NOWRAP_NAME)
        )
    return [str(name) for name in written]


def plan(loaded: Project, *, no_wrap: bool = False) -> dict[str, Any]:
    """Return what :func:`run_render` would run and write, without doing it.

    The upstream artifacts are checked here too, so a dry run refuses for the
    same reason — and with the same exit code — as the real thing would.
    """
    audio = _audio_path(loaded)
    _transcript_path(loaded)
    timeline = _timeline(loaded, _load_cuts(loaded))
    renderer = _renderer(loaded)
    writes = [str(loaded.root / VIDEO_NAME), str(loaded.root / SUBTITLES_NAME)]
    if no_wrap:
        writes.append(str(loaded.root / NOWRAP_NAME))
    writes.append(str(loaded.root / project_module.MANIFEST_NAME))
    return {
        "action": "render",
        "project": str(loaded.root),
        "renderer": renderer.NAME,
        "commands": renderer.commands(_job(loaded, timeline, audio)),
        "writes": writes,
    }


def run_render(loaded: Project, *, no_wrap: bool = False) -> Result:
    """Apply the approved cuts of *loaded* and write the video and subtitles.

    Everything that can fail cheaply fails first: the material is re-hashed,
    the artifacts are parsed and checked, and the subtitles are built in memory
    before a single frame is encoded (design.md §5.5).

    Args:
        loaded: The project to render; its source material is only read.
        no_wrap: Also write the SRT without line breaking, to compare the
            breaks BudouX proposed against the unbroken text.

    Returns:
        What was applied and what the output measures.

    Raises:
        UsageError: If an upstream stage has not run yet.
        HashMismatchError: If the material was replaced since ``init``.
        SchemaInvalidError: If ``cuts.json`` or ``transcript.json`` is invalid.
        InvariantViolationError: If the output fails the length, synchronisation
            or loudness checks of verification-plan.md §8.
    """
    # Repeated for the sake of the library caller: the CLI has verified the
    # material already, and rendering from a source that no longer matches the
    # cuts is the one failure that would look like a successful run.
    project_module.verify_source(loaded)
    audio = _audio_path(loaded)
    cuts = _load_cuts(loaded)
    timeline = _timeline(loaded, cuts)
    subtitles = _build_subtitles(loaded, timeline)

    # Through the protocol, not through the concrete class: a renderer that
    # cuts without re-encoding (design.md §8) drops in here unchanged.
    renderer: Renderer = _renderer(loaded)
    rendered = renderer.render(
        loaded.source_path,
        timeline.keeps,
        audio,
        loaded.profile,
        loaded.root / VIDEO_NAME,
    )
    outputs = [str(VIDEO_NAME), *_write_subtitles(loaded, subtitles, no_wrap=no_wrap)]

    versions = {"ffmpeg": doctor_module.check_ffmpeg().get("version") or "unknown"}
    project_module.record_stage(loaded, STAGE, versions)
    statuses = [cut.status for cut in cuts]
    return Result(
        rendered=rendered,
        approved=statuses.count("approved"),
        skipped_proposed=statuses.count("proposed"),
        skipped_rejected=statuses.count("rejected"),
        source_duration=loaded.manifest.source.duration,
        removed_duration=timeline.removed_duration,
        subtitles=subtitles,
        outputs=tuple(outputs),
    )
