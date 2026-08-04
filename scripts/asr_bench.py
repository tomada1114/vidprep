"""Run the ASR benchmark matrix (verification-plan.md §12.2).

Usage:
    uv run python scripts/asr_bench.py <bench audio> [--reference <transcript>]

Every candidate is run twice and the second run is reported (REQ-005), wrapped
in ``/usr/bin/time -l`` so wall time and peak RSS come from one place (REQ-006).
Raw output is kept under ``--out-dir`` as ``<model>/run{1,2}.{json,time}``.

Without ``--reference`` the run still works and simply leaves the CER column
empty: that is the bootstrap pass whose transcripts become the draft for the
human reference (verification-plan.md §3.2 step 1).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path

import asr_backends
import bench_metrics
import cer

DEFAULT_OUT_DIR = Path("fixtures/bench")
DEFAULT_RUNS = 2


@dataclass(frozen=True, slots=True)
class BenchContext:
    """Everything a candidate is measured against."""

    audio: Path
    out_dir: Path
    runs: int
    duration: float
    silences: list[bench_metrics.Interval]
    reference: str | None


def _run_series(
    candidate: asr_backends.Candidate,
    context: BenchContext,
    directory: Path,
    vad_model: Path | None,
) -> tuple[bench_metrics.TimeMeasurement, Path]:
    """Run the candidate ``context.runs`` times and return the last run.

    The first run pays for model loading and disk caches, so only the last one
    is reported (REQ-005); every run leaves its own raw log behind.

    Raises:
        ValueError: If fewer than one run was asked for.
    """
    if context.runs < 1:
        msg = f"runs must be at least 1, got {context.runs}"
        raise ValueError(msg)
    for index in range(1, context.runs + 1):
        prefix = directory / f"run{index}"
        command = asr_backends.build_command(
            candidate, context.audio, prefix, vad_model
        )
        measurement = asr_backends.run_measured(command, prefix.with_suffix(".time"))
        transcript = asr_backends.transcript_path(candidate, context.audio, prefix)
    return measurement, transcript


def measure(
    candidate: asr_backends.Candidate,
    context: BenchContext,
    vad_model: Path | None = None,
) -> bench_metrics.BenchRow:
    """Run *candidate* the agreed number of times and report the last one.

    Failures become an ``unavailable`` row instead of an exception: REQ-004
    wants every candidate accounted for, and one backend refusing to run must
    not cost the numbers already gathered for the others.

    Besides the raw logs, the reported run is written out as ``transcript.txt``
    (one segment per line): that is the draft the human reference is corrected
    from (verification-plan.md §3.2 step 1), and line breaks do not affect CER
    because normalisation drops whitespace.
    """
    name = candidate.name
    reason = asr_backends.unavailable_reason(candidate)
    if reason is not None:
        return bench_metrics.BenchRow(name=name, unavailable=reason)
    directory = context.out_dir / candidate.slug
    directory.mkdir(parents=True, exist_ok=True)
    try:
        measurement, transcript = _run_series(candidate, context, directory, vad_model)
        segments = bench_metrics.load_segments(transcript)
        lines = "".join(f"{segment.text.strip()}\n" for segment in segments)
        (directory / "transcript.txt").write_text(lines, encoding="utf-8")
    except (OSError, RuntimeError, ValueError) as exc:
        return bench_metrics.BenchRow(name=name, unavailable=str(exc))
    return bench_metrics.BenchRow(
        name=name,
        cer=_cer_against(context.reference, segments),
        wall_seconds=measurement.wall_seconds,
        realtime_ratio=measurement.wall_seconds / context.duration,
        hallucinations=bench_metrics.count_hallucinations(segments, context.silences),
        peak_rss_bytes=measurement.peak_rss_bytes,
    )


def _cer_against(
    reference: str | None, segments: list[bench_metrics.Segment]
) -> float | None:
    if reference is None:
        return None
    return cer.measure(reference, bench_metrics.transcript_text(segments)).cer


def compare_vad(
    winner: str, context: BenchContext, vad_model: Path | None
) -> list[bench_metrics.BenchRow]:
    """Re-run the selected model with VAD enabled (REQ-008).

    Only the VAD-on row is measured here; the VAD-off numbers are the ones the
    matrix already reported, so the pair stays a like-for-like comparison. The
    re-run gets its own directory so it cannot overwrite the raw logs the
    matrix row was read from.
    """
    candidate = next(
        (row for row in asr_backends.CANDIDATES if row.name == winner), None
    )
    if candidate is None or candidate.backend != asr_backends.WHISPER_CPP:
        return []
    if vad_model is None:
        return [
            bench_metrics.BenchRow(
                name=f"{winner} (VAD on)",
                unavailable=(
                    f"no {asr_backends.VAD_MODEL_GLOB} in "
                    f"{asr_backends.model_dir()} (fetch one with "
                    "whisper.cpp/models/download-vad-model.sh, or pass --vad-model)"
                ),
            )
        ]
    with_vad = replace(
        candidate, name=f"{winner} (VAD on)", slug=f"{candidate.slug}-vad"
    )
    return [measure(with_vad, context, vad_model)]


def _relabel(row: bench_metrics.BenchRow, name: str) -> bench_metrics.BenchRow:
    return bench_metrics.BenchRow(
        name=name,
        cer=row.cer,
        wall_seconds=row.wall_seconds,
        realtime_ratio=row.realtime_ratio,
        hallucinations=row.hallucinations,
        peak_rss_bytes=row.peak_rss_bytes,
        unavailable=row.unavailable,
    )


def _row_as_dict(row: bench_metrics.BenchRow) -> dict[str, object]:
    return {
        "name": row.name,
        "cer": row.cer,
        "wall_seconds": row.wall_seconds,
        "realtime_ratio": row.realtime_ratio,
        "hallucinations": row.hallucinations,
        "peak_rss_bytes": row.peak_rss_bytes,
        "unavailable": row.unavailable,
    }


def report(
    rows: list[bench_metrics.BenchRow],
    vad_rows: list[bench_metrics.BenchRow],
    decision: bench_metrics.Decision,
    context: BenchContext,
) -> str:
    """Render the matrix, the VAD comparison and the selection rationale."""
    blocks = [bench_metrics.format_matrix(rows), f"-> {decision.rationale}"]
    if vad_rows:
        vad_off = [
            _relabel(row, f"{row.name} (VAD off)")
            for row in rows
            if row.name == decision.winner
        ]
        blocks.append("VAD comparison (REQ-008):")
        blocks.append(bench_metrics.format_matrix(vad_off + vad_rows))
    if context.reference is None:
        blocks.append(
            "note: no --reference given, so the CER column is empty. Correct the "
            "best transcript.txt by hand into fixtures/expected/golden.reference.txt "
            "(verification-plan.md §3.2) and run this again."
        )
    return "\n\n".join(blocks)


def _write_results(
    text: str,
    rows: list[bench_metrics.BenchRow],
    decision: bench_metrics.Decision,
    context: BenchContext,
) -> None:
    """Persist the rendered matrix and the numbers behind it."""
    (context.out_dir / "matrix.md").write_text(text + "\n", encoding="utf-8")
    document = {
        "audio": str(context.audio),
        "duration": context.duration,
        "runs": context.runs,
        "silences": len(context.silences),
        "rows": [_row_as_dict(row) for row in rows],
        "winner": decision.winner,
        "rationale": decision.rationale,
    }
    (context.out_dir / "bench.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audio", type=Path, help="loudnorm-processed bench audio")
    parser.add_argument("--reference", type=Path, help="human reference transcript")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--duration", type=float, help="skip the ffprobe call")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="run only candidates whose name contains this text",
    )
    parser.add_argument("--vad-model", type=Path, help="Silero weights for --vad")
    parser.add_argument("--skip-vad-compare", action="store_true")
    return parser.parse_args(argv)


def _build_context(arguments: argparse.Namespace) -> BenchContext:
    duration = arguments.duration or asr_backends.probe_duration(arguments.audio)
    return BenchContext(
        audio=arguments.audio,
        out_dir=arguments.out_dir,
        runs=arguments.runs,
        duration=duration,
        silences=asr_backends.detect_silences(arguments.audio, duration),
        reference=(
            arguments.reference.read_text(encoding="utf-8")
            if arguments.reference is not None
            else None
        ),
    )


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    if not arguments.audio.is_file():
        msg = f"bench audio not found: {arguments.audio}"
        raise SystemExit(msg)
    context = _build_context(arguments)
    context.out_dir.mkdir(parents=True, exist_ok=True)
    selected = [
        candidate
        for candidate in asr_backends.CANDIDATES
        if not arguments.only
        or any(needle in candidate.name for needle in arguments.only)
    ]
    rows = [measure(candidate, context) for candidate in selected]
    decision = bench_metrics.choose(rows)
    vad_rows = (
        []
        if arguments.skip_vad_compare or decision.winner is None
        else compare_vad(
            decision.winner, context, asr_backends.find_vad_model(arguments.vad_model)
        )
    )
    text = report(rows, vad_rows, decision, context)
    _write_results(text, rows + vad_rows, decision, context)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
