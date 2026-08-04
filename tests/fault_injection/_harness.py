"""A throwaway project, and stand-ins for every tool the cases would need.

The fault injections are about the checks, not about ffmpeg, so the material is
a miniature written straight to disk — three spoken segments, two silences cut
out of the gaps between them — and the encoder, the prober and the recogniser
are replaced by :class:`FakeMedia`, which answers from the commands it is
handed. Nothing here decodes or encodes anything, so the cases run anywhere.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from vidprep import _ffmpeg, doctor
from vidprep import project as project_module
from vidprep.models import Manifest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from vidprep.project import Project

DURATION = 60.0
FPS = "25/1"
SOURCE_SHA256 = "a" * 64

#: ``(id, start, end, text)``. Three sentences with silence between them.
SEGMENTS = (
    ("s0001", 1.0, 4.0, "こんにちは今日はクロードコードの話をします"),
    ("s0002", 10.0, 14.0, "コマンドを実行すると作業状況が復元されます"),
    ("s0003", 20.0, 24.0, "それでは実際にやってみましょう"),
)

#: What the VAD reported, one region per segment.
SPEECH = ((1.0, 4.0), (10.0, 14.0), (20.0, 24.0))

#: ``(id, start, end, reason, status)``. Both cuts sit in a silence, and both
#: land on the 25fps frame grid — otherwise ``render`` snaps them inwards and a
#: timeline built here from the same numbers would not be the one it used.
CUTS = (
    ("c0001", 4.4, 9.6, "silence", "approved"),
    ("c0002", 14.4, 19.6, "silence", "approved"),
)

#: The loudness a correctly normalised render measures.
NORMALISED_LUFS = "-14.08"

#: The loudness of the golden sample before ``audio-fix`` touches it
#: (verification-plan.md §2), which is what case 1 renders with.
UNPROCESSED_LUFS = "-22.24"

VIDEO_TRIM = re.compile(r"\[0:v\]trim=start=([\d.]+):end=([\d.]+)")

_WHISPER_LOG = (
    "whisper_vad: VAD is enabled, processing speech segments only\n"
    "whisper_vad: detected 3 speech segments\n"
)


def refusal(action: Callable[[], object], expected: type[Exception], what: str) -> str:
    """Run *action* and return the message the check refused it with.

    The inversion every case is built on lives here: being refused is the
    passing outcome, so *not* raising is the assertion failure. The message is
    handed back rather than asserted on in place, so each case can name what it
    expects to read in it — a check that fires for the wrong reason is not the
    check being proved.

    Args:
        action: The call that must be refused.
        expected: The exception the check refuses with.
        what: How to describe the broken input if it is accepted.

    Returns:
        The refusal message.

    Raises:
        AssertionError: If *action* returned instead of raising.
    """
    try:
        action()
    except expected as caught:
        return str(caught)
    msg = f"{what} was accepted; the check meant to catch it is not enforced"
    raise AssertionError(msg)


@contextlib.contextmanager
def workspace() -> Iterator[Path]:
    """Yield a directory the case may fill and that is removed after it."""
    with tempfile.TemporaryDirectory(prefix="vidprep-fault-") as directory:
        yield Path(directory)


def _manifest(source: Path) -> Manifest:
    """Return the manifest ``init`` would have written for *source*."""
    return Manifest.model_validate(
        {
            "version": "1",
            "created_at": datetime.now(tz=UTC).astimezone().isoformat(),
            "source": {
                "path": str(source),
                "sha256": project_module.sha256_file(source),
                "duration": DURATION,
                "video": {
                    "codec": "h264",
                    "width": 1920,
                    "height": 1080,
                    "fps": FPS,
                },
                "audio": {"codec": "aac", "sample_rate": 44100, "channels": 2},
            },
        }
    )


def _write(path: Path, payload: object) -> None:
    """Write *payload* as JSON, creating the directory it lives in."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def build_project(
    root: Path, cuts: Sequence[tuple[str, float, float, str, str]] = CUTS
) -> Project:
    """Write a project directory holding every artifact a render needs.

    Args:
        root: Directory to create; it must not exist yet.
        cuts: ``(id, start, end, reason, status)`` for ``cuts.json``.

    Returns:
        The loaded project, exactly as the CLI would have loaded it.
    """
    root.mkdir(parents=True)
    source = root / "material.mp4"
    source.write_bytes(b"pretend this is the golden sample")
    (root / "audio").mkdir()
    (root / "audio" / "processed.wav").write_bytes(b"pretend this is 16kHz PCM")
    project_module.write_json(root / project_module.MANIFEST_NAME, _manifest(source))
    project_module.write_json(
        root / project_module.PROFILE_NAME, project_module.default_profile()
    )
    _write(
        root / "transcript.json",
        {
            "version": "1",
            "audio_source": "audio/processed.wav",
            "asr": {
                "backend": "whisper.cpp",
                "model": "large-v3-turbo",
                "vad": "silero-v5",
            },
            "segments": [
                {"id": identifier, "start": start, "end": end, "text": text}
                for identifier, start, end, text in SEGMENTS
            ],
        },
    )
    _write(
        root / "report" / "vad.json",
        {
            "version": "1",
            "backend": "silero-v5",
            "segments": [{"start": start, "end": end} for start, end in SPEECH],
        },
    )
    write_cuts(root, cuts)
    return project_module.load_project(root)


