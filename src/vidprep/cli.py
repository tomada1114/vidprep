"""The ``vidprep`` command line application (design.md §6).

Commands stay thin: they parse the three common flags, run a preamble that
proves the project is intact, and delegate. Stages that are not built yet are
present as skeleton subcommands so the interface — and the checks that guard
it — exist from the start.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import typer

from . import project as project_module
from .errors import (
    EXIT_USAGE,
    StageNotImplementedError,
    UsageError,
    VidprepError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .project import Project

app = typer.Typer(
    name="vidprep",
    help="Semi-automated preprocessing for YouTube videos.",
    no_args_is_help=True,
    add_completion=False,
)

ProjectOption = Annotated[
    Path | None,
    typer.Option("--project", "-p", help="Project directory (default: current)."),
]
JsonOption = Annotated[
    bool,
    typer.Option("--json", help="Print the result as JSON on stdout, logs on stderr."),
]
DryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Show the execution plan without writing anything."),
]

#: Subcommand name -> stage key in the manifest, for stages not built yet.
PENDING_STAGES = {
    "audio-fix": "audio_fix",
    "transcribe": "transcribe",
    "correct": "correct",
    "detect": "detect",
    "render": "render",
    "report": "report",
}


@dataclass(frozen=True, slots=True)
class CommonOptions:
    """The three flags every subcommand accepts."""

    project: Path | None = None
    json_output: bool = False
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class Output:
    """What a command produced: a machine result plus human-readable lines."""

    result: dict[str, Any]
    lines: Sequence[str] = ()


def _log(message: str, options: CommonOptions) -> None:
    """Print a human-readable line, keeping stdout clean in ``--json`` mode."""
    typer.echo(message, err=options.json_output)


def _run(options: CommonOptions, action: Callable[[], Output]) -> None:
    """Execute *action*, then report its result on the right stream.

    Raises:
        typer.Exit: With the exit code that matches the failure (design.md §6).
    """
    try:
        output = action()
    except VidprepError as exc:
        if options.json_output:
            typer.echo(json.dumps({"error": exc.code, "detail": str(exc)}))
        else:
            typer.echo(f"✖ {exc}", err=True)
        raise typer.Exit(exc.exit_code) from exc
    for line in output.lines:
        _log(line, options)
    if options.json_output:
        typer.echo(json.dumps(output.result, ensure_ascii=False))


def _prepare(stage: str, options: CommonOptions) -> tuple[Project, list[str]]:
    """Load the project and verify everything a stage depends on.

    Every stage starts here so that a replaced source file or a corrupted
    artifact is caught before any work begins.

    Returns:
        The loaded project and the warnings that should be shown to the user.
    """
    loaded = project_module.load_project(options.project)
    project_module.verify_source(loaded)
    project_module.validate_artifacts(loaded)
    warnings = project_module.stale_upstream_warnings(loaded, stage)
    return loaded, [f"⚠ {warning}" for warning in warnings]


@app.command()
def init(  # noqa: PLR0913 — one parameter per CLI flag is typer's contract
    directory: Annotated[
        Path | None,
        typer.Argument(help="Directory to create (default: --project, else current)."),
    ] = None,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Video file to process. Never written to."),
    ] = None,
    copy_source: Annotated[
        bool,
        typer.Option("--copy-source", help="Import the material into the project."),
    ] = False,
    project: ProjectOption = None,
    json_output: JsonOption = False,
    dry_run: DryRunOption = False,
) -> None:
    """Create a project directory for a source video."""
    options = CommonOptions(project, json_output, dry_run)
    target = directory or project or Path.cwd()

    def action() -> Output:
        if source is None:
            msg = "init requires --source <video>"
            raise UsageError(msg)
        if options.dry_run:
            plan = project_module.init_plan(target, source, copy_source=copy_source)
            return Output(plan, _plan_lines(plan))
        created = project_module.init_project(target, source, copy_source=copy_source)
        manifest = created.manifest
        specs = manifest.source
        return Output(
            {
                "project": str(created.root),
                "manifest": manifest.model_dump(mode="json"),
            },
            [
                f"✔ ffprobe: {specs.duration:.2f}s / {specs.video.width}x"
                f"{specs.video.height} {specs.video.fps} / {specs.audio.codec} "
                f"{specs.audio.sample_rate}Hz {specs.audio.channels}ch",
                f"✔ sha256: {specs.sha256}",
                f"✔ created {created.root / project_module.MANIFEST_NAME}, "
                f"{created.root / project_module.PROFILE_NAME}",
            ],
        )

    _run(options, action)


def _plan_lines(plan: dict[str, Any]) -> list[str]:
    """Render a dry-run plan as the commands and writes it would perform."""
    lines = ["dry-run: nothing was written"]
    lines += [f"  would run: {' '.join(command)}" for command in plan["commands"]]
    lines += [f"  would write: {target}" for target in plan["writes"]]
    return lines


@app.command()
def doctor(
    project: ProjectOption = None,
    json_output: JsonOption = False,
    dry_run: DryRunOption = False,
) -> None:
    """Check that the external tools vidprep needs are installed."""
    options = CommonOptions(project, json_output, dry_run)

    def action() -> Output:
        msg = "doctor is not implemented yet"
        raise StageNotImplementedError(msg)

    _run(options, action)


def _pending_command(name: str, summary: str) -> None:
    """Register a subcommand that validates the project but does no work yet."""
    stage = PENDING_STAGES[name]

    def command(
        project: ProjectOption = None,
        json_output: JsonOption = False,
        dry_run: DryRunOption = False,
    ) -> None:
        options = CommonOptions(project, json_output, dry_run)

        def action() -> Output:
            _, warnings = _prepare(stage, options)
            for warning in warnings:
                _log(warning, options)
            msg = f"{name} is not implemented yet"
            raise StageNotImplementedError(msg)

        _run(options, action)

    command.__doc__ = summary
    app.command(name=name)(command)


_pending_command("audio-fix", "Denoise, high-pass and loudness-normalise the audio.")
_pending_command("transcribe", "Transcribe the processed audio with VAD + ASR.")
_pending_command("correct", "Apply the misconversion dictionary and LLM patches.")
_pending_command("detect", "Detect silence and filler words as cut candidates.")
_pending_command("render", "Apply approved cuts and write the output video.")
_pending_command("report", "Regenerate statistics, waveforms and the cut digest.")


def _parameter_error_reporter(error: Exception) -> Callable[[], None] | None:
    """Return the reporter of a command-line parsing error, or ``None``.

    Typer ships its own copy of click's exception classes, and which copy the
    parser raises depends on the installed version, so these errors are
    recognised by shape (``show()`` plus ``exit_code``) rather than by class.
    """
    show = getattr(error, "show", None)
    if callable(show) and isinstance(getattr(error, "exit_code", None), int):
        return cast("Callable[[], None]", show)
    return None


def main() -> None:
    """Console entry point mapping every outcome onto design.md §6 exit codes.

    Command-line parsing errors exit ``1`` rather than click's default ``2``,
    which vidprep reserves for a stage that failed while running.

    Raises:
        SystemExit: Always; its code is the exit status of the command.
    """
    try:
        result = app(standalone_mode=False)
    except typer.Abort:
        typer.echo("Aborted.", err=True)
        raise SystemExit(EXIT_USAGE) from None
    except Exception as error:
        report = _parameter_error_reporter(error)
        if report is None:
            raise
        if str(error):  # empty for "no arguments", whose help is already shown
            report()
        raise SystemExit(EXIT_USAGE) from None
    raise SystemExit(result if isinstance(result, int) else 0)
