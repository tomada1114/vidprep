"""Verification that reads the finished render back (verification-plan.md §8.1).

Two checks live here, both of them "look at what was actually written" rather
than "trust what the stage reported".

``render --verify-asr`` transcribes ``out/output.mp4`` a second time with the
very settings ``transcript.json`` records, and compares it with what the kept
segments say it should contain. Words lost at a cut boundary — the failure this
pipeline is most likely to produce and least likely to notice — show up as text
the second pass never heard, within two seconds of a boundary.

:func:`missing_subtitle_entries` reads ``out/subtitles.srt`` back and checks
every entry the mapping produced is really in the file (verification-plan.md §9).

The check is a gate: one flag fails the run. It began as advisory, on the
assumption that a recogniser run twice does not return the same string twice,
and was promoted once that assumption had been measured — three comparisons of
one render returned identical numbers, and no normal boundary has been flagged
in 364 of them (verification-plan.md §8.1). A project that wants the old
behaviour sets ``render.verify_asr_mode = "advisory"`` in ``profile.json``,
which reports the same flags and leaves the exit code alone.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pysubs2
from pydantic import ValidationError

from . import _asr, _ffmpeg, _fillers, _retranscribe, _text
from ._retranscribe import BoundaryFlag
from .errors import AsrFailedError, UsageError
from .models import AsrProfile, VerifyAsrMode, describe_validation_error, to_ms

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._subtitles import Entry
    from .models import Cut, Profile, Transcript
    from .timeline import Timeline

__all__ = [
    "BoundaryFlag",
    "Subject",
    "VerifyResult",
    "check_available",
    "commands",
    "missing_subtitle_entries",
    "run_verify_asr",
]

#: Where the extracted audio of the render is written while it is transcribed.
WORKSPACE_PREFIX = ".verify-"
AUDIO_STEM = "rendered"

#: The two values ``profile.json`` gives ``render.verify_asr_mode``.
ADVISORY: VerifyAsrMode = "advisory"
GATE: VerifyAsrMode = "gate"

CER_DECIMALS = 4
ELAPSED_DECIMALS = 2


@dataclass(frozen=True, slots=True)
class Subject:
    """What the re-transcription check reads, and the plan it checks against.

    Attributes:
        root: The project directory; a workspace is made and removed inside it.
        video: The rendered output. It is only read (REQ-040).
        transcript: The transcript the output was built from, which also names
            the recogniser settings the second pass has to repeat (REQ-001).
        approved: The cuts that were applied, for their reasons and boundaries.
        timeline: The mapping those cuts define.
        profile: The project profile — the spoken language, the filler
            dictionary switch and the advisory/gate mode.
    """

    root: Path
    video: Path
    transcript: Transcript
    approved: tuple[Cut, ...]
    timeline: Timeline
    profile: Profile


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """One re-transcription comparison of a rendered output."""

    mode: VerifyAsrMode
    backend: str
    model: str
    vad: str
    expected_chars: int
    reasr_chars: int
    missing_hunks: int
    flags: tuple[BoundaryFlag, ...]
    boundaries: int
    global_cer: float
    elapsed_seconds: float

    @property
    def passed(self) -> bool:
        """Whether no difference could be blamed on a cut (REQ-006)."""
        return not self.flags

    @property
    def false_positive_rate(self) -> float | None:
        """Flags per cut boundary, the figure gate promotion is judged on.

        ``None`` when nothing was cut, which leaves the rate undefined rather
        than zero (REQ-013).
        """
        if not self.boundaries:
            return None
        return len(self.flags) / self.boundaries

    def to_dict(self) -> dict[str, Any]:
        """Render the comparison as the ``verify_asr`` section of ``--json``."""
        return {
            "mode": self.mode,
            "backend": self.backend,
            "model": self.model,
            "vad": self.vad,
            "expected_chars": self.expected_chars,
            "reasr_chars": self.reasr_chars,
            "missing_hunks": self.missing_hunks,
            "near_boundary_flags": len(self.flags),
            "boundaries": self.boundaries,
            "flags": [flag.to_dict() for flag in self.flags],
            "global_cer": round(self.global_cer, CER_DECIMALS),
            "elapsed_sec": round(self.elapsed_seconds, ELAPSED_DECIMALS),
        }

    def lines(self) -> list[str]:
        """Render the comparison for a human, flag by flag."""
        mark = "✔" if self.passed else "⚠"
        reported = [
            f"{mark} verify-asr ({self.mode}): {len(self.flags)} boundary flags, "
            f"{self.missing_hunks} missing hunks, "
            f"CER {self.global_cer * 100:.2f}% "
            f"({self.backend} {self.model}, {self.elapsed_seconds:.1f}s)"
        ]
        reported += [
            f"  {flag.cut_id} @ {flag.src_time:.3f}s: {flag.missing!r} missing"
            for flag in self.flags
        ]
        return reported


def _settings(subject: Subject) -> AsrProfile:
    """Return the recogniser settings the transcript was made with (REQ-001).

    The language is not recorded in ``transcript.json``, so it comes from the
    profile; everything the second pass could differ on comes from the
    transcript itself.

    Raises:
        UsageError: If the transcript names a backend or a detector this build
            cannot drive, which makes an identical second pass impossible.
    """
    asr = subject.transcript.asr
    try:
        return AsrProfile.model_validate(
            {
                "backend": asr.backend,
                "model": asr.model,
                "language": subject.profile.asr.language,
                "vad": asr.vad,
            }
        )
    except ValidationError as exc:
        msg = (
            "the transcript was made with settings this build cannot repeat: "
            f"{describe_validation_error(exc)}"
        )
        raise UsageError(msg) from exc


def check_available(subject: Subject) -> None:
    """Fail now if the second pass could not run at all.

    Called before a frame is encoded, so a missing recogniser costs a moment
    rather than a whole render (design.md §5.5).

    Raises:
        UsageError: If the recogniser the transcript names, or its weights, are
            not installed.
    """
    _asr.resolve(_settings(subject))


def commands(subject: Subject) -> list[list[str]]:
    """Return the commands :func:`run_verify_asr` would run, for ``--dry-run``.

    Raises:
        UsageError: If the recogniser or its weights are not installed.
    """
    backend = _asr.resolve(_settings(subject))
    workspace = subject.root / f"{WORKSPACE_PREFIX}XXXXXX"
    audio = workspace / f"{AUDIO_STEM}.wav"
    return [
        _asr.slice_command(subject.video, None, audio),
        backend.whisper_command(audio, workspace),
    ]


def _reasr(subject: Subject, backend: _asr.Backend) -> str:
    """Transcribe the rendered output and return everything it said.

    The audio is extracted first because a recogniser is handed 16 kHz mono
    PCM, not a container; the extraction and the transcript both live in a
    workspace that is removed whatever happens, and neither touches the render.
    """
    workspace = Path(tempfile.mkdtemp(dir=subject.root, prefix=WORKSPACE_PREFIX))
    audio = workspace / f"{AUDIO_STEM}.wav"
    try:
        duration = _ffmpeg.duration(subject.video)
        _ffmpeg.run(
            _asr.slice_command(subject.video, _asr.Interval(0.0, duration), audio)
        )
        _asr.run(backend.whisper_command(audio, workspace))
        raw = _asr.read_transcript(backend, workspace)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return "".join(segment.text for segment in raw)


def run_verify_asr(subject: Subject) -> VerifyResult:
    """Compare the rendered output against what the kept segments should say.

    Args:
        subject: The render to read and the cut plan behind it.

    Returns:
        The comparison, including every difference near a cut boundary. A
        result that flags something is still returned rather than raised: it is
        the caller — the CLI — that decides what advisory and gate mean.

    Raises:
        UsageError: If the recogniser named by the transcript is not installed.
        AsrFailedError: If the second pass failed, or if either side of the
            comparison is empty, which makes the comparison impossible rather
            than merely bad.
    """
    backend = _asr.resolve(_settings(subject))
    words = _fillers.load_dictionary(subject.root).words(
        enable_weak=subject.profile.filler.enable_weak
    )
    expected = _retranscribe.build_expected(
        subject.transcript.segments, subject.timeline, subject.approved, words
    )
    if not expected.text:
        msg = (
            "the kept segments contain no text, so there is nothing to compare "
            "the render against"
        )
        raise AsrFailedError(msg)
    started = time.monotonic()
    actual = _text.normalize(_reasr(subject, backend))
    elapsed = time.monotonic() - started
    if not actual:
        msg = (
            f"the second pass over {subject.video.name} returned no text; the "
            "render could not be verified"
        )
        raise AsrFailedError(msg)
    hunks = _retranscribe.missing_hunks(expected.text, actual)
    flags = _retranscribe.flag_boundaries(
        expected, hunks, subject.timeline, subject.approved
    )
    return VerifyResult(
        mode=subject.profile.render.verify_asr_mode,
        backend=backend.name,
        model=backend.model,
        vad=subject.transcript.asr.vad,
        expected_chars=len(expected.text),
        reasr_chars=len(actual),
        missing_hunks=len(hunks),
        flags=tuple(flags),
        boundaries=len(_retranscribe.boundaries(subject.approved)),
        global_cer=_retranscribe.character_error_rate(expected.text, actual),
        elapsed_seconds=elapsed,
    )


def missing_subtitle_entries(path: Path, entries: Sequence[Entry]) -> list[str]:
    """Return the segments whose subtitle entry is absent from the SRT file.

    The mapping of design.md §4 already decides which segments get an entry and
    reports the ones it dropped, so an entry missing from the file on disk is a
    defect of the writing, not of the mapping — which is what the "no missing
    entries" condition of verification-plan.md §9 checks, and what the fault
    injection of §10 case 3 deletes an entry to prove is checked.

    An entry is matched on its timing *and* its text, and matched entries are
    consumed one for one: two entries the separation trim left with identical
    timings need two events in the file, and an event whose text was emptied is
    as missing as one that was deleted.

    Args:
        path: The SRT to read.
        entries: The entries the mapping produced, in any order.

    Returns:
        The identifiers of the segments with no entry of their own in the file,
        in the order they were given.

    Raises:
        UsageError: If the file is not there to be read.
    """
    if not path.is_file():
        msg = f"{path} not found — run `vidprep render` first"
        raise UsageError(msg)
    written = Counter(
        (event.start, event.end, event.plaintext.replace("\n", ""))
        for event in pysubs2.SSAFile.from_string(
            path.read_text(encoding="utf-8"), format_="srt"
        )
    )
    absent = []
    for entry in entries:
        key = (to_ms(entry.start), to_ms(entry.end), entry.text)
        if written[key] > 0:
            written[key] -= 1
        else:
            absent.append(entry.segment_id)
    return absent
