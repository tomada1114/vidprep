"""Tests for the CLI skeleton: subcommands, common flags and exit codes."""

from __future__ import annotations

import json
import re

import pytest

from vidprep import _ffmpeg
from vidprep import project as project_module
from vidprep.errors import (
    EXIT_EXECUTION,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    FfmpegError,
)

SUBCOMMANDS = (
    "init",
    "doctor",
    "audio-fix",
    "transcribe",
    "correct",
    "detect",
    "render",
    "report",
)
PENDING_SUBCOMMANDS = SUBCOMMANDS[2:]
OVERLAPPING_CUTS = {
    "version": "1",
    "cuts": [
        {
            "id": "c0001",
            "start": 10.500,
            "end": 13.240,
            "reason": "silence",
            "confidence": 0.95,
            "status": "approved",
            "note": None,
        },
        {
            "id": "c0002",
            "start": 12.000,
            "end": 15.000,
            "reason": "silence",
            "confidence": 0.90,
            "status": "approved",
            "note": None,
        },
    ],
}


class TestInterface:
    """REQ-020 / REQ-021: eight subcommands, three common flags on each."""

    def test_help_lists_every_subcommand(self, run_cli):
        result = run_cli("--help")

        assert result.exit_code == EXIT_OK
        for name in SUBCOMMANDS:
            assert name in result.stdout

    @pytest.mark.parametrize("name", SUBCOMMANDS)
    def test_subcommand_accepts_the_common_flags(self, run_cli, name):
        result = run_cli(name, "--help")

        assert result.exit_code == EXIT_OK
        for flag in ("--json", "--dry-run"):
            assert flag in result.stdout
        # "-p" alone would also match inside "--project"
        assert re.search(r"--project\s+-p\b", result.stdout)

    def test_unknown_option_is_a_usage_error(self, run_cli):
        result = run_cli("detect", "--nope")

        assert result.exit_code == EXIT_USAGE
        assert "No such option" in result.stderr

    def test_unknown_subcommand_is_a_usage_error(self, run_cli):
        assert run_cli("transcode").exit_code == EXIT_USAGE


@pytest.mark.usefixtures("fake_probe")
class TestInit:
    def test_creates_the_project(self, run_cli, tmp_path, source_video):
        target = tmp_path / "work" / "talk01"

        result = run_cli("init", str(target), "--source", str(source_video))

        assert result.exit_code == EXIT_OK
        assert (target / "vidprep.json").is_file()
        assert (target / "profile.json").is_file()

    def test_project_flag_selects_the_target(self, run_cli, tmp_path, source_video):
        target = tmp_path / "work"

        result = run_cli("init", "-p", str(target), "--source", str(source_video))

        assert result.exit_code == EXIT_OK
        assert (target / "vidprep.json").is_file()

    def test_json_output_is_parsable_on_its_own(self, run_cli, tmp_path, source_video):
        target = tmp_path / "work"

        result = run_cli("init", str(target), "--source", str(source_video), "--json")

        payload = json.loads(result.stdout)
        assert payload["manifest"]["source"]["duration"] == 298.92
        assert payload["manifest"]["source"]["video"]["fps"] == "25/1"
        assert "✔" in result.stderr

    def test_dry_run_writes_nothing_and_shows_the_command(
        self, run_cli, tmp_path, source_video
    ):
        target = tmp_path / "work"

        result = run_cli(
            "init", str(target), "--source", str(source_video), "--dry-run"
        )

        assert result.exit_code == EXIT_OK
        assert not target.exists()
        assert "ffprobe" in result.stdout

    def test_missing_source_option_is_a_usage_error(self, run_cli, tmp_path):
        result = run_cli("init", str(tmp_path / "work"))

        assert result.exit_code == EXIT_USAGE
        assert "--source" in result.stderr

    def test_existing_non_empty_directory_is_a_usage_error(
        self, run_cli, tmp_path, source_video
    ):
        target = tmp_path / "work"
        target.mkdir()
        (target / "notes.md").write_text("keep me")

        result = run_cli("init", str(target), "--source", str(source_video))

        assert result.exit_code == EXIT_USAGE

    def test_target_that_is_a_file_is_a_usage_error(
        self, run_cli, tmp_path, source_video
    ):
        target = tmp_path / "work"
        target.write_text("a file, not a directory")

        result = run_cli("init", str(target), "--source", str(source_video))

        assert result.exit_code == EXIT_USAGE
        assert "not an empty directory" in result.stderr

    def test_ffprobe_failure_exits_with_the_execution_code(
        self, run_cli, tmp_path, source_video, monkeypatch
    ):
        def _fail(source):
            msg = "ffprobe exited with 1: Invalid data found when processing input"
            raise FfmpegError(msg)

        monkeypatch.setattr(_ffmpeg, "probe", _fail)

        result = run_cli("init", str(tmp_path / "work"), "--source", str(source_video))

        assert result.exit_code == EXIT_EXECUTION
        assert "Invalid data found" in result.stderr
        assert not (tmp_path / "work").exists()


class TestPendingStages:
    """Stages that are not built yet still guard the project they run in."""

    @pytest.mark.parametrize("name", PENDING_SUBCOMMANDS)
    def test_reports_that_it_is_not_implemented(self, run_cli, project_dir, name):
        result = run_cli(name, "-p", str(project_dir))

        assert result.exit_code == EXIT_USAGE
        assert "not implemented yet" in result.stderr

    def test_outside_a_project_the_directory_is_reported(self, run_cli, tmp_path):
        result = run_cli("detect", "-p", str(tmp_path))

        assert result.exit_code == EXIT_USAGE
        assert "is not a vidprep project" in result.stderr

    def test_stale_upstream_stage_only_warns(self, run_cli, project_dir):
        loaded = project_module.load_project(project_dir)
        project_module.record_stage(loaded, "audio_fix")
        changed = loaded.profile
        changed.audio.highpass_hz = 120
        project_module.write_json(project_dir / "profile.json", changed)

        result = run_cli("transcribe", "-p", str(project_dir))

        assert "may be stale" in result.stdout
        assert result.exit_code == EXIT_USAGE  # only because the stage is a skeleton


class TestVerificationFailures:
    """REQ-012 / REQ-024: verification problems exit 3, with or without --json."""

    def test_overlapping_approved_cuts_stop_render(self, run_cli, project_dir):
        (project_dir / "cuts.json").write_text(json.dumps(OVERLAPPING_CUTS))

        result = run_cli("render", "-p", str(project_dir), "--json")

        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout) == {
            "error": "schema_invalid",
            "detail": (
                "cuts.json: approved cuts overlap: "
                "c0001(10.500-13.240) x c0002(12.000-15.000)"
            ),
        }

    def test_replaced_source_stops_the_stage(self, run_cli, project_dir, source_video):
        source_video.write_bytes(b"a different recording entirely")

        result = run_cli("detect", "-p", str(project_dir))

        assert result.exit_code == EXIT_VALIDATION
        assert "sha256 mismatch" in result.stderr

    def test_undecodable_artifact_stops_the_stage(self, run_cli, project_dir):
        (project_dir / "cuts.json").write_bytes(b'{"version":"1","cuts":[],"n":"\xe3"}')

        result = run_cli("render", "-p", str(project_dir))

        assert result.exit_code == EXIT_VALIDATION
        assert "cuts.json" in result.stderr

    def test_replaced_source_reports_json_when_asked(
        self, run_cli, project_dir, source_video
    ):
        source_video.write_bytes(b"a different recording entirely")

        result = run_cli("detect", "-p", str(project_dir), "--json")

        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout)["error"] == "hash_mismatch"
