"""Subtitles for the cut timeline: line breaking, readability, the SRT file.

The timing is not decided here — :mod:`vidprep.timeline` has already mapped the
transcript onto the cut timeline, and this module only dresses the result. What
it adds is the part a reader notices: where a line may be broken (BudouX finds
the phrase boundaries, and a break never falls inside one), how wide a line is
allowed to be, and which entries go by too fast to read.

Width is counted in full-width characters — East Asian Width ``W``/``F`` count
as one column, everything else as half — because ``max_chars_per_line`` and
``max_cps`` in ``profile.json`` are stated for Japanese text that mixes in
latin words (design.md §3.6).

Nothing is ever truncated. Text that will not fit ``max_lines`` lines of
``max_chars_per_line`` is packed into the last line and reported, because a
subtitle missing its ending is a defect a viewer cannot recover from, while an
overlong one is a judgement call the reader of the report can make.

:meth:`Subtitles.to_text` builds a second reading of the same entries: prose
grouped into paragraphs instead of a subtitle box's short-lived lines. vidprep
has no notion of where a topic turns, so a paragraph ends only on signals
already in the data — the recogniser's own sentence-ending punctuation, or a
pause that survived the cut — never on interpretation.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Literal

import pysubs2
from budoux import load_default_japanese_parser

from .models import to_ms

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from budoux import Parser

    from .models import SubtitleProfile
    from .timeline import SegmentWarning, TimedSegment

SRT_FORMAT = "srt"

#: How a line break is written inside an event; pysubs2 turns it into a newline.
LINE_BREAK = r"\N"

#: East Asian Width classes that occupy a whole column.
WIDE_CLASSES = frozenset({"W", "F"})
FULL_WIDTH = 1.0
HALF_WIDTH = 0.5

CPS_DECIMALS = 2

#: Punctuation :func:`_paragraphs` treats as the end of a sentence: the
#: ideographic and full-width marks a Japanese transcript uses, and their ASCII
#: forms for a non-Japanese ``asr.language``.
SENTENCE_ENDINGS = (
    "。",  # ideographic full stop
    "．",  # noqa: RUF001 — full-width full stop
    "！",  # noqa: RUF001 — full-width exclamation mark
    "？",  # noqa: RUF001 — full-width question mark
    "!",
    "?",
)

#: A paragraph never breaks before this many full-width characters — the
#: minimum that keeps ``to_text`` reading as prose rather than the SRT again.
MIN_PARAGRAPH_WIDTH = 100

#: A paragraph always breaks at this many full-width characters, signal or not.
MAX_PARAGRAPH_WIDTH = 300

#: A gap to the next entry this long, in seconds, ends a paragraph on its own.
PARAGRAPH_PAUSE = 0.6

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

WarningKind = Literal["dropped_by_cut", "min_display", "max_cps", "line_overflow"]


@dataclass(frozen=True, slots=True)
class SubtitleWarning:
    """One remark about a subtitle entry, for the report (design.md §5.6).

    Attributes:
        segment_id: The transcript segment the remark is about.
        kind: What is remarkable — the segment was dropped by a cut, or the
            entry is displayed too briefly, read too fast, or too wide.
        value: What was measured, where a measurement makes sense.
        threshold: The limit ``value`` is compared against.
    """

    segment_id: str
    kind: WarningKind
    value: float | None = None
    threshold: float | None = None


@dataclass(frozen=True, slots=True)
class Entry:
    """One subtitle entry, timed on the cut timeline and already broken."""

    segment_id: str
    start: float
    end: float
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        """The entry's text as a single line."""
        return "".join(self.lines)

    @property
    def display(self) -> float:
        """How long the entry is on screen, in seconds."""
        return self.end - self.start

    @property
    def cps(self) -> float:
        """Reading speed in full-width characters per second.

        An entry the separation trim reduced to zero length would be read
        infinitely fast; it is already reported as ``min_display``, and saying
        "infinite" here keeps it out of the arithmetic.
        """
        return text_width(self.text) / self.display if self.display > 0 else math.inf

    @property
    def width(self) -> float:
        """Width of the widest line, in full-width characters."""
        return max((text_width(line) for line in self.lines), default=0.0)


