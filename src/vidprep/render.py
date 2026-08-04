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
``--no-wrap``, ``out/subtitles.nowrap.srt``. ``--preview`` adds the telop track
``out/telops.ass`` and ``out/preview.mp4``, the render with it burned in.

``--verify-asr`` adds a read-only pass over the finished file: it is
transcribed a second time and compared with what the kept segments say it
should contain, which is how a word clipped at a cut boundary is found
(verification-plan.md §8.1). It is delegated whole to :mod:`vidprep.verify`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _ass, _preview, _reencode, _subtitles, verify
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
    from collections.abc import Mapping, Sequence

    from ._ass import TelopPlan
    from ._subtitles import Subtitles
    from .models import Cut, StylePreset
    from .project import Project
    from .timeline import SegmentWarning, TimedSegment

__all__ = [
    "Preview",
    "ReencodeRenderer",
    "RenderResult",
    "Renderer",
    "Result",
    "build_subtitles",
    "plan",
    "run_render",
    "subject",
]

STAGE = "render"

OUT_DIR = Path("out")
VIDEO_NAME = OUT_DIR / "output.mp4"
SUBTITLES_NAME = OUT_DIR / "subtitles.srt"
NOWRAP_NAME = OUT_DIR / "subtitles.nowrap.srt"
TELOPS_ASS_NAME = OUT_DIR / "telops.ass"
PREVIEW_NAME = OUT_DIR / "preview.mp4"

SECONDS_DECIMALS = 3
LUFS_DECIMALS = 2


@dataclass(frozen=True, slots=True)
class Preview:
    """What ``--preview`` drew, and which presets it drew it with."""

    plan: TelopPlan
    presets: tuple[str, ...]
    styles_source: str
    duration: float

    def to_dict(self) -> dict[str, Any]:
        """Render the telop and style sections of the ``--json`` document."""
        telops = self.plan
        return {
            "telops": {
                "total": len(telops.events) + telops.dropped_by_cut,
                "by_segment_id": telops.by_segment_id,
                "by_start_duration": telops.by_start_duration,
                "dropped_by_cut": telops.dropped_by_cut,
                "warnings": list(telops.warnings),
            },
            "styles": {"presets": list(self.presets), "source": self.styles_source},
        }

    def lines(self) -> list[str]:
        """Render what was drawn for a human, warnings first."""
        drawn = len(self.plan.events)
        return [
            *(f"⚠ {warning}" for warning in self.plan.warnings),
            f"✔ {TELOPS_ASS_NAME} ({drawn} telops, "
            f"{len(self.presets)} style presets from the {self.styles_source})",
            f"✔ {PREVIEW_NAME} ({self.duration:.2f}s, telops burned in)",
        ]


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
    preview: Preview | None = None
    verified: verify.VerifyResult | None = None

    def to_dict(self) -> dict[str, Any]:
        """Render the result as the JSON document ``--json`` prints."""
        produced = self.rendered
        subtitles = self.subtitles
        preview = {} if self.preview is None else self.preview.to_dict()
        verified = (
            {} if self.verified is None else {"verify_asr": self.verified.to_dict()}
        )
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
            **preview,
            **verified,
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
            *([] if self.preview is None else self.preview.lines()),
            *([] if self.verified is None else self.verified.lines()),
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


@dataclass(frozen=True, slots=True)
class _Mapped:
    """The transcript, and where the cut timeline put each of its segments.

    Subtitles and telops are both built from this one mapping rather than from
    two calls with the same arguments: a telop that follows a ``segment_id``
    then shows for exactly as long as the subtitle of that segment (REQ-041).
    """

    transcript: Transcript
    segments: tuple[TimedSegment, ...]
    warnings: tuple[SegmentWarning, ...]


def _map_transcript(loaded: Project, timeline: Timeline) -> _Mapped:
    """Map every transcript segment onto the cut timeline (design.md §4)."""
    transcript = _load_transcript(loaded)
    mapped, warnings = timeline.map_segments(
        [(segment.id, segment.start, segment.end) for segment in transcript.segments],
        min_display=loaded.profile.subtitle.min_display,
    )
    return _Mapped(transcript, tuple(mapped), tuple(warnings))


def _build_subtitles(loaded: Project, mapped: _Mapped) -> Subtitles:
    """Dress the mapped segments as subtitles."""
    texts = {segment.id: segment.text for segment in mapped.transcript.segments}
    return _subtitles.build(
        mapped.segments, texts, mapped.warnings, loaded.profile.subtitle
    )


