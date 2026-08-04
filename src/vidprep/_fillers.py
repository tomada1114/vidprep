"""Filler detection: the dictionary, the scanner and the cuts they justify.

Only two positions can be cut without word-level timestamps, which Japanese ASR
does not give reliably (design.md §1, decision 3): a segment that is *nothing
but* filler, and a filler at a segment's edge next to silence. A filler in the
middle of a sentence is found here too, but only so it can be reported — there
is no honest way to say where inside the sentence it starts.

The dictionary ships with the package and a project may add its own entries;
the two tiers exist because the weak words ("まあ", "なんか", "こう") are also
ordinary Japanese, so cutting them has to be asked for (``filler.enable_weak``).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from functools import cache
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ._intervals import Candidate, Span, merge_spans, subtract
from .errors import SchemaInvalidError
from .models import describe_validation_error, to_ms

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import Profile, Segment, SilenceProfile

#: The ``reason`` this module's candidates carry (design.md §3.4).
REASON = "filler"

#: Shortest filler cut worth proposing, in seconds. A candidate trimmed below
#: this by the silence cuts around it is already gone.
MIN_FILLER_DURATION = 0.1

#: Confidence is a review order, not a probability: a segment that is nothing
#: but filler is a surer thing than a filler at an edge, and the weak tier is
#: less sure than the strong one.
_CONFIDENCE: dict[tuple[str, str], float] = {
    ("whole", "strong"): 0.8,
    ("whole", "weak"): 0.6,
    ("edge", "strong"): 0.6,
    ("edge", "weak"): 0.4,
}

#: A filler at an edge is cut to a speech-region boundary inside the segment,
#: so a segment the VAD never split cannot be cut at all.
_MIN_REGIONS = 2

#: Where the packaged dictionary lives inside the installed package.
FILLER_RESOURCE = "dictionaries/fillers.json"

#: Where a project may add entries of its own, relative to the project root.
PROJECT_DICTIONARY = Path("dictionaries") / "fillers.json"

#: Unicode category initials dropped before matching: punctuation, separators,
#: symbols and control characters. "えーと、" and "えーと" are the same word.
_DROPPED_CATEGORIES = frozenset({"P", "Z", "S", "C"})

#: Held-vowel mark. A run of them is one mark: "えーーー" is still "えー".
_PROLONGED = "ー"

Tier = Literal["strong", "weak"]


class FillerDictionary(BaseModel):
    """``dictionaries/fillers.json`` — filler words in two tiers."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    note: str | None = None
    strong: list[str] = Field(default_factory=list)
    weak: list[str] = Field(default_factory=list)

    def words(self, *, enable_weak: bool) -> tuple[tuple[str, Tier], ...]:
        """Return the words to match against, longest first.

        Args:
            enable_weak: Include the weak tier. Off by default, and off is what
                keeps "なんか" from being proposed for deletion mid-sentence.

        Returns:
            ``(normalised word, tier)`` pairs ordered so the first one that
            matches at a position is also the longest — "えーと" wins over "えー".
        """
        entries: list[tuple[str, Tier]] = [
            (normalise(word), "strong") for word in self.strong
        ]
        if enable_weak:
            entries += [(normalise(word), "weak") for word in self.weak]
        unique: dict[str, Tier] = {}
        for word, tier in entries:
            if word:
                unique.setdefault(word, tier)
        return tuple(
            sorted(unique.items(), key=lambda entry: (-len(entry[0]), entry[0]))
        )


