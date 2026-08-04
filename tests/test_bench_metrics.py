"""Tests for scripts/bench_metrics.py — the §12.2 matrix arithmetic."""

from __future__ import annotations

import json

import pytest

import bench_metrics

TIME_OUTPUT = """\
       98.42 real        95.11 user         2.30 sys
          3221225472  maximum resident set size
                   0  average shared memory size
"""

ONE_GIB = 1024**3

SILENCE_LOG = """\
[silencedetect @ 0x600001] silence_start: 1.5
[silencedetect @ 0x600001] silence_end: 3.25 | silence_duration: 1.75
[silencedetect @ 0x600001] silence_start: 10.0
[silencedetect @ 0x600001] silence_end: 12.5 | silence_duration: 2.5
"""


def _row(name, cer, ratio=1.0):
    return bench_metrics.BenchRow(
        name=name,
        cer=cer,
        wall_seconds=ratio * 100,
        realtime_ratio=ratio,
        hallucinations=0,
        peak_rss_bytes=ONE_GIB,
    )


def test_parse_time_output_reads_wall_time_and_peak_rss():
    measurement = bench_metrics.parse_time_output(TIME_OUTPUT)

    assert measurement.wall_seconds == pytest.approx(98.42)
    assert measurement.peak_rss_bytes == 3221225472


def test_parse_time_output_rejects_a_report_without_the_numbers():
    with pytest.raises(ValueError, match="maximum resident set size"):
        bench_metrics.parse_time_output("real 98.42\n")


def test_strip_time_report_leaves_only_what_the_tool_printed():
    stderr = "error: failed to initialize whisper context\n" + TIME_OUTPUT

    assert bench_metrics.strip_time_report(stderr).strip() == (
        "error: failed to initialize whisper context"
    )


def test_strip_time_report_keeps_output_that_has_no_report():
    assert bench_metrics.strip_time_report("only an error\n") == "only an error\n"


def test_parse_silence_log_pairs_starts_with_ends():
    intervals = bench_metrics.parse_silence_log(SILENCE_LOG, duration=20.0)

    assert intervals == [
        bench_metrics.Interval(1.5, 3.25),
        bench_metrics.Interval(10.0, 12.5),
    ]


def test_parse_silence_log_closes_a_trailing_silence_at_the_end_of_the_file():
    log = "[silencedetect] silence_start: 18.0\n"

    assert bench_metrics.parse_silence_log(log, duration=20.0) == [
        bench_metrics.Interval(18.0, 20.0)
    ]


def test_parse_silence_log_clamps_a_negative_start():
    log = "[silencedetect] silence_start: -0.012\n[silencedetect] silence_end: 2.0\n"

    assert bench_metrics.parse_silence_log(log, duration=20.0) == [
        bench_metrics.Interval(0.0, 2.0)
    ]


def test_parse_silence_log_without_any_silence():
    assert bench_metrics.parse_silence_log("", duration=20.0) == []


def test_load_segments_reads_whisper_cpp_millisecond_offsets(tmp_path):
    path = tmp_path / "run2.json"
    path.write_text(
        json.dumps(
            {
                "transcription": [
                    {"offsets": {"from": 1500, "to": 3250}, "text": "こんにちは"}
                ]
            }
        ),
        encoding="utf-8",
    )

    assert bench_metrics.load_segments(path) == [
        bench_metrics.Segment(1.5, 3.25, "こんにちは")
    ]


def test_load_segments_reads_mlx_whisper_seconds(tmp_path):
    path = tmp_path / "run2.json"
    path.write_text(
        json.dumps(
            {
                "text": "こんにちは",
                "segments": [{"start": 1.5, "end": 3.25, "text": "こんにちは"}],
            }
        ),
        encoding="utf-8",
    )

    assert bench_metrics.load_segments(path) == [
        bench_metrics.Segment(1.5, 3.25, "こんにちは")
    ]


def test_load_segments_rejects_an_unknown_shape(tmp_path):
    path = tmp_path / "run2.json"
    path.write_text(json.dumps({"result": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="neither a `transcription` nor a `segments`"):
        bench_metrics.load_segments(path)


def test_transcript_text_joins_the_segments():
    segments = [
        bench_metrics.Segment(0.0, 1.0, "こんにちは"),
        bench_metrics.Segment(1.0, 2.0, "さようなら"),
    ]

    assert bench_metrics.transcript_text(segments) == "こんにちはさようなら"


def test_count_hallucinations_counts_segments_that_start_in_silence():
    silences = [bench_metrics.Interval(10.0, 12.5)]
    segments = [
        bench_metrics.Segment(2.0, 4.0, "speech"),
        bench_metrics.Segment(11.0, 11.8, "invented"),
        bench_metrics.Segment(12.5, 13.0, "onset at the end of the silence"),
        bench_metrics.Segment(9.5, 11.0, "real speech running into the silence"),
    ]

    assert bench_metrics.count_hallucinations(segments, silences) == 1


def test_count_hallucinations_without_silence_is_zero():
    segments = [bench_metrics.Segment(2.0, 4.0, "speech")]

    assert bench_metrics.count_hallucinations(segments, []) == 0


def test_choose_takes_the_lowest_cer_when_it_leads_by_more_than_a_point():
    rows = [_row("slow-and-good", 0.05, ratio=0.9), _row("fast", 0.08, ratio=0.2)]

    decision = bench_metrics.choose(rows)

    assert decision.winner == "slow-and-good"
    assert "lowest CER" in decision.rationale


def test_choose_prefers_the_faster_candidate_inside_the_tie_threshold():
    rows = [_row("slow-and-good", 0.069, ratio=0.71), _row("fast", 0.073, ratio=0.33)]

    decision = bench_metrics.choose(rows)

    assert decision.winner == "fast"
    assert "0.33x" in decision.rationale


def test_choose_treats_exactly_one_point_as_a_tie():
    rows = [_row("slow-and-good", 0.060, ratio=0.71), _row("fast", 0.070, ratio=0.33)]

    assert bench_metrics.choose(rows).winner == "fast"


def test_choose_ignores_candidates_that_could_not_be_run():
    rows = [
        _row("measured", 0.09, ratio=0.9),
        bench_metrics.BenchRow(name="kotoba", unavailable="no ggml weights"),
    ]

    assert bench_metrics.choose(rows).winner == "measured"


def test_choose_without_any_measurement():
    rows = [bench_metrics.BenchRow(name="kotoba", unavailable="no ggml weights")]

    decision = bench_metrics.choose(rows)

    assert decision.winner is None
    assert "nothing to choose from" in decision.rationale


def test_format_matrix_fills_every_column():
    rendered = bench_metrics.format_matrix([_row("whisper.cpp large-v3", 0.069, 0.71)])

    assert rendered.splitlines()[-1] == (
        "| whisper.cpp large-v3 | 6.90% | 0.71x | 0 | 1.0 GiB |"
    )


def test_format_matrix_keeps_the_reason_a_candidate_was_skipped():
    rows = [bench_metrics.BenchRow(name="kotoba-whisper v2.0", unavailable="no ggml")]

    assert bench_metrics.format_matrix(rows).splitlines()[-1] == (
        "| kotoba-whisper v2.0 | unavailable (reason: no ggml) |"
    )
