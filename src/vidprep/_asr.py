"""The recognisers the ``transcribe`` stage drives (design.md §5.2).

Everything that knows how to call whisper.cpp or mlx-whisper lives here: where
the weights are looked up, the argument vectors, and how each backend's output
is read back. ``transcribe.py`` composes them into the stage, which is what
lets the stage be tested without either backend installed.

Voice activity detection is not a separate tool. whisper.cpp ships Silero as a
front-end to its own transcription (``--vad``): it detects the speech regions,
feeds the recogniser those regions only, reports every one of them in its log,
and maps the timestamps it emits back onto the original timeline. That is the
"detect speech, transcribe each region, correct the timestamps" chain of
design.md §5.2 run in a single process, which its Technical Notes explicitly
allow — so the whisper.cpp backend reads the regions out of the very run that
used them, and nothing has to be sliced or re-timed here.

mlx-whisper has no such front-end, so it borrows this one: a whisper.cpp run
stopped right after detection supplies the regions, each region is extracted
with ffmpeg, and mlx transcribes it on its own — the stage then adds the
region's offset to what comes back. Either way whisper.cpp must be installed,
because it is where Silero lives.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import _ffmpeg, doctor
from .errors import AsrFailedError, FfmpegError, UsageError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .models import AsrProfile

WHISPER_CPP = "whisper.cpp"
MLX_WHISPER = "mlx-whisper"
MLX_BINARY = "mlx_whisper"

#: Silero weights whisper.cpp's ``--vad`` needs, looked up next to the models
#: so ``$VIDPREP_WHISPER_MODEL_DIR`` moves both at once (design.md §5.7). The
#: pattern lives in :mod:`doctor`, which reports on the same file.
VAD_MODEL_GLOB = doctor.VAD_MODEL_GLOB

#: Stem every backend writes its transcript under, inside the workspace.
OUTPUT_STEM = "asr"

#: Format one detected region is extracted in for a backend that cannot detect
#: speech itself: what every whisper implementation resamples to anyway.
SLICE_CODEC = "pcm_s16le"
SLICE_SAMPLE_RATE = 16000

#: What ``--dry-run`` prints where only a finished detection can supply a number.
PLACEHOLDER_START = "<region start>"
PLACEHOLDER_DURATION = "<region duration>"

#: whisper.cpp always loads a transcription model, even for a run that only has
#: to detect speech, so the mlx-whisper path loads the cheapest one it finds.
_MODEL_GLOB = "ggml-*.bin"

#: Milliseconds of audio the detection-only run asks to transcribe: none of it
#: is wanted, but the flag is what makes whisper.cpp stop once Silero is done.
_PROBE_DURATION_MS = "1"

_MS_PER_SECOND = 1000.0

#: The mapping table whisper.cpp builds to put its timestamps back on the
#: original timeline; one line per speech region, printed as it is used.
_SPEECH_REGION = re.compile(
    r"vad_segment_info:\s*orig_start:\s*([\d.]+),\s*orig_end:\s*([\d.]+)"
)

#: Proof that voice activity detection actually ran, printed even when it found
#: nothing at all, which is how "no speech" is told apart from "no VAD".
_VAD_RAN = re.compile(r"^whisper_vad", re.MULTILINE)

_VERSION = re.compile(r"whisper\.cpp version:?\s*(\S+)")


@dataclass(frozen=True, slots=True)
class Interval:
    """A speech region, in original-timeline seconds."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        """How long the region lasts."""
        return self.end - self.start

    def overlap(self, start: float, end: float) -> float:
        """Return how much of ``[start, end]`` falls inside this region."""
        return max(0.0, min(self.end, end) - max(self.start, start))


@dataclass(frozen=True, slots=True)
class RawSegment:
    """One segment a backend produced, already on the original timeline."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Backend:
    """A resolved recogniser: the tools and weights this machine will use.

    Attributes:
        name: ``whisper.cpp`` or ``mlx-whisper``, as ``profile.json`` names it.
        model: The model identifier recorded in ``transcript.json``.
        language: Spoken language passed to the recogniser.
        whisper_binary: The whisper.cpp CLI, which hosts Silero for both paths.
        whisper_model: ggml weights that CLI loads — the transcription model
            itself for whisper.cpp, the cheapest installed one for mlx-whisper.
        vad_model: Silero weights ``--vad`` runs.
        mlx_binary: The mlx-whisper CLI, or ``None`` for the whisper.cpp path.
    """

    name: str
    model: str
    language: str
    whisper_binary: str
    whisper_model: Path
    vad_model: Path
    mlx_binary: str | None = None

    def transcript_path(self, workspace: Path, stem: str = OUTPUT_STEM) -> Path:
        """Return the JSON the backend writes inside *workspace*."""
        return workspace / f"{stem}.json"

    def _vad_options(self) -> list[str]:
        return ["--vad", "--vad-model", str(self.vad_model)]

    def whisper_command(self, audio: Path, workspace: Path) -> list[str]:
        """Return the whisper.cpp run that detects speech and transcribes it."""
        return [
            self.whisper_binary,
            "-m",
            str(self.whisper_model),
            "-l",
            self.language,
            "-oj",
            "-of",
            str(workspace / OUTPUT_STEM),
            *self._vad_options(),
            "-f",
            str(audio),
        ]

    def detect_command(self, audio: Path) -> list[str]:
        """Return the whisper.cpp run that only detects the speech regions."""
        return [
            self.whisper_binary,
            "-m",
            str(self.whisper_model),
            "-l",
            self.language,
            "-d",
            _PROBE_DURATION_MS,
            *self._vad_options(),
            "-f",
            str(audio),
        ]

    def mlx_command(self, audio: Path, workspace: Path, stem: str) -> list[str]:
        """Return the mlx-whisper run over one detected region.

        One region per run, rather than one run with ``--clip-timestamps`` over
        all of them: asked for many short clips at once, whisper stamps its
        output with times that wander outside the clip it was decoding — on the
        golden sample 23 of 106 segments landed outside the speech, some of
        them past the end of the recording. A region on its own comes back in
        its own seconds, which :mod:`vidprep.transcribe` puts on the original
        timeline.

        Args:
            audio: The extracted region, not the whole recording.
            workspace: Where the transcript is written.
            stem: File name the transcript is written under, without suffix.
        """
        return [
            self.mlx_binary or MLX_BINARY,
            str(audio),
            "--model",
            self.model,
            "--language",
            self.language,
            "--output-format",
            "json",
            "--output-name",
            stem,
            "--output-dir",
            str(workspace),
        ]


