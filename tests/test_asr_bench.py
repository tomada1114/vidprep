"""Tests for scripts/asr_bench.py — the §12.2 bench runner.

No ASR backend is involved: the runs are faked at the one place that spawns a
subprocess, so the harness itself (two runs, second one reported, raw logs,
unavailable rows) is what gets exercised.
"""

from __future__ import annotations

import json
import shutil

import pytest

import asr_backends
import asr_bench
import bench_metrics
from vidprep import doctor

WHISPER_CLI = "/opt/homebrew/bin/whisper-cli"
TURBO = next(row for row in asr_backends.CANDIDATES if row.model == "large-v3-turbo")
MLX = next(
    row for row in asr_backends.CANDIDATES if row.backend == asr_backends.MLX_WHISPER
)
KOTOBA = next(row for row in asr_backends.CANDIDATES if "kotoba" in row.slug)
ONE_GIB = 1024**3


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


@pytest.fixture
def context(tmp_path):
    """A bench context whose audio is 100 seconds, half of it silent."""
    audio = tmp_path / "processed.wav"
    audio.write_bytes(b"not really a wav")
    return asr_bench.BenchContext(
        audio=audio,
        out_dir=tmp_path / "bench",
        runs=2,
        duration=100.0,
        silences=[bench_metrics.Interval(50.0, 100.0)],
        reference="こんにちは",
    )


def _fake_run(transcripts, measurements, calls):
    def run(command, log_path):
        index = len(calls)
        calls.append(list(command))
        log_path.write_text("fake time -l output", encoding="utf-8")
        log_path.with_suffix(".json").write_text(
            json.dumps(transcripts[index]), encoding="utf-8"
        )
        return measurements[index]

    return run


def _transcript(text, start=0.0, end=1.0):
    return {"segments": [{"start": start, "end": end, "text": text}]}


def test_measure_reports_the_second_run_and_keeps_both_logs(
    monkeypatch, model_dir, context
):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        asr_backends,
        "run_measured",
        _fake_run(
            [_transcript("一回目"), _transcript("こんにちは", start=60.0, end=61.0)],
            [
                bench_metrics.TimeMeasurement(90.0, 4 * ONE_GIB),
                bench_metrics.TimeMeasurement(30.0, 3 * ONE_GIB),
            ],
            calls,
        ),
    )

    row = asr_bench.measure(TURBO, context)

    assert len(calls) == 2
    assert row.wall_seconds == 30.0
    assert row.realtime_ratio == pytest.approx(0.3)
    assert row.peak_rss_bytes == 3 * ONE_GIB
    assert row.cer == 0.0
    assert row.hallucinations == 1
    directory = context.out_dir / TURBO.slug
    assert sorted(path.name for path in directory.iterdir()) == [
        "run1.json",
        "run1.time",
        "run2.json",
        "run2.time",
        "transcript.txt",
    ]


def test_measure_writes_the_draft_for_the_human_reference(
    monkeypatch, model_dir, context
):
    monkeypatch.setattr(
        asr_backends,
        "run_measured",
        _fake_run(
            [_transcript(" 一回目 "), _transcript(" こんにちは ")],
            [bench_metrics.TimeMeasurement(90.0, ONE_GIB)] * 2,
            [],
        ),
    )

    asr_bench.measure(TURBO, context)

    draft = context.out_dir / TURBO.slug / "transcript.txt"
    assert draft.read_text(encoding="utf-8") == "こんにちは\n"


def test_measure_reports_a_candidate_that_cannot_run(model_dir, context):
    row = asr_bench.measure(KOTOBA, context)

    assert row.unavailable is not None
    assert row.cer is None


def test_measure_reports_a_backend_that_failed_instead_of_raising(
    monkeypatch, model_dir, context
):
    def explode(command, log_path):
        msg = "exited with 3: error: failed to initialize whisper context"
        raise RuntimeError(msg)

    monkeypatch.setattr(asr_backends, "run_measured", explode)

    row = asr_bench.measure(TURBO, context)

    assert row.unavailable == (
        "exited with 3: error: failed to initialize whisper context"
    )


def test_measure_rejects_a_run_count_below_one(monkeypatch, model_dir, context):
    monkeypatch.setattr(asr_backends, "run_measured", _fake_run([], [], []))
    zero_runs = asr_bench.BenchContext(
        audio=context.audio,
        out_dir=context.out_dir,
        runs=0,
        duration=context.duration,
        silences=context.silences,
        reference=context.reference,
    )

    assert asr_bench.measure(TURBO, zero_runs).unavailable == (
        "runs must be at least 1, got 0"
    )


