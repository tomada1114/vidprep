"""Tests for the golden-run comparison (verification-plan.md §3.3, §11)."""

from __future__ import annotations

import json
import os

import pytest

import compare_stats

STATS = {
    "version": "1",
    "generated_at": "2026-08-13T10:00:00+09:00",
    "duration": {"source": 298.92, "rendered": 197.508, "reduction_ratio": 0.339},
    "loudness": {"rendered": -14.08, "target": -14.0},
    "subtitles": {"entries": 163, "warnings": {"max_cps": [1, 2], "min_display": []}},
}


def write_run(directory, stats=None, summary=None):
    """Write one archived run and return its directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / compare_stats.STATS_NAME).write_text(
        json.dumps(stats if stats is not None else STATS), encoding="utf-8"
    )
    if summary is not None:
        (directory / compare_stats.SUMMARY_NAME).write_text(
            json.dumps(summary), encoding="utf-8"
        )
    return directory


class TestCompare:
    def test_two_identical_runs_report_nothing(self):
        assert compare_stats.compare(STATS, STATS) == []

    def test_a_changed_number_is_reported_with_its_delta(self):
        later = json.loads(json.dumps(STATS))
        later["duration"]["reduction_ratio"] = 0.341

        (line,) = compare_stats.compare(STATS, later)

        assert line.startswith("duration.reduction_ratio  0.339 → 0.341")
        assert "+0.002" in line

    def test_a_warning_list_is_compared_by_its_length(self):
        later = json.loads(json.dumps(STATS))
        later["subtitles"]["warnings"]["max_cps"] = [1, 2, 3, 4, 5]

        (line,) = compare_stats.compare(STATS, later)

        assert "subtitles.warnings.max_cps  2 → 5" in line
        assert "⚠ increased" in line

    def test_a_shrinking_warning_list_is_not_marked(self):
        later = json.loads(json.dumps(STATS))
        later["subtitles"]["warnings"]["max_cps"] = []

        (line,) = compare_stats.compare(STATS, later)

        assert "⚠" not in line

    def test_a_value_that_stopped_being_measured_is_reported(self):
        later = json.loads(json.dumps(STATS))
        later["duration"]["rendered"] = None

        (line,) = compare_stats.compare(STATS, later)

        assert "duration.rendered  197.508 → —" in line

    def test_a_changed_recogniser_is_reported(self):
        before = {"verify_asr": {"model": "large-v3-turbo", "near_boundary_flags": 0}}
        after = {"verify_asr": {"model": "small", "near_boundary_flags": 0}}

        (line,) = compare_stats.compare(before, after)

        assert line == "verify_asr.model  large-v3-turbo → small"

    def test_the_timestamp_of_the_run_is_not_a_difference(self):
        later = json.loads(json.dumps(STATS))
        later["generated_at"] = "2026-08-20T10:00:00+09:00"

        assert compare_stats.compare(STATS, later) == []


class TestLoadRun:
    def test_the_verify_asr_section_is_folded_in(self, tmp_path):
        directory = write_run(
            tmp_path / "2026-08-20",
            summary={
                "stages": {
                    "render": {"result": {"verify_asr": {"near_boundary_flags": 2}}}
                },
                "warnings": ["render: ⚠ something"],
            },
        )

        document = compare_stats.load_run(directory)

        assert document["verify_asr"]["near_boundary_flags"] == 2
        assert document["run.warnings"] == ["render: ⚠ something"]

    def test_a_run_that_never_reached_report_is_an_error(self, tmp_path):
        directory = tmp_path / "2026-08-20"
        directory.mkdir()

        with pytest.raises(SystemExit, match="never reached"):
            compare_stats.load_run(directory)


class TestMain:
    def test_the_first_run_is_reported_and_exits_zero(self, tmp_path, capsys):
        runs = tmp_path / "runs"
        write_run(runs / "2026-08-20")

        code = main_with(runs)

        assert code == compare_stats.EXIT_OK
        assert "first run" in capsys.readouterr().out

    def test_no_runs_at_all_still_exits_zero(self, tmp_path, capsys):
        code = main_with(tmp_path / "runs")

        assert code == compare_stats.EXIT_OK
        assert "first run" in capsys.readouterr().out

    def test_the_two_most_recent_runs_are_compared(self, tmp_path, capsys):
        runs = tmp_path / "runs"
        write_run(runs / "2026-08-13")
        later = json.loads(json.dumps(STATS))
        later["subtitles"]["entries"] = 170
        write_run(runs / "2026-08-20", later)

        code = main_with(runs)

        assert code == compare_stats.EXIT_OK
        assert "subtitles.entries  163 → 170" in capsys.readouterr().out

    def test_runs_are_ordered_by_when_they_were_written(self, tmp_path):
        """A label that does not sort by name must not fool the lookup."""
        runs = tmp_path / "runs"
        older = write_run(runs / "2026-08-04-9")
        newer = write_run(runs / "2026-08-04-10")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))

        assert compare_stats.recent_runs(runs, limit=2) == [older, newer]

    def test_two_directories_may_be_named(self, tmp_path, capsys):
        runs = tmp_path / "runs"
        first = write_run(runs / "2026-08-13")
        later = json.loads(json.dumps(STATS))
        later["subtitles"]["entries"] = 170
        second = write_run(runs / "2026-08-20", later)

        code = compare_stats.main([str(first), str(second)])

        assert code == compare_stats.EXIT_OK
        assert "163 → 170" in capsys.readouterr().out


def main_with(runs):
    """Run the comparison against *runs* as its archive directory."""
    return compare_stats.main(["--runs-dir", str(runs)])
