"""Pydantic schemas for the intermediate JSON files described in design.md §3.

The intermediate JSON *is* the design: every stage reads JSON and writes JSON,
so the invariants that matter (identifier shape, interval sanity, approved cuts
never overlapping) are enforced here rather than in each stage.

Interval upper bounds depend on the source duration, which lives in the
manifest rather than in the artifact being validated. Pass it through the
validation context::

    Cuts.model_validate(payload, context={"duration": manifest.source.duration})

Without the context the duration bound is simply not checked.
"""

from __future__ import annotations

import itertools
from collections import Counter
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationError,
    ValidationInfo,
    model_validator,
)

MS_PER_SECOND = 1000
SECONDS_DECIMALS = 3
SEGMENT_ID_PATTERN = r"^s\d{4}$"
CUT_ID_PATTERN = r"^c\d{4}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
FPS_PATTERN = r"^\d+/\d+$"

Seconds = Annotated[
    float,
    Field(ge=0.0),
    PlainSerializer(lambda value: round(value, SECONDS_DECIMALS), return_type=float),
]
SegmentId = Annotated[str, Field(pattern=SEGMENT_ID_PATTERN)]
CutId = Annotated[str, Field(pattern=CUT_ID_PATTERN)]


def to_ms(seconds: float) -> int:
    """Return *seconds* as whole milliseconds, the unit all comparisons use."""
    return round(seconds * MS_PER_SECOND)


def describe_validation_error(error: ValidationError) -> str:
    """Render a pydantic error as a single line naming location and constraint."""
    parts = []
    for entry in error.errors():
        location = ".".join(str(item) for item in entry["loc"])
        message = entry["msg"].removeprefix("Value error, ")
        parts.append(f"{location}: {message}" if location else message)
    return "; ".join(parts)


def _context_duration(info: ValidationInfo) -> float | None:
    """Return the source duration supplied through the validation context."""
    context = info.context
    if not isinstance(context, dict):
        return None
    duration = context.get("duration")
    return float(duration) if isinstance(duration, int | float) else None


def _check_interval(
    item_id: str, start: float, end: float, info: ValidationInfo
) -> None:
    """Enforce ``0 <= start < end <= duration`` for one interval.

    Raises:
        ValueError: If the interval is empty, inverted, or runs past the source.
    """
    if to_ms(start) >= to_ms(end):
        msg = f"{item_id}: start ({start:.3f}) must be strictly before end ({end:.3f})"
        raise ValueError(msg)
    duration = _context_duration(info)
    if duration is not None and to_ms(end) > to_ms(duration):
        msg = f"{item_id}: end ({end:.3f}) is past the source duration ({duration:.3f})"
        raise ValueError(msg)


def _check_unique_ids(ids: list[str], label: str) -> None:
    """Raise when *ids* contains duplicates, naming the repeated identifiers.

    Raises:
        ValueError: If any identifier appears more than once.
    """
    duplicates = sorted(value for value, count in Counter(ids).items() if count > 1)
    if duplicates:
        msg = f"duplicate {label} ids: {', '.join(duplicates)}"
        raise ValueError(msg)


