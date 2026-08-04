"""The external tools the ASR bench drives (verification-plan.md §12.2).

Everything that knows how to call something outside this repository lives here:
which candidates exist, whether their weights are installed, the argument
vectors that transcribe or inspect the bench audio, and the measured run
itself. ``asr_bench.py`` composes these into the matrix.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

import bench_metrics
from vidprep import doctor

if TYPE_CHECKING:
    from pathlib import Path

WHISPER_CPP = "whisper.cpp"
MLX_WHISPER = "mlx-whisper"

#: The one timing source REQ-006 allows, for both wall time and peak RSS.
TIME_BINARY = "/usr/bin/time"
MLX_BINARY = "mlx_whisper"
FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"
LANGUAGE = "ja"

#: Silero VAD weights for whisper.cpp's ``--vad``, looked up next to the models.
VAD_MODEL_GLOB = "ggml-silero-*.bin"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One row of the matrix, before it has been run.

    Attributes:
        name: How verification-plan.md §12.2 names the row.
        slug: Directory name the raw logs are kept under.
        backend: ``whisper.cpp`` or ``mlx-whisper``.
        model: ggml basename for whisper.cpp, repository id for mlx-whisper.
    """

    name: str
    slug: str
    backend: str
    model: str


CANDIDATES = (
    Candidate("whisper.cpp large-v3", "whisper-cpp-large-v3", WHISPER_CPP, "large-v3"),
    Candidate(
        "whisper.cpp large-v3-turbo",
        "whisper-cpp-large-v3-turbo",
        WHISPER_CPP,
        "large-v3-turbo",
    ),
    Candidate(
        "mlx-whisper large-v3-turbo",
        "mlx-whisper-large-v3-turbo",
        MLX_WHISPER,
        "mlx-community/whisper-large-v3-turbo",
    ),
    Candidate(
        "kotoba-whisper v2.0",
        "kotoba-whisper-v2.0",
        WHISPER_CPP,
        "kotoba-whisper-v2.0",
    ),
)


def whisper_cpp_binary() -> str | None:
    """Return the whisper.cpp CLI on PATH, under any of its names."""
    return next(
        (found for name in doctor.WHISPER_BINARIES if (found := shutil.which(name))),
        None,
    )


def model_dir() -> Path:
    """Return the directory whisper.cpp weights are looked up in."""
    return doctor.whisper_model_dir()


def model_path(model: str) -> Path:
    """Return where the ggml weights for *model* are expected to live."""
    return model_dir() / f"ggml-{model}.bin"


def unavailable_reason(candidate: Candidate) -> str | None:
    """Return why *candidate* cannot be run, or ``None`` when it can.

    A missing ggml file is the expected outcome for kotoba-whisper, which ships
    only as a transformers checkpoint; REQ-021 wants that recorded as a reason
    rather than silently dropped, so the hint names the conversion step.
    """
    if candidate.backend == WHISPER_CPP:
        if whisper_cpp_binary() is None:
            return f"none of {', '.join(doctor.WHISPER_BINARIES)} found in PATH"
        weights = model_path(candidate.model)
        if not weights.is_file():
            return (
                f"no {weights.name} in {weights.parent} "
                "(convert it with whisper.cpp/models/convert-h5-to-ggml.py)"
            )
        return None
    if shutil.which(MLX_BINARY) is None:
        return f"{MLX_BINARY} not found in PATH (`uv sync --group asr`)"
    return None


def build_command(
    candidate: Candidate, audio: Path, output_prefix: Path, vad_model: Path | None
) -> list[str]:
    """Return the argument vector that transcribes *audio* once.

    Args:
        candidate: The model and backend to run.
        audio: The loudnorm-processed bench audio.
        output_prefix: Path stem the transcript is written to, without suffix.
        vad_model: Silero weights to enable whisper.cpp's ``--vad`` with, or
            ``None`` for the plain run hallucinations are counted on.
    """
    if candidate.backend == WHISPER_CPP:
        binary = whisper_cpp_binary()
        assert binary is not None  # noqa: S101 — guarded by unavailable_reason
        command = [
            binary,
            "-m",
            str(model_path(candidate.model)),
            "-l",
            LANGUAGE,
            "-oj",
            "-of",
            str(output_prefix),
            "-f",
            str(audio),
        ]
        if vad_model is not None:
            command += ["--vad", "--vad-model", str(vad_model)]
        return command
    return [
        MLX_BINARY,
        str(audio),
        "--model",
        candidate.model,
        "--language",
        LANGUAGE,
        "--output-format",
        "json",
        "--output-dir",
        str(output_prefix.parent),
    ]


def transcript_path(candidate: Candidate, audio: Path, prefix: Path) -> Path:
    """Return the JSON the backend just wrote, renamed to ``run<n>.json``.

    whisper.cpp writes where it is told; mlx-whisper insists on naming the file
    after the audio, so its output is moved into place.
    """
    written = prefix.with_suffix(".json")
    if candidate.backend == MLX_WHISPER:
        produced = prefix.parent / f"{audio.stem}.json"
        if produced != written and produced.is_file():
            produced.replace(written)
    return written


def run_measured(command: list[str], log_path: Path) -> bench_metrics.TimeMeasurement:
    """Run one transcription under ``time -l``, keeping the raw report.

    Raises:
        RuntimeError: If the backend exits non-zero; the message carries the
            last line of its stderr so the matrix can record the reason.
    """
    # Fixed argument vector, shell=False: nothing is interpolated into a shell.
    completed = subprocess.run(  # noqa: S603
        [TIME_BINARY, "-l", *command], capture_output=True, text=True, check=False
    )
    log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        detail = bench_metrics.strip_time_report(completed.stderr).strip().splitlines()
        reason = detail[-1].strip() if detail else "no output"
        msg = f"exited with {completed.returncode}: {reason}"
        raise RuntimeError(msg)
    return bench_metrics.parse_time_output(completed.stderr)


def duration_command(audio: Path) -> list[str]:
    """Return the ffprobe command that reports the length of *audio*."""
    return [
        FFPROBE,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        str(audio),
    ]


def silence_command(audio: Path) -> list[str]:
    """Return the ffmpeg command that lists the silent spans of *audio*."""
    filter_spec = (
        f"silencedetect=noise={bench_metrics.SILENCE_NOISE_DB}dB"
        f":d={bench_metrics.SILENCE_MIN_SECONDS}"
    )
    return [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-i",
        str(audio),
        "-af",
        filter_spec,
        "-f",
        "null",
        "-",
    ]


def probe_duration(audio: Path) -> float:
    """Return the length of the bench audio in seconds, via ffprobe."""
    # Fixed argument vector, shell=False.
    completed = subprocess.run(  # noqa: S603
        duration_command(audio), capture_output=True, text=True, check=True
    )
    return float(completed.stdout.strip())


def detect_silences(audio: Path, duration: float) -> list[bench_metrics.Interval]:
    """Return the silent spans hallucinations are counted against (REQ-007)."""
    # Fixed argument vector, shell=False.
    completed = subprocess.run(  # noqa: S603
        silence_command(audio), capture_output=True, text=True, check=True
    )
    return bench_metrics.parse_silence_log(completed.stderr, duration)


def find_vad_model(explicit: Path | None) -> Path | None:
    """Return the Silero weights whisper.cpp's ``--vad`` needs, if present."""
    if explicit is not None:
        return explicit if explicit.is_file() else None
    directory = model_dir()
    if not directory.is_dir():
        return None
    return next(iter(sorted(directory.glob(VAD_MODEL_GLOB))), None)
