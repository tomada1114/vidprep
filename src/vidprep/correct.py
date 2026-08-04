"""The ``correct`` stage: dictionary replacement and verified patch application.

Two ways of changing transcript text meet here, and both answer to the same
invariant: **only the text may change** (design.md §5.3). Segment identifiers,
timestamps, count and order are what every later stage — cut detection,
subtitle mapping, the report — joins on, so a correction that moved them would
silently invalidate work that already happened.

The dictionary side is deterministic and idempotent, so ``correct`` can be run
again after editing the dictionary without anyone having to remember whether it
already ran. The patch side is the opposite: the edits come from a language
model, which is exactly why they are checked against the transcript before
anything is written, and the result is checked again afterwards. A patch that
fails either check is rejected whole — never half-applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from . import _dictionary
from . import project as project_module
from .errors import (
    InvariantViolationError,
    PatchInvalidError,
    UsageError,
)
from .models import (
    Edit,
    SegmentId,
    Transcript,
    describe_validation_error,
    to_ms,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ._dictionary import AsrDictionary, Hit
    from .models import Segment
    from .project import Project

STAGE = "correct"
TRANSCRIPT_NAME = "transcript.json"

#: How an empty segment text is shown, so "cleared this segment" cannot be
#: mistaken for a rendering accident in the diff summary.
EMPTY_TEXT = "<empty>"


class PatchEdit(BaseModel):
    """One edit a patch asks for: a segment, and the text it should now hold."""

    model_config = ConfigDict(extra="forbid")

    id: SegmentId
    text: str


class Patch(BaseModel):
    """``patch.json`` — the LLM correction patch ``--apply-patch`` reads.

    Timestamps are absent from the schema by design: a patch cannot ask for
    them even if the model that wrote it wanted to. ``edits`` is required
    rather than defaulted, so a patch that lost its payload is a rejection
    instead of a silent no-op; an explicit ``"edits": []`` is still accepted.
    """

    model_config = ConfigDict(extra="forbid")

    edits: list[PatchEdit]


@dataclass(frozen=True, slots=True)
class SegmentChange:
    """One segment whose text a correction would change.

    Attributes:
        hits: The dictionary matches behind the change; empty for a patch,
            whose reasoning lives in the model that produced it.
    """

    id: str
    before: str
    after: str
    hits: tuple[Hit, ...] = ()

    def line(self) -> str:
        """Render the change as the two lines the diff summary shows."""
        reasons = "".join(f"  [dict/{hit.stage}] {hit.correct}" for hit in self.hits)
        return f"{self.id}: {_shown(self.before)}\n    -> {_shown(self.after)}{reasons}"

    def to_dict(self) -> dict[str, Any]:
        """Render the change as the JSON object ``--json`` prints."""
        return {
            "id": self.id,
            "before": self.before,
            "after": self.after,
            "hits": [
                {"stage": hit.stage, "matched": hit.matched, "correct": hit.correct}
                for hit in self.hits
            ],
        }


@dataclass(frozen=True, slots=True)
class Skip:
    """A dictionary match left alone because its entry is ``context``."""

    id: str
    hit: Hit

    def line(self) -> str:
        """Render the skip as the line ``--dry-run`` shows."""
        return (
            f"{self.id}: {_shown(self.hit.matched)} left alone "
            f"[skipped: confidence=context] {self.hit.correct}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Render the skip as the JSON object ``--json`` prints."""
        return {
            "id": self.id,
            "stage": self.hit.stage,
            "matched": self.hit.matched,
            "correct": self.hit.correct,
        }


