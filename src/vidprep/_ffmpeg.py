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

#: A full transcode of a long recording is legitimately slow, so the timeout is
#: only there to turn a hung subprocess into a reported failure.
DEFAULT_TIMEOUT_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Container specs read from the source material."""

    duration: float
    video: VideoStream
    audio: AudioStream


def _execute(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run *args* to completion, mapping every failure onto a vidprep error.

    Raises:
        UsageError: If the executable is not installed.
        ExecutionFailedError: If the command did not finish within *timeout*.
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
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        msg = f"{command[0]} was not found on PATH"
        raise UsageError(msg) from exc
    except subprocess.TimeoutExpired as exc:
        msg = f"{command[0]} did not finish within {timeout:g}s"
        raise ExecutionFailedError(msg) from exc
    if completed.returncode != 0:
        tail = completed.stderr.strip()[-STDERR_TAIL_CHARS:]
        msg = f"{command[0]} exited with {completed.returncode}: {tail}"
        raise FfmpegError(msg)
    return completed


def run(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run one of the external media tools and return its stdout.

    Used for the commands whose job is to produce a file (or, for ffprobe, to
    print one value); the sibling audio tools vidprep shells out to — such as
    DeepFilterNet — go through here too, so no stage module owns a subprocess.

    Args:
        args: Full argument vector, starting with the executable name.
        timeout: Seconds to wait before giving up on a hung command.

    Returns:
        The command's standard output.
    """
    return _execute(args, timeout).stdout


def run_analysis(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run an ffmpeg measurement pass and return its stderr.

    ffmpeg filters that report numbers — ``loudnorm``, ``silencedetect``,
    ``astats`` — print them to stderr along with the rest of the log, so an
    analysis pass is read from there rather than from stdout.
    """
    return _execute(args, timeout).stderr


def duration_command(path: Path) -> list[str]:
    """Return the ffprobe command line that prints the duration of *path*."""
    return [
        FFPROBE,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
    ]


def duration(path: Path) -> float:
    """Return the duration of *path* in seconds.

    Unlike :func:`probe` this accepts audio-only files, which is what the
    stages compare when they must prove they did not change a length.

    Raises:
        ExecutionFailedError: If ffprobe printed no duration vidprep can read.
    """
    output = run(duration_command(path)).strip()
    try:
        return float(output)
    except ValueError as exc:
        msg = f"could not read the duration of {path}: {output!r}"
        raise ExecutionFailedError(msg) from exc


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