def _model_dir() -> Path:
    """Return the directory whisper.cpp weights are looked up in."""
    return doctor.whisper_model_dir()


def _require(path: Path, what: str, remedy: str) -> Path:
    """Return *path*, or explain what to install instead.

    Raises:
        UsageError: If *path* is not a file.
    """
    if not path.is_file():
        msg = f"{what} not found: {path} — {remedy}"
        raise UsageError(msg)
    return path


def vad_model() -> Path:
    """Return the Silero weights, looked up next to the whisper.cpp models.

    Raises:
        UsageError: If no ``ggml-silero-*.bin`` is installed. Detection cannot
            be skipped (design.md §5.2), so a missing model stops the stage
            instead of quietly degrading it into a transcription that
            hallucinates in the silences.
    """
    directory = _model_dir()
    found = sorted(directory.glob(VAD_MODEL_GLOB)) if directory.is_dir() else []
    if not found:
        msg = (
            f"no {VAD_MODEL_GLOB} in {directory}; voice activity detection is "
            "mandatory — fetch the weights with whisper.cpp's "
            "`models/download-vad-model.sh silero-v5.1.2` (or point "
            f"${doctor.WHISPER_MODEL_DIR_ENV} at where they already are)"
        )
        raise UsageError(msg)
    return found[-1]


