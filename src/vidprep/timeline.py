"""Mapping between the original and the cut timeline (design.md §4).

Video rendering and subtitle output must agree on where a moment in the source
ends up after the cuts are applied, so both share this single implementation:
if they ever disagree, the subtitles drift. Boundary fades do not overlap
(design.md §1, decision 9), which keeps the mapping a plain translation per
kept interval and makes the inverse exact.

The module is pure — it spawns nothing, reads nothing, writes nothing — and
cuts arrive already filtered to ``approved`` ones (design.md §3.4). Arithmetic
stays in float seconds; only :meth:`Timeline.map_segments`, whose output
becomes SRT timestamps, rounds, and comparisons go through milliseconds.
"""

from __future__ import annotations

import itertools
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple, NotRequired, TypedDict

from .models import MS_PER_SECOND, to_ms

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: Shortest subtitle display time that goes unremarked (design.md §3.6).
DEFAULT_MIN_DISPLAY = 0.8

#: Gap forced between two entries that touch after mapping, in milliseconds.
SEPARATION_MS = 1


class TimedSegment(NamedTuple):
    """A subtitle segment with its interval on one timeline."""

    segment_id: str
    start: float
    end: float


class SegmentWarning(TypedDict):
    """One remark about a mapped segment, for the report (design.md §5.6).

    ``value`` and ``threshold`` are present only for ``min_display``.
    """

    segment_id: str
    kind: Literal["dropped_by_cut", "min_display"]
    value: NotRequired[float]
    threshold: NotRequired[float]


@dataclass(slots=True)
class _Entry:
    """A segment being mapped, in milliseconds, plus its position in the input."""

    order: int
    segment_id: str
    start_ms: int
    end_ms: int

    def to_segment(self) -> TimedSegment:
        """Return this entry as rounded seconds."""
        return TimedSegment(
            self.segment_id, self.start_ms / MS_PER_SECOND, self.end_ms / MS_PER_SECOND
        )


def _check_interval(start: float, end: float, duration: float) -> None:
    """Reject an interval that is empty, inverted, or outside ``[0, duration]``."""
    if not start < end:
        msg = f"invalid interval: start({start}) must be < end({end})"
        raise ValueError(msg)
    if to_ms(start) < 0 or to_ms(end) > to_ms(duration):
        msg = f"interval must lie within [0, {duration}], got ({start}, {end})"
        raise ValueError(msg)


def normalize_cuts(
    cuts: Iterable[tuple[float, float]], duration: float
) -> tuple[tuple[float, float], ...]:
    """Return *cuts* as an ordered, pairwise disjoint interval list.

    Overlapping and merely touching intervals are merged, so the result is the
    ``C = [(a1, b1), ..., (an, bn)]`` the mapping formula assumes.

    Args:
        cuts: Cut intervals in original-timeline seconds, in any order.
        duration: Duration of the source, in seconds.

    Returns:
        The merged intervals, ordered by start.

    Raises:
        ValueError: If any interval is empty, inverted, or out of range.
    """
    ordered = sorted(cuts, key=lambda cut: (cut[0], cut[1]))
    merged: list[list[float]] = []
    for start, end in ordered:
        _check_interval(start, end, duration)
        if merged and to_ms(start) <= to_ms(merged[-1][1]):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


