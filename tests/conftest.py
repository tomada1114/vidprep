"""Shared test fixtures."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from vidprep import _ffmpeg, cli, project
from vidprep.models import AudioStream, VideoStream

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SAMPLE_DURATION = 298.92
SAMPLE_VIDEO = VideoStream(codec="h264", width=1920, height=1080, fps="25/1")
SAMPLE_AUDIO = AudioStream(codec="aac", sample_rate=44100, channels=2)


@pytest.fixture
def source_video(tmp_path: Path) -> Path:
    """A stand-in for the source material; its bytes are never decoded."""
    path = tmp_path / "material" / "clip.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"pretend this is a video")
    return path


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ffprobe with the specs of the golden sample."""

    def _probe(source: Path) -> _ffmpeg.ProbeResult:
        assert source.is_file()
        return _ffmpeg.ProbeResult(
            duration=SAMPLE_DURATION, video=SAMPLE_VIDEO, audio=SAMPLE_AUDIO
        )

    monkeypatch.setattr(_ffmpeg, "probe", _probe)


@pytest.fixture
def project_dir(tmp_path: Path, source_video: Path, fake_probe: None) -> Path:
    """An initialised project directory."""
    return project.init_project(tmp_path / "work", source_video).root


@dataclass(frozen=True)
class CliResult:
    """Outcome of one CLI invocation."""

    exit_code: int
    stdout: str
    stderr: str


@pytest.fixture
def run_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., CliResult]:
    """Run the console entry point exactly as the installed script would."""

    def _run(*args: str) -> CliResult:
        monkeypatch.setattr(sys, "argv", ["vidprep", *args])
        exit_code = 0
        try:
            cli.main()
        except SystemExit as exc:
            exit_code = 0 if exc.code is None else int(exc.code)
        captured = capsys.readouterr()
        return CliResult(exit_code, captured.out, captured.err)

    return _run