def test_measure_picks_up_the_transcript_mlx_whisper_names_after_the_audio(
    monkeypatch, context
):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(command, log_path):
        log_path.write_text("fake time -l output", encoding="utf-8")
        produced = log_path.parent / f"{context.audio.stem}.json"
        produced.write_text(json.dumps(_transcript("こんにちは")), encoding="utf-8")
        return bench_metrics.TimeMeasurement(25.0, ONE_GIB)

    monkeypatch.setattr(asr_backends, "run_measured", run)

    row = asr_bench.measure(MLX, context)

    assert row.cer == 0.0
    assert (context.out_dir / MLX.slug / "run2.json").is_file()


def test_compare_vad_records_why_it_could_not_run(model_dir, context):
    rows = asr_bench.compare_vad(TURBO.name, context, None)

    assert len(rows) == 1
    assert rows[0].name == f"{TURBO.name} (VAD on)"
    assert rows[0].unavailable is not None


def test_compare_vad_keeps_the_matrix_run_logs(monkeypatch, model_dir, context):
    monkeypatch.setattr(
        asr_backends,
        "run_measured",
        _fake_run(
            [_transcript("こんにちは")] * 4,
            [bench_metrics.TimeMeasurement(30.0, ONE_GIB)] * 4,
            [],
        ),
    )
    asr_bench.measure(TURBO, context)

    rows = asr_bench.compare_vad(TURBO.name, context, model_dir / "ggml-silero.bin")

    assert rows[0].unavailable is None
    assert (context.out_dir / TURBO.slug / "run2.time").is_file()
    assert (context.out_dir / f"{TURBO.slug}-vad" / "run2.time").is_file()


def test_compare_vad_skips_a_backend_without_vad_support(context):
    assert asr_bench.compare_vad(MLX.name, context, None) == []


def test_report_pairs_the_selected_model_with_its_vad_run(context):
    rows = [bench_metrics.BenchRow(name=TURBO.name, cer=0.07, hallucinations=6)]
    vad_rows = [
        bench_metrics.BenchRow(
            name=f"{TURBO.name} (VAD on)", cer=0.07, hallucinations=0
        )
    ]

    text = asr_bench.report(rows, vad_rows, bench_metrics.choose(rows), context)

    assert f"| {TURBO.name} (VAD off) |" in text
    assert f"| {TURBO.name} (VAD on) |" in text


def test_report_asks_for_a_reference_when_none_was_given(context):
    without_reference = asr_bench.BenchContext(
        audio=context.audio,
        out_dir=context.out_dir,
        runs=context.runs,
        duration=context.duration,
        silences=context.silences,
        reference=None,
    )
    rows = [bench_metrics.BenchRow(name="whisper.cpp large-v3", cer=None)]

    text = asr_bench.report(rows, [], bench_metrics.choose(rows), without_reference)

    assert "golden.reference.txt" in text


def test_main_writes_the_matrix_and_the_raw_numbers(
    monkeypatch, model_dir, tmp_path, capsys
):
    audio = tmp_path / "processed.wav"
    audio.write_bytes(b"not really a wav")
    out_dir = tmp_path / "bench"
    monkeypatch.setattr(asr_backends, "detect_silences", lambda *_: [])
    monkeypatch.setattr(
        asr_backends,
        "run_measured",
        _fake_run(
            [_transcript("一回目"), _transcript("こんにちは")],
            [
                bench_metrics.TimeMeasurement(90.0, 4 * ONE_GIB),
                bench_metrics.TimeMeasurement(30.0, 3 * ONE_GIB),
            ],
            [],
        ),
    )
    reference = tmp_path / "golden.reference.txt"
    reference.write_text("こんにちは", encoding="utf-8")

    exit_code = asr_bench.main(
        [
            str(audio),
            "--reference",
            str(reference),
            "--out-dir",
            str(out_dir),
            "--duration",
            "100",
            "--only",
            TURBO.name,
            "--skip-vad-compare",
        ]
    )

    assert exit_code == 0
    assert TURBO.name in capsys.readouterr().out
    written = json.loads((out_dir / "bench.json").read_text(encoding="utf-8"))
    assert written["winner"] == TURBO.name
    assert written["rows"][0]["wall_seconds"] == 30.0
    assert (out_dir / "matrix.md").is_file()


def test_main_rejects_a_missing_audio_file(tmp_path):
    with pytest.raises(SystemExit, match="bench audio not found"):
        asr_bench.main([str(tmp_path / "nope.wav")])
