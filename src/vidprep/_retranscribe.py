"""The arithmetic behind the re-transcription check (verification-plan.md §8.1).

Nothing here spawns a recogniser; this module only knows how to build the text
the output *should* say, how to compare it with what a second pass actually
heard, and how to put a difference back on the original timeline.
:mod:`vidprep.verify` supplies the recogniser and turns the result into a
report.

The idea the check rests on: the same model over the same audio makes the same
mistakes, so its habitual errors appear on both sides and cancel out, and what
is left over concentrates on the words a cut removed by accident. That only
holds while both sides are reduced the same way, which is why every comparison
runs through :func:`vidprep._text.normalize`.
"""

from __future__ import annotations

import difflib
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import _fillers, _text
from .models import to_ms
from .timeline import DEFAULT_MIN_DISPLAY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import Cut, Segment
    from .timeline import Timeline

#: Shortest difference worth reporting (verification-plan.md §8.1-4). A single
#: character is below the noise floor of two runs of the same recogniser.
MIN_HUNK_CHARS = 2

#: How close to a cut boundary a difference must land to be blamed on the cut
#: (verification-plan.md §8.1-4). Exactly 2.000s counts; 2.001s does not.
BOUNDARY_WINDOW = 2.0

SECONDS_DECIMALS = 3


@dataclass(frozen=True, slots=True)
class ExpectedSegment:
    """One transcript segment as it should be heard in the rendered output.

    Attributes:
        segment_id: The transcript segment this text came from.
        text: Its text, normalised and with any cut-away filler removed.
        start: Where the segment begins on the cut timeline, in seconds.
        end: Where it ends on the cut timeline, in seconds.
    """

    segment_id: str
    text: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class ExpectedText:
    """The concatenated expectation, plus the table that dates each character.

    ``offsets[i]`` is where ``segments[i]`` starts inside :attr:`text`, which is
    what lets a difference found in a flat string be traced back to the segment
    — and therefore to the moment — it belongs to.
    """

    segments: tuple[ExpectedSegment, ...]
    text: str
    offsets: tuple[int, ...]

    def locate(self, index: int) -> tuple[str, float]:
        """Return the segment and the cut-timeline second at character *index*.

        The character is placed by linear interpolation across its segment: a
        transcript has no word timings, so the only honest assumption is that
        the segment was spoken at an even pace.

        Raises:
            IndexError: If the text is empty, which leaves nothing to locate.
        """
        if not self.segments:
            msg = "the expected text is empty"
            raise IndexError(msg)
        position = bisect_right(self.offsets, index) - 1
        position = min(max(position, 0), len(self.segments) - 1)
        segment = self.segments[position]
        within = index - self.offsets[position]
        ratio = within / len(segment.text) if segment.text else 0.0
        span = segment.end - segment.start
        return segment.segment_id, segment.start + min(max(ratio, 0.0), 1.0) * span


@dataclass(frozen=True, slots=True)
class MissingHunk:
    """A run of characters the expectation has and the second pass does not."""

    text: str
    index: int


@dataclass(frozen=True, slots=True)
class BoundaryFlag:
    """A missing hunk that landed close enough to a cut to blame it.

    Attributes:
        cut_id: The nearest cut boundary's cut.
        src_time: Where the hunk sits on the original timeline, in seconds.
        missing: The characters that went missing.
    """

    cut_id: str
    src_time: float
    missing: str

    def to_dict(self) -> dict[str, object]:
        """Render the flag as it appears in ``--json`` output (REQ-006)."""
        return {
            "cut_id": self.cut_id,
            "src_time": round(self.src_time, SECONDS_DECIMALS),
            "missing": self.missing,
            "len": len(self.missing),
        }


def _overlaps(cut: Cut, segment: Segment) -> bool:
    """Report whether *cut* removes any part of *segment*."""
    return to_ms(cut.start) < to_ms(segment.end) and to_ms(cut.end) > to_ms(
        segment.start
    )


def _removed_by(
    cut: Cut, segment: Segment, reading: _fillers.Reading
) -> list[tuple[int, int]]:
    """Return the character ranges of *reading* that *cut* took out.

    A cut reaching the start of the segment took the filler it opens with, and
    one reaching the end took the filler it ends with; both boundaries are
    exact, because that is where :mod:`vidprep._fillers` put them. A cut sitting
    wholly inside the segment — which only a hand-edited ``cuts.json`` produces,
    since detection never proposes one — is placed by the same even-pace
    assumption the rest of this module makes, and takes the fillers it covers.
    """
    if to_ms(cut.start) <= to_ms(segment.start) and reading.leading is not None:
        return [(0, reading.leading.end)]
    if to_ms(cut.end) >= to_ms(segment.end) and reading.trailing is not None:
        return [(reading.trailing.start, len(reading.text))]
    span = segment.end - segment.start
    if span <= 0:
        return []
    scale = len(reading.text) / span
    first = (cut.start - segment.start) * scale
    last = (cut.end - segment.start) * scale
    return [
        (hit.start, hit.end)
        for hit in reading.inside
        if hit.start < last and hit.end > first
    ]


def _without_fillers(
    segment: Segment,
    filler_cuts: Sequence[Cut],
    words: Sequence[tuple[str, _fillers.Tier]],
) -> str:
    """Return the segment's text with the fillers an approved cut removed.

    Which word a cut took is read back from the filler dictionary rather than
    from the cut's note, so a reviewer rewording the note cannot change what the
    check expects.

    A segment no filler cut touches is returned untouched, so the fillers'
    slightly stricter normalisation only applies where a word was actually taken
    out.
    """
    touching = [cut for cut in filler_cuts if _overlaps(cut, segment)]
    if not touching:
        return segment.text
    reading = _fillers.scan(segment.text, words)
    dropped: set[int] = set()
    for cut in touching:
        for start, end in _removed_by(cut, segment, reading):
            dropped.update(range(start, end))
    return "".join(
        char for index, char in enumerate(reading.text) if index not in dropped
    )


