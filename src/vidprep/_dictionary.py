"""The misconversion dictionary and the two-stage replacement it drives.

The dictionary (design.md §3.7) fixes the proper nouns and technical terms
Japanese ASR reliably gets wrong. It is applied in two stages:

1. **Surface** — a literal match against one of an entry's ``misrecognized``
   spellings. Deterministic and the only stage that needs no analyser.
2. **Reading** — the SudachiPy reading of a run of tokens compared against the
   entry's ``yomi``, which catches spellings nobody wrote down yet
   ("クロード・コード" for "クロードコード").

Only ``confidence: always`` entries are replaced automatically. A ``context``
entry is a homophone of an everyday word ("クラウド" is "Claude" only among
developers), so its matches are *reported* and left to LLM correction — that is
what keeps over-replacement at zero.

Both stages scan left to right and take the longest match at each position, so
a replacement is never re-examined: that is what makes the result deterministic
regardless of how the entries are ordered, and idempotent on a second run.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import cache, partial
from importlib import resources
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import doctor
from .errors import SchemaInvalidError
from .models import describe_validation_error

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

#: Where the packaged dictionary lives inside the installed package.
DICTIONARY_RESOURCE = "dictionaries/asr-dict.json"

#: Longest run of tokens the reading stage will join before giving up. Entries
#: are single terms, so anything longer is noise rather than a missed match.
MAX_READING_TOKENS = 8

_KATAKANA_FIRST = 0x30A1
_KATAKANA_LAST = 0x30FA
_HIRAGANA_FIRST = 0x3041
_HIRAGANA_LAST = 0x3096
_KANA_OFFSET = 0x60

#: Kana marks that carry a reading but sit outside the katakana block.
_READING_EXTRAS = "ーヽヾ"

#: Unicode category initials of characters that may sit *inside* a reading run
#: without contributing to it: punctuation, separators and symbols. "クロード・
#: コード" must join across the "・" while "クロードXコード" must not.
_JOINER_CATEGORIES = frozenset({"P", "Z", "S"})

Confidence = Literal["always", "context"]
Stage = Literal["surface", "yomi"]


class DictionaryEntry(BaseModel):
    """One term, its known misrecognitions and how confidently to fix them."""

    model_config = ConfigDict(extra="forbid")

    correct: str = Field(min_length=1)
    misrecognized: list[str] = Field(default_factory=list)
    yomi: str = Field(min_length=1)
    confidence: Confidence
    note: str | None = None


class AsrDictionary(BaseModel):
    """``dictionaries/asr-dict.json`` — the misconversion dictionary."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    note: str | None = None
    entries: list[DictionaryEntry] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReadingToken:
    """One analysed token: where it sits in the text and how it is read.

    Attributes:
        begin: Index of the token's first character in the analysed text.
        end: Index one past its last character.
        surface: The text as written.
        reading: Its reading, already normalised to bare katakana. Empty when
            the analyser has none, which is also the case for punctuation.
    """

    begin: int
    end: int
    surface: str
    reading: str


class Reader(Protocol):
    """Turns text into the tokens the reading stage compares against ``yomi``."""

    def __call__(self, text: str) -> Sequence[ReadingToken]:
        """Return the tokens of *text* in the order they appear."""
        ...


@dataclass(frozen=True, slots=True)
class Hit:
    """One dictionary match found in a segment.

    Attributes:
        stage: Which stage found it.
        matched: The text that matched, as it was written.
        correct: The entry's canonical spelling.
        applied: Whether the text was replaced. ``False`` means the entry is
            ``context`` and the decision was left to LLM correction.
    """

    stage: Stage
    matched: str
    correct: str
    applied: bool


