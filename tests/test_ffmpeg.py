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

    def test_a_hung_command_is_given_up_on(self):
        script = "import time; time.sleep(30)"

        with pytest.raises(ExecutionFailedError, match=r"did not finish within 0\.2s"):
            _ffmpeg.run([sys.executable, "-c", script], timeout=0.2)


class TestRunAnalysis:
    def test_returns_the_stderr_the_filters_printed_to(self):
        script = "import sys; sys.stderr.write('RMS level dB: -58.3')"

        assert _ffmpeg.run_analysis([sys.executable, "-c", script]) == (
            "RMS level dB: -58.3"
        )


class TestDuration:
    @pytest.fixture
    def probe_duration(self, monkeypatch):
        def _set(text: str) -> None:
            monkeypatch.setattr(_ffmpeg, "run", lambda *_args, **_kwargs: text)

        return _set

    def test_reads_the_printed_seconds(self, probe_duration):
        probe_duration("298.920000\n")

        assert _ffmpeg.duration(Path("processed.wav")) == 298.92

    def test_unreadable_output_is_reported(self, probe_duration):
        probe_duration("N/A\n")

        with pytest.raises(ExecutionFailedError, match="could not read the duration"):
            _ffmpeg.duration(Path("processed.wav"))

    def test_command_names_the_file(self):
        command = _ffmpeg.duration_command(Path("/tmp/processed.wav"))  # noqa: S108

        assert command[0] == "ffprobe"
        assert command[-1] == "/tmp/processed.wav"  # noqa: S108


class TestStreamDuration:
    @pytest.fixture
    def probe_stream(self, monkeypatch):
        def _set(text: str) -> None:
            monkeypatch.setattr(_ffmpeg, "run", lambda *_args, **_kwargs: text)

        return _set

    def test_reads_the_printed_seconds(self, probe_stream):
        probe_stream("197.508000\n")

        assert _ffmpeg.stream_duration(Path("output.mp4"), "v") == 197.508

    def test_only_the_first_stream_of_the_kind_is_read(self, probe_stream):
        probe_stream("197.508000\n197.520000\n")

        assert _ffmpeg.stream_duration(Path("output.mp4"), "a") == 197.508

    def test_a_stream_without_a_duration_is_reported(self, probe_stream):
        probe_stream("N/A\n")

        with pytest.raises(
            ExecutionFailedError, match="could not read the v stream duration"
        ):
            _ffmpeg.stream_duration(Path("output.mp4"), "v")

    def test_command_selects_the_stream_and_names_the_file(self):
        command = _ffmpeg.stream_duration_command(Path("/tmp/output.mp4"), "a")  # noqa: S108

        assert command[command.index("-select_streams") + 1] == "a"
        assert command[-1] == "/tmp/output.mp4"  # noqa: S108


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
