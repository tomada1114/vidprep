"""The ``report`` stage: the review gate and the numbers behind it (design.md §5.6).

Four artifacts come out of here. ``report/stats.json`` is the one every other
tool reads — the golden-run comparison of verification-plan.md §3.3 diffs it
between runs, so its keys are a contract and change only deliberately.
``report/boundaries/*.png`` and ``report/boundary_digest.mp4`` are what makes
reviewing thirty cuts a job of minutes instead of an evening of seeking through
a timeline. ``--cuts`` prints what each candidate would delete, which is the
material a ``status`` decision is actually made from.

The stage is read-only by design (REQ-040): it never touches ``cuts.json``,
``transcript.json`` or ``vidprep.json``, not even to record that it ran. It also
refuses to fail on a missing input. Running ``report`` before ``detect`` or
before ``render`` is normal — that is when the numbers are most wanted — so a
missing artifact leaves its section empty or ``null`` and adds a warning, and
the exit code stays ``0``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _boundaries, _ffmpeg, _review
from . import audio as audio_module
from . import project as project_module
from . import transcribe as transcribe_module
from .detect import CUTS_NAME
from .errors import ExecutionFailedError, UsageError
from .models import Cuts, NoiseFloorReport, Transcript, to_ms
from .timeline import Timeline

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .audio import Measurement
    from .models import Cut, Loudnorm, Segment, SubtitleProfile
    from .project import Project

STAGE = "report"

REPORT_DIR = Path("report")
STATS_NAME = REPORT_DIR / "stats.json"

#: Written by ``render`` (#9); read here only to measure what came out of it.
RENDERED_NAME = Path("out") / "output.mp4"

#: Bumped when a section of the document changes shape rather than value —
#: ``noise_floor`` did in #33, so a reader that knew the old keys cannot be
#: left thinking it still knows them.
STATS_VERSION = "2"

#: Always present in ``cuts.by_reason``, even at zero, so a consumer never has
#: to guess whether a missing key means "none" or "not measured".
REASONS = ("silence", "filler", "manual")
STATUSES = ("approved", "proposed", "rejected")

#: How far the integrated loudness may sit from the target (verification-plan.md §4).
LOUDNESS_TOLERANCE = 0.5

SECONDS_DECIMALS = 3
RATIO_DECIMALS = 3
LEVEL_DECIMALS = 2

#: Measuring is diagnostics: a missing ffmpeg or a failed analysis costs the
#: number it would have produced, not the report.
_RECOVERABLE = (ExecutionFailedError, UsageError)


@dataclass(frozen=True, slots=True)
class Inputs:
    """What ``report`` found in the project before it started measuring."""

    cuts: tuple[Cut, ...]
    transcript: Transcript | None
    audio: Path | None
    rendered: Path | None
    noise_floor: NoiseFloorReport | None
    warnings: tuple[str, ...]

    @property
    def approved(self) -> tuple[Cut, ...]:
        """The cuts ``render`` would apply, which are the ones the stats model."""
        return tuple(cut for cut in self.cuts if cut.status == "approved")


@dataclass(frozen=True, slots=True)
class Levels:
    """Loudness at each point of the pipeline, where it could be measured."""

    source: Measurement | None = None
    processed: Measurement | None = None
    rendered: Measurement | None = None


@dataclass(frozen=True, slots=True)
class Result:
    """One ``report`` run: the statistics document plus what went wrong."""

    stats: dict[str, Any]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return exactly what was written to ``report/stats.json`` (REQ-005)."""
        return self.stats

    def lines(self) -> list[str]:
        """Render the run for a human, warnings first."""
        duration = self.stats["duration"]
        artifacts = self.stats["artifacts"]
        rendered = duration["rendered"]
        length = "rendered length unknown" if rendered is None else f"{rendered:.2f}s"
        reported = [f"⚠ {warning}" for warning in self.warnings]
        reported.append(
            f"✔ {STATS_NAME} ({duration['source']:.2f}s source, {length}, "
            f"{self.stats['cuts']['approved_total_sec']:.2f}s approved to cut)"
        )
        reported.append(
            f"✔ {_boundaries.BOUNDARIES_DIR}: {artifacts['boundaries_png']} waveforms"
        )
        if artifacts["boundary_digest"] is not None:
            measured = artifacts["boundary_digest_sec"]
            shown = "length not measured" if measured is None else f"{measured:.1f}s"
            reported.append(
                f"✔ {artifacts['boundary_digest']} ({shown}, "
                f"{artifacts['boundary_digest_expected_sec']:.1f}s expected)"
            )
        return reported


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """One ``report --cuts`` listing."""

    payload: dict[str, Any]
    warnings: tuple[str, ...] = ()
    listing: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the listing as JSON, for the ``review-cuts`` skill (REQ-022)."""
        return self.payload

    def lines(self) -> list[str]:
        """Render the listing for a human, warnings first."""
        return [f"⚠ {warning}" for warning in self.warnings] + list(self.listing)


# --------------------------------------------------------------------------- #
#  Reading what is there
# --------------------------------------------------------------------------- #


def _read(loaded: Project) -> Inputs:
    """Load every artifact ``report`` can use, warning about the absent ones."""
    duration = loaded.manifest.source.duration
    warnings: list[str] = []

    cuts: tuple[Cut, ...] = ()
    cuts_path = loaded.root / CUTS_NAME
    if cuts_path.is_file():
        cuts = tuple(
            project_module.load_artifact(cuts_path, Cuts, duration).cuts,
        )
    else:
        warnings.append(f"{CUTS_NAME} not found; the cut sections are empty")

    transcript = None
    transcript_path = loaded.root / transcribe_module.TRANSCRIPT_NAME
    if transcript_path.is_file():
        transcript = project_module.load_artifact(transcript_path, Transcript, duration)
    else:
        warnings.append(
            f"{transcribe_module.TRANSCRIPT_NAME} not found; "
            "the subtitle statistics are empty"
        )

    audio = loaded.root / audio_module.OUTPUT_NAME
    if not audio.is_file():
        warnings.append(
            f"{audio_module.OUTPUT_NAME} not found; run `vidprep audio-fix`"
        )
    rendered = loaded.root / RENDERED_NAME
    if not rendered.is_file():
        warnings.append(
            f"{RENDERED_NAME} not found; the output statistics are null "
            "until `vidprep render` has run"
        )

    floor = None
    floor_path = loaded.root / audio_module.NOISE_FLOOR_NAME
    if floor_path.is_file():
        floor = project_module.load_artifact(floor_path, NoiseFloorReport, duration)
    else:
        warnings.append(
            f"{audio_module.NOISE_FLOOR_NAME} not found; run "
            "`vidprep audio-fix --stats` for the denoising comparison"
        )
    return Inputs(
        cuts=cuts,
        transcript=transcript,
        audio=audio if audio.is_file() else None,
        rendered=rendered if rendered.is_file() else None,
        noise_floor=floor,
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------- #
#  The sections of stats.json
# --------------------------------------------------------------------------- #


def _seconds(value: float | None) -> float | None:
    """Round a length to milliseconds, leaving "not measured" as ``None``."""
    return None if value is None else round(value, SECONDS_DECIMALS)


def _duration_section(source: float, rendered: float | None) -> dict[str, Any]:
    """Return original length, rendered length and how much was taken off."""
    ratio = None
    if rendered is not None and source > 0:
        ratio = round(1.0 - rendered / source, RATIO_DECIMALS)
    return {
        "source": round(source, SECONDS_DECIMALS),
        "rendered": None if rendered is None else round(rendered, SECONDS_DECIMALS),
        "reduction_ratio": ratio,
    }


def _by_reason(cuts: Sequence[Cut]) -> dict[str, Any]:
    """Return the count, the length and the status split of every reason.

    Reasons stay an open set (design.md §8), so a value only a human wrote
    still gets its own entry next to the three that always appear.
    """
    reasons = [*REASONS, *sorted({cut.reason for cut in cuts} - set(REASONS))]
    summary: dict[str, Any] = {}
    for reason in reasons:
        matching = [cut for cut in cuts if cut.reason == reason]
        summary[reason] = {
            "count": len(matching),
            "sec": round(
                sum(cut.end - cut.start for cut in matching), SECONDS_DECIMALS
            ),
            **{
                status: sum(1 for cut in matching if cut.status == status)
                for status in STATUSES
            },
        }
    return summary


def _cut_section(cuts: Sequence[Cut]) -> dict[str, Any]:
    """Return the reason breakdown plus the length ``render`` would remove."""
    approved = [cut for cut in cuts if cut.status == "approved"]
    return {
        "by_reason": _by_reason(cuts),
        "approved_total_sec": round(
            sum(cut.end - cut.start for cut in approved), SECONDS_DECIMALS
        ),
    }


def _integrated(measurement: Measurement | None) -> float | None:
    """Return the integrated loudness of *measurement*, if there is one."""
    if measurement is None:
        return None
    return round(measurement.integrated_lufs, LEVEL_DECIMALS)


def _output_floor(measurement: Measurement | None) -> dict[str, float] | None:
    """Return the floor of the finished audio, absolute and level-matched.

    Neither figure answers "did denoising help?" — that is what
    ``noise_floor.denoise`` is for (#33) — but both describe what a listener
    is left with: how loud the quiet parts are, and how far they sit under the
    programme level, which is roughly how audible they are.
    """
    if measurement is None or measurement.noise_floor_rms_db is None:
        return None
    floor = measurement.noise_floor_rms_db
    return {
        "rms_db": round(floor, LEVEL_DECIMALS),
        "below_programme_db": round(
            measurement.integrated_lufs - floor, LEVEL_DECIMALS
        ),
    }


def _loudness_section(levels: Levels, targets: Loudnorm) -> dict[str, Any]:
    """Return the loudness of the material, the processed audio and the output."""
    return {
        "source": _integrated(levels.source),
        "processed": _integrated(levels.processed),
        "rendered": _integrated(levels.rendered),
        "target": targets.i,
        "tolerance": LOUDNESS_TOLERANCE,
    }


def _covering_cut(segment: Segment, cuts: Sequence[Cut]) -> str | None:
    """Return the identifier of the approved cut that swallows *segment*."""
    for cut in cuts:
        if to_ms(segment.start) >= to_ms(cut.start) and to_ms(segment.end) <= to_ms(
            cut.end
        ):
            return cut.id
    return None


def _cps_warnings(
    displayed: Sequence[tuple[str, float]], texts: dict[str, str], limit: float
) -> list[dict[str, Any]]:
    """Return the entries reading faster than *limit* characters per second."""
    warnings = []
    for segment_id, seconds in displayed:
        if seconds <= 0:
            continue  # already reported as a min_display violation
        cps = len(texts[segment_id]) / seconds
        if cps > limit:
            warnings.append(
                {
                    "segment_id": segment_id,
                    "cps": round(cps, 2),
                    "threshold": limit,
                }
            )
    return warnings


def _empty_subtitles() -> dict[str, Any]:
    """Return the subtitle section with every key present and nothing in it."""
    return {
        "entries": None,
        "warnings": {"dropped_by_cut": [], "min_display": [], "max_cps": []},
    }


def _subtitle_section(
    transcript: Transcript,
    approved: Sequence[Cut],
    timeline: Timeline,
    limits: SubtitleProfile,
) -> dict[str, Any]:
    """Return how many subtitle entries survive the cuts, and what to look at.

    Three things are worth a second look after mapping (design.md §5.6): an
    entry a cut deleted, one that flashes past below ``min_display``, and one
    nobody can read at ``max_cps``. All three lists are always present, empty
    included, because "no warnings" and "not measured" must not look alike.

    Args:
        transcript: The segments to map onto the cut timeline.
        approved: The cuts behind that timeline, kept alongside it because the
            mapping reports *that* an entry was dropped and this section has
            to say *which* cut dropped it.
        timeline: The mapping built from *approved*.
        limits: The readability thresholds from ``profile.json``.

    Returns:
        The ``subtitles`` section of ``report/stats.json``.
    """
    segments = transcript.segments
    mapped, remarks = timeline.map_segments(
        [(segment.id, segment.start, segment.end) for segment in segments],
        limits.min_display,
    )
    texts = {segment.id: segment.text for segment in segments}
    by_id = {segment.id: segment for segment in segments}
    dropped = [
        {
            "segment_id": remark["segment_id"],
            "cut_id": _covering_cut(by_id[remark["segment_id"]], approved),
        }
        for remark in remarks
        if remark["kind"] == "dropped_by_cut"
    ]
    short = [
        {
            "segment_id": remark["segment_id"],
            "display_sec": round(remark["value"], SECONDS_DECIMALS),
            "threshold": remark["threshold"],
        }
        for remark in remarks
        if remark["kind"] == "min_display"
    ]
    displayed = [(entry.segment_id, entry.end - entry.start) for entry in mapped]
    return {
        "entries": len(mapped),
        "warnings": {
            "dropped_by_cut": dropped,
            "min_display": short,
            "max_cps": _cps_warnings(displayed, texts, limits.max_cps),
        },
    }


# --------------------------------------------------------------------------- #
#  Measuring
# --------------------------------------------------------------------------- #


def _measure(
    path: Path, targets: Loudnorm, intervals: Sequence[tuple[float, float]]
) -> tuple[Measurement | None, list[str]]:
    """Measure *path*, reporting a failure as a warning instead of raising."""
    try:
        return audio_module.measure(path, targets, intervals), []
    except _RECOVERABLE as exc:
        return None, [f"{path.name} could not be measured ({exc})"]


def _silence(loaded: Project) -> tuple[list[tuple[float, float]], list[str]]:
    """Return the silent stretches of the source, or nothing with the reason."""
    try:
        intervals = audio_module.detect_silence(
            loaded.source_path, loaded.manifest.source.duration
        )
    except _RECOVERABLE as exc:
        return [], [
            f"the silence of the source could not be detected ({exc}); "
            "the noise floor was not measured"
        ]
    if not intervals:
        return [], ["no silence was found; the noise floor was not measured"]
    return intervals, []


def _levels(loaded: Project, inputs: Inputs) -> tuple[Levels, list[str]]:
    """Measure the material, the processed audio and the rendered output.

    Only ``audio/processed.wav`` has its noise floor read here, over the silent
    stretches of the source — the two share a timeline, so the intervals still
    point at the same moments. What denoising did to the floor is not measured
    here at all: that comparison has to be made before ``loudnorm`` runs, so
    ``audio-fix --stats`` makes it and this stage quotes it (#33). The rendered
    output lives on the cut timeline, where those intervals no longer mean
    anything — and where the silence has largely been removed on purpose — so
    only its loudness is measured.
    """
    targets = loaded.profile.audio.loudnorm
    source, warnings = _measure(loaded.source_path, targets, ())
    processed = None
    if inputs.audio is not None:
        intervals, detected = _silence(loaded)
        warnings += detected
        processed, failed = _measure(inputs.audio, targets, intervals)
        warnings += failed
    rendered = None
    if inputs.rendered is not None:
        rendered, failed = _measure(inputs.rendered, targets, ())
        warnings += failed
    return Levels(source, processed, rendered), warnings


def _rendered_duration(rendered: Path | None) -> tuple[float | None, list[str]]:
    """Return the length of the rendered output, or nothing with a warning."""
    if rendered is None:
        return None, []
    try:
        return _ffmpeg.duration(rendered), []
    except _RECOVERABLE as exc:
        return None, [f"the length of {RENDERED_NAME} could not be read ({exc})"]


# --------------------------------------------------------------------------- #
#  The stage
# --------------------------------------------------------------------------- #


def plan(loaded: Project) -> dict[str, Any]:
    """Return what :func:`run_report` would run and write, without doing it."""
    inputs = _read(loaded)
    targets = loaded.profile.audio.loudnorm
    source = loaded.source_path
    commands = [audio_module.measurement_command(source, targets)]
    if inputs.audio is not None:
        # The silence is only looked for when there is processed audio to read
        # the floor over; nothing else in the report uses it.
        commands.append(audio_module.silence_command(source))
    commands.extend(
        audio_module.measurement_command(path, targets)
        for path in (inputs.audio, inputs.rendered)
        if path is not None
    )
    windows = _boundaries.windows(inputs.cuts, loaded.manifest.source.duration)
    material = _material(loaded, inputs)
    workspace = loaded.root / f"{_boundaries.WORKSPACE_PREFIX}XXXXXX"
    for index, window in enumerate(windows):
        if material.audio is not None:
            commands.append(
                _boundaries.waveform_command(
                    material.audio,
                    window,
                    loaded.root / _boundaries.BOUNDARIES_DIR / window.image_name,
                )
            )
        clip = workspace / _boundaries.CLIP_FORMAT.format(index=index)
        commands.append(_boundaries.clip_command(material, window, clip))
    if windows:
        commands.append(
            _boundaries.separator_command(
                material, workspace / _boundaries.SEPARATOR_FILE
            )
        )
        commands.append(
            _boundaries.concat_command(
                workspace / _boundaries.CONCAT_FILE,
                workspace / _boundaries.DIGEST_FILE,
            )
        )
    return {
        "action": "report",
        "project": str(loaded.root),
        "commands": commands,
        "writes": [
            str(loaded.root / STATS_NAME),
            str(loaded.root / _boundaries.BOUNDARIES_DIR),
            str(loaded.root / _boundaries.DIGEST_NAME),
        ],
        "warnings": list(inputs.warnings),
    }


def _material(loaded: Project, inputs: Inputs) -> _boundaries.Material:
    """Return the files the boundary artifacts are cut out of."""
    return _boundaries.Material(
        root=loaded.root,
        source=loaded.source_path,
        audio=inputs.audio,
        video=loaded.manifest.source.video,
    )


def _subtitles(loaded: Project, inputs: Inputs) -> tuple[dict[str, Any], list[str]]:
    """Return the subtitle section, or an empty one with the reason why."""
    if inputs.transcript is None:
        return _empty_subtitles(), []
    duration = loaded.manifest.source.duration
    try:
        timeline = Timeline([(cut.start, cut.end) for cut in inputs.approved], duration)
    except ValueError as exc:
        return _empty_subtitles(), [f"the subtitles could not be mapped ({exc})"]
    section = _subtitle_section(
        inputs.transcript, inputs.approved, timeline, loaded.profile.subtitle
    )
    return section, []


def run_report(loaded: Project) -> Result:
    """Regenerate every report artifact for *loaded*.

    Args:
        loaded: The project to describe. Nothing in it is modified except the
            contents of ``report/``.

    Returns:
        The statistics document that was written, and every warning raised
        while collecting it. A missing input is a warning, never a failure.
    """
    inputs = _read(loaded)
    rendered_seconds, warnings = _rendered_duration(inputs.rendered)
    levels, measured = _levels(loaded, inputs)
    subtitles, mapped = _subtitles(loaded, inputs)
    windows = _boundaries.windows(inputs.cuts, loaded.manifest.source.duration)
    artifacts = _boundaries.generate(_material(loaded, inputs), windows)
    stats = {
        "version": STATS_VERSION,
        "generated_at": datetime.now(tz=UTC).astimezone().isoformat(),
        "duration": _duration_section(
            loaded.manifest.source.duration, rendered_seconds
        ),
        "cuts": _cut_section(inputs.cuts),
        "loudness": _loudness_section(levels, loaded.profile.audio.loudnorm),
        "noise_floor": {
            "denoise": audio_module.floor_section(inputs.noise_floor),
            "output": _output_floor(levels.processed),
        },
        "subtitles": subtitles,
        "artifacts": {
            "boundaries_png": len(artifacts.waveforms),
            "boundary_digest": artifacts.digest,
            "boundary_digest_sec": _seconds(artifacts.digest_seconds),
            "boundary_digest_expected_sec": _seconds(artifacts.expected_seconds),
        },
    }
    _publish(loaded, stats)
    return Result(
        stats,
        (
            *inputs.warnings,
            *warnings,
            *measured,
            *mapped,
            *artifacts.warnings,
        ),
    )


def _publish(loaded: Project, stats: dict[str, Any]) -> None:
    """Write ``report/stats.json`` atomically, creating ``report/`` if needed."""
    target = loaded.root / STATS_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    project_module.atomic_write_text(
        target, json.dumps(stats, indent=2, ensure_ascii=False) + "\n"
    )


def run_review(loaded: Project) -> ReviewResult:
    """List every cut candidate with the transcript around it (``--cuts``).

    Nothing is written and no media is generated: the listing is the fast path
    a reviewer runs between edits of ``cuts.json``.

    Returns:
        The listing as JSON and as text, plus the warning about a transcript
        that is not there to give context with.
    """
    inputs = _read(loaded)
    warnings = []
    if not (loaded.root / CUTS_NAME).is_file():
        warnings.append(
            f"{CUTS_NAME} not found; run `vidprep detect` for cut candidates"
        )
    if inputs.transcript is None:
        warnings.append(
            f"{transcribe_module.TRANSCRIPT_NAME} not found; "
            "the listing shows intervals without their text"
        )
    segments: Sequence[Segment] | None = (
        None if inputs.transcript is None else inputs.transcript.segments
    )
    entries = _review.review(inputs.cuts, segments)
    return ReviewResult(
        _review.to_dict(entries), tuple(warnings), tuple(_review.lines(entries))
    )