@dataclass(frozen=True, slots=True)
class Subtitles:
    """The entries of one render, and everything remarkable about them."""

    entries: tuple[Entry, ...]
    warnings: tuple[SubtitleWarning, ...]

    def to_srt(self, *, wrapped: bool = True) -> str:
        """Render the entries as an SRT document.

        Args:
            wrapped: Keep the line breaking; ``False`` puts each entry on one
                line, which is the ``--no-wrap`` version used to judge whether
                the breaks BudouX proposed read well.
        """
        subs = pysubs2.SSAFile()
        for entry in self.entries:
            text = LINE_BREAK.join(entry.lines) if wrapped else entry.text
            subs.append(
                pysubs2.SSAEvent(
                    start=to_ms(entry.start), end=to_ms(entry.end), text=text
                )
            )
        return subs.to_string(SRT_FORMAT)

    def to_text(self) -> str:
        """Render the entries as timestamped prose, grouped into paragraphs.

        Where ``to_srt`` keeps every entry on its own timing, this joins
        consecutive entries into a paragraph and drops the line breaking, so
        the result reads as a document rather than a sequence of captions. Each
        paragraph starts a new ``[timestamp] `` line, timed on its first entry.
        See :func:`_paragraphs` for where a paragraph ends.
        """
        paragraphs = [
            f"[{_timestamp(start)}] {text}" for start, text in _paragraphs(self.entries)
        ]
        return "\n\n".join(paragraphs) + "\n" if paragraphs else ""

    def count(self, kind: WarningKind) -> int:
        """Return how many warnings of *kind* were raised."""
        return sum(1 for warning in self.warnings if warning.kind == kind)


def text_width(text: str) -> float:
    """Return the width of *text* counted in full-width characters."""
    return sum(
        FULL_WIDTH if unicodedata.east_asian_width(char) in WIDE_CLASSES else HALF_WIDTH
        for char in text
    )


@cache
def _parser() -> Parser:
    """Return the shared BudouX parser; building one loads a bundled model."""
    return load_default_japanese_parser()


def phrases(text: str) -> list[str]:
    """Return the BudouX phrases of *text*; a line break may not fall inside one."""
    return _parser().parse(text) if text else []


def wrap(text: str, max_chars_per_line: int, max_lines: int) -> tuple[str, ...]:
    """Pack the phrases of *text* greedily into at most *max_lines* lines.

    Args:
        text: The entry's text, unbroken.
        max_chars_per_line: Width a line should not exceed, in full-width
            characters.
        max_lines: How many lines an entry may occupy.

    Returns:
        The lines, whose concatenation is *text* — a phrase that does not fit
        is left overlong rather than split or dropped.
    """
    lines = [""]
    for phrase in phrases(text):
        too_wide = text_width(lines[-1] + phrase) > max_chars_per_line
        if lines[-1] and too_wide and len(lines) < max_lines:
            lines.append(phrase)
        else:
            lines[-1] += phrase
    return tuple(lines)


def _append(text: str, addition: str) -> str:
    """Join *addition* onto a paragraph's accumulated *text*.

    Two full-width (Japanese) entries concatenate directly, the way BudouX
    lines do; a half-width one — a non-Japanese ``asr.language``, or a stray
    ASCII word — gets a space at the join so words do not run together, but
    only when both sides of the join are half-width, since a full-width side
    already carries no notion of a word boundary to protect.
    """
    if not text or not addition:
        return text + addition
    boundary_is_half_width = (
        unicodedata.east_asian_width(text[-1]) not in WIDE_CLASSES
        and unicodedata.east_asian_width(addition[0]) not in WIDE_CLASSES
    )
    return f"{text} {addition}" if boundary_is_half_width else f"{text}{addition}"


