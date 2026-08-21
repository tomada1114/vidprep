# /// script
# requires-python = ">=3.12"
# ///
"""PostToolUse hook: format and lint Python files touched by the last edit.

Reads the normalized hook payload from stdin and runs Ruff on edited files only.
Exit code 2 feeds remaining (unfixable) violations back to the agent as context;
it does not block, because the edit has already happened.
"""

from __future__ import annotations

import os
import subprocess
import sys

from hook_payload import load_event, project_root

# Drop the wrapper script's own venv so the nested `uv run` targets .venv.
SUBPROCESS_ENV = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def main() -> int:
    """Run Ruff fix and format on files reported in the hook payload."""
    event = load_event()
    if event.name != "PostToolUse":
        return 0

    project_dir = project_root()
    failed = False
    for file_path in event.files:
        if not file_path.name.endswith(".py"):
            continue
        if not file_path.is_file() or not file_path.resolve().is_relative_to(
            project_dir
        ):
            continue
        for args in (
            ["uv", "run", "ruff", "check", "--fix", str(file_path)],
            ["uv", "run", "ruff", "format", str(file_path)],
        ):
            result = subprocess.run(  # noqa: S603
                args,
                capture_output=True,
                text=True,
                check=False,
                cwd=project_dir,
                env=SUBPROCESS_ENV,
            )
            if result.returncode != 0:
                failed = True
                sys.stderr.write(result.stdout)
                sys.stderr.write(result.stderr)

    # Exit 2 surfaces the remaining violations to the agent so it can fix them.
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
