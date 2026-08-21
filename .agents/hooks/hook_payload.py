"""Normalize hook payloads for every supported agent host.

The host-specific payload dialects are converted into one small event object:
edited files are absolute paths, shell commands remain strings, and stop-hook
state is available to prevent recursive checks. The helper uses only the
standard library and is imported by the shared hook scripts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

SHELL_TOOLS = {"bash", "shell"}
READ_TOOLS = {"read"}
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch"}

PATCH_PATH_RE = re.compile(
    r"^\*\*\*\s+(?:Add File|Update File|Delete File|Move to):\s*(\S.*?)\s*$",
    re.MULTILINE,
)


@dataclass
class Event:
    """A host-independent hook payload."""

    name: str | None = None
    tool: str | None = None
    tool_name: str | None = None
    command: str | None = None
    files: list[Path] = field(default_factory=list)
    cwd: Path = field(default_factory=Path.cwd)
    stop_hook_active: bool = False
    raw: dict = field(default_factory=dict)


def _absolute(value: str, base: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return Path(os.path.normpath(str(path)))


def patch_files(patch: str, base: Path) -> list[Path]:
    """Return absolute paths named by a patch, preserving their order."""
    return [_absolute(match.group(1), base) for match in PATCH_PATH_RE.finditer(patch)]


def from_payload(raw: dict) -> Event:
    """Build an :class:`Event` from an already-parsed payload."""
    tool_input = raw.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    tool_name = raw.get("tool_name") or None
    key = str(tool_name or "").lower()
    if key in SHELL_TOOLS:
        tool = "shell"
    elif key in READ_TOOLS:
        tool = "read"
    elif key in EDIT_TOOLS or isinstance(tool_input.get("file_path"), str):
        tool = "edit"
    elif tool_name:
        tool = "other"
    else:
        tool = None

    cwd_value = raw.get("cwd")
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()

    command = tool_input.get("command") if tool == "shell" else None
    if not isinstance(command, str):
        command = None

    files: list[Path] = []
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path:
        files.append(_absolute(file_path, cwd))
    if tool == "edit":
        patch = tool_input.get("command")
        if isinstance(patch, str) and "*** " in patch:
            files.extend(patch_files(patch, cwd))

    seen: dict[Path, None] = {}
    for path in files:
        seen.setdefault(path, None)

    return Event(
        name=raw.get("hook_event_name") or None,
        tool=tool,
        tool_name=tool_name,
        command=command,
        files=list(seen),
        cwd=cwd,
        stop_hook_active=bool(raw.get("stop_hook_active")),
        raw=raw,
    )


def load_event(stream: IO[str] | None = None) -> Event:
    """Read one JSON payload; unreadable input becomes a no-op event."""
    try:
        raw = json.load(stream if stream is not None else sys.stdin)
    except Exception:
        return Event()
    if not isinstance(raw, dict):
        return Event()
    return from_payload(raw)


def project_root(anchor: str | Path = __file__) -> Path:
    """Resolve the project root from the shared hook directory or Git."""
    start = Path(anchor).resolve()
    base = start if start.is_dir() else start.parent
    for candidate in (base, *base.parents):
        if (candidate / ".agents" / "hooks").is_dir():
            return candidate
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return Path.cwd()
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path.cwd()


def relative_to_root(path: Path, event: Event, root: Path | None = None) -> str:
    """Return a slash-separated path relative to the project root."""
    root = root or project_root()
    for base in (root, event.cwd):
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.name
