"""Inspection of the external tools the pipeline depends on (design.md §5.7).

Every check answers one question — "can the pipeline rely on this?" — and
records the answer instead of raising, so a broken environment is reported in
full rather than one failure at a time. Nothing here writes to disk or mutates
external state: ``doctor`` is a read-only probe of the machine.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

#: External commands are probed, not used for work, so they must answer fast.
COMMAND_TIMEOUT_SECONDS = 5.0

#: Checks the pipeline cannot run without (design.md §5.7, REQ-020).
REQUIRED_CHECKS = ("ffmpeg", "ffprobe", "auto_editor", "asr", "sudachipy")

#: `--export v3` became an explicit export name in auto-editor 28.0.0.
MIN_AUTO_EDITOR_MAJOR = 28

#: Names whisper.cpp has shipped its main CLI under, newest first.
WHISPER_BINARIES = ("whisper-cli", "whisper-cpp", "main")

#: Overrides where whisper.cpp models are looked up (handy for the ASR bench).
WHISPER_MODEL_DIR_ENV = "VIDPREP_WHISPER_MODEL_DIR"
DEFAULT_WHISPER_MODEL_DIR = Path.home() / ".cache" / "whisper.cpp"
WHISPER_MODEL_GLOB = "ggml-*.bin"

#: DeepFilterNet has shipped under both spellings; either one satisfies it.
DEEPFILTERNET_BINARIES = ("deep-filter", "deepFilter")
DEEPFILTERNET_FALLBACK = "afftdn"

#: SudachiPy dictionary flavours, from the smallest useful one upwards.
SUDACHI_DICTS = ("core", "full", "small")
SUDACHI_PROBE_WORD = "形態素解析"

_VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)*\S*")
_SUBTITLES_FILTER = re.compile(r"^\s*\S+\s+subtitles\s", re.MULTILINE)

#: One check's findings. The keys differ per check — ffmpeg reports ``libass``,
#: the ASR check nests a result per backend — because the document this becomes
#: is the contract, so ``Any`` is the honest type for the values.
Check = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Report:
    """The outcome of a full inspection.

    Attributes:
        checks: One entry per inspected category, keyed by check name.
        missing: Names of the required checks that failed, in report order.
    """

    checks: dict[str, Check] = field(default_factory=dict)
    missing: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        """``"ng"`` when something required is missing, else ``"warn"``/``"ok"``."""
        if self.missing:
            return "ng"
        optional = (check for check in self.checks.values() if check.get("optional"))
        return "warn" if any(not check["ok"] for check in optional) else "ok"

    def to_dict(self) -> dict[str, Any]:
        """Render the report as the JSON document ``--json`` prints."""
        return {
            "status": self.status,
            "checks": self.checks,
            "missing": list(self.missing),
        }


@dataclass(frozen=True, slots=True)
class _Completed:
    """What running a probe command told us."""

    ok: bool
    output: str = ""
    error: str = ""


def _run_command(args: list[str]) -> _Completed:
    """Run a probe command, converting any failure into a recorded reason.

    A doctor that dies on a broken binary is useless, so every failure mode —
    missing, not executable, hanging, exiting non-zero — comes back as data
    (REQ-022).
    """
    try:
        # Fixed argument vector, shell=False: no interpolation into a shell.
        completed = subprocess.run(  # noqa: S603
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _Completed(False, error=f"timed out after {COMMAND_TIMEOUT_SECONDS:g}s")
    except OSError as exc:
        return _Completed(False, error=f"could not run {args[0]}: {exc}")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        reason = detail[-1] if detail else "no output"
        return _Completed(False, error=f"exited with {completed.returncode}: {reason}")
    return _Completed(True, output=completed.stdout + completed.stderr)


def _find_version(text: str, prefix: str) -> str | None:
    """Return the version number following *prefix* in a ``--version`` banner."""
    _, _, tail = text.partition(prefix)
    match = _VERSION_PATTERN.search(tail or text)
    return match.group(0) if match else None


def _missing(name: str) -> Check:
    """Return the check result for a command that is not installed."""
    return {"ok": False, "error": f"{name} not found in PATH"}


def _check_version(command: str, flag: str, prefix: str) -> Check:
    """Resolve *command* on PATH and read the version it prints.

    Args:
        command: Executable to look for.
        flag: The flag that makes it print its version banner.
        prefix: Text the version follows in that banner.

    Returns:
        A finished check: installed and readable, or the reason it is not.
    """
    path = shutil.which(command)
    if path is None:
        return _missing(command)
    banner = _run_command([path, flag])
    if not banner.ok:
        return {"ok": False, "path": path, "error": banner.error}
    return {"ok": True, "version": _find_version(banner.output, prefix), "path": path}


def check_ffmpeg() -> Check:
    """Check ffmpeg and whether it was built with libass (REQ-002).

    The ``subtitles`` filter is what ``render --preview`` burns telops with, so
    an ffmpeg without it is treated as unusable rather than merely limited.
    """
    check = _check_version("ffmpeg", "-version", "ffmpeg version ")
    if not check["ok"]:
        return check
    filters = _run_command([check["path"], "-filters"])
    check["libass"] = bool(filters.ok and _SUBTITLES_FILTER.search(filters.output))
    if not check["libass"]:
        check["ok"] = False
        check["error"] = filters.error or "built without libass (no subtitles filter)"
    return check


def check_ffprobe() -> Check:
    """Check that ffprobe can be executed."""
    return _check_version("ffprobe", "-version", "ffprobe version ")


def _supports_export_v3(version: str | None) -> bool:
    """Report whether *version* of auto-editor accepts ``--export v3``."""
    if version is None:
        return False
    digits = re.match(r"\d+", version.split(".")[0])
    if digits is None:  # a banner we cannot read is not a version we can trust
        return False
    return int(digits.group(0)) >= MIN_AUTO_EDITOR_MAJOR


def check_auto_editor() -> Check:
    """Check auto-editor and its support for the v3 timeline export.

    auto-editor validates ``--export`` only after opening its input file, so
    the format cannot be probed without decoding media; the version — the one
    thing a read-only check can ask for — decides instead.
    """
    check = _check_version("auto-editor", "--version", "auto-editor ")
    if not check["ok"]:
        return check
    version = check["version"]
    check["export_v3"] = _supports_export_v3(version)
    if not check["export_v3"]:
        check["ok"] = False
        check["error"] = (
            f"--export v3 needs auto-editor >= {MIN_AUTO_EDITOR_MAJOR}, found {version}"
        )
    return check


def whisper_model_dir() -> Path:
    """Return the directory whisper.cpp models are looked up in."""
    override = os.environ.get(WHISPER_MODEL_DIR_ENV)
    return Path(override).expanduser() if override else DEFAULT_WHISPER_MODEL_DIR


def check_whisper_cpp() -> Check:
    """Check the whisper.cpp binary and the ggml models next to it."""
    path = next(
        (found for name in WHISPER_BINARIES if (found := shutil.which(name))), None
    )
    directory = whisper_model_dir()
    models = (
        sorted(model.name for model in directory.glob(WHISPER_MODEL_GLOB))
        if directory.is_dir()
        else []
    )
    check: Check = {
        "ok": bool(path) and bool(models),
        "path": path,
        "model_dir": str(directory),
        "models": models,
    }
    if path is None:
        check["error"] = f"none of {', '.join(WHISPER_BINARIES)} found in PATH"
    elif not models:
        check["error"] = f"no {WHISPER_MODEL_GLOB} model in {directory}"
    return check


def check_mlx_whisper() -> Check:
    """Check that the ``mlx_whisper`` package is importable (REQ-003).

    The module is located rather than imported: importing it pulls in the whole
    MLX runtime, which is far too slow for a check that must answer in seconds.
    """
    try:
        spec = importlib.util.find_spec("mlx_whisper")
    except (ImportError, ValueError) as exc:
        return {"ok": False, "error": f"mlx_whisper is not importable: {exc}"}
    if spec is None:
        return {"ok": False, "error": "mlx_whisper is not installed"}
    try:
        installed = package_version("mlx-whisper")
    except PackageNotFoundError:
        installed = None
    return {"ok": True, "version": installed}


def check_asr() -> Check:
    """Check both ASR backends; either one on its own is enough."""
    whisper_cpp = check_whisper_cpp()
    mlx_whisper = check_mlx_whisper()
    check: Check = {
        "ok": whisper_cpp["ok"] or mlx_whisper["ok"],
        "whisper_cpp": whisper_cpp,
        "mlx_whisper": mlx_whisper,
    }
    if not check["ok"]:
        check["error"] = "no ASR backend available (whisper.cpp or mlx-whisper)"
    return check


def check_deepfilternet() -> Check:
    """Check DeepFilterNet, which audio-fix can do without (REQ-021)."""
    path = next(
        (found for name in DEEPFILTERNET_BINARIES if (found := shutil.which(name))),
        None,
    )
    if path is None:
        return {
            "ok": False,
            "optional": True,
            "fallback": DEEPFILTERNET_FALLBACK,
            "error": f"none of {', '.join(DEEPFILTERNET_BINARIES)} found in PATH",
        }
    return {"ok": True, "optional": True, "path": path}


def check_sudachipy() -> Check:
    """Check SudachiPy by analysing one word with an installed dictionary.

    A dictionary that is present but unreadable fails exactly like a missing
    one, which is why the check tokenises rather than looking for files
    (REQ-005).
    """
    try:
        from sudachipy import Dictionary  # noqa: PLC0415 — heavy optional import
    except ImportError as exc:
        return {"ok": False, "error": f"sudachipy is not installed: {exc}"}
    failures = []
    for name in SUDACHI_DICTS:
        try:
            tokenizer = Dictionary(dict=name).create()
            if list(tokenizer.tokenize(SUDACHI_PROBE_WORD)):
                return {"ok": True, "dict": name}
            failures.append(f"{name}: analysed nothing")
        except Exception as exc:
            failures.append(f"{name}: {exc}")
    return {"ok": False, "error": "no usable dictionary (" + "; ".join(failures) + ")"}


#: Check name -> the function that performs it, in the order they are reported.
CHECKS = {
    "ffmpeg": check_ffmpeg,
    "ffprobe": check_ffprobe,
    "auto_editor": check_auto_editor,
    "asr": check_asr,
    "deepfilternet": check_deepfilternet,
    "sudachipy": check_sudachipy,
}


def diagnose() -> Report:
    """Run every check and collect the required ones that failed."""
    checks = {name: check() for name, check in CHECKS.items()}
    missing = tuple(name for name in REQUIRED_CHECKS if not checks[name]["ok"])
    return Report(checks=checks, missing=missing)


def _asr_backends(check: Check) -> list[str]:
    """Return one label per working ASR backend."""
    labels = []
    whisper_cpp = check["whisper_cpp"]
    if whisper_cpp["ok"]:
        labels.append(f"whisper.cpp ({', '.join(whisper_cpp['models'])})")
    if check["mlx_whisper"]["ok"]:
        labels.append("mlx-whisper")
    return labels


#: How a satisfied check introduces itself.
_SATISFIED: dict[str, Callable[[Check], str]] = {
    "ffmpeg": lambda check: f"ffmpeg {check['version']} (libass: yes)",
    "ffprobe": lambda check: f"ffprobe {check['version']}",
    "auto_editor": lambda check: f"auto-editor {check['version']} (--export v3: yes)",
    "asr": lambda check: f"asr: {', '.join(_asr_backends(check))}",
    "deepfilternet": lambda check: f"deepfilternet: {check['path']}",
    "sudachipy": lambda check: f"sudachipy: {check['dict']} dictionary OK",
}

#: What to do about a check that failed — the point of running doctor at all.
_REMEDIES = {
    "ffmpeg": "install an ffmpeg built with libass, e.g. `brew install ffmpeg`",
    "ffprobe": "install ffmpeg, e.g. `brew install ffmpeg`",
    "auto_editor": "`uv tool install auto-editor`",
    "asr": (
        "`brew install whisper-cpp` plus a ggml model in "
        f"{DEFAULT_WHISPER_MODEL_DIR} (or ${WHISPER_MODEL_DIR_ENV}), "
        "or `uv sync --group asr` for mlx-whisper"
    ),
    "deepfilternet": (
        f"optional: install DeepFilterNet, or accept the {DEEPFILTERNET_FALLBACK} "
        "fallback in audio-fix"
    ),
    "sudachipy": "`uv pip install sudachidict_core`",
}


def _describe(name: str, check: Check) -> str:
    """Return the human-readable detail line for one finished check."""
    if check["ok"]:
        return _SATISFIED[name](check)
    reason = check.get("error", "unavailable")
    return f"{name}: {reason} → {_REMEDIES[name]}"


def summary_lines(report: Report) -> list[str]:
    """Render the report for a human, one line per check plus a verdict."""
    lines = []
    for name, check in report.checks.items():
        ok = check["ok"]
        mark = "✔" if ok else ("⚠" if check.get("optional") else "✖")
        lines.append(f"{mark} {_describe(name, check)}")
    if report.missing:
        lines.append(f"✖ missing required dependencies: {', '.join(report.missing)}")
    return lines
