"""The ``report --cuts`` listing: what each candidate removes, and from where.

A cut is judged on the words around it, not on its timestamps, so every
candidate is shown with the transcript segments it would delete and with the
one before and the one after left in place (design.md §5.6). That is the
material both a human and the ``review-cuts`` skill work from, which is why the
same content is available as JSON.

This module only reads: deciding a candidate's ``status`` is somebody else's
job, and ``report`` never writes ``cuts.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .models import to_ms

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .models import Cut, Segment

#: Printed instead of the context when there is no transcript to read.
NO_TRANSCRIPT = "(transcript.json not found: no context available)"

#: Printed when a candidate removes silence rather than speech.
NO_SPEECH = "(no speech removed)"


@dataclass(frozen=True, slots=True)
class Entry:
    """One cut candidate together with the transcript around it.

    Attributes:
        cut: The candidate being reviewed.
        removed: Segments the cut would delete whole, or ``None`` when there
            is no transcript — an empty tuple means "reviewed, removes no
            speech", which is a different answer.
        before: The last segment that starts before the cut, if any.
        after: The first segment that ends after the cut, if any.
    """

    cut: Cut
    removed: tuple[Segment, ...] | None
    before: Segment | None
    after: Segment | None


def _segment_dict(segment: Segment) -> dict[str, Any]:
    """Render one segment as the object the JSON listing carries."""
    return {
        "segment_id": segment.id,
        "start": round(segment.start, 3),
        "end": round(segment.end, 3),
        "text": segment.text,
    }


def _is_removed(segment: Segment, cut: Cut) -> bool:
    """Report whether *cut* swallows *segment* whole, as the mapping would."""
    return to_ms(segment.start) >= to_ms(cut.start) and to_ms(segment.end) <= to_ms(
        cut.end
    )


def _context(cut: Cut, segments: Sequence[Segment]) -> Entry:
    """Return *cut* with the segments it deletes and the ones that survive it."""
    removed = tuple(segment for segment in segments if _is_removed(segment, cut))
    kept = [segment for segment in segments if not _is_removed(segment, cut)]
    before = [item for item in kept if to_ms(item.start) < to_ms(cut.start)]
    after = [item for item in kept if to_ms(item.end) > to_ms(cut.end)]
    return Entry(
        cut=cut,
        removed=removed,
        before=before[-1] if before else None,
        after=after[0] if after else None,
    )


def review(cuts: Iterable[Cut], segments: Sequence[Segment] | None) -> list[Entry]:
    """Pair every candidate with its transcript context, in timeline order.

    Args:
        cuts: The candidates in ``cuts.json``, in any order.
        segments: The transcript, or ``None`` when it has not been produced;
            the listing then carries intervals only (REQ-020 boundary).

    Returns:
        One entry per cut, ordered by start then by cut id.
    """
    ordered = sorted(cuts, key=lambda cut: (cut.start, cut.end, cut.id))
    if segments is None:
        return [Entry(cut, None, None, None) for cut in ordered]
    in_order = sorted(segments, key=lambda segment: (segment.start, segment.end))
    return [_context(cut, in_order) for cut in ordered]


def to_dict(entries: Sequence[Entry]) -> dict[str, Any]:
    """Render the listing as the JSON document ``--cuts --json`` prints."""
    return {
        "cuts": [
            {
                "id": entry.cut.id,
                "start": round(entry.cut.start, 3),
                "end": round(entry.cut.end, 3),
                "duration": round(entry.cut.end - entry.cut.start, 3),
                "reason": entry.cut.reason,
                "status": entry.cut.status,
                "confidence": entry.cut.confidence,
                "note": entry.cut.note,
                "removed": None
                if entry.removed is None
                else [_segment_dict(segment) for segment in entry.removed],
                "before": None if entry.before is None else _segment_dict(entry.before),
                "after": None if entry.after is None else _segment_dict(entry.after),
            }
            for entry in entries
        ]
    }


def _headline(cut: Cut) -> str:
    """Render the identifying line of one candidate (REQ-021)."""
    return (
        f"{cut.id}  {cut.start:.3f}-{cut.end:.3f} ({cut.end - cut.start:.3f}s)  "
        f"reason={cut.reason}  status={cut.status}  conf={cut.confidence:.2f}"
    )


def _segment_line(marker: str, label: str, segment: Segment) -> str:
    """Render one context segment under its candidate."""
    return (
        f"  {marker} {label:<7} {segment.id} "
        f"({segment.start:.3f}-{segment.end:.3f})  {segment.text}"
    )


def _entry_lines(entry: Entry) -> list[str]:
    """Render one candidate and the transcript around it."""
    lines = [_headline(entry.cut)]
    if entry.cut.note:
        lines.append(f"    note: {entry.cut.note}")
    if entry.removed is None:
        lines.append(f"    {NO_TRANSCRIPT}")
        return lines
    if entry.before is not None:
        lines.append(_segment_line(" ", "before", entry.before))
    if entry.removed:
        lines += [_segment_line("✖", "removed", segment) for segment in entry.removed]
    else:
        lines.append(f"    {NO_SPEECH}")
    if entry.after is not None:
        lines.append(_segment_line(" ", "after", entry.after))
    return lines


def lines(entries: Sequence[Entry]) -> list[str]:
    """Render the whole listing for a human (REQ-020, REQ-021)."""
    if not entries:
        return ["no cut candidates to review"]
    return [line for entry in entries for line in _entry_lines(entry)]