@dataclass(frozen=True, slots=True)
class Hit:
    """One filler word found in a segment, positioned in the normalised text."""

    word: str
    tier: Tier
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Reading:
    """Every filler in one segment, and where it sits.

    Attributes:
        text: The segment text as it was matched — normalised, so the indices
            in :attr:`hits` are indices into *this* string, not the original.
        hits: The fillers found, in the order they appear and never overlapping.
    """

    text: str
    hits: tuple[Hit, ...]

    @property
    def is_whole(self) -> bool:
        """Whether the segment is nothing but filler words (condition a)."""
        covered = sum(hit.end - hit.start for hit in self.hits)
        return bool(self.hits) and covered == len(self.text)

    @property
    def leading(self) -> Hit | None:
        """The filler the segment starts with, if it is not the whole segment."""
        if self.is_whole or not self.hits or self.hits[0].start != 0:
            return None
        return self.hits[0]

    @property
    def trailing(self) -> Hit | None:
        """The filler the segment ends with, if it is not the whole segment."""
        if self.is_whole or not self.hits or self.hits[-1].end != len(self.text):
            return None
        return self.hits[-1]

    @property
    def inside(self) -> tuple[Hit, ...]:
        """The fillers surrounded by other words — reported, never cut."""
        if self.is_whole:
            return ()
        return tuple(
            hit for hit in self.hits if hit.start > 0 and hit.end < len(self.text)
        )

    @property
    def tier(self) -> Tier:
        """The strongest tier among the hits; "weak" only if all of them are."""
        return "strong" if any(hit.tier == "strong" for hit in self.hits) else "weak"


def normalise(text: str) -> str:
    """Return *text* in the form fillers are matched in.

    NFKC-folded, stripped of punctuation, spaces and symbols, with runs of the
    held-vowel mark collapsed: whether the recogniser wrote "えーと、", "えーと"
    or "えーーと" is a transcription detail, not a different word.
    """
    kept: list[str] = []
    for character in unicodedata.normalize("NFKC", text):
        if unicodedata.category(character)[0] in _DROPPED_CATEGORIES:
            continue
        if character == _PROLONGED and kept and kept[-1] == _PROLONGED:
            continue
        kept.append(character)
    return "".join(kept)


def scan(text: str, words: Sequence[tuple[str, Tier]]) -> Reading:
    """Find every filler in *text*, taking the longest match at each position.

    Args:
        text: One segment's text, as the transcript holds it.
        words: The pairs :meth:`FillerDictionary.words` returned.

    Returns:
        The normalised text and the fillers found in it.
    """
    normalised = normalise(text)
    hits: list[Hit] = []
    index = 0
    while index < len(normalised):
        found = next(
            (
                (word, tier)
                for word, tier in words
                if normalised.startswith(word, index)
            ),
            None,
        )
        if found is None:
            index += 1
            continue
        word, tier = found
        hits.append(Hit(word=word, tier=tier, start=index, end=index + len(word)))
        index += len(word)
    return Reading(text=normalised, hits=tuple(hits))


def _parse(raw: bytes, name: str) -> FillerDictionary:
    """Parse one dictionary file.

    Raises:
        SchemaInvalidError: If it is not JSON or violates the schema.
    """
    try:
        return FillerDictionary.model_validate_json(raw)
    except ValidationError as exc:  # also raised for malformed JSON
        msg = f"{name}: {describe_validation_error(exc)}"
        raise SchemaInvalidError(msg) from exc


@cache
def packaged_dictionary() -> FillerDictionary:
    """Return the filler dictionary shipped with vidprep.

    Raises:
        SchemaInvalidError: If the packaged file does not match its schema.
    """
    resource = resources.files(__package__).joinpath(FILLER_RESOURCE)
    return _parse(resource.read_bytes(), FILLER_RESOURCE)


def load_dictionary(project_root: Path | None = None) -> FillerDictionary:
    """Return the packaged dictionary, extended by the project's own entries.

    Args:
        project_root: Project directory to look for
            ``dictionaries/fillers.json`` in. Its entries are added to the
            packaged ones rather than replacing them, so a project only has to
            write down what is missing.

    Returns:
        The dictionary cut detection should match with.

    Raises:
        SchemaInvalidError: If either file violates the schema.
    """
    packaged = packaged_dictionary()
    if project_root is None:
        return packaged
    path = project_root / PROJECT_DICTIONARY
    if not path.is_file():
        return packaged
    extra = _parse(path.read_bytes(), str(PROJECT_DICTIONARY))
    return FillerDictionary(
        version=packaged.version,
        note=packaged.note,
        strong=[*packaged.strong, *extra.strong],
        weak=[*packaged.weak, *extra.weak],
    )


