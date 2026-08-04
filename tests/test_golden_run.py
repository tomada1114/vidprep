"""Tests for the golden-run harness (verification-plan.md §11).

The pipeline itself is not run here — that needs the material and three external
tools, which is the whole reason the harness exists as a local-only target. What
is checked is the harness: that it runs the six stages in order, that a stage
that fails stops the run and is recorded, and that the archive lands where
``scripts/compare_stats.py`` looks for it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

import golden_run
from vidprep.errors import EXIT_EXECUTION, EXIT_OK, AsrFailedError

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeStage:
    """A stage that reports one number and one line."""

    def __init__(self, name: str) -> None:
        self.name = name

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.name, "value": 1}

    def lines(self) -> list[str]:
        return [f"⚠ {self.name} had a remark", f"✔ {self.name} finished"]


@pytest.fixture
def pipeline(monkeypatch):
    """Replace the six stages with fakes, and return the names they ran under."""
    ran: list[str] = []

    def _stage(name: str) -> Callable[[Any], FakeStage]:
        def _run(loaded):
            ran.append(name)
            return FakeStage(name)

        return _run

    monkeypatch.setattr(
        golden_run,
        "STAGES",
        tuple((name, _stage(name)) for name, _ in golden_run.STAGES),
    )
    monkeypatch.setattr(golden_run, "_prepare", lambda project: project)
    return ran


class TestRunStages:
    def test_the_six_stages_run_in_the_order_of_the_plan(self, pipeline, tmp_path):
        summary = golden_run.run_stages(tmp_path)

        assert pipeline == [
            "audio-fix",
            "transcribe",
            "correct",
            "detect",
            "render",
            "report",
        ]
        assert summary["failed"] is None

    def test_every_warning_is_collected_under_its_stage(self, pipeline, tmp_path):
        summary = golden_run.run_stages(tmp_path)

        assert summary["warnings"][0] == "audio-fix: ⚠ audio-fix had a remark"
        assert len(summary["warnings"]) == len(golden_run.STAGES)

    def test_a_failing_stage_stops_the_run_and_is_recorded(
        self, pipeline, tmp_path, monkeypatch
    ):
        def _fail(loaded):
            msg = "1 segments start outside every detected speech region"
            raise AsrFailedError(msg)

        monkeypatch.setattr(
            golden_run,
            "STAGES",
            (("audio-fix", lambda _: FakeStage("audio-fix")), ("transcribe", _fail)),
        )

        summary = golden_run.run_stages(tmp_path)

        assert summary["failed"] == "transcribe"
        assert summary["stages"]["transcribe"]["error"] == "asr_failed"
        assert (
            "outside every detected speech" in summary["stages"]["transcribe"]["detail"]
        )

    def test_a_stage_raising_something_unmodelled_is_still_recorded(
        self, pipeline, tmp_path, monkeypatch
    ):
        def _fail(loaded):
            msg = "interval must lie within [0, 60]"
            raise ValueError(msg)

        monkeypatch.setattr(golden_run, "STAGES", (("render", _fail),))

        summary = golden_run.run_stages(tmp_path)

        assert summary["failed"] == "render"
        assert summary["stages"]["render"]["error"] == "ValueError"

    def test_the_archive_survives_a_stage_raising_something_unmodelled(
        self, pipeline, tmp_path, capsys, monkeypatch
    ):
        def _fail(loaded):
            msg = "the disk went away"
            raise OSError(msg)

        monkeypatch.setattr(golden_run, "STAGES", (("audio-fix", _fail),))
        project = tmp_path / "project"
        project.mkdir()
        (project / "vidprep.json").write_text("{}", encoding="utf-8")

        code = golden_run.main(
            ["--project", str(project), "--runs", str(tmp_path / "runs")]
        )

        assert code == EXIT_EXECUTION
        assert "saved:" in capsys.readouterr().out


class TestArchive:
    def test_the_statistics_and_the_warnings_are_copied(self, tmp_path):
        project = tmp_path / "project"
        stats = project / "report" / "stats.json"
        stats.parent.mkdir(parents=True)
        stats.write_text(json.dumps({"version": "1"}), encoding="utf-8")
        summary: dict[str, Any] = {
            "stages": {},
            "warnings": ["render: ⚠ one"],
            "failed": None,
        }

        target = golden_run.archive(project, tmp_path / "runs" / "2026-08-20", summary)

        assert json.loads((target / "stats.json").read_text()) == {"version": "1"}
        assert json.loads((target / "warnings.json").read_text()) == ["render: ⚠ one"]
        assert json.loads((target / "summary.json").read_text())["failed"] is None

    def test_a_run_without_statistics_still_archives_what_it_has(self, tmp_path):
        summary: dict[str, Any] = {
            "stages": {},
            "warnings": [],
            "failed": "transcribe",
        }

        target = golden_run.archive(tmp_path / "project", tmp_path / "run", summary)

        assert not (target / "stats.json").exists()
        assert (
            json.loads((target / "summary.json").read_text())["failed"] == "transcribe"
        )


def test_the_archive_is_outside_git():
    """REQ-041: the recording is private, and so is everything derived from it."""
    ignored = (golden_run.REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "fixtures/" in ignored.splitlines()
    assert golden_run.RUNS_DIR.is_relative_to(golden_run.REPO_ROOT / "fixtures")


class TestRunDirectory:
    def test_a_free_name_is_used_as_it_is(self, tmp_path):
        assert golden_run.run_directory("2026-08-20", tmp_path).name == "2026-08-20"

    def test_a_second_run_on_the_same_day_never_overwrites_the_first(self, tmp_path):
        (tmp_path / "2026-08-20").mkdir()

        assert golden_run.run_directory("2026-08-20", tmp_path).name == "2026-08-20-02"


class TestMain:
    def test_a_completed_run_exits_zero_and_reports_where_it_was_saved(
        self, pipeline, tmp_path, capsys
    ):
        project = tmp_path / "project"
        project.mkdir()
        (project / "vidprep.json").write_text("{}", encoding="utf-8")

        code = golden_run.main(
            ["--project", str(project), "--runs", str(tmp_path / "runs")]
        )

        assert code == EXIT_OK
        assert "saved:" in capsys.readouterr().out

    def test_a_missing_golden_sample_is_explained_rather_than_traced(self, tmp_path):
        with pytest.raises(SystemExit, match="golden sample is not at"):
            golden_run.main(
                [
                    "--project",
                    str(tmp_path / "project"),
                    "--source",
                    str(tmp_path / "nowhere.mp4"),
                ]
            )

    def test_a_run_that_stopped_early_exits_two(self, tmp_path, capsys, monkeypatch):
        def _fail(loaded):
            msg = "the recogniser refused the transcript"
            raise AsrFailedError(msg)

        monkeypatch.setattr(golden_run, "STAGES", (("transcribe", _fail),))
        monkeypatch.setattr(golden_run, "_prepare", lambda project: project)
        project = tmp_path / "project"
        project.mkdir()
        (project / "vidprep.json").write_text("{}", encoding="utf-8")

        code = golden_run.main(
            ["--project", str(project), "--runs", str(tmp_path / "runs")]
        )

        assert code == EXIT_EXECUTION
        assert "stopped at transcribe" in capsys.readouterr().out
