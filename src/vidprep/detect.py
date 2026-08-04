"""The ``detect`` stage: silence, filler words, and a merge that keeps reviews.

Three things meet here (design.md §5.4). auto-editor decides where the material
is quiet (:mod:`vidprep._autoeditor`); the transcript and the speech regions
behind it decide where a filler word can be cut out (:mod:`vidprep._fillers`);
and the merge rules in this module decide what happens to the candidates a
human already judged. The third is the one that matters most: detection is
meant to be re-run after every parameter change, and a re-run that threw away
"I already listened to this one and said no" would make the review pointless.

Cut candidates are proposals, not edits — ``render`` applies the ``approved``
ones and nothing else — so the stage errs towards offering too much rather than
too little, with one exception it will not make: a cut may not remove speech.
Every ``silence`` cut is checked against the speech the transcript and Silero
VAD agree on, and detection fails rather than publishing a cut that talks over
somebody (verification-plan.md §7).

What is written: ``cuts.json``, merged with whatever was there before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from . import _autoeditor, _ffmpeg, _fillers
from . import audio as audio_module
from . import doctor as doctor_module
from . import project as project_module
from . import transcribe as transcribe_module
from ._intervals import Candidate, Span, intersection_over_union
from .errors import (
    InvariantViolationError,
    SchemaInvalidError,
    UsageError,
)
from .models import (
    Cut,
    Cuts,
    Transcript,
    VadReport,
    describe_validation_error,
    to_ms,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .project import Project

STAGE = "detect"
CUTS_NAME = "cuts.json"

#: ``reason`` values this stage produces, plus the one only a human writes.
SILENCE = "silence"
FILLER = _fillers.REASON
MANUAL = "manual"

#: Candidates and existing cuts are the same cut when they overlap by at least
#: this much of their union (design.md §3.4).
MERGE_IOU = 0.5

#: How much speech one ``silence`` cut may overlap before the run is refused,
#: in seconds — the padding is allowed to graze a word, nothing more
#: (verification-plan.md §7).
MAX_SPEECH_OVERLAP = 0.2

#: Silence detection is the reliable half of this stage, which is why its
#: candidates arrive approved (design.md §1, decision 8).
SILENCE_CONFIDENCE = 0.95

_ID_FORMAT = "c{:04d}"
_MAX_CUT_ID = 9999


@dataclass(frozen=True, slots=True)
class MergeResult:
    """The outcome of merging fresh candidates into the reviewed cuts."""

    cuts: tuple[Cut, ...]
    matched: int
    kept_unmatched: int
    dropped_proposed: int
    added: int
    demoted: tuple[str, ...]
    next_id: str


# --------------------------------------------------------------------------- #
#  The merge (design.md §3.4)
# --------------------------------------------------------------------------- #


def _pair_up(
    existing: Sequence[Cut], candidates: Sequence[Candidate]
) -> dict[int, int]:
    """Match existing cuts to candidates of the same reason by overlap.

    Returns:
        Candidate index -> existing index, best overlap first, one to one.
    """
    scored: list[tuple[float, int, int]] = []
    for candidate_index, candidate in enumerate(candidates):
        for existing_index, cut in enumerate(existing):
            if cut.reason != candidate.reason:
                continue
            score = intersection_over_union(
                Span(cut.start, cut.end), Span(candidate.start, candidate.end)
            )
            if score >= MERGE_IOU:
                scored.append((score, candidate_index, existing_index))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    pairs: dict[int, int] = {}
    taken: set[int] = set()
    for _, candidate_index, existing_index in scored:
        if candidate_index in pairs or existing_index in taken:
            continue
        pairs[candidate_index] = existing_index
        taken.add(existing_index)
    return pairs


def _next_id(existing: Sequence[Cut]) -> int:
    """Return the first identifier number never used by *existing*."""
    numbers = [int(cut.id[1:]) for cut in existing]
    return max(numbers, default=0) + 1


def _is_reviewed(cut: Cut) -> bool:
    """Report whether an unmatched cut survives detection (design.md §3.4).

    ``approved`` and ``rejected`` are decisions, and a ``manual`` cut was
    written by hand; only an untouched ``proposed`` candidate is this stage's
    to withdraw.
    """
    return cut.status in {"approved", "rejected"} or cut.reason == MANUAL


def merge(existing: Sequence[Cut], candidates: Sequence[Candidate]) -> MergeResult:
    """Merge fresh candidates into the reviewed cuts (design.md §3.4).

    A candidate that matches an existing cut updates its interval and
    confidence and changes nothing else: the identifier, the status and the
    note are the review, and the review outlives the parameters it was made
    under. An existing cut nothing matched is kept unless it is an untouched
    proposal. New candidates are numbered above every identifier the file
    holds, so a withdrawn proposal's number is not handed to a different
    interval (REQ-023).

    Args:
        existing: The cuts already in ``cuts.json``.
        candidates: What this run detected.

    Returns:
        The merged cuts in timeline order, with the counts that describe what
        the merge did.

    Raises:
        InvariantViolationError: If the project needs more cuts than the
            identifier format can express.
    """
    pairs = _pair_up(existing, candidates)
    matched_existing = set(pairs.values())
    number = _next_id(existing)
    cuts: list[Cut] = []
    fresh: set[str] = set()
    for index, candidate in enumerate(candidates):
        partner = existing[pairs[index]] if index in pairs else None
        if partner is None:
            if number > _MAX_CUT_ID:
                msg = f"a project cannot hold more than {_MAX_CUT_ID} cuts"
                raise InvariantViolationError(msg)
            identifier = _ID_FORMAT.format(number)
            number += 1
            fresh.add(identifier)
        else:
            identifier = partner.id
        cuts.append(
            Cut(
                id=identifier,
                start=candidate.start,
                end=candidate.end,
                reason=candidate.reason,
                confidence=candidate.confidence,
                status=partner.status if partner is not None else candidate.status,
                note=partner.note if partner is not None else candidate.note,
            )
        )
    kept = [
        cut
        for index, cut in enumerate(existing)
        if index not in matched_existing and _is_reviewed(cut)
    ]
    dropped = len(existing) - len(matched_existing) - len(kept)
    ordered, demoted = _resolve_overlaps([*kept, *cuts], fresh)
    return MergeResult(
        cuts=tuple(ordered),
        matched=len(pairs),
        kept_unmatched=len(kept),
        dropped_proposed=dropped,
        added=len(candidates) - len(pairs),
        demoted=demoted,
        next_id=_ID_FORMAT.format(number),
    )


def _resolve_overlaps(
    cuts: Sequence[Cut], fresh: set[str]
) -> tuple[list[Cut], tuple[str, ...]]:
    """Keep the ``approved`` cuts from overlapping each other (REQ-040).

    Two approved cuts can only overlap after a re-run moved one of them onto a
    cut somebody approved elsewhere. Nothing is thrown away: the later of the
    two goes back to ``proposed`` so its interval, note and reason survive for
    the next review, and a cut this run invented gives way to one a human
    already judged.

    Returns:
        The cuts in timeline order, and the identifiers that were demoted.
    """
    order = sorted(
        cuts, key=lambda cut: (cut.id in fresh, to_ms(cut.start), to_ms(cut.end))
    )
    approved: list[Cut] = []
    demoted: list[str] = []
    resolved: list[Cut] = []
    for cut in order:
        if cut.status != "approved":
            resolved.append(cut)
            continue
        clash = any(
            to_ms(min(cut.end, other.end)) > to_ms(max(cut.start, other.start))
            for other in approved
        )
        if clash:
            demoted.append(cut.id)
            resolved.append(cut.model_copy(update={"status": "proposed"}))
            continue
        approved.append(cut)
        resolved.append(cut)
    resolved.sort(key=lambda cut: (to_ms(cut.start), to_ms(cut.end), cut.id))
    return resolved, tuple(sorted(demoted))


# --------------------------------------------------------------------------- #
#  Verification
# --------------------------------------------------------------------------- #


def speech_overlaps(
    cuts: Sequence[Cut], spoken: Sequence[Span]
) -> list[tuple[Cut, float]]:
    """Return every ``silence`` cut with how much speech it would remove."""
    return [
        (cut, sum(span.overlap(Span(cut.start, cut.end)) for span in spoken))
        for cut in cuts
        if cut.reason == SILENCE
    ]


def verify_speech(cuts: Sequence[Cut], spoken: Sequence[Span]) -> float:
    """Check that no silence cut talks over the speech (REQ-041).

    Args:
        cuts: The merged cuts about to be written.
        spoken: The speech the transcript and the VAD agree on.

    Returns:
        The largest overlap found, in seconds, for the run report.

    Raises:
        InvariantViolationError: If one cut would remove more than
            :data:`MAX_SPEECH_OVERLAP` of speech. This is the cross-check
            between two independent detectors and the reason cut detection can
            be trusted to run unattended, so the run is discarded rather than
            written (verification-plan.md §7).
    """
    measured = speech_overlaps(cuts, spoken)
    over = [
        (cut, overlap)
        for cut, overlap in measured
        if to_ms(overlap) > to_ms(MAX_SPEECH_OVERLAP)
    ]
    if over:
        shown = ", ".join(
            f"{cut.id}({cut.start:.3f}-{cut.end:.3f}) removes {overlap:.3f}s"
            for cut, overlap in over[:5]
        )
        msg = (
            f"{len(over)} silence cuts would remove more than "
            f"{MAX_SPEECH_OVERLAP:.1f}s of speech ({shown}); {CUTS_NAME} was "
            "left untouched"
        )
        raise InvariantViolationError(msg)
    return max((overlap for _, overlap in measured), default=0.0)


def segments_over_silence(
    cuts: Sequence[Cut], speech: _fillers.Speech, spoken: Sequence[Span]
) -> list[str]:
    """Return the segments whose timestamps run over a silence cut.

    Their words are safe — :func:`verify_speech` proved no speech is removed —
    but a segment claiming to last through a silence is a segment whose end the
    recogniser guessed, and every subtitle built from it inherits the guess.
    Reported, never deleted: the transcript belongs to ``transcribe``, and this
    is the second line of defence behind its own hallucination check
    (design.md §5.2).
    """
    silence_cuts = [Span(cut.start, cut.end) for cut in cuts if cut.reason == SILENCE]
    flagged: list[str] = []
    for segment in speech.segments:
        span = Span(segment.start, segment.end)
        claimed = sum(cut.overlap(span) for cut in silence_cuts)
        real = sum(
            cut.overlap(piece)
            for cut in silence_cuts
            for piece in spoken
            if piece.overlap(span) > 0
        )
        if to_ms(claimed - real) > to_ms(MAX_SPEECH_OVERLAP):
            flagged.append(segment.id)
    return flagged


# --------------------------------------------------------------------------- #
#  The stage
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Result:
    """What one ``detect`` run found, merged and verified."""

    auto_editor_version: str
    silence_detected: int
    silence_dropped: int
    silence_seconds: float
    filler_detected: int
    filler_seconds: float
    in_sentence: int
    weak_enabled: bool
    merged: MergeResult
    max_speech_overlap: float | None
    flagged_segments: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def silence_cuts(self) -> int:
        """How many silence cuts survived the padding."""
        return self.silence_detected - self.silence_dropped

    def to_dict(self) -> dict[str, Any]:
        """Render the result as the JSON document ``--json`` prints."""
        identifiers = sorted(cut.id for cut in self.merged.cuts)
        return {
            "auto_editor_version": self.auto_editor_version,
            "silence": {
                "detected": self.silence_detected,
                "after_padding": self.silence_cuts,
                "dropped_by_min_cut_duration": self.silence_dropped,
                "total_sec": round(self.silence_seconds, 3),
                "status": "approved",
            },
            "filler": {
                "candidates": self.filler_detected,
                "in_sentence_notes": self.in_sentence,
                "total_sec": round(self.filler_seconds, 3),
                "status": "proposed",
                "weak_enabled": self.weak_enabled,
            },
            "merged": {
                "matched": self.merged.matched,
                "kept_unmatched": self.merged.kept_unmatched,
                "dropped_proposed": self.merged.dropped_proposed,
                "new": self.merged.added,
                "demoted": list(self.merged.demoted),
            },
            "speech": {
                # None when there is no transcript to check the cuts against.
                "max_overlap_sec": None
                if self.max_speech_overlap is None
                else round(self.max_speech_overlap, 3),
                "limit_sec": MAX_SPEECH_OVERLAP,
                "segments_over_silence": list(self.flagged_segments),
            },
            "cuts_total": len(self.merged.cuts),
            "id_range": [identifiers[0], identifiers[-1]] if identifiers else [],
            "next_id": self.merged.next_id,
            "output": CUTS_NAME,
        }

    def lines(self) -> list[str]:
        """Render the result for a human."""
        merged = self.merged
        lines = [f"⚠ {warning}" for warning in self.warnings]
        lines.append(
            f"✔ silence: {self.silence_cuts} cuts, {self.silence_seconds:.1f}s "
            f"(approved; {self.silence_dropped} dropped under min_cut_duration)"
        )
        lines.append(
            f"✔ filler: {self.filler_detected} candidates, "
            f"{self.filler_seconds:.1f}s "
            f"(proposed; {self.in_sentence} mid-sentence fillers left alone)"
        )
        lines.append(
            f"✔ merged: {merged.matched} matched, {merged.kept_unmatched} kept, "
            f"{merged.dropped_proposed} withdrawn, {merged.added} new "
            f"→ {len(merged.cuts)} cuts in {CUTS_NAME}"
        )
        if merged.demoted:
            lines.append(
                f"⚠ {', '.join(merged.demoted)} overlapped an approved cut and "
                "went back to proposed"
            )
        if self.flagged_segments:
            shown = ", ".join(self.flagged_segments[:5])
            lines.append(
                f"⚠ {len(self.flagged_segments)} transcript segments run over a "
                f"silence cut ({shown}); their end timestamps are unreliable"
            )
        return lines


def _audio_path(loaded: Project) -> Path:
    """Return the processed audio silence detection runs on.

    Raises:
        UsageError: If ``audio-fix`` has not produced it yet.
    """
    path = loaded.root / audio_module.OUTPUT_NAME
    if not path.is_file():
        msg = f"{audio_module.OUTPUT_NAME} not found — run `vidprep audio-fix` first"
        raise UsageError(msg)
    return path


def _auto_editor() -> tuple[str, str]:
    """Return the auto-editor executable and its version.

    Raises:
        UsageError: If it is missing or too old for ``--export v3`` (REQ-002).
    """
    check = doctor_module.check_auto_editor()
    if not check["ok"]:
        msg = f"auto-editor is not usable: {check.get('error')}"
        raise UsageError(msg)
    return str(check["path"]), str(check["version"])


def plan(loaded: Project) -> dict[str, Any]:
    """Return what :func:`run_detect` would run and write, without doing it."""
    audio = _audio_path(loaded)
    return {
        "action": "detect",
        "project": str(loaded.root),
        "commands": [_autoeditor.command(audio, loaded.profile.silence)],
        "writes": [
            str(loaded.root / CUTS_NAME),
            str(loaded.root / project_module.MANIFEST_NAME),
        ],
    }


def _load_speech(loaded: Project) -> tuple[_fillers.Speech | None, list[str]]:
    """Load the transcript and the speech regions filler detection needs.

    Returns:
        What was read, or ``None`` with the warning explaining why filler
        detection is being skipped this run.
    """
    duration = loaded.manifest.source.duration
    transcript_path = loaded.root / transcribe_module.TRANSCRIPT_NAME
    vad_path = loaded.root / transcribe_module.VAD_REPORT_NAME
    missing = [
        str(name)
        for name, path in (
            (transcribe_module.TRANSCRIPT_NAME, transcript_path),
            (transcribe_module.VAD_REPORT_NAME, vad_path),
        )
        if not path.is_file()
    ]
    if missing:
        return None, [
            f"{', '.join(missing)} not found — detecting silence only; run "
            "`vidprep transcribe` for filler candidates"
        ]
    transcript = project_module.load_artifact(transcript_path, Transcript, duration)
    vad = project_module.load_artifact(vad_path, VadReport, duration)
    return (
        _fillers.Speech(
            segments=tuple(transcript.segments),
            regions=tuple(Span(region.start, region.end) for region in vad.segments),
            duration=duration,
        ),
        [],
    )


def _load_cuts(loaded: Project) -> list[Cut]:
    """Return the cuts already in the project, or nothing on the first run."""
    path = loaded.root / CUTS_NAME
    if not path.is_file():
        return []
    duration = loaded.manifest.source.duration
    return list(project_module.load_artifact(path, Cuts, duration).cuts)


def _publish(loaded: Project, cuts: Sequence[Cut]) -> None:
    """Write ``cuts.json``, validated against every invariant (REQ-040).

    Raises:
        SchemaInvalidError: If the merged cuts break the schema — duplicate
            identifiers, an interval past the end of the material, or two
            approved cuts overlapping.
    """
    payload = {"version": "1", "cuts": [cut.model_dump(mode="json") for cut in cuts]}
    duration = loaded.manifest.source.duration
    try:
        validated = Cuts.model_validate(payload, context={"duration": duration})
    except ValidationError as exc:
        msg = f"{CUTS_NAME}: {describe_validation_error(exc)}"
        raise SchemaInvalidError(msg) from exc
    project_module.write_json(loaded.root / CUTS_NAME, validated)


def _silence_candidates(
    loaded: Project, auto_editor: tuple[str, str]
) -> tuple[list[Span], int, int]:
    """Run auto-editor and turn its timeline into cuttable intervals.

    Args:
        loaded: The project being detected in.
        auto_editor: The executable to run and the version it reports.

    Returns:
        The padded intervals, how many silences were detected, and how many of
        those the padding left too short to cut.
    """
    executable, version = auto_editor
    silence = loaded.profile.silence
    _, *arguments = _autoeditor.command(_audio_path(loaded), silence)
    timeline = _autoeditor.parse_timeline(
        _ffmpeg.run([executable, *arguments]), version
    )
    detected = _autoeditor.silence_spans(
        _autoeditor.kept_spans(timeline),
        loaded.manifest.source.duration,
        silence.min_duration,
    )
    cuttable, dropped = _autoeditor.pad_spans(detected, silence)
    return cuttable, len(detected), dropped


def run_detect(loaded: Project) -> Result:
    """Detect the cut candidates of *loaded*, merge them in and record the stage.

    Returns:
        What was detected and merged, once it has passed verification.

    Raises:
        UsageError: If ``audio-fix`` has not run, or auto-editor is unusable.
        TimelineSchemaError: If auto-editor exported a timeline shape vidprep
            does not know (exit ``2``).
        InvariantViolationError: If a silence cut would remove speech; nothing
            is written in that case.
    """
    auto_editor = _auto_editor()
    version = auto_editor[1]
    cuttable, detected, dropped = _silence_candidates(loaded, auto_editor)
    candidates: list[Candidate] = [
        Candidate(
            start=span.start,
            end=span.end,
            reason=SILENCE,
            confidence=SILENCE_CONFIDENCE,
            status="approved",
        )
        for span in cuttable
    ]
    speech, warnings = _load_speech(loaded)
    fillers: list[Candidate] = []
    in_sentence = 0
    if speech is not None:
        fillers, in_sentence = _fillers.candidates(
            speech, loaded.profile, _fillers.load_dictionary(loaded.root), cuttable
        )
        candidates += fillers
    merged = merge(_load_cuts(loaded), candidates)
    spoken = speech.spoken_spans() if speech is not None else []
    overlap = verify_speech(merged.cuts, spoken) if speech is not None else None
    flagged = (
        segments_over_silence(merged.cuts, speech, spoken) if speech is not None else []
    )
    _publish(loaded, merged.cuts)
    project_module.record_stage(loaded, STAGE, {"auto_editor": version})
    return Result(
        auto_editor_version=version,
        silence_detected=detected,
        silence_dropped=dropped,
        silence_seconds=sum(
            cut.end - cut.start for cut in merged.cuts if cut.reason == SILENCE
        ),
        filler_detected=len(fillers),
        filler_seconds=sum(
            cut.end - cut.start for cut in merged.cuts if cut.reason == FILLER
        ),
        in_sentence=in_sentence,
        weak_enabled=loaded.profile.filler.enable_weak,
        merged=merged,
        max_speech_overlap=overlap,
        flagged_segments=tuple(flagged),
        warnings=tuple(warnings),
    )
