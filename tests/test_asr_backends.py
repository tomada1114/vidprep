"""Tests for scripts/asr_backends.py — what the bench asks of external tools."""

from __future__ import annotations

import shutil
import subprocess

import pytest

import asr_backends
from vidprep import doctor

WHISPER_CLI = "/opt/homebrew/bin/whisper-cli"
TURBO = next(row for row in asr_backends.CANDIDATES if row.model == "large-v3-turbo")
MLX = next(
    row for row in asr_backends.CANDIDATES if row.backend == asr_backends.MLX_WHISPER
)
KOTOBA = next(row for row in asr_backends.CANDIDATES if "kotoba" in row.slug)

TIME_REPORT = """\
        1.99 real         0.39 user         1.07 sys
          3615686656  maximum resident set size
          3615673392  peak memory footprint
"""


@pytest.fixture
def model_dir(tmp_path, monkeypatch):
    """A whisper.cpp model directory holding only the turbo weights."""
    directory = tmp_path / "models"
    directory.mkdir()
    (directory / "ggml-large-v3-turbo.bin").write_bytes(b"weights")
    monkeypatch.setenv(doctor.WHISPER_MODEL_DIR_ENV, str(directory))
    monkeypatch.setattr(
        shutil, "which", lambda name: WHISPER_CLI if name == "whisper-cli" else None
    )
    return directory


def test_missing_weights_are_reported_with_the_conversion_hint(model_dir):
    reason = asr_backends.unavailable_reason(KOTOBA)

    assert reason is not None
    assert "ggml-kotoba-whisper-v2.0.bin" in reason
    assert "convert-h5-to-ggml.py" in reason


def test_present_weights_make_a_candidate_runnable(model_dir):
    assert asr_backends.unavailable_reason(TURBO) is None


def test_a_missing_whisper_binary_is_reported(monkeypatch, model_dir):
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    reason = asr_backends.unavailable_reason(TURBO)

    assert reason is not None
    assert "PATH" in reason


def test_a_missing_mlx_whisper_is_reported(model_dir):
    reason = asr_backends.unavailable_reason(MLX)

    assert reason is not None
    assert asr_backends.MLX_BINARY in reason


def test_whisper_command_writes_json_next_to_the_run_log(model_dir, tmp_path):
    audio = tmp_path / "processed.wav"

    command = asr_backends.build_command(TURBO, audio, tmp_path / "run2", None)

    assert command[0] == WHISPER_CLI
    assert "-oj" in command
    assert command[command.index("-of") + 1] == str(tmp_path / "run2")
    assert command[command.index("-f") + 1] == str(audio)
    assert "--vad" not in command


def test_whisper_command_enables_vad_when_weights_are_given(model_dir, tmp_path):
    vad_model = tmp_path / "ggml-silero-v5.1.2.bin"

    command = asr_backends.build_command(
        TURBO, tmp_path / "processed.wav", tmp_path / "run2", vad_model
    )

    assert "--vad" in command
    assert command[command.index("--vad-model") + 1] == str(vad_model)


def test_mlx_command_writes_into_the_run_directory(tmp_path):
    command = asr_backends.build_command(
        MLX, tmp_path / "processed.wav", tmp_path / "runs" / "run2", None
    )

    assert command[0] == asr_backends.MLX_BINARY
    assert command[command.index("--model") + 1] == MLX.model
    assert command[command.index("--output-dir") + 1] == str(tmp_path / "runs")


def test_transcript_path_moves_what_mlx_whisper_named_after_the_audio(tmp_path):
    audio = tmp_path / "processed.wav"
    (tmp_path / "processed.json").write_text("{}", encoding="utf-8")

    path = asr_backends.transcript_path(MLX, audio, tmp_path / "run2")

    assert path == tmp_path / "run2.json"
    assert path.is_file()
    assert not (tmp_path / "processed.json").exists()


def test_run_measured_keeps_the_raw_report_and_reads_it(monkeypatch, tmp_path):
    log_path = tmp_path / "run1.time"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=TIME_REPORT
        ),
    )

    measurement = asr_backends.run_measured(["whisper-cli"], log_path)

    assert measurement.wall_seconds == pytest.approx(1.99)
    assert measurement.peak_rss_bytes == 3615686656
    assert "maximum resident set size" in log_path.read_text(encoding="utf-8")


def test_run_measured_reports_the_tool_error_not_the_time_footer(monkeypatch, tmp_path):
    stderr = "error: failed to initialize whisper context\n" + TIME_REPORT
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=3, stdout="", stderr=stderr
        ),
    )

    with pytest.raises(RuntimeError, match="failed to initialize whisper context"):
        asr_backends.run_measured(["whisper-cli"], tmp_path / "run1.time")


def test_find_vad_model_picks_the_silero_weights(model_dir):
    weights = model_dir / "ggml-silero-v5.1.2.bin"
    weights.write_bytes(b"vad")

    assert asr_backends.find_vad_model(None) == weights


def test_find_vad_model_without_any_weights(model_dir):
    assert asr_backends.find_vad_model(None) is None


def test_find_vad_model_ignores_a_path_that_does_not_exist(tmp_path):
    assert asr_backends.find_vad_model(tmp_path / "nope.bin") is None


def test_silence_command_uses_the_documented_threshold(tmp_path):
    command = asr_backends.silence_command(tmp_path / "processed.wav")

    assert "silencedetect=noise=-40dB:d=0.5" in command