def build_expected(
    segments: Sequence[Segment],
    timeline: Timeline,
    approved: Sequence[Cut],
    words: Sequence[tuple[str, _fillers.Tier]] = (),
) -> ExpectedText:
    """Return what the rendered output should say, in cut-timeline order.

    The kept segments are the ones :meth:`vidprep.timeline.Timeline.map_segments`
    leaves — the same mapping the subtitles were built with, so the expectation
    cannot disagree with the file the viewer sees — and the fillers an approved
    ``reason: filler`` cut removed are taken out of their text (REQ-002).

    Args:
        segments: The transcript, in original-timeline seconds.
        timeline: The cut plan the output was rendered with.
        approved: The cuts behind that plan, needed for their reasons.
        words: The filler dictionary entries in force, from
            :meth:`vidprep._fillers.FillerDictionary.words`.

    Returns:
        The expectation and the character-to-segment table over it.
    """
    mapped, _ = timeline.map_segments(
        [(segment.id, segment.start, segment.end) for segment in segments],
        DEFAULT_MIN_DISPLAY,
    )
    by_id = {segment.id: segment for segment in segments}
    # Status as well as reason: a filler nobody approved is still in the
    # recording, so the render is expected to contain it.
    filler_cuts = [
        cut
        for cut in approved
        if cut.reason == _fillers.REASON and cut.status == "approved"
    ]
    kept: list[ExpectedSegment] = []
    offsets: list[int] = []
    parts: list[str] = []
    total = 0
    for entry in mapped:
        text = _text.normalize(
            _without_fillers(by_id[entry.segment_id], filler_cuts, words)
        )
        if not text:
            continue
        kept.append(ExpectedSegment(entry.segment_id, text, entry.start, entry.end))
        offsets.append(total)
        parts.append(text)
        total += len(text)
    return ExpectedText(tuple(kept), "".join(parts), tuple(offsets))


def _matcher(expected: str, actual: str) -> difflib.SequenceMatcher[str]:
    """Return the character-level alignment both measurements read from.

    Autojunk would drop characters that occur in more than 1% of a text, which
    for Japanese means the most common kana — exactly the ones a cut is likely
    to shave off.
    """
    return difflib.SequenceMatcher(None, expected, actual, autojunk=False)


def missing_hunks(expected: str, actual: str) -> list[MissingHunk]:
    """Return the runs of *expected* that *actual* dropped entirely.

    Only deletions count, and only from :data:`MIN_HUNK_CHARS` characters up
    (verification-plan.md §8.1-4). A region the recogniser replaced with
    something else is not a deletion: it heard audio there and read it wrong,
    which is the model noise this check is built to cancel out rather than
    report.
    """
    return [
        MissingHunk(text=expected[start:stop], index=start)
        for tag, start, stop, _, _ in _matcher(expected, actual).get_opcodes()
        if tag == "delete" and stop - start >= MIN_HUNK_CHARS
    ]


def character_error_rate(expected: str, actual: str) -> float:
    """Return the error rate between the two texts, as a reference figure only.

    Measured off the same alignment the hunks come from, so the two numbers in
    the report always describe the same comparison. It is not a minimal edit
    distance and it decides nothing: verification-plan.md §8.1-6 records it to
    show the noise level of running the recogniser twice (REQ-007).

    Raises:
        ValueError: If the expectation is empty, which leaves the rate
            undefined rather than merely large.
    """
    if not expected:
        msg = "the expected text is empty"
        raise ValueError(msg)
    errors = 0
    for tag, start, stop, other_start, other_stop in _matcher(
        expected, actual
    ).get_opcodes():
        if tag == "delete":
            errors += stop - start
        elif tag == "insert":
            errors += other_stop - other_start
        elif tag == "replace":
            errors += max(stop - start, other_stop - other_start)
    return errors / len(expected)


def boundaries(approved: Sequence[Cut]) -> list[tuple[str, float]]:
    """Return every cut edge the output was joined at, as ``(cut_id, second)``."""
    edges: list[tuple[str, float]] = []
    for cut in approved:
        edges.append((cut.id, cut.start))
        edges.append((cut.id, cut.end))
    return edges


def flag_boundaries(
    expected: ExpectedText,
    hunks: Sequence[MissingHunk],
    timeline: Timeline,
    approved: Sequence[Cut],
) -> list[BoundaryFlag]:
    """Return the hunks that sit within :data:`BOUNDARY_WINDOW` of a cut.

    Each hunk is placed on the cut timeline by its character index and put back
    on the original timeline with the inverse mapping of design.md §4 — the same
    interval table the video was cut with — so "near a boundary" is measured in
    the seconds the reviewer will seek to (REQ-005).
    """
    edges = boundaries(approved)
    if not edges or not expected.segments:
        return []
    limit = to_ms(BOUNDARY_WINDOW)
    flags: list[BoundaryFlag] = []
    for hunk in hunks:
        _, cut_time = expected.locate(hunk.index)
        source = timeline.inverse(min(max(cut_time, 0.0), timeline.cut_duration))
        cut_id, edge = min(edges, key=lambda item: abs(item[1] - source))
        if to_ms(abs(edge - source)) <= limit:
            flags.append(BoundaryFlag(cut_id, source, hunk.text))
    return flags
