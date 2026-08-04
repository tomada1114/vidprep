"""Measurements the ASR bench derives from tool output (verification-plan.md §12.2).

Everything here is pure: it turns the text and JSON the external tools leave
behind into numbers, and applies the §12.2-4 selection rule to them. Keeping it
free of subprocesses is what makes the bench testable without an ASR backend.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

#: Silence definition shared with the golden sample measurements (§2).
SILENCE_NOISE_DB = -40
SILENCE_MIN_SECONDS = 0.5

#: Two candidates this close in CER count as equal, and speed decides (§12.2-4).
CER_TIE_POINTS = 1.0

#: Slack for the tie comparison, well below the precision any CER is reported at.
_TOLERANCE = 1e-9

_GIB = 1024**3
_MILLISECONDS_PER_SECOND = 1000.0

_REAL_SECONDS = re.compile(r"^\s*([\d.]+)\s+real\b", re.MULTILINE)
_PEAK_RSS_BYTES = re.compile(r"^\s*(\d+)\s+maximum resident set size\b", re.MULTILINE)
_SILENCE_EVENT = re.compile(r"silence_(start|end):\s*(-?[\d.]+)")


@dataclass(frozen=True, slots=True)
class TimeMeasurement:
    """What ``/usr/bin/time -l`` reported about one run."""

    wall_seconds: float
    peak_rss_bytes: int


@dataclass(frozen=True, slots=True)
class Interval:
    """A half-open ``[start, end)`` span of the source timeline, in seconds."""

    start: float
    end: float

    def contains(self, moment: float) -> bool:
        """Report whether *moment* falls inside the span."""
        return self.start <= moment < self.end


@dataclass(frozen=True, slots=True)
class Segment:
    """One transcript segment, with times in seconds on the source timeline."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class BenchRow:
    """One row of the §12.2 matrix.

    A candidate that could not be run keeps *unavailable* set and every metric
    at ``None``: REQ-021 wants the reason recorded rather than the row dropped.
    """

    name: str
    cer: float | None = None
    wall_seconds: float | None = None
    realtime_ratio: float | None = None
    hallucinations: int | None = None
    peak_rss_bytes: int | None = None
    unavailable: str | None = None

    @property
    def is_measured(self) -> bool:
        """Report whether this row carries a CER the decision rule can use."""
        return self.unavailable is None and self.cer is not None


def parse_time_output(text: str) -> TimeMeasurement:
    """Read wall time and peak RSS out of a ``/usr/bin/time -l`` report.

    Raises:
        ValueError: If either number is absent, which means the run was not
            measured the way REQ-006 requires and must not be reported.
    """
    real = _REAL_SECONDS.search(text)
    peak = _PEAK_RSS_BYTES.search(text)
    if real is None or peak is None:
        msg = "no `real` time / `maximum resident set size` in the time -l output"
        raise ValueError(msg)
    return TimeMeasurement(float(real.group(1)), int(peak.group(1)))


def strip_time_report(text: str) -> str:
    """Return *text* with the trailing ``time -l`` block removed.

    ``time`` appends its report to the wrapped program's stderr, so the last
    line of a failed run is always "peak memory footprint" rather than the
    error. Cutting the report off puts the real reason back at the end.
    """
    match = _REAL_SECONDS.search(text)
    return text if match is None else text[: match.start()]


def parse_silence_log(text: str, duration: float) -> list[Interval]:
    """Turn ffmpeg ``silencedetect`` output into silent intervals.

    A file that ends while still silent produces a ``silence_start`` with no
    matching ``silence_end``; that trailing span is closed at *duration* so the
    hallucination count covers the tail as well.
    """
    intervals: list[Interval] = []
    start: float | None = None
    for kind, value in _SILENCE_EVENT.findall(text):
        moment = float(value)
        if kind == "start":
            start = max(moment, 0.0)
        elif start is not None:
            intervals.append(Interval(start, moment))
            start = None
    if start is not None:
        intervals.append(Interval(start, duration))
    return intervals


def _whisper_cpp_segments(payload: list[Any]) -> list[Segment]:
    segments = []
    for entry in payload:
        offsets = entry["offsets"]
        segments.append(
            Segment(
                start=float(offsets["from"]) / _MILLISECONDS_PER_SECOND,
                end=float(offsets["to"]) / _MILLISECONDS_PER_SECOND,
                text=str(entry["text"]),
            )
        )
    return segments


