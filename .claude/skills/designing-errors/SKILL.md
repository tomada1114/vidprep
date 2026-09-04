---
name: designing-errors
description: >
  Covers the VidprepError exception hierarchy in src/vidprep/errors.py, the
  exit_code/code ClassVars and payload() method, the 0/1/2/3 exit-code
  vocabulary, cli.py's _run funnel that turns a raised VidprepError into a
  process exit, and the gap where a bare ValueError (timeline.py) or
  IndexError (_retranscribe.py) can escape that funnel unmapped. Use when
  adding a new failure mode, deciding whether it needs a VidprepError
  subclass, wiring a --json payload, or touching cli.py's exception handling
  around _run, main, or a command that raises typer.Exit(EXIT_VALIDATION)
  directly.
metadata:
  platforms: claude-code, codex
---

# Designing Errors

**Owns:** the `VidprepError` exception hierarchy in `src/vidprep/errors.py`, the
`exit_code`/`code` ClassVars and `payload()` method, the 0/1/2/3 exit-code vocabulary,
`cli.py`'s `_run` exception-to-exit-code funnel, when a new subclass earns its place,
and the gap where builtin exceptions can escape without being mapped to an exit code.
**Does not own:** general module style (`writing-python`); the CLI's own command shape
and option handling (`writing-cli-commands`); what is exported at the package level
(`public-api-contract`).

## The hierarchy maps directly onto the four exit codes

`errors.py` defines `EXIT_OK = 0`, `EXIT_USAGE = 1`, `EXIT_EXECUTION = 2`,
`EXIT_VALIDATION = 3`. Every `VidprepError` subclass fixes both an `exit_code:
ClassVar[int]` and a `code: ClassVar[str]` — `code` is the stable string `--json`
prints under `"error"`. Three tiers exist, one per exit code above 0: `UsageError`
(1, `code="usage"`) for a wrong invocation or unready environment, with
`NotAProjectError` (`"not_a_project"`) under it; `ExecutionFailedError` (2,
`code="exec_failed"`) for a stage that started but could not finish, with
`FfmpegError` (`"ffmpeg_failed"`), `AsrFailedError` (`"asr_failed"`) and
`TimelineSchemaError` (`"timeline_schema"`) under it; and five siblings at exit 3 for
a verification failure that discards the work rather than publishing it —
`SchemaInvalidError`, `InvariantViolationError`, `PatchInvalidError`,
`TelopInvalidError`, `HashMismatchError`. `VidprepError` itself defaults to
`exit_code=EXIT_EXECUTION`, `code="error"`, so a bare `VidprepError(...)` is a safe
fallback but still climbs to exit 2, never 0.

## `payload()` is what `--json` prints — two classes carry a list of complaints

The base `payload()` returns `{"error": self.code, "detail": str(self)}`.
`PatchInvalidError` and `TelopInvalidError` both take `details: Sequence[str]` in
`__init__`, join them with `"; "` for `str(self)`, and keep the original list on
`self.details`; their `payload()` overrides return `{"error": self.code, "detail":
self.details, ...}` — `PatchInvalidError` adds `"applied": 0`, `TelopInvalidError`
does not. Both exist because their source (`patch.json`, `telops.json`) is written by
a language model or by hand, and reporting one complaint per round trip would mean
one rerun per mistake — the docstrings on both classes say this explicitly. A
subclass earns its place the same way: when a caller (a script parsing `--json`, or
`cli.py` itself) needs to branch on `code` or needs a shape `payload()`'s default
does not give it. Reusing `SchemaInvalidError` or `InvariantViolationError` is
correct when nothing downstream distinguishes the new failure from an existing one.

## `cli.py`'s `_run` is the one funnel, and only for raised `VidprepError`

Every subcommand builds an `action: Callable[[], Output]` and hands it to `_run`,
which does the actual dispatch: `except VidprepError as exc:` prints `exc.payload()`
as JSON on `--json` or `f"✖ {exc}"` to stderr otherwise, then `raise
typer.Exit(exc.exit_code) from exc`. Nothing else is caught here — a `ValueError` or
any other builtin exception raised inside `action` propagates straight past `_run`
uncaught.

## `main()` handles two more cases outside `_run`, by shape, not by class

`typer.Abort` (raised by `typer.confirm(..., abort=True)` in `correct`) becomes
`SystemExit(EXIT_USAGE)`. Click/typer parsing errors are recognised structurally by
`_parameter_error_reporter`, which checks for a callable `show` attribute and an
`int` `exit_code` attribute rather than an exact class — typer vendors its own copy
of click's exception types, and which one is raised depends on the installed
version. `main()` deliberately overrides click's own default exit code of 2 for
these, forcing `SystemExit(EXIT_USAGE)` so a bad flag and a broken stage never share
an exit code.