def normalise_reading(text: str) -> str:
    """Return *text* as bare katakana, the form both sides of a reading compare in.

    Half-width kana are widened, hiragana is folded to katakana and everything
    else — latin letters, punctuation, the "・" between words — is dropped, so
    a hand-written ``yomi`` needs no more care than "write it in kana".
    """
    folded = unicodedata.normalize("NFKC", text)
    kept = []
    for character in folded:
        code = ord(character)
        if _HIRAGANA_FIRST <= code <= _HIRAGANA_LAST:
            character = chr(code + _KANA_OFFSET)  # noqa: PLW2901 — fold in place
            code = ord(character)
        if _KATAKANA_FIRST <= code <= _KATAKANA_LAST or character in _READING_EXTRAS:
            kept.append(character)
    return "".join(kept)


def load_dictionary(path: Path | None = None) -> AsrDictionary:
    """Load the misconversion dictionary, the packaged one by default.

    Args:
        path: Read this file instead of the packaged dictionary.

    Returns:
        The parsed dictionary.

    Raises:
        SchemaInvalidError: If the file is not JSON or violates the schema.
    """
    if path is None:
        resource = resources.files(__package__).joinpath(DICTIONARY_RESOURCE)
        raw = resource.read_bytes()
        name = DICTIONARY_RESOURCE
    else:
        raw = path.read_bytes()
        name = path.name
    try:
        return AsrDictionary.model_validate_json(raw)
    except ValidationError as exc:  # also raised for malformed JSON
        msg = f"{name}: {describe_validation_error(exc)}"
        raise SchemaInvalidError(msg) from exc


@cache
def default_reader() -> Reader | None:
    """Return the SudachiPy reader, or ``None`` when no dictionary is installed.

    SudachiPy ships without a dictionary on purpose (``doctor`` reports which
    flavour is present), so the reading stage is treated as unavailable rather
    than fatal: surface replacement still works without it.
    """
    try:
        from sudachipy import Dictionary, SplitMode  # noqa: PLC0415 — heavy import
    except ImportError:
        return None
    for name in doctor.SUDACHI_DICTS:
        tokenizer = _open_tokenizer(Dictionary, name)
        if tokenizer is not None:
            # Mode C keeps compound terms whole, which is the granularity a
            # dictionary entry is written at.
            return _SudachiReader(partial(tokenizer.tokenize, mode=SplitMode.C))
    return None


# Any: SudachiPy ships no py.typed marker and has no stubs on typeshed.
def _open_tokenizer(dictionary: Any, flavour: str) -> Any:
    """Return a tokenizer for the *flavour* dictionary, or ``None`` if unusable.

    Every failure means the same thing to the caller — try the next flavour —
    so they are not told apart here; ``doctor`` is what explains them.
    """
    try:
        opened = dictionary(dict=flavour).create()
    except Exception:
        return None
    return opened


@dataclass(frozen=True, slots=True)
class _SudachiReader:
    """Adapts SudachiPy morphemes to :class:`ReadingToken`."""

    # Any: SudachiPy ships no py.typed marker and has no stubs on typeshed.
    tokenize: Any

    def __call__(self, text: str) -> Sequence[ReadingToken]:
        """Return the tokens of *text*, with readings already normalised."""
        return [
            ReadingToken(
                begin=morpheme.begin(),
                end=morpheme.end(),
                surface=morpheme.surface(),
                reading=normalise_reading(morpheme.reading_form()),
            )
            for morpheme in self.tokenize(text)
        ]


def correct_text(
    text: str,
    dictionary: AsrDictionary,
    reader: Reader | None = None,
) -> tuple[str, list[Hit]]:
    """Apply both dictionary stages to *text*.

    Args:
        text: The segment text to correct.
        dictionary: The entries to apply, in the order they are written.
        reader: Analyser for the reading stage; ``None`` skips that stage.

    Returns:
        The corrected text and every match found: the surface stage's hits
        first, each stage's in the order they occur. Matches that changed
        nothing — an already-correct spelling — are not reported, so applying
        the result again yields no hits at all.
    """
    corrected, hits = _apply_surface(text, dictionary.entries)
    if reader is not None:
        corrected, reading_hits = _apply_readings(corrected, dictionary.entries, reader)
        hits += reading_hits
    # A ``context`` match the reading stage then replaced anyway is no longer
    # something the user has to decide about, so it is not reported as pending.
    return corrected, [hit for hit in hits if hit.applied or hit.matched in corrected]