@dataclass(frozen=True, slots=True)
class Speech:
    """What the transcript and the VAD report together say about the words.

    Attributes:
        segments: The transcript segments, in timeline order.
        regions: The speech regions Silero found, ordered and disjoint.
        duration: Length of the source material, in seconds.
    """

    segments: tuple[Segment, ...]
    regions: tuple[Span, ...]
    duration: float

    def quiet_before(self, index: int) -> float:
        """Return the silence in front of segment *index*, in seconds.

        Bounded by the previous segment as well as by the previous speech
        region: two segments inside one region are not separated by silence,
        however far away the region started.
        """
        segment = self.segments[index]
        spoken = self.segments[index - 1].end if index > 0 else 0.0
        ends = [
            region.end
            for region in self.regions
            if to_ms(region.end) <= to_ms(segment.start)
        ]
        if ends:
            spoken = max(spoken, ends[-1])
        return max(0.0, segment.start - spoken)

    def quiet_after(self, index: int) -> float:
        """Return the silence after segment *index*, in seconds."""
        segment = self.segments[index]
        following = (
            self.segments[index + 1].start
            if index + 1 < len(self.segments)
            else self.duration
        )
        starts = [
            region.start
            for region in self.regions
            if to_ms(region.start) >= to_ms(segment.end)
        ]
        if starts:
            following = min(following, starts[0])
        return max(0.0, following - segment.end)

    def regions_in(self, segment: Segment) -> list[Span]:
        """Return the speech regions that overlap *segment*."""
        span = Span(segment.start, segment.end)
        return [region for region in self.regions if region.overlap(span) > 0]

    def spoken_spans(self) -> list[Span]:
        """Return the parts of the transcript the VAD also heard as speech.

        Segment timestamps alone are not evidence of speech: whisper.cpp with
        VAD enabled reports a segment's end at the end of the region it stopped
        in, which on the golden sample stretched one segment across 55 seconds
        of measured silence. The intersection is what both agree on.
        """
        spoken: list[Span] = []
        for segment in self.segments:
            span = Span(segment.start, segment.end)
            spoken += [
                Span(max(span.start, region.start), min(span.end, region.end))
                for region in self.regions
                if region.overlap(span) > 0
            ]
        return merge_spans(spoken)


def _note(reading: Reading, placement: str) -> str:
    """Describe a filler candidate for the reviewer."""
    hit = {
        "whole": reading.hits[0] if reading.hits else None,
        "leading": reading.leading,
        "trailing": reading.trailing,
    }[placement]
    word = hit.word if hit is not None else ""
    described = {
        "whole": f"segment is only the filler 「{word}」",
        "leading": f"segment opens with the filler 「{word}」",
        "trailing": f"segment ends with the filler 「{word}」",
    }[placement]
    return f"{described}, cut to the VAD boundary"


def _confidence(placement: str, tier: Tier) -> float:
    """Return the review-ordering confidence of a filler candidate."""
    return _CONFIDENCE["whole" if placement == "whole" else "edge", tier]


def _whole_span(speech: Speech, index: int, silence: SilenceProfile) -> Span:
    """Return the cut for a segment that is nothing but filler (condition a).

    The adjacent silence joins the cut, but only as far as the padding the
    silence cuts leave behind, so the two abut instead of overlapping.
    """
    segment = speech.segments[index]
    return Span(
        segment.start - min(silence.pad_post, speech.quiet_before(index)),
        segment.end + min(silence.pad_pre, speech.quiet_after(index)),
    )