def _detection_host_model() -> Path:
    """Return the cheapest ggml model, loaded only so Silero can run.

    Raises:
        UsageError: If no transcription model is installed at all.
    """
    directory = _model_dir()
    candidates = [
        model
        for model in (directory.glob(_MODEL_GLOB) if directory.is_dir() else ())
        if not model.match(VAD_MODEL_GLOB)
    ]
    if not candidates:
        msg = (
            f"no {_MODEL_GLOB} in {directory}; whisper.cpp hosts the Silero "
            "front-end the mlx-whisper backend needs, and it loads a "
            "transcription model to do so"
        )
        raise UsageError(msg)
    return min(candidates, key=lambda model: model.stat().st_size)


def resolve(settings: AsrProfile) -> Backend:
    """Resolve *settings* against what this machine has installed.

    Raises:
        UsageError: If a binary or a set of weights is missing; the message
            names what to install.
    """
    binary = next(
        (found for name in doctor.WHISPER_BINARIES if (found := shutil.which(name))),
        None,
    )
    if binary is None:
        msg = (
            f"none of {', '.join(doctor.WHISPER_BINARIES)} found in PATH; "
            "whisper.cpp runs the mandatory Silero VAD front-end for every "
            "backend (`brew install whisper-cpp`)"
        )
        raise UsageError(msg)
    is_mlx = settings.backend == MLX_WHISPER
    mlx_binary = None
    if is_mlx:
        mlx_binary = shutil.which(MLX_BINARY)
        if mlx_binary is None:
            msg = f"{MLX_BINARY} not found in PATH (`uv sync --group asr`)"
            raise UsageError(msg)
    model = (
        _detection_host_model()
        if is_mlx
        else _require(
            _model_dir() / f"ggml-{settings.model}.bin",
            f"whisper.cpp model {settings.model!r}",
            f"download it into that directory, or set ${doctor.WHISPER_MODEL_DIR_ENV}",
        )
    )
    return Backend(
        name=settings.backend,
        model=settings.model,
        language=settings.language,
        whisper_binary=binary,
        whisper_model=model,
        vad_model=vad_model(),
        mlx_binary=mlx_binary,
    )


def run(command: Sequence[str]) -> str:
    """Run one backend command and return its log.

    Returns:
        Everything the command wrote to stderr, where whisper.cpp reports the
        speech regions it detected.

    Raises:
        AsrFailedError: If the backend exited non-zero. Nothing has been
            written at that point, so the previous transcript survives
            untouched (REQ-021).
    """
    try:
        return _ffmpeg.run_analysis(command)
    except FfmpegError as exc:
        raise AsrFailedError(str(exc)) from exc


def parse_speech(log: str) -> list[Interval]:
    """Return the speech regions whisper.cpp reported in *log*.

    Returns:
        The regions in the order they were detected; empty when detection ran
        but found no speech, which the caller reports as a failed stage.

    Raises:
        AsrFailedError: If the log shows no sign of detection having run at
            all — a build without VAD support would otherwise transcribe the
            silences and be recorded as if it had not.
    """
    regions = [
        Interval(float(start), float(end)) for start, end in _SPEECH_REGION.findall(log)
    ]
    if not regions and not _VAD_RAN.search(log):
        msg = (
            "whisper.cpp reported no voice activity detection; the installed "
            "build must support --vad (whisper.cpp >= 1.7.4)"
        )
        raise AsrFailedError(msg)
    return regions


def region_stem(index: int) -> str:
    """Return the file name stem the *index*-th region's files are written under."""
    return f"{OUTPUT_STEM}-{index:04d}"