def _surface_patterns(
    entries: Iterable[DictionaryEntry],
) -> list[tuple[str, DictionaryEntry]]:
    """Return every ``misrecognized`` spelling, longest first.

    Sorting by length is what implements the "longest match wins" rule: a
    spelling that contains a shorter one can no longer lose to it, so
    no replacement is ever built out of two overlapping entries. ``sorted`` is
    stable, so entries of equal length keep the order the dictionary lists.
    """
    patterns = [
        (surface, entry)
        for entry in entries
        for surface in entry.misrecognized
        if surface
    ]
    return sorted(patterns, key=lambda pattern: -len(pattern[0]))


def _apply_surface(
    text: str, entries: Iterable[DictionaryEntry]
) -> tuple[str, list[Hit]]:
    """Replace literal misrecognitions, scanning left to right."""
    patterns = _surface_patterns(entries)
    pieces: list[str] = []
    hits: list[Hit] = []
    index = 0
    while index < len(text):
        for surface, entry in patterns:
            if text.startswith(surface, index):
                pieces.append(_take(entry, surface, "surface", hits))
                index += len(surface)
                break
        else:
            pieces.append(text[index])
            index += 1
    return "".join(pieces), hits


def _take(entry: DictionaryEntry, matched: str, stage: Stage, hits: list[Hit]) -> str:
    """Return what a match contributes to the output, recording it in *hits*.

    A ``context`` entry contributes the text unchanged but is still reported;
    an entry whose canonical spelling is already there is not reported at all,
    whatever its confidence, because there is nothing for the user to review.
    """
    if entry.correct == matched:
        return matched
    if entry.confidence != "always":
        hits.append(Hit(stage, matched, entry.correct, applied=False))
        return matched
    hits.append(Hit(stage, matched, entry.correct, applied=True))
    return entry.correct


def _apply_readings(
    text: str, entries: Sequence[DictionaryEntry], reader: Reader
) -> tuple[str, list[Hit]]:
    """Replace terms whose *reading* matches an entry, scanning left to right."""
    tokens = reader(text)
    readings = {normalise_reading(entry.yomi): entry for entry in reversed(entries)}
    pieces: list[str] = []
    hits: list[Hit] = []
    cursor = 0
    index = 0
    while index < len(tokens):
        match = _match_reading(tokens, index, readings)
        if match is None:
            index += 1
            continue
        stop, entry = match
        begin, end = tokens[index].begin, tokens[stop - 1].end
        pieces += [text[cursor:begin], _take(entry, text[begin:end], "yomi", hits)]
        cursor = end
        index = stop
    pieces.append(text[cursor:])
    return "".join(pieces), hits


def _match_reading(
    tokens: Sequence[ReadingToken],
    index: int,
    readings: dict[str, DictionaryEntry],
) -> tuple[int, DictionaryEntry] | None:
    """Return the longest entry whose reading starts at *tokens[index]*.

    Returns:
        The token index one past the match and the entry it matched, or
        ``None`` when nothing starts here.
    """
    if not tokens[index].reading:
        return None
    limit = min(index + MAX_READING_TOKENS, len(tokens))
    joined = ""
    best: tuple[int, DictionaryEntry] | None = None
    for stop in range(index + 1, limit + 1):
        token = tokens[stop - 1]
        if not token.reading and not _is_joiner(token.surface):
            break
        joined += token.reading
        entry = readings.get(joined)
        if entry is not None and tokens[stop - 1].reading:
            best = (stop, entry)
    return best


def _is_joiner(surface: str) -> bool:
    """Return whether *surface* may sit inside a reading run without a reading."""
    return bool(surface) and all(
        unicodedata.category(character)[0] in _JOINER_CATEGORIES
        for character in surface
    )