@dataclass(frozen=True, slots=True)
class Plan:
    """What a correction would do, before it is allowed to do it.

    Attributes:
        tool: Which kind of correction this is, recorded in every ``edits``
            entry it appends.
        original: The transcript as it was read.
        corrected: The transcript as it would be written.
        changes: One entry per segment whose text differs.
        skipped: Dictionary matches reported but deliberately not applied.
        warnings: What degraded, e.g. the reading stage being unavailable.
        checks: Verifications already performed, shown before the confirmation
            prompt so the user knows what "yes" is agreeing to.
    """

    tool: Literal["dict", "llm"]
    original: Transcript
    corrected: Transcript
    changes: tuple[SegmentChange, ...] = ()
    skipped: tuple[Skip, ...] = ()
    warnings: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()

    def lines(self, *, verbose: bool = False) -> list[str]:
        """Render the diff summary, one block per changed segment.

        Args:
            verbose: Also list the matches that were deliberately skipped.
        """
        reported = [f"⚠ {warning}" for warning in self.warnings]
        if self.checks:
            reported.append("verified: " + ", ".join(self.checks))
        reported += [change.line() for change in self.changes]
        if verbose:
            reported += [skip.line() for skip in self.skipped]
        reported.append(f"{len(self.changes)} segments to update")
        return reported

    def to_dict(self, applied: int) -> dict[str, Any]:
        """Render the plan as the JSON object ``--json`` prints.

        Args:
            applied: How many segments were actually written; ``0`` for a
                plan that was only shown.
        """
        return {
            "action": STAGE,
            "tool": self.tool,
            "changed": len(self.changes),
            "applied": applied,
            "segments": [change.to_dict() for change in self.changes],
            "skipped": [skip.to_dict() for skip in self.skipped],
            "warnings": list(self.warnings),
        }


def _shown(text: str) -> str:
    """Return *text* for display, naming emptiness rather than showing nothing."""
    return text or EMPTY_TEXT


def load_transcript(loaded: Project) -> Transcript:
    """Read ``transcript.json`` from *loaded*.

    Raises:
        UsageError: If the project has no transcript yet.
        SchemaInvalidError: If the transcript violates its schema.
    """
    path = loaded.root / TRANSCRIPT_NAME
    if not path.is_file():
        msg = f"{path} not found; run `vidprep transcribe` first"
        raise UsageError(msg)
    return project_module.load_artifact(path, Transcript)


def plan_dictionary(
    loaded: Project,
    dictionary: AsrDictionary | None = None,
    reader: _dictionary.Reader | None = None,
) -> Plan:
    """Work out what the misconversion dictionary would change (design.md §3.7).

    Args:
        loaded: The project whose transcript is corrected.
        dictionary: Entries to apply; the packaged dictionary when omitted.
        reader: Analyser for the reading stage; resolved from the installed
            SudachiDict when omitted.

    Returns:
        The plan, which changes nothing until :func:`apply` is called.
    """
    transcript = load_transcript(loaded)
    entries = _dictionary.load_dictionary() if dictionary is None else dictionary
    warnings: tuple[str, ...] = ()
    if reader is None:
        reader = _dictionary.default_reader()
        if reader is None and entries.entries:
            warnings = (
                "no usable SudachiDict: matching by reading is skipped "
                "(run `vidprep doctor`)",
            )

    now = datetime.now(tz=UTC).astimezone()
    segments: list[Segment] = []
    changes: list[SegmentChange] = []
    skipped: list[Skip] = []
    for segment in transcript.segments:
        text, hits = _dictionary.correct_text(segment.text, entries, reader)
        skipped += [Skip(segment.id, hit) for hit in hits if not hit.applied]
        if text == segment.text:
            segments.append(segment)
            continue
        applied = tuple(hit for hit in hits if hit.applied)
        changes.append(SegmentChange(segment.id, segment.text, text, applied))
        segments.append(_rewrite(segment, text, "dict", now))
    return Plan(
        tool="dict",
        original=transcript,
        corrected=transcript.model_copy(update={"segments": segments}),
        changes=tuple(changes),
        skipped=tuple(skipped),
        warnings=warnings,
    )


