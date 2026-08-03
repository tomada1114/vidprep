"""The only module allowed to spawn ffmpeg / ffprobe subprocesses.

Centralising argument assembly and exit-code handling keeps every other module
free of ``subprocess`` (design.md §2.2) and gives ``--dry-run`` a single place
to ask "which external command would you run?" without running it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import ExecutionFailedError, FfmpegError, UsageError
from .models import AudioStream, VideoStream

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"
STDERR_TAIL_CHARS = 2000


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Container specs read from the source material."""

    duration: float
    video: VideoStream
    audio: AudioStream


def run(args: Sequence[str]) -> str:
    """Run an ffmpeg-family command and return its stdout.

    Args:
        args: Full argument vector, starting with the executable name.

    Returns:
        The command's standard output.

    Raises:
        UsageError: If the executable is not installed.
        FfmpegError: If the command exits non-zero; the message carries the
            tail of stderr so the failure is diagnosable from the log alone.
    """
    command = list(args)
    try:
        # Fixed argument vector, shell=False: no interpolation into a shell.
        completed = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        msg = f"{command[0]} was not found on PATH"
        raise UsageError(msg) from exc
    if completed.returncode != 0:
        tail = completed.stderr.strip()[-STDERR_TAIL_CHARS:]
        msg = f"{command[0]} exited with {completed.returncode}: {tail}"
        raise FfmpegError(msg)
    return completed.stdout


def probe_command(source: Path) -> list[str]:
    """Return the ffprobe command line used to inspect *source*."""
    return [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source),
    ]


def _stream(streams: list[dict[str, Any]], kind: str, source: Path) -> dict[str, Any]:
    """Return the first stream of *kind*.

    Raises:
        ExecutionFailedError: If the container has no such stream.
    """
    for stream in streams:
        if stream.get("codec_type") == kind:
            return stream
    msg = f"{source} has no {kind} stream"
    raise ExecutionFailedError(msg)


def probe(source: Path) -> ProbeResult:
    """Read duration and stream specs from *source* with ffprobe.

    Raises:
        ExecutionFailedError: If ffprobe returns output vidprep cannot read.
    """
    output = run(probe_command(source))
    try:
        payload = json.loads(output)
        streams = payload["streams"]
        duration = float(payload["format"]["duration"])
        video = _stream(streams, "video", source)
        audio = _stream(streams, "audio", source)
        return ProbeResult(
            duration=duration,
            video=VideoStream(
                codec=video["codec_name"],
                width=int(video["width"]),
                height=int(video["height"]),
                fps=video["r_frame_rate"],
            ),
            audio=AudioStream(
                codec=audio["codec_name"],
                sample_rate=int(audio["sample_rate"]),
                channels=int(audio["channels"]),
            ),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        msg = f"could not read ffprobe output for {source}: {exc}"
        raise ExecutionFailedError(msg) from exc