def _leading_span(speech: Speech, index: int, silence: SilenceProfile) -> Span | None:
    """Return the cut for a filler the segment opens with (condition b).

    Without word timestamps the only honest end for the cut is a speech-region
    boundary inside the segment, so a segment the VAD never split is reported
    rather than cut.
    """
    segment = speech.segments[index]
    regions = speech.regions_in(segment)
    if len(regions) < _MIN_REGIONS or to_ms(regions[0].end) >= to_ms(segment.end):
        return None
    pause = max(0.0, regions[1].start - regions[0].end)
    return Span(
        segment.start - min(silence.pad_post, speech.quiet_before(index)),
        regions[0].end + min(silence.pad_pre, pause),
    )


def _trailing_span(speech: Speech, index: int, silence: SilenceProfile) -> Span | None:
    """Return the cut for a filler the segment ends with (condition b)."""
    segment = speech.segments[index]
    regions = speech.regions_in(segment)
    if len(regions) < _MIN_REGIONS or to_ms(regions[-1].start) <= to_ms(segment.start):
        return None
    pause = max(0.0, regions[-1].start - regions[-2].end)
    return Span(
        regions[-1].start - min(silence.pad_post, pause),
        segment.end + min(silence.pad_pre, speech.quiet_after(index)),
    )


def _segment_candidates(
    speech: Speech, index: int, reading: Reading, profile: Profile
) -> list[Candidate]:
    """Return the cut candidates one segment's fillers justify."""
    silence = profile.silence
    required = to_ms(profile.filler.require_adjacent_silence)
    found: list[tuple[str, Span | None]] = []
    if reading.is_whole:
        found.append(("whole", _whole_span(speech, index, silence)))
    else:
        if (
            reading.leading is not None
            and to_ms(speech.quiet_before(index)) >= required
        ):
            found.append(("leading", _leading_span(speech, index, silence)))
        if (
            reading.trailing is not None
            and to_ms(speech.quiet_after(index)) >= required
        ):
            found.append(("trailing", _trailing_span(speech, index, silence)))
    return [
        Candidate(
            start=span.start,
            end=span.end,
            reason=REASON,
            confidence=_confidence(placement, reading.tier),
            status="proposed",
            note=_note(reading, placement),
        )
        for placement, span in found
        if span is not None and to_ms(span.duration) > 0
    ]


def _trim(candidate: Candidate, silence_cuts: Sequence[Span]) -> Candidate | None:
    """Return *candidate* with the silence cuts removed from it, or ``None``.

    ``None`` means the silence cuts already remove all of it, or all but a
    sliver too short to be worth reviewing.
    """
    pieces = subtract(Span(candidate.start, candidate.end), silence_cuts)
    if not pieces:
        return None
    longest = max(pieces, key=lambda piece: piece.duration)
    if to_ms(longest.duration) < to_ms(MIN_FILLER_DURATION):
        return None
    return replace(candidate, start=longest.start, end=longest.end)


def candidates(
    speech: Speech,
    profile: Profile,
    dictionary: FillerDictionary,
    silence_cuts: Sequence[Span],
) -> tuple[list[Candidate], int]:
    """Detect the fillers worth cutting, and count the ones that are not.

    Args:
        speech: The transcript and the speech regions behind it.
        profile: The project's parameters; ``filler.enable_weak`` decides
            whether the weak tier is matched at all (REQ-011).
        dictionary: The filler words to look for.
        silence_cuts: The silence candidates of this run. Filler cuts are
            trimmed against them so approving both can never produce two
            overlapping ``approved`` cuts.

    Returns:
        The candidates, and how many mid-sentence fillers were found but left
        alone (REQ-014).
    """
    words = dictionary.words(enable_weak=profile.filler.enable_weak)
    found: list[Candidate] = []
    in_sentence = 0
    for index, segment in enumerate(speech.segments):
        reading = scan(segment.text, words)
        if not reading.hits:
            continue
        in_sentence += len(reading.inside)
        for candidate in _segment_candidates(speech, index, reading, profile):
            trimmed = _trim(candidate, silence_cuts)
            if trimmed is not None:
                found.append(trimmed)
    return found, in_sentence