def build_subtitles(loaded: Project, timeline: Timeline) -> Subtitles:
    """Map the transcript onto *timeline* and dress it as subtitles.

    Public because the entries are also what the written SRT is checked against
    (:func:`vidprep.verify.missing_subtitle_entries`): a checker that built its
    own idea of the entries would prove nothing.

    Raises:
        UsageError: If ``transcribe`` has not produced a transcript yet.
        SchemaInvalidError: If the transcript breaks its schema.
    """
    return _build_subtitles(loaded, _map_transcript(loaded, timeline))


def _write_subtitles(
    loaded: Project, subtitles: Subtitles, *, no_wrap: bool
) -> list[str]:
    """Write the SRT files, read the main one back, and name what was written.

    Reading it back is the "no missing entries" condition of
    verification-plan.md §9: what the mapping produced has to be what ends up in
    the file, and the only way to know is to parse the file rather than the
    object it was built from.

    Raises:
        InvariantViolationError: If an entry did not survive being written.
    """
    written = [SUBTITLES_NAME]
    if no_wrap:
        written.append(NOWRAP_NAME)
    for name in written:
        path = loaded.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        project_module.atomic_write_text(
            path, subtitles.to_srt(wrapped=name != NOWRAP_NAME)
        )
    missing = verify.missing_subtitle_entries(
        loaded.root / SUBTITLES_NAME, subtitles.entries
    )
    if missing:
        msg = (
            f"{len(missing)} mapped segments are not in {SUBTITLES_NAME} "
            f"({', '.join(missing[:5])})"
        )
        raise InvariantViolationError(msg)
    return [str(name) for name in written]


@dataclass(frozen=True, slots=True)
class _Telops:
    """The telops of ``telops.json``, placed and ready to be drawn."""

    plan: TelopPlan
    presets: Mapping[str, StylePreset]
    source: str


def _resolve_telops(loaded: Project, timeline: Timeline, mapped: _Mapped) -> _Telops:
    """Place the telops on the cut timeline, before anything is encoded.

    Raises:
        UsageError: If this ffmpeg cannot draw subtitles, or the project has no
            ``telops.json``.
        SchemaInvalidError: If ``telops.json`` or ``styles.json`` is invalid.
        TelopInvalidError: If a telop names a segment or a preset that is not
            there.
    """
    _preview.require_libass()
    telops = _ass.load_telops(loaded.root, loaded.manifest.source.duration)
    styles, source = _ass.load_styles(loaded.root)
    placement = _ass.Placement(
        timeline=timeline,
        mapped={segment.segment_id: segment for segment in mapped.segments},
        known=frozenset(segment.id for segment in mapped.transcript.segments),
        presets=styles.presets,
    )
    return _Telops(_ass.resolve(telops.telops, placement), styles.presets, source)


def _write_preview(loaded: Project, telops: _Telops, frame_ms: float) -> Preview:
    """Write the ASS track and burn it into the preview (REQ-010, REQ-011).

    Raises:
        InvariantViolationError: If the preview does not last as long as the
            render it was drawn over.
    """
    video = loaded.manifest.source.video
    path = loaded.root / TELOPS_ASS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    project_module.atomic_write_text(
        path,
        _ass.document(telops.plan, telops.presets, f"{video.width}x{video.height}"),
    )
    duration = _preview.burn(
        loaded.root / VIDEO_NAME,
        path,
        loaded.root / PREVIEW_NAME,
        loaded.profile.render,
        frame_ms,
    )
    return Preview(telops.plan, tuple(telops.presets), telops.source, duration)


def subject(loaded: Project, cuts: Sequence[Cut], timeline: Timeline) -> verify.Subject:
    """Describe the render the re-transcription check is to read back."""
    return verify.Subject(
        root=loaded.root,
        video=loaded.root / VIDEO_NAME,
        transcript=_load_transcript(loaded),
        approved=tuple(cut for cut in cuts if cut.status == "approved"),
        timeline=timeline,
        profile=loaded.profile,
    )


