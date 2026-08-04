"""Tests for vidprep.doctor."""

from __future__ import annotations

import importlib.util
import json
import sys
from typing import TYPE_CHECKING, Any

import pytest
import sudachipy

from vidprep import doctor
from vidprep.errors import EXIT_VALIDATION

if TYPE_CHECKING:
    from pathlib import Path

FFMPEG_BANNER = "ffmpeg version 7.1.1 Copyright (c) 2000-2025 the FFmpeg developers"
FFPROBE_BANNER = "ffprobe version 7.1.1 Copyright (c) 2007-2025 the FFmpeg developers"
SUBTITLES_FILTER = " ..C subtitles         V->V       Render text subtitles onto video."
OTHER_FILTERS = " ... afftdn            A->A       Denoise audio samples using FFT.\n"
MODEL_NAME = "ggml-large-v3-turbo.bin"
ALL_CHECKS = ("ffmpeg", "ffprobe", "auto_editor", "asr", "deepfilternet", "sudachipy")


def write_tool(directory: Path, name: str, script: str, *, mode: int = 0o755) -> Path:
    """Install a fake executable that answers the probes doctor sends it."""
    path = directory / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(mode)
    return path


def install_hanging(directory: Path, name: str) -> Path:
    """Install an executable that never answers in time.

    PATH is replaced wholesale by the ``bin_dir`` fixture, so the script cannot
    call out to ``sleep``; the interpreter behind its shebang is absolute.
    """
    path = directory / name
    path.write_text(f"#!{sys.executable}\nimport time\n\ntime.sleep(30)\n")
    path.chmod(0o755)
    return path


def install_unrunnable(directory: Path, name: str) -> Path:
    """Install an executable that the kernel refuses to start."""
    path = directory / name
    path.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def install_ffmpeg(directory: Path, *, has_libass: bool = True) -> None:
    filters = OTHER_FILTERS + (SUBTITLES_FILTER if has_libass else "")
    write_tool(
        directory,
        "ffmpeg",
        f'case "$1" in\n'
        f'  -version) echo "{FFMPEG_BANNER}" ;;\n'
        f"  -filters) printf '%s\\n' '{filters}' ;;\n"
        f"esac",
    )


def install_ffprobe(directory: Path) -> None:
    write_tool(directory, "ffprobe", f'echo "{FFPROBE_BANNER}"')


def install_auto_editor(directory: Path, version: str = "29.3.1") -> None:
    write_tool(directory, "auto-editor", f'echo "{version}"')


def install_whisper_cpp(
    directory: Path, models: Path, *, model: str = MODEL_NAME
) -> None:
    write_tool(directory, "whisper-cli", 'echo "usage: whisper-cli"')
    models.mkdir(parents=True, exist_ok=True)
    (models / model).write_bytes(b"pretend this is a ggml model")


class FakeTokenizer:
    """A Sudachi tokenizer that always finds one morpheme."""

    def tokenize(self, text: str) -> list[str]:
        return [text]


class FakeDictionary:
    """A Sudachi dictionary that loads."""

    def __init__(self, **kwargs: Any) -> None:
        self.flavour = kwargs.get("dict")

    def create(self) -> FakeTokenizer:
        return FakeTokenizer()


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory that is the whole of PATH for the test."""
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setenv("PATH", str(directory))
    return directory


@pytest.fixture
def model_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The directory doctor looks in for whisper.cpp models."""
    directory = tmp_path / "models"
    monkeypatch.setenv(doctor.WHISPER_MODEL_DIR_ENV, str(directory))
    return directory


