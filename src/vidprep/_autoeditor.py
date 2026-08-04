"""The auto-editor conversion layer: a v3 timeline becomes cut intervals.

auto-editor answers one question — where is this recording quiet? — and answers
it as an edit decision list: the clips it would keep. The silence is what is
missing between them, which is what this module reconstructs (design.md §5.4).

The document is parsed strictly. A timeline whose shape changed is a timeline
whose meaning may have changed, and a conversion layer that guessed would move
cut boundaries without anybody noticing, so an unfamiliar export stops the
stage instead (REQ-003).

Two of the parameters the profile states are applied here rather than by
auto-editor: ``min_duration``, which its CLI has no option for, and the padding,
which it could apply as ``--margin`` but must not — the padding has to be
checkable against the detector's own output (REQ-004), so the margin is pinned
to zero and vidprep does the shrinking.

The document comes back over stdout, which is also where auto-editor draws its
progress bar; ``--quiet`` silences its messages but not that bar, so the
progress display is switched off explicitly. Left on, it prepends the analysis
bar to the document whenever the analysis is slow enough to draw one — which
depends on whether auto-editor's audio-level cache is warm, so the same command
parses one run and fails the next (issue #30).
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ._intervals import Span, complement, merge_spans
from .errors import TimelineSchemaError
from .models import describe_validation_error, to_ms

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from .models import SilenceProfile

AUTO_EDITOR = "auto-editor"


class _Clip(BaseModel):
    """One clip of an ``--export v3`` timeline, in timebase units.

    ``offset`` is where the clip starts in the source and ``start`` where it
    lands in the export, so the clips are the *kept* material.
    """

    model_config = ConfigDict(extra="forbid")

    name: Literal["audio"]
    src: str
    start: int = Field(ge=0)
    dur: int = Field(gt=0)
    offset: int = Field(ge=0)
    stream: int = Field(ge=0)


class Timeline(BaseModel):
    """The ``--export v3`` document, accepted only in the shape vidprep knows.

    Detection runs on ``audio/processed.wav``, so ``v`` is empty and both track
    lists are typed as audio clips.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal["3"]
    timebase: str
    background: str
    resolution: list[int]
    samplerate: int
    layout: str
    v: list[list[_Clip]]
    a: list[list[_Clip]]


def command(audio: Path, silence: SilenceProfile) -> list[str]:
    """Return the auto-editor invocation the conversion layer reads."""
    return [
        AUTO_EDITOR,
        str(audio),
        "--edit",
        f"audio:threshold={silence.threshold}",
        "--margin",
        "0s",
        "--export",
        "v3",
        "-o",
        "-",
        "--no-open",
        "--quiet",
        "--progress",
        "none",
    ]


def parse_timeline(raw: str, version: str) -> Timeline:
    """Parse an ``--export v3`` document.

    Args:
        raw: What auto-editor printed.
        version: The auto-editor version that printed it, for the message.

    Returns:
        The parsed timeline.

    Raises:
        TimelineSchemaError: If the document is not the v3 shape vidprep knows
            (REQ-003).
    """
    try:
        return Timeline.model_validate_json(raw)
    except ValidationError as exc:  # also raised for malformed JSON
        msg = (
            f"auto-editor {version} exported a v3 timeline vidprep does not "
            f"know ({describe_validation_error(exc)}); the conversion layer "
            "needs updating — vidprep does not guess at cut positions"
        )
        raise TimelineSchemaError(msg) from exc


def kept_spans(timeline: Timeline) -> list[Span]:
    """Return the material auto-editor kept, in original-timeline seconds.

    Raises:
        TimelineSchemaError: If the timebase is not a readable positive
            fraction, which would put every clip at an unknown second.
    """
    try:
        timebase = Fraction(timeline.timebase)
    except (ValueError, ZeroDivisionError) as exc:
        msg = f"auto-editor exported an unreadable timebase: {timeline.timebase!r}"
        raise TimelineSchemaError(msg) from exc
    if timebase <= 0:
        msg = f"auto-editor exported a non-positive timebase: {timeline.timebase!r}"
        raise TimelineSchemaError(msg)
    return merge_spans(
        Span(
            float(Fraction(clip.offset) / timebase),
            float(Fraction(clip.offset + clip.dur) / timebase),
        )
        for track in (*timeline.a, *timeline.v)
        for clip in track
    )


def silence_spans(
    kept: Sequence[Span], duration: float, min_duration: float
) -> list[Span]:
    """Return the quiet stretches long enough to be worth considering."""
    return [
        gap
        for gap in complement(kept, duration)
        if to_ms(gap.duration) >= to_ms(min_duration)
    ]


def pad_spans(spans: Iterable[Span], silence: SilenceProfile) -> tuple[list[Span], int]:
    """Shrink each silence by the padding and drop what is then too short.

    Args:
        spans: The detected silences, unpadded.
        silence: The profile section holding ``pad_pre``, ``pad_post`` and
            ``min_cut_duration``.

    Returns:
        The cuttable intervals, and how many were dropped for being shorter
        than ``min_cut_duration`` once padded (REQ-005).
    """
    kept: list[Span] = []
    dropped = 0
    for span in spans:
        padded = Span(span.start + silence.pad_pre, span.end - silence.pad_post)
        if to_ms(padded.duration) >= to_ms(silence.min_cut_duration):
            kept.append(padded)
        else:
            dropped += 1
    return kept, dropped
