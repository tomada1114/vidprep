"""Tests for the ffmpeg / ffprobe wrapper."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from vidprep import _ffmpeg
from vidprep.errors import ExecutionFailedError, FfmpegError, UsageError

PROBE_OUTPUT: dict[str, Any] = {
    "format": {"duration": "298.920000"},
    "streams": [
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "44100",
            "channels": 2,
        },
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "25/1",
        },
    ],
}


class TestRun:
    def test_returns_stdout_on_success(self):
        assert _ffmpeg.run([sys.executable, "-c", "print('ok')"]).strip() == "ok"

    def test_non_zero_exit_carries_the_stderr_tail(self):
        script = "import sys; sys.stderr.write('boom: bad frame'); sys.exit(4)"

        with pytest.raises(FfmpegError, match="exited with 4: boom: bad frame"):
            _ffmpeg.run([sys.executable, "-c", script])

    def test_missing_executable_is_a_usage_error(self):
        with pytest.raises(UsageError, match="not found on PATH"):
            _ffmpeg.run(["vidprep-no-such-binary", "-version"])


class TestProbe:
    @pytest.fixture
    def probe_output(self, monkeypatch):
        def _set(payload: object) -> None:
            text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

            def _fake_run(args: list[str]) -> str:
                assert args[0] == "ffprobe"
                return text

            monkeypatch.setattr(_ffmpeg, "run", _fake_run)

        return _set

    def test_reads_duration_and_stream_specs(self, probe_output):
        probe_output(PROBE_OUTPUT)

        result = _ffmpeg.probe(Path("clip.mp4"))

        assert result.duration == 298.92
        assert result.video.fps == "25/1"
        assert result.audio.sample_rate == 44100

    def test_missing_video_stream_is_reported(self, probe_output):
        payload = {**PROBE_OUTPUT, "streams": PROBE_OUTPUT["streams"][:1]}
        probe_output(payload)

        with pytest.raises(ExecutionFailedError, match="has no video stream"):
            _ffmpeg.probe(Path("clip.mp4"))

    def test_unreadable_output_is_reported(self, probe_output):
        probe_output("not json at all")

        with pytest.raises(ExecutionFailedError, match="could not read ffprobe output"):
            _ffmpeg.probe(Path("clip.mp4"))

    def test_nonsense_stream_values_are_reported(self, probe_output):
        payload = json.loads(json.dumps(PROBE_OUTPUT))
        payload["streams"][1]["r_frame_rate"] = "twenty-five"
        probe_output(payload)

        with pytest.raises(ExecutionFailedError, match="could not read ffprobe output"):
            _ffmpeg.probe(Path("clip.mp4"))

    def test_command_names_the_source(self):
        command = _ffmpeg.probe_command(Path("/tmp/clip.mp4"))  # noqa: S108

        assert command[0] == "ffprobe"
        assert command[-1] == "/tmp/clip.mp4"  # noqa: S108