def write_cuts(root: Path, cuts: Sequence[tuple[str, float, float, str, str]]) -> None:
    """Write ``cuts.json`` without validating it, so a case may break it."""
    _write(
        root / "cuts.json",
        {
            "version": "1",
            "cuts": [
                {
                    "id": identifier,
                    "start": start,
                    "end": end,
                    "reason": reason,
                    "status": status,
                }
                for identifier, start, end, reason, status in cuts
            ],
        },
    )


def transcript_text(
    segments: Sequence[tuple[str, float, float, str]] = SEGMENTS,
) -> str:
    """Return everything the transcript says, as one string."""
    return "".join(text for _, _, _, text in segments)


@dataclass
class FakeMedia:
    """ffmpeg, ffprobe and whisper.cpp, answering from their own arguments.

    Attributes:
        integrated: What the loudness analysis reports for the render.
        reasr: What a second transcription pass returns; ``None`` means "what
            the kept segments say", which is the answer a faultless render
            would produce.
    """

    integrated: str = NORMALISED_LUFS
    reasr: str | None = None
    commands: list[list[str]] = field(default_factory=list)
    rendered: float = 0.0

    def run(self, args: Sequence[str], timeout: float = 0.0) -> str:
        """Stand in for one external command, writing what it would write."""
        command = list(args)
        self.commands.append(command)
        if Path(command[0]).name.startswith("whisper"):
            return self._whisper(command)
        if command[0] == _ffmpeg.FFMPEG:
            return self._ffmpeg(command)
        if "format=duration" in command:
            return f"{self.rendered:.6f}\n"
        return f"{self.rendered:.6f}\n"

    def run_analysis(self, args: Sequence[str], timeout: float = 0.0) -> str:
        """Answer the loudness measurement and the recogniser, which log."""
        command = list(args)
        if Path(command[0]).name.startswith("whisper"):
            self.commands.append(command)
            return self._whisper(command)
        self.commands.append(command)
        report = {
            "input_i": self.integrated,
            "input_tp": "-1.29",
            "input_lra": "7.4",
            "input_thresh": "-24.30",
            "target_offset": "0.10",
        }
        return f"[Parsed_loudnorm_1 @ 0x1] \n{json.dumps(report, indent=1)}\n"

    def _ffmpeg(self, command: list[str]) -> str:
        """Write what an encode or an extraction would have written."""
        if "-filter_complex" in command:
            graph = command[command.index("-filter_complex") + 1]
            self.rendered = sum(
                float(end) - float(start) for start, end in VIDEO_TRIM.findall(graph)
            )
            Path(command[-1]).write_bytes(b"rendered mp4")
            return ""
        Path(command[-1]).write_bytes(b"extracted wav")
        return ""

    def _whisper(self, command: list[str]) -> str:
        """Write the ``-oj`` transcript of the second pass and log its VAD."""
        if "--version" in command:
            return "whisper.cpp version: 1.9.1\n"
        stem = Path(command[command.index("-of") + 1])
        text = transcript_text() if self.reasr is None else self.reasr
        stem.with_suffix(".json").write_text(
            json.dumps(
                {"transcription": [{"offsets": {"from": 0, "to": 1000}, "text": text}]}
            ),
            encoding="utf-8",
        )
        return _WHISPER_LOG

    @contextlib.contextmanager
    def installed(self, root: Path) -> Iterator[FakeMedia]:
        """Replace the media tools, and the recogniser's binary and weights.

        The whisper.cpp binary is a real (empty, executable) file on a ``PATH``
        of its own rather than a patched lookup, because that is what
        :func:`vidprep._asr.resolve` searches for.
        """
        binaries = root / "bin"
        models = root / "models"
        binaries.mkdir(parents=True, exist_ok=True)
        models.mkdir(parents=True, exist_ok=True)
        whisper = binaries / "whisper-cli"
        whisper.write_text("#!/bin/sh\n", encoding="utf-8")
        whisper.chmod(whisper.stat().st_mode | stat.S_IXUSR)
        (models / "ggml-large-v3-turbo.bin").write_bytes(b"weights")
        (models / "ggml-silero-v5.1.2.bin").write_bytes(b"silero")
        environment = {
            "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            doctor.WHISPER_MODEL_DIR_ENV: str(models),
        }
        with (
            patch.dict(os.environ, environment),
            patch.object(_ffmpeg, "run", self.run),
            patch.object(_ffmpeg, "run_analysis", self.run_analysis),
        ):
            yield self