def plan_patch(loaded: Project, path: Path) -> Plan:
    """Verify an LLM correction patch against the transcript (design.md §5.3).

    Args:
        loaded: The project whose transcript is corrected.
        path: The patch file to read.

    Returns:
        The plan, which changes nothing until :func:`apply` is called.

    Raises:
        PatchInvalidError: If the patch is unreadable, violates its schema,
            names a segment that does not exist, or names one twice. Every
            complaint is reported together and nothing is applied.
    """
    transcript = load_transcript(loaded)
    patch = _read_patch(path)
    _check_patch(patch, transcript)

    now = datetime.now(tz=UTC).astimezone()
    texts = {edit.id: edit.text for edit in patch.edits}
    segments: list[Segment] = []
    changes: list[SegmentChange] = []
    for segment in transcript.segments:
        text = texts.get(segment.id, segment.text)
        if text == segment.text:
            segments.append(segment)
            continue
        changes.append(SegmentChange(segment.id, segment.text, text))
        segments.append(_rewrite(segment, text, "llm", now))
    return Plan(
        tool="llm",
        original=transcript,
        corrected=transcript.model_copy(update={"segments": segments}),
        changes=tuple(changes),
        checks=(
            f"{len(patch.edits)} known ids",
            "no duplicates",
            "timings, count and order unchanged",
        ),
    )


def apply(loaded: Project, plan: Plan) -> int:
    """Write the corrected transcript and record the stage in the manifest.

    Returns:
        How many segments were changed; ``0`` leaves the file untouched, which
        is what makes a second dictionary run a no-op rather than a rewrite.

    Raises:
        InvariantViolationError: If anything but segment text differs. The
            transcript is not written, so the failure costs nothing.
    """
    _verify_shape(plan.original, plan.corrected)
    if plan.changes:
        project_module.write_json(loaded.root / TRANSCRIPT_NAME, plan.corrected)
    project_module.record_stage(loaded, STAGE)
    return len(plan.changes)


def _rewrite(
    segment: Segment, text: str, tool: Literal["dict", "llm"], now: datetime
) -> Segment:
    """Return *segment* with new *text*, its provenance and history updated."""
    return segment.model_copy(
        update={
            "text": text,
            "source": tool,
            "edits": [*segment.edits, Edit(at=now, tool=tool, before=segment.text)],
        }
    )


def _read_patch(path: Path) -> Patch:
    """Parse *path* as a patch.

    Raises:
        PatchInvalidError: If the file cannot be read or violates the schema.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PatchInvalidError([f"cannot read {path}: {exc.strerror}"]) from exc
    try:
        # Bytes, not text: pydantic reports undecodable input as invalid JSON
        # rather than letting a UnicodeDecodeError escape as a crash.
        return Patch.model_validate_json(raw)
    except ValidationError as exc:  # also raised for malformed JSON
        raise PatchInvalidError([describe_validation_error(exc)]) from exc


def _check_patch(patch: Patch, transcript: Transcript) -> None:
    """Check every edit against the transcript before any of them is applied.

    Raises:
        PatchInvalidError: Listing every unknown and every repeated identifier.
    """
    known = {segment.id for segment in transcript.segments}
    unknown: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for edit in patch.edits:
        if edit.id not in known and edit.id not in unknown:
            unknown.append(edit.id)
        if edit.id in seen and edit.id not in duplicates:
            duplicates.append(edit.id)
        seen.add(edit.id)
    details = [f"unknown segment id: {name}" for name in unknown]
    details += [f"duplicate segment id: {name}" for name in duplicates]
    if details:
        raise PatchInvalidError(details)


def _shape(transcript: Transcript) -> list[tuple[str, int, int]]:
    """Return everything about a transcript that a correction must not change."""
    return [
        (segment.id, to_ms(segment.start), to_ms(segment.end))
        for segment in transcript.segments
    ]


def _verify_shape(before: Transcript, after: Transcript) -> None:
    """Check that only text changed, after the correction was worked out.

    The patch schema cannot express a timestamp, so this can only fail through
    a bug in vidprep itself — which is precisely the case worth catching before
    the transcript every later stage joins on is overwritten.

    Raises:
        InvariantViolationError: Naming the first identifier that differs.
    """
    original, corrected = _shape(before), _shape(after)
    if len(original) != len(corrected):
        msg = (
            f"correction changed the segment count: {len(original)} -> {len(corrected)}"
        )
        raise InvariantViolationError(msg)
    for was, now in zip(original, corrected, strict=True):
        if was != now:
            msg = f"correction changed more than text: {was} -> {now}"
            raise InvariantViolationError(msg)