def _openai_segments(payload: list[Any]) -> list[Segment]:
    return [
        Segment(
            start=float(entry["start"]),
            end=float(entry["end"]),
            text=str(entry["text"]),
        )
        for entry in payload
    ]


def load_segments(path: Path) -> list[Segment]:
    """Read a transcript written by either backend.

    whisper.cpp (``-oj``) and mlx-whisper (``--output-format json``) disagree on
    both the key and the time unit, so the shape decides how to read it.

    Raises:
        ValueError: If the document matches neither backend.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(document, dict):
        if isinstance(document.get("transcription"), list):
            return _whisper_cpp_segments(document["transcription"])
        if isinstance(document.get("segments"), list):
            return _openai_segments(document["segments"])
    msg = f"{path} has neither a `transcription` nor a `segments` list"
    raise ValueError(msg)


def transcript_text(segments: list[Segment]) -> str:
    """Join segment texts into the single string CER is measured on."""
    return "".join(segment.text for segment in segments)


def count_hallucinations(segments: list[Segment], silences: list[Interval]) -> int:
    """Count segments that start inside a silent span (REQ-007).

    The start is what is tested: a segment that begins in silence was invented
    there, while one that merely runs into the following silence is a real
    utterance with a generous end time.
    """
    return sum(
        1
        for segment in segments
        if any(silence.contains(segment.start) for silence in silences)
    )


@dataclass(frozen=True, slots=True)
class Decision:
    """The model the §12.2-4 rule selects, and why."""

    winner: str | None
    rationale: str


def choose(rows: list[BenchRow]) -> Decision:
    """Apply "lowest CER, but speed decides within 1pt" to the matrix.

    A gap of exactly 1pt counts as a tie, so the comparison carries a tolerance:
    CERs are fractions, and 0.070 - 0.060 lands just above 1pt in binary
    floating point.
    """
    measured = [row for row in rows if row.is_measured]
    if not measured:
        return Decision(None, "no candidate produced a CER; nothing to choose from")
    best = min(measured, key=lambda row: row.cer or 0.0)
    tied = [
        row
        for row in measured
        if ((row.cer or 0.0) - (best.cer or 0.0)) * 100 <= CER_TIE_POINTS + _TOLERANCE
    ]
    if len(tied) == 1:
        runner_up = min(
            (row for row in measured if row is not best),
            key=lambda row: row.cer or 0.0,
            default=None,
        )
        margin = (
            ""
            if runner_up is None
            else f", more than {CER_TIE_POINTS:.1f}pt ahead of {runner_up.name}"
        )
        return Decision(
            best.name,
            f"{best.name} has the lowest CER ({_percent(best.cer)}){margin}",
        )
    winner = min(tied, key=lambda row: row.wall_seconds or float("inf"))
    field = ", ".join(f"{row.name} {_percent(row.cer)}" for row in tied)
    return Decision(
        winner.name,
        f"{field} are within {CER_TIE_POINTS:.1f}pt of each other, "
        f"so the shortest runtime decides: {winner.name} "
        f"({_ratio(winner.realtime_ratio)})",
    )


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.2f}%"


def _ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}x"


def _gibibytes(value: int | None) -> str:
    return "-" if value is None else f"{value / _GIB:.1f} GiB"


def _count(value: int | None) -> str:
    return "-" if value is None else str(value)


#: Matrix headers, in the order verification-plan.md §12.2 lists the columns.
MATRIX_HEADERS = (
    "model x backend",
    "CER",
    "runtime (x realtime)",
    "hallucinations",
    "peak RSS",
)


def format_matrix(rows: list[BenchRow]) -> str:
    """Render the matrix as the markdown table §12.2 expects to be filled in."""
    lines = [
        "| " + " | ".join(MATRIX_HEADERS) + " |",
        "|" + "|".join(["---"] * len(MATRIX_HEADERS)) + "|",
    ]
    for row in rows:
        if row.unavailable is not None:
            lines.append(f"| {row.name} | unavailable (reason: {row.unavailable}) |")
            continue
        lines.append(
            "| "
            + " | ".join(
                (
                    row.name,
                    _percent(row.cer),
                    _ratio(row.realtime_ratio),
                    _count(row.hallucinations),
                    _gibibytes(row.peak_rss_bytes),
                )
            )
            + " |"
        )
    return "\n".join(lines)
