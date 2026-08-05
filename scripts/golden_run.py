"""The golden run: the whole pipeline over the fixed sample (verification-plan.md §11).

Usage:
    uv run python scripts/golden_run.py [--project DIR] [--source VIDEO]
    uv run python scripts/golden_run.py --skip correct   # attribution run

Six stages in order — audio-fix, transcribe, correct, detect, render
(with ``--verify-asr``), report — over the material of verification-plan.md §2,
after which ``report/stats.json`` and every warning raised are archived under
``fixtures/runs/<date>/`` for ``scripts/compare_stats.py`` to diff against the
run before it. Run it after a parameter change, a dependency update or a new
feature; what it costs is two ASR passes and a re-encode.

``--skip`` leaves a named stage out of an otherwise identical run, which is how
a number is attributed to one stage rather than to the pipeline: the run that
told §8.1 how much of the global CER is the dictionary's doing was this run
without ``correct``. It is a measurement tool, not a fast path — a run missing a
stage is not a baseline, so what it skipped is recorded in ``summary.json``.

Nothing is archived outside ``fixtures/``, which is not in git: the recording is
private and so is everything derived from it (verification-plan.md §2).

The stages run in-process rather than as subprocesses, so a failure arrives as
the exception the stage raised and is recorded with the reason intact. A stage
that fails stops the run — every stage downstream reads what it would have
written — but the archive is written anyway, because "where it stopped and why"
is the output somebody asked for.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from vidprep import audio, correct, detect, render, report, transcribe
from vidprep import project as project_module
from vidprep.errors import EXIT_EXECUTION, EXIT_OK, VidprepError

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from vidprep.project import Project

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "fixtures" / "raw" / "VID_20260507_144024.mp4"
DEFAULT_PROJECT = REPO_ROOT / "fixtures" / "golden"
RUNS_DIR = REPO_ROOT / "fixtures" / "runs"

STATS_NAME = "stats.json"
WARNINGS_NAME = "warnings.json"
SUMMARY_NAME = "summary.json"


class Stage(Protocol):
    """One pipeline stage as this harness needs it."""

    def to_dict(self) -> dict[str, Any]:
        """The stage's machine-readable result."""

    def lines(self) -> list[str]:
        """The stage's report for a human, one line each."""


def _correct(loaded: Project) -> Stage:
    """Run the dictionary pass, which has no single-call entry point."""
    plan = correct.plan_dictionary(loaded)
    applied = correct.apply(loaded, plan)

    class _Result:
        def to_dict(self) -> dict[str, Any]:
            return plan.to_dict(applied=applied)

        def lines(self) -> list[str]:
            return [f"✔ updated {applied} segments (source={plan.tool})"]

    return _Result()


#: The pipeline, in the order verification-plan.md §11 states it.
STAGES: tuple[tuple[str, Callable[[Project], Stage]], ...] = (
    ("audio-fix", lambda loaded: audio.run_audio_fix(loaded, with_stats=True)),
    ("transcribe", transcribe.run_transcribe),
    ("correct", _correct),
    ("detect", detect.run_detect),
    ("render", lambda loaded: render.run_render(loaded, verify_asr=True)),
    ("report", report.run_report),
)

#: The stage names ``--skip`` accepts, in pipeline order.
STAGE_NAMES: tuple[str, ...] = tuple(name for name, _ in STAGES)


def run_directory(label: str, root: Path = RUNS_DIR) -> Path:
    """Return where this run is archived, never overwriting an earlier one.

    Args:
        label: The run's name, the date by default.
        root: Where runs are kept.

    Returns:
        A directory that did not exist; two runs on one day become ``<date>``
        and ``<date>-02``, so a same-day comparison stays possible. The counter
        is zero-padded because ``scripts/compare_stats.py`` falls back to
        sorting these names when it has to guess which run came last.
    """
    candidate = root / label
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"{label}-{suffix:02d}"
    return candidate


def _prepare(project: Path) -> Project:
    """Load the project and check it is intact, as every CLI command does."""
    loaded = project_module.load_project(project)
    project_module.verify_source(loaded)
    project_module.validate_artifacts(loaded)
    return loaded


def _warnings(payload: Sequence[str]) -> list[str]:
    """Return the reported lines that are warnings rather than results."""
    return [line for line in payload if line.startswith("⚠")]


