---
name: writing-cli-commands
description: >
  Covers src/vidprep/cli.py — the typer app, the module-level Annotated option
  aliases (ProjectOption, JsonOption, DryRunOption, PrepProjectOption and the
  rest), the CommonOptions/Output dataclasses, the action()/_prepare()/_run()
  closure idiom every command follows, the --json-on-stdout convention _log()
  enforces, the ✔/⚠/✖/▶/· line-prefix vocabulary, and main()'s exit-code
  mapping. Use when adding a new @app.command(), wiring a --dry-run plan,
  deciding what a command should print on stdout versus stderr, or touching
  _prepare, _run, _log, or _parameter_error_reporter.
metadata:
  platforms: claude-code, codex
---

# Writing CLI Commands

**Owns:** `cli.py` — the typer app structure, the `Annotated` reusable option aliases,
`CommonOptions`/`Output`, the `action()` closure idiom every command follows,
`_prepare`/`_run`/`_log`, the `--json`-on-stdout-with-humans-on-stderr convention, the
`✔`/`⚠`/`✖`/`▶`/`·` line-prefix vocabulary, and `main()`'s exit-code mapping. **Does not
own:** the exception hierarchy and exit-code values themselves (`designing-errors`);
what each stage module actually does (`processing-audio`, `transcribing-speech`,
`correcting-transcripts`, `detecting-cuts`, `rendering-output`, `verifying-renders`);
loading and staleness of the project directory (`managing-the-project-dir`).

## Nine commands, one shape, one closure

`app = typer.Typer(name="vidprep", help="Semi-automated preprocessing for YouTube
videos.", no_args_is_help=True, add_completion=False)`. The subcommands, in file
order, are `init`, `doctor`, `audio_fix` (registered as `@app.command(name="audio-fix")`
so the CLI verb keeps the hyphen typer would otherwise drop), `transcribe`, `detect`,
`correct`, `render`, `report`, `prep`. Every one — `init` included — builds a
`CommonOptions`, defines a local `def action() -> Output:` closure, and calls
`_run(options, action)`; six of the nine (`init`, `audio_fix`, `transcribe`, `detect`,
`correct`, `report`) end their function on that call, but `doctor`, `render`, and `prep`
each add a post-`_run` exit-code check of their own — `doctor` raises
`typer.Exit(EXIT_VALIDATION)` when `output.result["missing"]` is non-empty, and `render`
and `prep` each do the same when `output.result`'s (or `prep`'s nested `render`
result's) `verify_asr` payload reports `mode == verify_module.GATE` with non-empty
`gating_flags` — the gate condition `verifying-renders` owns. `detect`'s `action()` is
the cleanest instance of the closure shape itself:
`loaded, stale = _prepare(detect_module.STAGE, options)`, then, on `options.dry_run`,
`plan = detect_module.plan(loaded); return Output(plan, [*stale, *_plan_lines(plan)])`,
otherwise `result = detect_module.run_detect(loaded); return Output(result.to_dict(),
[*stale, *result.lines()])`. `options.dry_run` branches to the stage module's `plan()`
instead of its `run_<stage>()`; both results end up wrapped in the same `Output`, so
`_run` never has to know which path was taken.

## `CommonOptions` and `Output` are the two shapes every command trades in

Both are `@dataclass(frozen=True, slots=True)` (the dataclass convention itself is
`writing-python`'s, not repeated here). `CommonOptions` packs the three flags every
subcommand accepts: `project: Path | None`, `json_output: bool`, `dry_run: bool`.
`Output` is what an `action()` returns: `result: dict[str, Any]` — the machine payload
— plus `lines: Sequence[str] = ()`, the human-readable lines to print alongside it.

## The `Annotated` aliases exist to give one flag two different help strings

Near the top of the file, `ProjectOption`, `JsonOption`, `DryRunOption`, `StatsOption`,
`PatchOption`, `DictOption`, `YesOption`, `NoWrapOption`, `PreviewOption`,
`VerifyAsrOption`, `CutsOption`, `PrepProjectOption`, `SkipProofreadingOption`,
`KeepFillersOption`, `PrepVerifyAsrOption` are module-level `Annotated[T,
typer.Option(...)]` aliases, reused verbatim across the commands that share a flag.
`PrepProjectOption` duplicates `ProjectOption`'s `--project`/`-p` only because its
default differs by context — "Project directory (default: current)" versus "(default:
<video>.vidprep)" — and `SkipProofreadingOption` duplicates `YesOption`'s `--yes` only
because `prep`'s help text ("Do not stop for LLM proofreading of the transcript.")
differs from `correct`'s ("Apply the patch without asking for confirmation."). Same
flag, same type, different help string to carry — that is what earns a near-duplicate
alias its own name; when adding a flag whose wording needs to differ per command,
follow this pattern rather than reusing an existing alias with generic help text.

## `_prepare(stage, options)` is the mandatory first line of every non-dry action

`_prepare(stage: str, options: CommonOptions) -> tuple[Project, list[str]]` calls
`project_module.load_project(options.project)`, `verify_source(loaded)`,
`validate_artifacts(loaded)`, and `stale_upstream_warnings(loaded, stage)`, then
returns the loaded `Project` alongside the warnings pre-formatted as `f"⚠ {warning}"`
strings ready to splice into `Output.lines`. What `load_project`, `verify_source`,
`validate_artifacts`, and `stale_upstream_warnings` actually check belongs to
`managing-the-project-dir` — here it only matters that every stage command calls
`_prepare` before doing any work, so a replaced source file or a corrupted artifact is
caught before a single ffmpeg process spawns.

