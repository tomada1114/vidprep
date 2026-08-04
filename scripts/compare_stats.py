"""Diff two golden runs (verification-plan.md §3.3 and §11).

Usage:
    uv run python scripts/compare_stats.py [previous] [current]

With no arguments the two most recent directories under ``fixtures/runs/`` are
compared; with one, it is the current run and its predecessor is looked up.
Reporting "first run" and exiting ``0`` when there is nothing to compare against
is deliberate: the first golden run of a project must not look like a failure.

What is compared is every number in ``report/stats.json`` — including the length
of each warning list, which is how "the max_cps warnings went from 11 to 14"
shows up — plus the ``verify_asr`` section the run's ``render`` reported. A path
whose name mentions warnings or flags is marked when it grows: those are the
numbers a regression shows up in first.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = REPO_ROOT / "fixtures" / "runs"

STATS_NAME = "stats.json"
SUMMARY_NAME = "summary.json"

EXIT_OK = 0
EXIT_USAGE = 1

#: Paths whose growth is worth pointing at rather than merely printing.
_ALERTING = ("warning", "flag", "error")

#: Numbers that say when a run happened, not what it measured.
_IGNORED = ("generated_at",)


def _flatten(
    document: Any, prefix: str = ""
) -> Iterator[tuple[str, float | str | None]]:
    """Yield every leaf of *document* as a dotted path.

    A list contributes its length rather than its items: the warning lists are
    what regression is measured on, and "three more entries" is the change worth
    seeing before the entries themselves. Text leaves are kept as they are —
    ``verify_asr.model`` changing from one set of weights to another is exactly
    the kind of change a golden run after a dependency update exists to catch.
    """
    if isinstance(document, dict):
        for key, value in document.items():
            if key in _IGNORED:
                continue
            yield from _flatten(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(document, list):
        yield prefix, float(len(document))
    elif isinstance(document, int | float):  # bool is an int, and counts as 0/1
        yield prefix, float(document)
    elif isinstance(document, str):
        yield prefix, document
    elif document is None:
        yield prefix, None


def load_run(directory: Path) -> dict[str, Any]:
    """Return the numbers one archived run is compared on.

    Args:
        directory: A ``fixtures/runs/<date>`` directory.

    Returns:
        The statistics document, with the ``verify_asr`` section of that run's
        ``render`` and the warning count folded in.

    Raises:
        SystemExit: If the directory holds no ``stats.json``, which means the
            run never reached ``report``.
    """
    stats_path = directory / STATS_NAME
    if not stats_path.is_file():
        msg = f"{stats_path} not found; that run never reached `report`"
        raise SystemExit(msg)
    document: dict[str, Any] = json.loads(stats_path.read_text(encoding="utf-8"))
    summary_path = directory / SUMMARY_NAME
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        rendered = summary.get("stages", {}).get("render", {}).get("result", {})
        if "verify_asr" in rendered:
            document["verify_asr"] = rendered["verify_asr"]
        # Namespaced rather than "warnings", which stats.json may grow itself.
        document["run.warnings"] = summary.get("warnings", [])
    return document


def recent_runs(root: Path = RUNS_DIR, limit: int = 2) -> list[Path]:
    """Return the *limit* most recent run directories, newest last.

    Ordered by when the directory was written rather than by its name: a run
    may be labelled anything (``golden_run.py --label``), and even the default
    date labels grow a counter that does not sort the way a reader expects.
    """
    if not root.is_dir():
        return []
    found = sorted(
        (entry for entry in root.iterdir() if entry.is_dir()),
        key=lambda entry: (entry.stat().st_mtime, entry.name),
    )
    return found[-limit:]


def _format(value: float | str | None) -> str:
    """Render one measurement, "—" standing for "not measured"."""
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return f"{value:.0f}" if value == int(value) else f"{value:.3f}"


def compare(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Return one line per number that changed between two runs.

    A path only one of the runs has is reported too: a section that stopped
    being measured is the kind of silent change this comparison exists for.
    """
    before = dict(_flatten(previous))
    after = dict(_flatten(current))
    lines = []
    for path in sorted(before.keys() | after.keys()):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        line = f"{path}  {_format(old)} → {_format(new)}"
        if isinstance(old, float) and isinstance(new, float):
            line += f"  ({new - old:+.3f})"
            if new > old and any(word in path for word in _ALERTING):
                line += "  ⚠ increased"
        lines.append(line)
    return lines


def _resolve(paths: Sequence[Path], root: Path) -> tuple[Path | None, Path | None]:
    """Return the (previous, current) directories the arguments ask for."""
    if len(paths) >= 2:  # noqa: PLR2004 — "two directories were given"
        return paths[-2], paths[-1]
    if len(paths) == 1:
        earlier = [found for found in recent_runs(root, limit=2) if found != paths[0]]
        return (earlier[-1] if earlier else None), paths[0]
    found = recent_runs(root, limit=2)
    if not found:
        return None, None
    return (found[-2] if len(found) > 1 else None), found[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", type=Path, nargs="*", help="[previous] [current]")
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    arguments = parser.parse_args(argv)
    previous, current = _resolve(arguments.runs, arguments.runs_dir)
    if current is None:
        print(f"first run: no golden run archived under {arguments.runs_dir} yet")
        return EXIT_OK
    if previous is None:
        print(f"first run: {current} has nothing to be compared against")
        return EXIT_OK
    changes = compare(load_run(previous), load_run(current))
    print(f"{previous} → {current}")
    if not changes:
        print("no measured value changed")
        return EXIT_OK
    for line in changes:
        print(line)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