def plan(
    loaded: Project,
    *,
    no_wrap: bool = False,
    preview: bool = False,
    verify_asr: bool = False,
) -> dict[str, Any]:
    """Return what :func:`run_render` would run and write, without doing it.

    The upstream artifacts are checked here too, so a dry run refuses for the
    same reason — and with the same exit code — as the real thing would; with
    ``verify_asr`` that includes the recogniser the second pass would need.
    """
    audio = _audio_path(loaded)
    _transcript_path(loaded)
    cuts = _load_cuts(loaded)
    timeline = _timeline(loaded, cuts)
    renderer = _renderer(loaded)
    commands = renderer.commands(_job(loaded, timeline, audio))
    writes = [str(loaded.root / VIDEO_NAME), str(loaded.root / SUBTITLES_NAME)]
    if no_wrap:
        writes.append(str(loaded.root / NOWRAP_NAME))
    if preview:
        _resolve_telops(loaded, timeline, _map_transcript(loaded, timeline))
        commands += _preview.commands(
            loaded.root / VIDEO_NAME,
            loaded.root / TELOPS_ASS_NAME,
            loaded.root / PREVIEW_NAME,
            loaded.profile.render,
        )
        writes += [
            str(loaded.root / TELOPS_ASS_NAME),
            str(loaded.root / PREVIEW_NAME),
        ]
    writes.append(str(loaded.root / project_module.MANIFEST_NAME))
    if verify_asr:
        commands += verify.commands(subject(loaded, cuts, timeline))
    return {
        "action": "render",
        "project": str(loaded.root),
        "renderer": renderer.NAME,
        "commands": commands,
        "writes": writes,
    }


def run_render(
    loaded: Project,
    *,
    no_wrap: bool = False,
    preview: bool = False,
    verify_asr: bool = False,
) -> Result:
    """Apply the approved cuts of *loaded* and write the video and subtitles.

    Everything that can fail cheaply fails first: the material is re-hashed,
    the artifacts are parsed and checked, and the subtitles and telops are
    built in memory before a single frame is encoded (design.md §5.5).

    Args:
        loaded: The project to render; its source material is only read.
        no_wrap: Also write the SRT without line breaking, to compare the
            breaks BudouX proposed against the unbroken text.
        preview: Also write ``out/telops.ass`` and burn it into
            ``out/preview.mp4``. ``out/output.mp4`` is only read for it.
        verify_asr: Transcribe the finished output a second time and compare it
            with what the kept segments should say (verification-plan.md §8.1).
            The output is only read; nothing about it changes either way.

    Returns:
        What was applied and what the output measures.

    Raises:
        UsageError: If an upstream stage has not run yet, if ``--preview`` was
            asked for without ``telops.json`` or without libass, or if
            ``verify_asr`` was asked for and the recogniser is not installed.
        HashMismatchError: If the material was replaced since ``init``.
        SchemaInvalidError: If ``cuts.json`` or ``transcript.json`` is invalid.
        TelopInvalidError: If a telop names a segment or a preset that is not
            there.
        InvariantViolationError: If the output fails the length, synchronisation
            or loudness checks of verification-plan.md §8.
        AsrFailedError: If the second pass could not be compared at all.
    """
    # Repeated for the sake of the library caller: the CLI has verified the
    # material already, and rendering from a source that no longer matches the
    # cuts is the one failure that would look like a successful run.
    project_module.verify_source(loaded)
    audio = _audio_path(loaded)
    cuts = _load_cuts(loaded)
    timeline = _timeline(loaded, cuts)
    mapped = _map_transcript(loaded, timeline)
    subtitles = _build_subtitles(loaded, mapped)
    telops = _resolve_telops(loaded, timeline, mapped) if preview else None

    # Whether the second pass could run at all is settled before the encode,
    # not after it: a missing recogniser should not cost a whole render.
    checked = subject(loaded, cuts, timeline) if verify_asr else None
    if checked is not None:
        verify.check_available(checked)

    # Through the protocol, not through the concrete class: a renderer that
    # cuts without re-encoding (design.md §8) drops in here unchanged.
    encoder = _renderer(loaded)
    renderer: Renderer = encoder
    rendered = renderer.render(
        loaded.source_path,
        timeline.keeps,
        audio,
        loaded.profile,
        loaded.root / VIDEO_NAME,
    )
    outputs = [str(VIDEO_NAME), *_write_subtitles(loaded, subtitles, no_wrap=no_wrap)]
    drawn = None
    if telops is not None:
        drawn = _write_preview(loaded, telops, encoder.frame_ms)
        outputs += [str(TELOPS_ASS_NAME), str(PREVIEW_NAME)]

    # The stage is recorded before the second pass, not after: the render is
    # finished and verified by this point, and a comparison that cannot be made
    # must not leave the outputs on disk without the record that produced them.
    versions = {"ffmpeg": doctor_module.check_ffmpeg().get("version") or "unknown"}
    project_module.record_stage(loaded, STAGE, versions)

    # Read-only, and last: the second pass reads the very file the reviewer
    # will watch.
    verified = None if checked is None else verify.run_verify_asr(checked)
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
        preview=drawn,
        verified=verified,
    )