def run_stages(project: Path, skip: Collection[str] = ()) -> dict[str, Any]:
    """Run the pipeline over *project* and return what each stage reported.

    Args:
        project: The project directory to run over.
        skip: Stage names to leave out (see the module docstring). A skipped
            stage's own outputs stay as the previous run left them, so the
            stages after it read those; that is the point of the comparison.

    Returns:
        ``{"stages": {...}, "skipped": [...], "warnings": [...],
        "failed": <name or None>}``. A stage that raises stops the run and is
        recorded with its error code and message; the stages that never started
        are simply absent, which is why the ones deliberately left out are named
        separately rather than inferred from what is missing.
    """
    stages: dict[str, Any] = {}
    warnings: list[str] = []
    failed: str | None = None
    planned = [(name, action) for name, action in STAGES if name not in skip]
    for index, (name, action) in enumerate(planned, start=1):
        started = time.monotonic()
        try:
            result = action(_prepare(project))
        # Anything at all: a stage is allowed to raise something vidprep does
        # not model (a ValueError out of the timeline, an OSError off the disk),
        # and losing the archive over it would lose the only record of the run.
        except Exception as exc:
            failed = name
            code = exc.code if isinstance(exc, VidprepError) else type(exc).__name__
            stages[name] = {"error": code, "detail": str(exc)}
            print(f"[{index}/{len(planned)}] {name} ... failed ({code}: {exc})")
            break
        elapsed = time.monotonic() - started
        reported = result.lines()
        warnings += [f"{name}: {line}" for line in _warnings(reported)]
        stages[name] = {"result": result.to_dict(), "elapsed_sec": round(elapsed, 2)}
        headline = next((line for line in reported if not line.startswith("⚠")), "")
        print(f"[{index}/{len(planned)}] {name} ... ok {headline}")
    return {
        "stages": stages,
        "skipped": [name for name in STAGE_NAMES if name in skip],
        "warnings": warnings,
        "failed": failed,
    }


def archive(project: Path, target: Path, summary: dict[str, Any]) -> Path:
    """Copy the run's statistics and warnings into *target* (REQ-021).

    Returns:
        The directory that was written.
    """
    target.mkdir(parents=True, exist_ok=True)
    stats = project / report.STATS_NAME
    if stats.is_file():
        shutil.copyfile(stats, target / STATS_NAME)
    document = {
        "generated_at": datetime.now(tz=UTC).astimezone().isoformat(),
        "project": str(project),
        **summary,
    }
    (target / SUMMARY_NAME).write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (target / WARNINGS_NAME).write_text(
        json.dumps(summary["warnings"], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target


def _initialise(project: Path, source: Path) -> None:
    """Create the project directory if this is the first run against *source*.

    Raises:
        SystemExit: If the material of verification-plan.md §2 is not there.
    """
    if (project / project_module.MANIFEST_NAME).is_file():
        return
    if not source.is_file():
        msg = (
            f"the golden sample is not at {source}; restore it as "
            "verification-plan.md §2 describes, or pass --source"
        )
        raise SystemExit(msg)
    project_module.init_project(project, source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--runs", type=Path, default=RUNS_DIR)
    parser.add_argument(
        "--label",
        default=datetime.now(tz=UTC).astimezone().date().isoformat(),
        help="name of the archive directory (default: today's date)",
    )
    parser.add_argument(
        "--skip",
        action="append",
        choices=STAGE_NAMES,
        default=[],
        metavar="STAGE",
        help=(
            "leave a stage out of the run, to attribute a number to it; "
            f"repeatable, one of: {', '.join(STAGE_NAMES)}"
        ),
    )
    arguments = parser.parse_args(argv)
    _initialise(arguments.project, arguments.source)
    summary = run_stages(arguments.project, arguments.skip)
    target = archive(
        arguments.project,
        run_directory(arguments.label, arguments.runs),
        summary,
    )
    if summary["skipped"]:
        print(f"skipped: {', '.join(summary['skipped'])} — not a baseline run")
    print(f"saved: {target}")
    if summary["failed"] is not None:
        print(f"the run stopped at {summary['failed']}")
        return EXIT_EXECUTION
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