class Timeline:
    """The cut plan for one source, as a mapping in both directions.

    ``forward`` implements ``f(t) = t - removed(t)``, the total length cut out
    of ``[0, t)``; a ``t`` inside a cut therefore lands where its end lands.
    ``inverse`` undoes it from the same interval table, so the report and the
    re-transcription check (verification-plan.md §8.1) read the same boundaries.
    """

    __slots__ = ("_cuts", "_images", "_removed", "_starts", "duration")

    def __init__(self, cuts: Iterable[tuple[float, float]], duration: float) -> None:
        """Build the interval table.

        Args:
            cuts: Approved cut intervals in original-timeline seconds.
            duration: Duration of the source, in seconds.

        Raises:
            ValueError: If ``duration`` is not positive or a cut is invalid.
        """
        if duration <= 0:
            msg = f"duration must be positive, got {duration}"
            raise ValueError(msg)
        self.duration = duration
        self._cuts = normalize_cuts(cuts, duration)
        self._starts = [start for start, _ in self._cuts]
        # Total length removed before cut i, for i in 0..n — the translation
        # applied to the kept interval that precedes each cut.
        lengths = (end - start for start, end in self._cuts)
        self._removed = list(itertools.accumulate(lengths, initial=0.0))
        # Where each cut collapses to on the cut timeline, in increasing order.
        self._images = [
            start - offset
            for (start, _), offset in zip(self._cuts, self._removed, strict=False)
        ]

    @property
    def cuts(self) -> tuple[tuple[float, float], ...]:
        """The normalized, pairwise disjoint cut intervals."""
        return self._cuts

    @property
    def keeps(self) -> tuple[tuple[float, float], ...]:
        """The intervals the cuts leave behind, in order.

        Rendering removes material by keeping everything else, and it reads the
        keep list from here rather than deriving its own: the boundaries the
        video is cut at are then the boundaries the subtitles were mapped
        across, by construction. A cut touching the start or the end of the
        material leaves no zero-length interval behind for ``trim`` to choke on.
        """
        kept: list[tuple[float, float]] = []
        cursor = 0.0
        for start, end in self._cuts:
            if to_ms(cursor) < to_ms(start):
                kept.append((cursor, start))
            cursor = end
        if to_ms(cursor) < to_ms(self.duration):
            kept.append((cursor, self.duration))
        return tuple(kept)

    @property
    def removed_duration(self) -> float:
        """Total length removed by the cuts, in seconds."""
        return self._removed[-1]

    @property
    def cut_duration(self) -> float:
        """Duration of the cut timeline, in seconds."""
        return self.duration - self.removed_duration

    def forward(self, t: float) -> float:
        """Map an original-timeline second onto the cut timeline.

        Args:
            t: A second in ``[0, duration]``; inside a cut it maps to where
                both endpoints of that cut land.

        Returns:
            The corresponding second on the cut timeline, unrounded.

        Raises:
            ValueError: If ``t`` lies outside ``[0, duration]``.
        """
        if to_ms(t) < 0 or to_ms(t) > to_ms(self.duration):
            msg = f"t must be in [0, {self.duration}], got {t}"
            raise ValueError(msg)
        index = bisect_right(self._starts, t) - 1
        if index < 0:
            return t
        start, end = self._cuts[index]
        if t <= end:
            return start - self._removed[index]
        return t - self._removed[index + 1]

    def inverse(self, u: float) -> float:
        """Map a cut-timeline second back onto the original timeline.

        Args:
            u: A second in ``[0, cut_duration]``. A second a cut collapsed onto
                has two preimages; the earlier one — the cut's start — wins,
                except at ``cut_duration``, which maps to ``duration``.

        Returns:
            The corresponding second on the original timeline, unrounded.

        Raises:
            ValueError: If ``u`` lies outside ``[0, cut_duration]``.
        """
        limit = self.cut_duration
        if to_ms(u) < 0 or to_ms(u) > to_ms(limit):
            msg = f"u must be in [0, {limit}], got {u}"
            raise ValueError(msg)
        if to_ms(u) == to_ms(limit):
            return self.duration
        return u + self._removed[bisect_left(self._images, u)]

    def map_segments(
        self,
        segments: Sequence[tuple[str, float, float]],
        min_display: float = DEFAULT_MIN_DISPLAY,
    ) -> tuple[list[TimedSegment], list[SegmentWarning]]:
        """Map subtitle segments onto the cut timeline (design.md §4).

        A segment a cut swallows whole is dropped; a segment a cut overlaps at
        one end is clipped to the cut boundary; a segment containing a cut is
        *not* split — it keeps its text and displays for less time. Entries that
        end up touching are separated by a millisecond, so the result is always
        strictly ordered.

        Args:
            segments: ``(segment_id, start, end)`` triples in original seconds.
            min_display: Display time below which an entry is still emitted but
                reported, in seconds.

        Returns:
            The mapped segments in cut-timeline order, rounded to milliseconds,
            and the warnings raised while mapping them, in input order.

        Raises:
            ValueError: If a segment interval is empty, inverted, or out of range.
        """
        entries: list[_Entry] = []
        warnings: list[tuple[int, SegmentWarning]] = []
        for order, (segment_id, start, end) in enumerate(segments):
            _check_interval(start, end, self.duration)
            if self._is_swallowed(start, end):
                warnings.append(
                    (order, {"segment_id": segment_id, "kind": "dropped_by_cut"})
                )
                continue
            clipped_start, clipped_end = self._clip(start, end)
            entries.append(
                _Entry(
                    order,
                    segment_id,
                    to_ms(self.forward(clipped_start)),
                    to_ms(self.forward(clipped_end)),
                )
            )
        entries.sort(key=lambda entry: (entry.start_ms, entry.end_ms))
        _separate(entries)
        warnings.extend(_min_display_warnings(entries, min_display))
        warnings.sort(key=lambda item: item[0])
        mapped = [entry.to_segment() for entry in entries]
        return mapped, [warning for _, warning in warnings]

    def _is_swallowed(self, start: float, end: float) -> bool:
        """Report whether one cut contains the whole ``[start, end]`` segment."""
        index = bisect_right(self._starts, start) - 1
        return index >= 0 and to_ms(end) <= to_ms(self._cuts[index][1])

    def _clip(self, start: float, end: float) -> tuple[float, float]:
        """Pull an end that overlaps a cut back to that cut's boundary."""
        index = bisect_right(self._starts, start) - 1
        if index >= 0 and to_ms(start) < to_ms(self._cuts[index][1]):
            start = self._cuts[index][1]
        index = bisect_left(self._starts, end) - 1
        if index >= 0 and to_ms(end) <= to_ms(self._cuts[index][1]):
            end = self._cuts[index][0]
        return start, end


def _separate(entries: list[_Entry]) -> None:
    """Trim ends in place so consecutive entries never touch or overlap."""
    for current, following in itertools.pairwise(entries):
        if current.end_ms >= following.start_ms:
            current.end_ms = max(current.start_ms, following.start_ms - SEPARATION_MS)


def _min_display_warnings(
    entries: list[_Entry], min_display: float
) -> list[tuple[int, SegmentWarning]]:
    """Report entries whose final display time falls under ``min_display``."""
    threshold_ms = to_ms(min_display)
    return [
        (
            entry.order,
            {
                "segment_id": entry.segment_id,
                "kind": "min_display",
                "value": (entry.end_ms - entry.start_ms) / MS_PER_SECOND,
                "threshold": min_display,
            },
        )
        for entry in entries
        if entry.end_ms - entry.start_ms < threshold_ms
    ]