class _Strict(BaseModel):
    """Base model rejecting unknown keys so typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class VideoStream(_Strict):
    """Video parameters of the source material."""

    codec: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: str = Field(pattern=FPS_PATTERN)


class AudioStream(_Strict):
    """Audio parameters of the source material."""

    codec: str
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)


class Source(_Strict):
    """Reference to the source material: never copied unless asked, only hashed."""

    path: str
    sha256: str = Field(pattern=SHA256_PATTERN)
    duration: Seconds
    video: VideoStream
    audio: AudioStream


class StageRecord(_Strict):
    """Provenance of one finished stage (design.md §3.2)."""

    done_at: AwareDatetime
    params_sha256: str = Field(pattern=SHA256_PATTERN)
    tool_versions: dict[str, str] = Field(default_factory=dict)


class Manifest(_Strict):
    """``vidprep.json`` — the project manifest."""

    version: Literal["1"] = "1"
    created_at: AwareDatetime
    source: Source
    stages: dict[str, StageRecord] = Field(default_factory=dict)


class Edit(_Strict):
    """One recorded text change, used to verify that correction is idempotent."""

    at: AwareDatetime
    tool: Literal["dict", "llm", "manual"]
    before: str


class Word(BaseModel):
    """Word-level timing. Not produced in v1; accepted so backends may add it."""

    model_config = ConfigDict(extra="allow")

    start: Seconds
    end: Seconds
    text: str


class Segment(_Strict):
    """One transcript segment. Its ``id`` is assigned once and never changes."""

    id: SegmentId
    start: Seconds
    end: Seconds
    text: str
    source: Literal["asr", "dict", "llm"] = "asr"
    edits: list[Edit] = Field(default_factory=list)
    words: list[Word] | None = None

    @model_validator(mode="after")
    def _validate_interval(self, info: ValidationInfo) -> Self:
        _check_interval(self.id, self.start, self.end, info)
        return self


class AsrInfo(_Strict):
    """Which backend produced the transcript."""

    backend: str
    model: str
    vad: str


class Transcript(_Strict):
    """``transcript.json`` — ASR output in original-timeline seconds."""

    version: Literal["1"] = "1"
    audio_source: str
    asr: AsrInfo
    segments: list[Segment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_segments(self) -> Self:
        _check_unique_ids([segment.id for segment in self.segments], "segment")
        return self


class SpeechSegment(_Strict):
    """One region Silero VAD reported as speech, in original-timeline seconds."""

    start: Seconds
    end: Seconds


class VadReport(_Strict):
    """``report/vad.json`` — the regions ``transcribe`` ran the recogniser over.

    Cut detection trims filler candidates to these boundaries (design.md §5.4)
    and the transcript's own verification joins on them, so the regions are
    required to be ordered and disjoint: two callers walking the list must
    agree on which moment is speech.
    """

    version: Literal["1"] = "1"
    backend: str
    segments: list[SpeechSegment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_segments(self, info: ValidationInfo) -> Self:
        previous: SpeechSegment | None = None
        for index, segment in enumerate(self.segments):
            _check_interval(f"speech[{index}]", segment.start, segment.end, info)
            if previous is not None and to_ms(segment.start) < to_ms(previous.end):
                msg = (
                    f"speech[{index}]: starts ({segment.start:.3f}) before the "
                    f"previous region ended ({previous.end:.3f})"
                )
                raise ValueError(msg)
            previous = segment
        return self


class Cut(_Strict):
    """One cut candidate. ``reason`` stays an open string (design.md §8)."""

    id: CutId
    start: Seconds
    end: Seconds
    reason: str = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: Literal["proposed", "approved", "rejected"] = "proposed"
    note: str | None = None

    @model_validator(mode="after")
    def _validate_interval(self, info: ValidationInfo) -> Self:
        _check_interval(self.id, self.start, self.end, info)
        return self


class Cuts(_Strict):
    """``cuts.json`` — cut candidates; only ``approved`` ones are rendered."""

    version: Literal["1"] = "1"
    cuts: list[Cut] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_cuts(self) -> Self:
        _check_unique_ids([cut.id for cut in self.cuts], "cut")
        approved = sorted(
            (cut for cut in self.cuts if cut.status == "approved"),
            key=lambda cut: (to_ms(cut.start), to_ms(cut.end)),
        )
        for earlier, later in itertools.pairwise(approved):
            if to_ms(later.start) < to_ms(earlier.end):
                msg = (
                    "approved cuts overlap: "
                    f"{earlier.id}({earlier.start:.3f}-{earlier.end:.3f}) x "
                    f"{later.id}({later.start:.3f}-{later.end:.3f})"
                )
                raise ValueError(msg)
        return self


class Telop(_Strict):
    """One on-screen caption, timed by segment reference or explicitly."""

    text: str
    style_preset: str = "default"
    segment_id: SegmentId | None = None
    start: Seconds | None = None
    duration: Seconds | None = None

    @model_validator(mode="after")
    def _validate_timing(self) -> Self:
        if self.segment_id is None and (self.start is None or self.duration is None):
            msg = f"telop {self.text!r}: needs segment_id, or both start and duration"
            raise ValueError(msg)
        return self


class Telops(_Strict):
    """``telops.json`` — captions burned in by ``render --preview``."""

    version: Literal["1"] = "1"
    telops: list[Telop] = Field(default_factory=list)


class Styles(_Strict):
    """``styles.json`` — ASS style presets keyed by preset name.

    Preset values stay loosely typed: ASS mixes strings (``fontname``),
    integers (``fontsize``, ``alignment``) and floats (``outline``, ``shadow``).
    """

    version: Literal["1"] = "1"
    presets: dict[str, dict[str, str | int | float]] = Field(default_factory=dict)


class Loudnorm(_Strict):
    """EBU R128 loudness targets."""

    i: float = -14.0
    tp: float = -1.0
    lra: float = 11.0


class AudioProfile(_Strict):
    """Parameters of the ``audio-fix`` chain."""

    denoise: str = "deepfilternet"
    highpass_hz: int = Field(default=80, ge=0)
    loudnorm: Loudnorm = Field(default_factory=Loudnorm)


class AsrProfile(_Strict):
    """Which recogniser ``transcribe`` drives, and on which weights.

    ``vad`` accepts one value rather than being a switch: voice activity
    detection is what stops whisper inventing speech in the silences — the
    Step 1 bench measured 6 hallucinated segments without it and 0 with it
    (verification-plan.md §12.2) — so there is no profile setting, and no flag,
    that turns it off (design.md §5.2).
    """

    backend: Literal["whisper.cpp", "mlx-whisper"] = "whisper.cpp"
    model: str = "large-v3-turbo"
    language: str = "ja"
    vad: Literal["silero-v5"] = "silero-v5"


class SilenceProfile(_Strict):
    """Silence detection and padding parameters."""

    threshold: str = "4%"
    min_duration: Seconds = 0.6
    pad_pre: Seconds = 0.3
    pad_post: Seconds = 0.3
    min_cut_duration: Seconds = 0.4


class FillerProfile(_Strict):
    """Filler-word detection parameters."""

    enable_weak: bool = False
    require_adjacent_silence: Seconds = 0.2


class RenderProfile(_Strict):
    """Re-encode parameters and the length-preserving boundary fade."""

    crf: int = Field(default=18, ge=0, le=51)
    preset: str = "slow"
    boundary_fade: Seconds = 0.010


class SubtitleProfile(_Strict):
    """Subtitle line-breaking and readability limits."""

    max_chars_per_line: int = Field(default=20, gt=0)
    max_lines: int = Field(default=2, gt=0)
    min_display: Seconds = 0.8
    max_cps: float = Field(default=8.0, gt=0)


class Profile(_Strict):
    """``profile.json`` — per-project processing parameters (design.md §3.6)."""

    version: Literal["1"] = "1"
    audio: AudioProfile = Field(default_factory=AudioProfile)
    asr: AsrProfile = Field(default_factory=AsrProfile)
    silence: SilenceProfile = Field(default_factory=SilenceProfile)
    filler: FillerProfile = Field(default_factory=FillerProfile)
    render: RenderProfile = Field(default_factory=RenderProfile)
    subtitle: SubtitleProfile = Field(default_factory=SubtitleProfile)
