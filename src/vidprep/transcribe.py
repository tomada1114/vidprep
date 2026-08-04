"""The ``transcribe`` stage: Silero VAD in front of ASR (design.md §5.2).

The transcript is the one document every later stage joins on — cut detection,
subtitles, telops, the re-transcription check — so a sentence invented over
silence does not stay a transcription problem: it comes back as a subtitle.
That is why detection runs first and cannot be switched off, and why this
module refuses to publish a transcript whose segments do not line up with the
speech that was found (REQ-040, REQ-042).

Recognition always runs on ``audio/processed.wav``, the full-length recording,
never on anything already cut (design.md §2.1) — timestamps are only worth
something while the timeline they were measured on is still intact.

What is written: ``transcript.json`` in original-timeline seconds, and
``report/vad.json`` with the speech regions, which cut detection uses to place
filler cuts and the verification uses to prove there was speech where the
transcript claims there was.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError

from . import _asr, _ffmpeg
from . import audio as audio_module
from . import project as project_module
from .errors import (
    AsrFailedError,
    InvariantViolationError,
    SchemaInvalidError,
    UsageError,
)
from .models import (
    AsrInfo,
    Segment,
    SpeechSegment,
    Transcript,
    VadReport,
    describe_validation_error,
    to_ms,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .project import Project

STAGE = "transcribe"
TRANSCRIPT_NAME = "transcript.json"
VAD_REPORT_NAME = Path("report") / "vad.json"
WORKSPACE_PREFIX = ".transcribe-"

#: Where the known hallucination phrases live inside the installed package.
HALLUCINATION_RESOURCE = "dictionaries/hallucinations.json"

#: Seconds of audio whisper.cpp carries across a region boundary
#: (``--vad-samples-overlap``, 0.1s by default), which is how far outside its
#: own region a segment may legitimately start once the timestamps are mapped
#: back. Anything past that is a segment placed where nobody spoke.
VAD_EDGE_TOLERANCE = 0.1

#: A segment counts as timed over silence when less than this much of it
#: overlaps a speech region; that is where an invented sentence lands.
MIN_SPEECH_COVERAGE = 0.5

_ID_FORMAT = "s{:04d}"


class _Hallucinations(BaseModel):
    """The packaged phrase list, kept out of ``models`` as it is not an artifact."""

    model_config = ConfigDict(extra="forbid")

    version: str
    note: str
    phrases: list[str]


@cache
def hallucination_phrases() -> tuple[str, ...]:
    """Return the phrases whisper is known to invent over silence.

    Raises:
        SchemaInvalidError: If the packaged list does not match its schema.
    """
    resource = resources.files(__package__).joinpath(HALLUCINATION_RESOURCE)
    try:
        loaded = _Hallucinations.model_validate_json(resource.read_text("utf-8"))
    except ValidationError as exc:
        msg = f"{HALLUCINATION_RESOURCE}: {describe_validation_error(exc)}"
        raise SchemaInvalidError(msg) from exc
    return tuple(loaded.phrases)


@dataclass(frozen=True, slots=True)
class Result:
    """What one ``transcribe`` run detected, recognised and verified."""

    backend: str
    model: str
    vad: str
    speech: tuple[_asr.Interval, ...]
    segments: tuple[Segment, ...]
    audio_seconds: float
    elapsed_seconds: float

    @property
    def speech_seconds(self) -> float:
        """How much of the recording was speech, as detected."""
        return sum(region.duration for region in self.speech)

    def to_dict(self) -> dict[str, Any]:
        """Render the result as the JSON document ``--json`` prints."""
        return {
            "backend": self.backend,
            "model": self.model,
            "vad": self.vad,
            "vad_segments": len(self.speech),
            "segments": len(self.segments),
            "speech_duration": round(self.speech_seconds, 3),
            "elapsed_sec": round(self.elapsed_seconds, 2),
            "realtime_factor": round(self.elapsed_seconds / self.audio_seconds, 3)
            if self.audio_seconds
            else None,
            # Both are verified before anything is written, so both are always
            # clean here; they are reported because the completion conditions
            # (verification-plan.md §5) are stated as numbers to read off.
            "vad_outside_starts": 0,
            "hallucination_hits": [],
            "output": TRANSCRIPT_NAME,
            "vad_report": str(VAD_REPORT_NAME),
        }

    def lines(self) -> list[str]:
        """Render the result for a human."""
        factor = self.elapsed_seconds / self.audio_seconds if self.audio_seconds else 0
        return [
            f"✔ {len(self.speech)} speech regions, {self.speech_seconds:.1f}s of "
            f"{self.audio_seconds:.1f}s ({self.vad})",
            f"✔ {TRANSCRIPT_NAME}: {len(self.segments)} segments "
            f"({self.backend} {self.model}, {self.elapsed_seconds:.1f}s, "
            f"{factor:.2f}x realtime)",
            f"✔ {VAD_REPORT_NAME}",
        ]


def _audio_path(loaded: Project) -> Path:
    """Return the processed audio recognition must run on.

    Raises:
        UsageError: If ``audio-fix`` has not produced it yet (REQ-020).
    """
    path = loaded.root / audio_module.OUTPUT_NAME
    if not path.is_file():
        msg = f"{audio_module.OUTPUT_NAME} not found — run `vidprep audio-fix` first"
        raise UsageError(msg)
    return path


def plan(loaded: Project) -> dict[str, Any]:
    """Return what :func:`run_transcribe` would run and write, without doing it."""
    audio = _audio_path(loaded)
    backend = _asr.resolve(loaded.profile.asr)
    workspace = loaded.root / f"{WORKSPACE_PREFIX}XXXXXX"
    stem = _asr.region_stem(1)
    extracted = workspace / f"{stem}.wav"
    commands = (
        [backend.whisper_command(audio, workspace)]
        if backend.name == _asr.WHISPER_CPP
        # The last two then repeat once per detected region, which is why they
        # are shown with the region they would cut left open.
        else [
            backend.detect_command(audio),
            _asr.slice_command(audio, None, extracted),
            backend.mlx_command(extracted, workspace, stem),
        ]
    )
    return {
        "action": "transcribe",
        "project": str(loaded.root),
        "backend": backend.name,
        "model": backend.model,
        "vad": loaded.profile.asr.vad,
        "commands": commands,
        "writes": [
            str(loaded.root / TRANSCRIPT_NAME),
            str(loaded.root / VAD_REPORT_NAME),
            str(loaded.root / project_module.MANIFEST_NAME),
        ],
    }


def _recognise(
    backend: _asr.Backend, audio: Path, workspace: Path
) -> tuple[list[_asr.Interval], list[_asr.RawSegment]]:
    """Detect the speech regions and transcribe them.

    whisper.cpp detects and transcribes in one process and hands back times
    already on the original timeline. mlx-whisper cannot detect, so detection
    runs on its own and each region is extracted and transcribed by itself,
    which leaves the shifting to :func:`_shift`.

    Returns:
        The regions Silero found, and the segments in original-timeline
        seconds.
    """
    if backend.name == _asr.WHISPER_CPP:
        speech = _asr.parse_speech(_asr.run(backend.whisper_command(audio, workspace)))
        return speech, _asr.read_transcript(backend, workspace)
    speech = _asr.parse_speech(_asr.run(backend.detect_command(audio)))
    segments: list[_asr.RawSegment] = []
    for index, region in enumerate(speech, start=1):
        stem = _asr.region_stem(index)
        extracted = workspace / f"{stem}.wav"
        # The cut is ffmpeg's work and reported as such; only what the
        # recogniser does is an ASR failure.
        _ffmpeg.run(_asr.slice_command(audio, region, extracted))
        _asr.run(backend.mlx_command(extracted, workspace, stem))
        segments += _shift(_asr.read_transcript(backend, workspace, stem), region)
    return speech, segments


def _shift(
    raw: Sequence[_asr.RawSegment], region: _asr.Interval
) -> list[_asr.RawSegment]:
    """Put one region's segments back on the original timeline (REQ-003).

    Whisper pads a region shorter than its 30-second window with silence and
    occasionally transcribes the padding, so anything starting past the end of
    the region is dropped: there was no audio there to have heard.
    """
    shifted = []
    for entry in raw:
        start = region.start + entry.start
        if to_ms(start) >= to_ms(region.end):
            continue
        end = min(region.start + entry.end, region.end)
        shifted.append(_asr.RawSegment(start=start, end=end, text=entry.text))
    return shifted


def _clamp_speech(
    speech: Sequence[_asr.Interval], duration: float
) -> list[_asr.Interval]:
    """Hold the detected regions inside the material the manifest describes.

    Detection runs on the audio stream, whose length can differ from the
    container duration recorded at ``init`` by a few milliseconds. The manifest
    is what every other stage measures against, so the regions are trimmed to
    it rather than the difference being argued about later.
    """
    return [
        _asr.Interval(region.start, min(region.end, duration))
        for region in speech
        if to_ms(region.start) < to_ms(duration)
    ]


def _build_segments(raw: Sequence[_asr.RawSegment], duration: float) -> list[Segment]:
    """Number the segments worth keeping and put them on the source timeline.

    A region the recogniser had nothing to say about produces no segment at all
    rather than an empty one, and an end past the source is pulled back to it:
    both backends round their timestamps, and a few milliseconds of overhang is
    not worth failing a five-minute transcription over.

    Raises:
        AsrFailedError: If the timestamps run backwards, or describe a segment
            that cannot exist at all (a negative start). Later stages assume
            transcript order is timeline order, so a transcript that breaks it
            is thrown away rather than published (design.md §5.2 boundary
            conditions).
    """
    segments: list[Segment] = []
    previous: _asr.RawSegment | None = None
    for entry in raw:
        if previous is not None and (
            to_ms(entry.start) < to_ms(previous.start)
            or to_ms(entry.end) < to_ms(previous.end)
        ):
            msg = (
                f"the recogniser returned segments out of order: "
                f"{previous.start:.3f}-{previous.end:.3f} then "
                f"{entry.start:.3f}-{entry.end:.3f}"
            )
            raise AsrFailedError(msg)
        previous = entry
        text = entry.text.strip()
        end = min(entry.end, duration)
        if not text or to_ms(entry.start) >= to_ms(end):
            continue
        try:
            segments.append(
                Segment(
                    id=_ID_FORMAT.format(len(segments) + 1),
                    start=entry.start,
                    end=end,
                    text=text,
                )
            )
        except ValidationError as exc:
            msg = (
                "the recogniser returned a segment vidprep cannot place: "
                f"{describe_validation_error(exc)}"
            )
            raise AsrFailedError(msg) from exc
    return segments


def _outside_speech(
    segments: Sequence[Segment], speech: Sequence[_asr.Interval]
) -> list[Segment]:
    """Return the segments that start where no speech was detected (REQ-040)."""
    return [
        segment
        for segment in segments
        if not any(
            region.start - VAD_EDGE_TOLERANCE
            <= segment.start
            <= region.end + VAD_EDGE_TOLERANCE
            for region in speech
        )
    ]


def _hallucinations(
    segments: Sequence[Segment], speech: Sequence[_asr.Interval]
) -> list[Segment]:
    """Return the segments carrying a known phrase but timed over silence.

    Coverage decides rather than the start alone: an invented sign-off is
    stamped over the quiet part of the recording, while the same words really
    spoken sit inside a region Silero found (REQ-042).
    """
    phrases = hallucination_phrases()
    hits = []
    for segment in segments:
        if not any(phrase in segment.text for phrase in phrases):
            continue
        covered = sum(region.overlap(segment.start, segment.end) for region in speech)
        if covered < (segment.end - segment.start) * MIN_SPEECH_COVERAGE:
            hits.append(segment)
    return hits


def _verify(segments: Sequence[Segment], speech: Sequence[_asr.Interval]) -> None:
    """Check the transcript against the speech that was actually detected.

    Raises:
        InvariantViolationError: If a segment starts outside every detected
            region, or repeats a known hallucination over silence. Both mean
            the transcript claims words where nobody spoke, so it is discarded
            instead of handed to the stages that would turn it into subtitles.
    """
    outside = _outside_speech(segments, speech)
    if outside:
        shown = ", ".join(f"{item.id}@{item.start:.3f}" for item in outside[:5])
        msg = (
            f"{len(outside)} segments start outside every detected speech "
            f"region ({shown}); {TRANSCRIPT_NAME} was left untouched"
        )
        raise InvariantViolationError(msg)
    invented = _hallucinations(segments, speech)
    if invented:
        shown = ", ".join(f"{item.id}: {item.text!r}" for item in invented[:5])
        msg = (
            f"{len(invented)} segments repeat a known hallucination over "
            f"silence ({shown}); {TRANSCRIPT_NAME} was left untouched"
        )
        raise InvariantViolationError(msg)


def _check_bounds(name: str, artifact: BaseModel, duration: float) -> None:
    """Re-validate *artifact* with the bound only the manifest knows.

    The models enforce their own invariants on construction, but the upper
    bound of every interval is the source duration, which reaches them through
    the validation context — so it is checked here, before anything is written.

    Raises:
        SchemaInvalidError: If an interval runs past the end of the material.
    """
    try:
        artifact.__class__.model_validate(
            artifact.model_dump(mode="json"), context={"duration": duration}
        )
    except ValidationError as exc:
        msg = f"{name}: {describe_validation_error(exc)}"
        raise SchemaInvalidError(msg) from exc


def _publish(loaded: Project, result: Result) -> None:
    """Write the speech regions and the transcript, atomically.

    Raises:
        SchemaInvalidError: If what was built violates the transcript schema,
            which includes every interval bound (REQ-041).
    """
    transcript = Transcript(
        audio_source=str(audio_module.OUTPUT_NAME),
        asr=AsrInfo(backend=result.backend, model=result.model, vad=result.vad),
        segments=list(result.segments),
    )
    report = VadReport(
        backend=result.vad,
        segments=[
            SpeechSegment(start=region.start, end=region.end)
            for region in result.speech
        ],
    )
    duration = loaded.manifest.source.duration
    _check_bounds(TRANSCRIPT_NAME, transcript, duration)
    _check_bounds(str(VAD_REPORT_NAME), report, duration)
    (loaded.root / VAD_REPORT_NAME).parent.mkdir(parents=True, exist_ok=True)
    project_module.write_json(loaded.root / VAD_REPORT_NAME, report)
    project_module.write_json(loaded.root / TRANSCRIPT_NAME, transcript)


def run_transcribe(loaded: Project) -> Result:
    """Transcribe the processed audio of *loaded* and record the stage.

    Returns:
        What was detected and recognised, once it has passed verification.

    Raises:
        UsageError: If ``audio-fix`` has not run, or a backend is missing.
        AsrFailedError: If the recogniser failed, returned nothing readable,
            or found no speech at all — an empty transcript is a failure, not
            a valid result (REQ-022).
        InvariantViolationError: If the transcript disagrees with the detected
            speech; nothing is written in that case.
    """
    audio = _audio_path(loaded)
    backend = _asr.resolve(loaded.profile.asr)
    workspace = Path(tempfile.mkdtemp(dir=loaded.root, prefix=WORKSPACE_PREFIX))
    started = time.monotonic()
    try:
        speech, raw = _recognise(backend, audio, workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    elapsed = time.monotonic() - started
    speech = _clamp_speech(speech, loaded.manifest.source.duration)
    if not speech:
        msg = (
            "Silero VAD detected no speech (0 regions) — check the material "
            f"and the result of `vidprep audio-fix`; {TRANSCRIPT_NAME} was not "
            "written"
        )
        raise AsrFailedError(msg)
    duration = loaded.manifest.source.duration
    segments = _build_segments(raw, duration)
    _verify(segments, speech)
    result = Result(
        backend=backend.name,
        model=backend.model,
        vad=loaded.profile.asr.vad,
        speech=tuple(speech),
        segments=tuple(segments),
        audio_seconds=duration,
        elapsed_seconds=elapsed,
    )
    _publish(loaded, result)
    project_module.record_stage(loaded, STAGE, _asr.tool_versions(backend))
    return result