@pytest.fixture
def healthy_env(
    bin_dir: Path, model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A machine on which every required dependency is present."""
    install_ffmpeg(bin_dir)
    install_ffprobe(bin_dir)
    install_auto_editor(bin_dir)
    install_whisper_cpp(bin_dir, model_dir)
    monkeypatch.setattr(sudachipy, "Dictionary", FakeDictionary)
    return bin_dir


class TestFfmpeg:
    def test_ffmpeg_with_the_subtitles_filter_reports_libass(self, bin_dir):
        install_ffmpeg(bin_dir, has_libass=True)

        check = doctor.check_ffmpeg()

        assert check == {
            "ok": True,
            "version": "7.1.1",
            "libass": True,
            "path": str(bin_dir / "ffmpeg"),
        }

    def test_ffmpeg_without_the_subtitles_filter_is_not_usable(self, bin_dir):
        install_ffmpeg(bin_dir, has_libass=False)

        check = doctor.check_ffmpeg()

        assert check["ok"] is False
        assert check["libass"] is False
        assert "libass" in check["error"]

    def test_ffmpeg_absent_from_path_is_reported_as_not_found(self, bin_dir):
        check = doctor.check_ffmpeg()

        assert check == {"ok": False, "error": "ffmpeg not found in PATH"}

    def test_ffmpeg_that_cannot_be_executed_is_reported_not_raised(self, bin_dir):
        install_unrunnable(bin_dir, "ffmpeg")

        check = doctor.check_ffmpeg()

        assert check["ok"] is False
        assert "could not run" in check["error"]

    def test_a_non_executable_ffmpeg_does_not_count_as_installed(self, bin_dir):
        write_tool(bin_dir, "ffmpeg", "exit 0", mode=0o644)

        assert doctor.check_ffmpeg() == {
            "ok": False,
            "error": "ffmpeg not found in PATH",
        }

    def test_ffmpeg_exiting_non_zero_keeps_its_reason(self, bin_dir):
        write_tool(bin_dir, "ffmpeg", 'echo "broken install" >&2\nexit 69')

        check = doctor.check_ffmpeg()

        assert check["ok"] is False
        assert check["error"] == "exited with 69: broken install"


class TestFfprobe:
    def test_ffprobe_reports_its_version(self, bin_dir):
        install_ffprobe(bin_dir)

        check = doctor.check_ffprobe()

        assert check["ok"] is True
        assert check["version"] == "7.1.1"

    def test_ffprobe_absent_from_path_is_reported_as_not_found(self, bin_dir):
        assert doctor.check_ffprobe() == {
            "ok": False,
            "error": "ffprobe not found in PATH",
        }

    def test_ffprobe_exiting_non_zero_is_not_ok(self, bin_dir):
        write_tool(bin_dir, "ffprobe", "exit 2")

        check = doctor.check_ffprobe()

        assert check["ok"] is False
        assert check["error"] == "exited with 2: no output"


class TestAutoEditor:
    def test_recent_auto_editor_supports_export_v3(self, bin_dir):
        install_auto_editor(bin_dir, "29.3.1")

        check = doctor.check_auto_editor()

        assert check["ok"] is True
        assert check["export_v3"] is True
        assert check["version"] == "29.3.1"

    @pytest.mark.parametrize(
        "version",
        [
            pytest.param("27.1.0", id="before-the-v3-export-name"),
            pytest.param("24w19a", id="calendar-versioned-nightly"),
        ],
    )
    def test_older_auto_editor_cannot_export_v3(self, bin_dir, version):
        install_auto_editor(bin_dir, version)

        check = doctor.check_auto_editor()

        assert check["ok"] is False
        assert check["export_v3"] is False
        assert f">= {doctor.MIN_AUTO_EDITOR_MAJOR}" in check["error"]

    def test_unreadable_version_banner_is_not_trusted(self, bin_dir):
        install_auto_editor(bin_dir, "unreleased")

        check = doctor.check_auto_editor()

        assert check["ok"] is False
        assert check["version"] is None

    def test_auto_editor_absent_from_path_is_reported_as_not_found(self, bin_dir):
        assert doctor.check_auto_editor()["error"] == "auto-editor not found in PATH"

    def test_auto_editor_that_hangs_times_out(self, bin_dir, monkeypatch):
        monkeypatch.setattr(doctor, "COMMAND_TIMEOUT_SECONDS", 0.5)
        install_hanging(bin_dir, "auto-editor")

        check = doctor.check_auto_editor()

        assert check["ok"] is False
        assert "timed out" in check["error"]


class TestAsr:
    def test_whisper_cpp_needs_a_binary_and_a_model(self, bin_dir, model_dir):
        install_whisper_cpp(bin_dir, model_dir)

        check = doctor.check_whisper_cpp()

        assert check["ok"] is True
        assert check["models"] == [MODEL_NAME]
        assert check["model_dir"] == str(model_dir)

    def test_whisper_cpp_without_a_model_is_not_ok(self, bin_dir, model_dir):
        write_tool(bin_dir, "whisper-cli", "exit 0")

        check = doctor.check_whisper_cpp()

        assert check["ok"] is False
        assert check["models"] == []
        assert str(model_dir) in check["error"]

    def test_whisper_cpp_model_without_a_binary_is_not_ok(self, bin_dir, model_dir):
        model_dir.mkdir()
        (model_dir / MODEL_NAME).write_bytes(b"model")

        check = doctor.check_whisper_cpp()

        assert check["ok"] is False
        assert check["path"] is None
        assert "found in PATH" in check["error"]

    def test_the_model_directory_can_be_overridden(self, tmp_path, monkeypatch):
        monkeypatch.setenv(doctor.WHISPER_MODEL_DIR_ENV, str(tmp_path / "elsewhere"))

        assert doctor.whisper_model_dir() == tmp_path / "elsewhere"

    def test_the_model_directory_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.delenv(doctor.WHISPER_MODEL_DIR_ENV, raising=False)

        assert doctor.whisper_model_dir() == doctor.DEFAULT_WHISPER_MODEL_DIR

    def test_mlx_whisper_alone_satisfies_the_asr_requirement(self, bin_dir, model_dir):
        # Arrange: whisper.cpp is absent, mlx-whisper answers for the backend.
        install_mlx = {"ok": True, "version": "0.4.3"}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(doctor, "check_mlx_whisper", lambda: install_mlx)

            check = doctor.check_asr()

        assert check["ok"] is True
        assert check["whisper_cpp"]["ok"] is False
        assert check["mlx_whisper"] == install_mlx

    def test_whisper_cpp_alone_satisfies_the_asr_requirement(
        self, bin_dir, model_dir, monkeypatch
    ):
        install_whisper_cpp(bin_dir, model_dir)
        monkeypatch.setattr(
            doctor, "check_mlx_whisper", lambda: {"ok": False, "error": "absent"}
        )

        check = doctor.check_asr()

        assert check["ok"] is True

    def test_no_backend_at_all_fails_the_asr_check(
        self, bin_dir, model_dir, monkeypatch
    ):
        monkeypatch.setattr(
            doctor, "check_mlx_whisper", lambda: {"ok": False, "error": "absent"}
        )

        check = doctor.check_asr()

        assert check["ok"] is False
        assert check["error"] == "no ASR backend available (whisper.cpp or mlx-whisper)"

    def test_an_uninstalled_mlx_whisper_is_reported(self, monkeypatch):
        monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)

        assert doctor.check_mlx_whisper() == {
            "ok": False,
            "error": "mlx_whisper is not installed",
        }

    def test_an_unimportable_mlx_whisper_is_reported(self, monkeypatch):
        def explode(name: str) -> None:
            raise ImportError(name)

        monkeypatch.setattr(importlib.util, "find_spec", explode)

        check = doctor.check_mlx_whisper()

        assert check["ok"] is False
        assert "not importable" in check["error"]


class TestDeepFilterNet:
    def test_a_missing_deepfilternet_records_the_fallback(self, bin_dir):
        check = doctor.check_deepfilternet()

        assert check["ok"] is False
        assert check["optional"] is True
        assert check["fallback"] == "afftdn"

    def test_an_installed_deepfilternet_is_reported_with_its_path(self, bin_dir):
        write_tool(bin_dir, "deep-filter", "exit 0")

        check = doctor.check_deepfilternet()

        assert check["ok"] is True
        assert check["path"] == str(bin_dir / "deep-filter")


class TestSudachiPy:
    def test_the_installed_dictionary_analyses_japanese(self):
        """The dev environment ships sudachidict-core; the probe must load it."""
        assert doctor.check_sudachipy() == {"ok": True, "dict": "core"}

    def test_a_loadable_dictionary_reports_its_flavour(self, monkeypatch):
        monkeypatch.setattr(sudachipy, "Dictionary", FakeDictionary)

        assert doctor.check_sudachipy() == {"ok": True, "dict": "core"}

    def test_a_dictionary_that_cannot_be_loaded_is_not_ok(self, monkeypatch):
        def explode(**kwargs: Any) -> FakeDictionary:
            message = f"no dictionary {kwargs['dict']}"
            raise RuntimeError(message)

        monkeypatch.setattr(sudachipy, "Dictionary", explode)

        check = doctor.check_sudachipy()

        assert check["ok"] is False
        for flavour in doctor.SUDACHI_DICTS:
            assert f"{flavour}: no dictionary {flavour}" in check["error"]

    def test_a_dictionary_that_analyses_nothing_is_not_ok(self, monkeypatch):
        class Silent(FakeDictionary):
            def create(self) -> Any:
                class Empty:
                    def tokenize(self, text: str) -> list[str]:
                        return []

                return Empty()

        monkeypatch.setattr(sudachipy, "Dictionary", Silent)

        check = doctor.check_sudachipy()

        assert check["ok"] is False
        assert "analysed nothing" in check["error"]


class TestReport:
    def test_a_complete_environment_is_ok(self, healthy_env, monkeypatch):
        monkeypatch.setattr(
            doctor, "check_mlx_whisper", lambda: {"ok": False, "error": "absent"}
        )
        write_tool(healthy_env, "deep-filter", "exit 0")

        report = doctor.diagnose()

        assert report.status == "ok"
        assert report.missing == ()
        assert tuple(report.checks) == ALL_CHECKS

    def test_only_the_optional_dependency_missing_warns(self, healthy_env):
        report = doctor.diagnose()

        assert report.status == "warn"
        assert report.missing == ()
        assert report.checks["deepfilternet"]["fallback"] == "afftdn"

    def test_a_missing_required_dependency_makes_the_report_ng(self, healthy_env):
        (healthy_env / "ffprobe").unlink()

        report = doctor.diagnose()

        assert report.status == "ng"
        assert report.missing == ("ffprobe",)

    def test_every_category_is_reported_even_when_all_are_broken(
        self, bin_dir, monkeypatch
    ):
        def no_dictionary(**kwargs: Any) -> FakeDictionary:
            message = f"no dictionary {kwargs['dict']}"
            raise RuntimeError(message)

        install_unrunnable(bin_dir, "ffmpeg")
        monkeypatch.setattr(sudachipy, "Dictionary", no_dictionary)
        # mlx-whisper is installed in the `asr` group, so an environment with
        # no ASR backend at all has to be arranged rather than assumed.
        monkeypatch.setattr(
            doctor, "check_mlx_whisper", lambda: {"ok": False, "error": "absent"}
        )

        report = doctor.diagnose()

        assert tuple(report.checks) == ALL_CHECKS
        assert report.missing == doctor.REQUIRED_CHECKS

    def test_the_report_serialises_to_the_documented_shape(self, healthy_env):
        payload = doctor.diagnose().to_dict()

        assert json.loads(json.dumps(payload)) == payload
        assert set(payload) == {"status", "checks", "missing"}
        assert payload["checks"]["ffmpeg"]["libass"] is True


class TestSummaryLines:
    def test_a_healthy_environment_reads_as_a_list_of_ticks(self, healthy_env):
        lines = doctor.summary_lines(doctor.diagnose())

        assert lines[0] == "✔ ffmpeg 7.1.1 (libass: yes)"
        assert lines[2] == "✔ auto-editor 29.3.1 (--export v3: yes)"
        assert f"whisper.cpp ({MODEL_NAME})" in lines[3]
        assert lines[4].startswith("⚠ deepfilternet:")
        assert lines[5] == "✔ sudachipy: core dictionary OK"

    def test_a_missing_dependency_is_followed_by_what_to_do_about_it(self, healthy_env):
        (healthy_env / "auto-editor").unlink()

        lines = doctor.summary_lines(doctor.diagnose())

        assert "✖ auto_editor: auto-editor not found in PATH → " in lines[2]
        assert "uv tool install auto-editor" in lines[2]
        assert lines[-1] == "✖ missing required dependencies: auto_editor"

    def test_a_working_mlx_whisper_is_named(self, healthy_env, monkeypatch):
        monkeypatch.setattr(
            doctor, "check_mlx_whisper", lambda: {"ok": True, "version": "0.4.3"}
        )

        lines = doctor.summary_lines(doctor.diagnose())

        assert lines[3].endswith("mlx-whisper")


class TestCli:
    def test_doctor_prints_only_json_on_stdout(self, run_cli, healthy_env):
        result = run_cli("doctor", "--json")

        payload = json.loads(result.stdout)
        assert set(payload["checks"]) == set(ALL_CHECKS)
        assert result.exit_code == 0
        assert "✔ ffmpeg" in result.stderr

    def test_doctor_reports_the_environment_on_stdout_by_default(
        self, run_cli, healthy_env
    ):
        result = run_cli("doctor")

        assert result.exit_code == 0
        assert "✔ ffmpeg 7.1.1 (libass: yes)" in result.stdout

    def test_doctor_exits_three_when_a_required_dependency_is_missing(
        self, run_cli, healthy_env
    ):
        (healthy_env / "ffprobe").unlink()

        result = run_cli("doctor", "--json")

        payload = json.loads(result.stdout)
        assert result.exit_code == EXIT_VALIDATION
        assert payload["status"] == "ng"
        assert payload["missing"] == ["ffprobe"]

    def test_doctor_exits_zero_when_only_deepfilternet_is_missing(
        self, run_cli, healthy_env
    ):
        result = run_cli("doctor", "--json")

        payload = json.loads(result.stdout)
        assert result.exit_code == 0
        assert payload["status"] == "warn"
        assert payload["checks"]["deepfilternet"]["fallback"] == "afftdn"

    def test_doctor_runs_outside_a_project(self, run_cli, healthy_env, tmp_path):
        result = run_cli("doctor", "-p", str(tmp_path))

        assert result.exit_code == 0
        assert "is not a vidprep project" not in result.stderr

    def test_doctor_writes_nothing(self, run_cli, healthy_env, tmp_path, monkeypatch):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.chdir(workspace)

        run_cli("doctor")

        assert list(workspace.iterdir()) == []