def _breaks(text: str, gap: float | None) -> bool:
    """Say whether a paragraph ends after the entry that brought it to *text*.

    Args:
        text: The paragraph's accumulated text, this entry included.
        gap: Seconds to the next entry's start, or ``None`` past the last
            entry, which always ends the paragraph regardless of this rule.

    A paragraph never breaks under :data:`MIN_PARAGRAPH_WIDTH`: doing so on
    every signal would give one- or two-sentence paragraphs, which is the SRT
    again. Past that it breaks on a sentence-ending mark or a pause of at least
    :data:`PARAGRAPH_PAUSE`, and past :data:`MAX_PARAGRAPH_WIDTH` it breaks
    regardless, so a transcript with neither signal still reads as paragraphs.
    """
    width = text_width(text)
    if width >= MAX_PARAGRAPH_WIDTH:
        return True
    if width < MIN_PARAGRAPH_WIDTH:
        return False
    # Compared in milliseconds, like every other timing in this module: `gap`
    # is a difference of two floats already rounded to milliseconds upstream,
    # and comparing it to PARAGRAPH_PAUSE in seconds can land a hair under a
    # boundary that is exactly equal at millisecond precision.
    return text.endswith(SENTENCE_ENDINGS) or (
        gap is not None and to_ms(gap) >= to_ms(PARAGRAPH_PAUSE)
    )


def _paragraphs(entries: Sequence[Entry]) -> Iterator[tuple[float, str]]:
    """Group *entries* into paragraphs, breaking only on signals already in the data.

    Yields:
        ``(start, text)`` pairs, ``start`` being the first grouped entry's
        start and ``text`` its and its successors' text joined by
        :func:`_append`.
    """
    start: float | None = None
    text = ""
    last = len(entries) - 1
    for index, entry in enumerate(entries):
        text = entry.text if start is None else _append(text, entry.text)
        start = entry.start if start is None else start
        gap = entries[index + 1].start - entry.end if index < last else None
        if index == last or _breaks(text, gap):
            yield start, text
            start, text = None, ""


def _timestamp(seconds: float) -> str:
    """Return *seconds* as ``MM:SS``, or ``H:MM:SS`` from an hour onward."""
    hours, remainder = divmod(int(seconds), SECONDS_PER_HOUR)
    minutes, secs = divmod(remainder, SECONDS_PER_MINUTE)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _mapping_warning(warning: SegmentWarning) -> SubtitleWarning:
    """Adapt one warning raised while mapping onto the cut timeline."""
    return SubtitleWarning(
        segment_id=warning["segment_id"],
        kind=warning["kind"],
        value=warning.get("value"),
        threshold=warning.get("threshold"),
    )


def _readability_warnings(
    entries: Sequence[Entry], profile: SubtitleProfile
) -> list[SubtitleWarning]:
    """Report the entries that read too fast or do not fit the box.

    Both limits are inclusive: exactly ``max_cps`` characters per second, and a
    line exactly ``max_chars_per_line`` wide, pass without a remark.
    """
    warnings: list[SubtitleWarning] = []
    for entry in entries:
        if entry.cps > profile.max_cps:
            warnings.append(
                SubtitleWarning(
                    entry.segment_id,
                    "max_cps",
                    round(entry.cps, CPS_DECIMALS),
                    profile.max_cps,
                )
            )
        if entry.width > profile.max_chars_per_line:
            warnings.append(
                SubtitleWarning(
                    entry.segment_id,
                    "line_overflow",
                    entry.width,
                    float(profile.max_chars_per_line),
                )
            )
    return warnings


def build(
    mapped: Sequence[TimedSegment],
    texts: Mapping[str, str],
    mapping_warnings: Sequence[SegmentWarning],
    profile: SubtitleProfile,
) -> Subtitles:
    """Turn mapped segments into subtitle entries and collect their warnings.

    Args:
        mapped: The segments :meth:`vidprep.timeline.Timeline.map_segments`
            placed on the cut timeline, already ordered and separated.
        texts: Segment identifier -> the transcript text to display.
        mapping_warnings: What the mapping itself reported — the segments a cut
            swallowed and the ones left on screen too briefly.
        profile: The ``subtitle`` section of ``profile.json``.

    Returns:
        The entries and every warning about them, ordered by segment identifier
        so the list reads in the order the transcript was recorded in.
    """
    entries = tuple(
        Entry(
            segment.segment_id,
            segment.start,
            segment.end,
            wrap(
                texts[segment.segment_id],
                profile.max_chars_per_line,
                profile.max_lines,
            ),
        )
        for segment in mapped
    )
    warnings = [_mapping_warning(warning) for warning in mapping_warnings]
    warnings += _readability_warnings(entries, profile)
    warnings.sort(key=lambda warning: (warning.segment_id, warning.kind))
    return Subtitles(entries, tuple(warnings))