## A real inconsistency: `audio_fix` passes a literal string where every sibling passes a constant

`audio.py` defines `STAGE = "audio_fix"` at module scope, exactly like `transcribe.py`
(`STAGE = "transcribe"`), `detect.py`, `correct.py`, `render.py`, and `report.py`. Every
one of those five commands calls `_prepare(<module>.STAGE, options)` — `detect` calls
`_prepare(detect_module.STAGE, options)`, `render` calls
`_prepare(render_module.STAGE, options)`, and so on. `audio_fix` is the odd one out:
its `action()` calls `_prepare("audio_fix", options)` with the bare string, even though
`audio_module.STAGE` holds the identical value. Nothing currently breaks because the
literal happens to match the constant, but a rename of the stage identifier would
silently desync them. When adding a new command, use `<module>.STAGE`, not a literal —
`audio_fix` is the one place in the file not to copy from.

## `_run` is the exception funnel; `_log` is the stdout/stderr split

`_run(options, action)` calls `action()`, and on `except VidprepError as exc:` prints
either `exc.payload()` as JSON (when `options.json_output`) or `f"✖ {exc}"` to stderr,
then `raise typer.Exit(exc.exit_code) from exc`. On success it walks `output.lines`
through `_log`, then — only in `--json` mode — prints `json.dumps(output.result,
ensure_ascii=False)` on stdout as the final line. The exit-code values and which
`VidprepError` subclass maps to which are `designing-errors`' territory; here what
matters is the mechanics of the catch and the two print destinations.

`_log(message, options)` is one line: `typer.echo(message, err=options.json_output)`.
When `--json` is active every human-readable line — warnings, progress, per-segment
results — goes to stderr, so stdout carries nothing but the one final JSON document.
That split is what makes `vidprep detect --json | jq` work: piping stdout never has to
filter out log noise, because there isn't any there.

## The glyph vocabulary: `✔ ⚠ ✖` in `cli.py`, `▶  ·` in `prep.py`

`cli.py` emits three prefixes: `✔ ` for a completed step (`init`'s `f"✔ ffprobe:
{specs.duration:.2f}s / ..."`), `⚠ ` for a warning (`_prepare`'s
`stale_upstream_warnings`, and `audio_fix`'s/`report`'s dry-run `plan["warnings"]`),
and `✖ ` for a failure, printed only inside `_run`'s exception branch. `prep.py` adds
two more, both with a two-space gap after the glyph, in `_due`: `▶  {stage}` when a
stage is about to run, `·  {stage}: up to date` when `_needs_run` says it can be
skipped. A stage's own output lines are re-indented three spaces (`f"   {line}"`) by
`_done`, so a `prep` transcript visually nests each stage's results under its header.

## `--dry-run` renders a plan dict through `_plan_lines`

`_plan_lines(plan: dict[str, Any]) -> list[str]` opens with `"dry-run: nothing was
written"`, then renders `plan["commands"]` (a list of argv lists) as `f"  would run:
{' '.join(command)}"` lines and `plan["writes"]` (a list of paths) as `f"  would write:
{target}"` lines. Every stage module's `plan()` returns exactly that
`"commands"`/`"writes"` shape for this to consume — a command's dry-run branch just
calls its module's `plan()` and passes the result straight to `_plan_lines`, as
`audio_fix`, `transcribe`, `detect`, `render`, and `report` all do.

## Two commands break the closure idiom, each for its own reason

Most commands' `action()` returns straight into `_run`, which does all the logging
through `Output.lines`. `correct`'s `action()` instead calls `_log` directly mid-body —
`for line in [*stale, *plan.lines(verbose=options.dry_run)]: _log(line, options)` — so
the diff summary prints *before* the confirmation prompt that follows it:
`typer.confirm(f"Apply {len(plan.changes)} changes?", abort=True,
err=options.json_output)`, gated on `apply_patch is not None and not yes`. No other
command calls `typer.confirm`, because no other command mutates a document
(`transcript.json`) that a patch could get wrong — the interactive "are you sure?"
only exists here.

`prep` breaks the idiom differently: its `action()` passes `run_prep` a
`lambda line: _log(line, options)` callback that fires once per stage as it completes
(`prep.py`'s own docstring: the callback exists "rather than batching it behind the
finished result the way a single-stage command does"), then returns
`Output(result.to_dict(), ())` — an empty `lines` tuple. All of `prep`'s progress
output reaches the user through that callback, never through `_run`'s `Output.lines`
walk at all; `correct` only reorders when `_log` runs relative to `_run`, while `prep`
bypasses `Output.lines` for progress output entirely.

## `main()` recognises a parsing error by shape, to override click's own exit code

`main()` runs `app(standalone_mode=False)` so typer never calls `sys.exit` itself, then
catches `typer.Abort` (raised by `correct`'s `typer.confirm(..., abort=True)`) into
`SystemExit(EXIT_USAGE)`, and falls back to `_parameter_error_reporter(error)` for
anything else typer/click raised while parsing: `show = getattr(error, "show", None);
if callable(show) and isinstance(getattr(error, "exit_code", None), int): return
cast("Callable[[], None]", show)`. The shape check exists because typer vendors its own
copy of click's exception types and which one gets raised depends on the installed
version — matching a callable `show` plus an `int` `exit_code` is the only stable thing
across versions. This is why `main()` needs the indirection at all: click's native
parse-error path exits `2` on its own and never touches a `VidprepError`, so nothing in
`_run`'s funnel would catch it — `main()` intercepts it separately and forces
`SystemExit(EXIT_USAGE)` so a bad flag and a broken stage never share an exit code. The
exit-code vocabulary itself is `designing-errors`' territory.
