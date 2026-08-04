"""Intervals of the original timeline, and the arithmetic detection needs.

Cut detection is interval arithmetic: silence is what the kept material leaves
between it, a filler cut is a segment widened into the quiet around it, and
"is this the cut somebody already reviewed?" is an overlap ratio. All of it
compares in whole milliseconds — the unit the schema rounds to (design.md
§3.6) — so a boundary stated in the design is a boundary the code can decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .models import to_ms

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class Span:
    """An interval of the original timeline, in seconds."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        """How long the interval lasts."""
        return self.end - self.start

    def overlap(self, other: Span) -> float:
        """Return how much of *other* falls inside this interval."""
        return max(0.0, min(self.end, other.end) - max(self.start, other.start))


@dataclass(frozen=True, slots=True)
class Candidate:
    """A cut one detector proposes, before it is given an identifier."""

    start: float
    end: float
    reason: str
    confidence: float
    status: Literal["proposed", "approved"]
    note: str | None = None


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    """Return *spans* ordered and pairwise disjoint, touching ones joined."""
    merged: list[Span] = []
    for span in sorted(spans, key=lambda item: (item.start, item.end)):
        if merged and to_ms(span.start) <= to_ms(merged[-1].end):
            merged[-1] = Span(merged[-1].start, max(merged[-1].end, span.end))
        else:
            merged.append(span)
    return merged


def complement(spans: Sequence[Span], duration: float) -> list[Span]:
    """Return the gaps between *spans* inside ``[0, duration]``."""
    gaps: list[Span] = []
    cursor = 0.0
    for span in spans:
        if to_ms(span.start) > to_ms(cursor):
            gaps.append(Span(cursor, min(span.start, duration)))
        cursor = max(cursor, span.end)
    if to_ms(cursor) < to_ms(duration):
        gaps.append(Span(cursor, duration))
    return [gap for gap in gaps if to_ms(gap.start) < to_ms(gap.end)]


def subtract(span: Span, others: Sequence[Span]) -> list[Span]:
    """Return what is left of *span* once every one of *others* is removed."""
    pieces = [span]
    for other in others:
        remaining: list[Span] = []
        for piece in pieces:
            if piece.overlap(other) <= 0:
                remaining.append(piece)
                continue
            if to_ms(piece.start) < to_ms(other.start):
                remaining.append(Span(piece.start, other.start))
            if to_ms(other.end) < to_ms(piece.end):
                remaining.append(Span(other.end, piece.end))
        pieces = remaining
    return pieces


def intersection_over_union(first: Span, second: Span) -> float:
    """Return the overlap of two intervals over their union, in milliseconds.

    Milliseconds rather than seconds because the result is compared against a
    threshold the design states exactly (design.md §3.4), and float seconds
    would make 0.500 a coin toss.
    """
    starts = (to_ms(first.start), to_ms(second.start))
    ends = (to_ms(first.end), to_ms(second.end))
    intersection = max(0, min(ends) - max(starts))
    union = max(ends) - min(starts)
    return intersection / union if union > 0 else 0.0