## Some commands raise `typer.Exit(EXIT_VALIDATION)` directly, outside `VidprepError`

`doctor` runs `_run` for its report, then separately checks `if
output.result["missing"]: raise typer.Exit(EXIT_VALIDATION)` — the report is always
printed, and the exit code is a verdict about the *result*, not an exception.
`render` and `prep` do the same after their own `_run` call: both inspect
`output.result["verify_asr"]` (via `output.result.get("render")` first, for `prep`)
and raise `typer.Exit(EXIT_VALIDATION)` only when `verified["mode"] ==
verify_module.GATE and verified["gating_flags"]`. None of these three go through a
`VidprepError` — there is nothing to catch, because nothing failed; the check just
found something worth reporting.

## The real gap: `timeline.py` raises bare `ValueError`, and `render` has no net under it

`Timeline.__init__` and the `normalize_cuts`/`_check_interval` helpers it calls raise
plain `ValueError` for a non-positive duration or an invalid interval — not a
`VidprepError` subclass — even though `Timeline` is exported public API (`from
.timeline import Timeline` in `__init__.py`, listed in `__all__`). `render.py`'s
`_timeline()` constructs `Timeline(...)` with no surrounding `try`/`except`, and
`cli.py`'s `render` command's `action` has no `except Exception` either — only
`prep`'s does (below). So a `ValueError` out of `Timeline()` sails past `_run`'s
`except VidprepError`, past `main()`'s shape check (a `ValueError` has neither `show`
nor `exit_code`), and comes out as an unhandled Python traceback instead of a clean
`--json` payload or a `✖ ` line. `_retranscribe.py` has the same shape of gap:
`ExpectedText.locate` raises bare `IndexError` when its expected text is empty, and
another path raises bare `ValueError`, both uncaught within that module. `_asr.py`'s
`_read_segment` raises a bare `TypeError` too, but that one is *not* a gap — its
caller wraps the call a few lines up in `except (KeyError, TypeError, ValueError) as
exc: raise AsrFailedError(...) from exc`, which is the pattern the other two should
be following instead of raising bare.

## `prep`'s command has a blanket safety net; a single-stage command usually does not

`cli.py`'s `prep` action wraps `prep_module.run_prep(...)` in `except VidprepError:
raise` followed by `except Exception as error: raise
ExecutionFailedError(f"{type(error).__name__}: {error}") from error`, with the
comment "vidprep does not model, which still needs a mapped exit code" — a blanket
net specifically because `prep` composes every stage and cannot enumerate every way
one of them can fail. This is exactly why `render` alone raising a bare `ValueError`
out of `Timeline()` is a real bug and not just a `prep`-only edge case: `prep`
calling that same code path is protected, but the equivalent single-stage command is
not. When a new failure mode is reachable from a single-stage command — not only
from `prep` — it needs a `VidprepError` subclass raised at the point of failure;
leaning on `prep`'s net does not help `render`, `correct`, or any other command run
alone.

## No `logging` module anywhere in `src/vidprep/` — failures become warning strings

`report.py` and `_boundaries.py` each define `_RECOVERABLE = (ExecutionFailedError,
UsageError)` and catch that tuple at their measurement/detection call sites, turning
the exception into a warning line returned alongside `None` or an empty result
rather than logging it — e.g. `report.py`'s `_measure` returns `(None,
[f"{path.name} could not be measured ({exc})"])` on `except _RECOVERABLE as exc`.
This is the real convention where recovery is possible: catch a narrow, named tuple
of `VidprepError` types, and surface the message to the user through `Output.lines`,
never through a log call.

## Two bare `except Exception:` blocks are deliberate, not swallows

`_dictionary.py`'s `_open_tokenizer` catches bare `Exception` and returns `None`,
with the docstring stating why: "every failure means the same thing to the caller —
try the next flavour — so they are not told apart here; `doctor` is what explains
them." `doctor.py`'s Sudachi dictionary probe instead catches `Exception as exc` and
appends `f"{name}: {exc}"` to a `failures` list that becomes the final `"error"`
string — nothing is discarded, every attempt's reason is kept and reported. Never
widen a narrower `except` to bare `Exception` without one of these two
justifications written down next to it — the tuple form (`_RECOVERABLE`, or
`(KeyError, TypeError, ValueError)` in `_asr.py`) is preferred whenever the failure
modes are actually known.