def slice_command(audio: Path, region: Interval | None, target: Path) -> list[str]:
    """Return the ffmpeg command that extracts *region* from *audio*.

    The cut is what a backend without its own detection front-end is given, so
    it is made where every other measurement is made — on the processed audio,
    at 16 kHz mono, the format every whisper implementation resamples to
    anyway.

    Args:
        audio: The processed recording, which is only read.
        region: The speech region to extract, or ``None`` for ``--dry-run``,
            which has not detected anything to name yet.
        target: Where the extracted audio is written.
    """
    start = PLACEHOLDER_START if region is None else f"{region.start:.3f}"
    length = PLACEHOLDER_DURATION if region is None else f"{region.duration:.3f}"
    return [
        _ffmpeg.FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-nostats",
        "-v",
        "error",
        "-y",
        "-ss",
        start,
        "-t",
        length,
        "-i",
        str(audio),
        "-map",
        "0:a:0",
        "-c:a",
        SLICE_CODEC,
        "-ar",
        str(SLICE_SAMPLE_RATE),
        "-ac",
        "1",
        str(target),
    ]


def _read_document(path: Path) -> dict[str, object]:
    """Read the JSON a backend wrote.

    Raises:
        AsrFailedError: If the file is absent or not a JSON object; the
            backend exited cleanly, so its output is all there is to go on.
    """
    try:
        document = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"the recogniser wrote no readable transcript to {path.name}: {exc}"
        raise AsrFailedError(msg) from exc
    if not isinstance(document, dict):
        msg = f"{path.name} is not a JSON object"
        raise AsrFailedError(msg)
    return document


def read_transcript(
    backend: Backend, workspace: Path, stem: str = OUTPUT_STEM
) -> list[RawSegment]:
    """Read the segments *backend* wrote under *stem*.

    whisper.cpp reports whole milliseconds under ``transcription``, mlx-whisper
    reports seconds under ``segments``. A whisper.cpp run transcribed the whole
    recording and its times are already on the original timeline; an
    mlx-whisper run transcribed one extracted region, so its times are relative
    to that region until the stage shifts them.

    Raises:
        AsrFailedError: If the document matches neither shape.
    """
    document = _read_document(backend.transcript_path(workspace, stem))
    entries = document.get(
        "transcription" if backend.name == WHISPER_CPP else "segments"
    )
    if not isinstance(entries, list):
        msg = (
            f"{backend.name} wrote a transcript vidprep cannot read: "
            "neither a `transcription` nor a `segments` list"
        )
        raise AsrFailedError(msg)
    try:
        return [_read_segment(entry) for entry in entries]
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"{backend.name} wrote a segment vidprep cannot read: {exc}"
        raise AsrFailedError(msg) from exc


def _read_segment(entry: object) -> RawSegment:
    """Return one segment from either backend's representation of it.

    Raises:
        TypeError: If the entry is not an object, which the caller reports.
    """
    if not isinstance(entry, dict):
        msg = f"expected an object, found {type(entry).__name__}"
        raise TypeError(msg)
    if (offsets := entry.get("offsets")) is not None:
        return RawSegment(
            start=float(offsets["from"]) / _MS_PER_SECOND,
            end=float(offsets["to"]) / _MS_PER_SECOND,
            text=str(entry["text"]),
        )
    return RawSegment(
        start=float(entry["start"]), end=float(entry["end"]), text=str(entry["text"])
    )


def tool_versions(backend: Backend) -> dict[str, str]:
    """Return what produced this transcript, for the manifest's stage record."""
    versions = {WHISPER_CPP: _whisper_version(backend)}
    if backend.name == MLX_WHISPER:
        versions[MLX_WHISPER] = str(doctor.check_mlx_whisper().get("version"))
    return versions


def _whisper_version(backend: Backend) -> str:
    """Return the whisper.cpp version, or ``unknown`` if it will not say."""
    try:
        banner = _ffmpeg.run([backend.whisper_binary, "--version"])
    except (UsageError, FfmpegError):
        return "unknown"
    match = _VERSION.search(banner)
    return match.group(1) if match else "unknown"
