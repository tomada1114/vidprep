---
name: running-subprocesses
description: >
  Covers the process-execution boundary in vidprep: src/vidprep/_ffmpeg.py's
  run/run_analysis wrappers around subprocess.run, doctor.py's second,
  sanctioned spawner and why it cannot reuse _ffmpeg's error-raising path,
  shutil.which PATH resolution, DEFAULT_TIMEOUT_SECONDS and
  COMMAND_TIMEOUT_SECONDS, the `# noqa: S603` justification, the --dry-run
  `{"commands", "writes"}` plan shape cli.py's _plan_lines renders, and
  tests/test_project.py's TestSubprocessIsolation.SPAWNERS check. Use when
  adding a new external tool invocation, wiring a stage module's plan()
  function, deciding whether new code may import subprocess, or touching
  _ffmpeg.py's timeout or stderr-tail handling.
metadata:
  platforms: claude-code, codex
---

# Running Subprocesses

**Owns:** the process-execution boundary — `_ffmpeg.run`/`run_analysis` as the one
sanctioned execution path, why `doctor.py` is a second, deliberate spawner,
`shutil.which` resolution, timeouts, the `# noqa: S603` justification, the
`--dry-run` `{"commands", "writes"}` plan shape, and
`tests/test_project.py`'s `TestSubprocessIsolation` check. **Does not own:** what
each stage's actual command line means or which flags it passes
(`processing-audio`, `transcribing-speech`, `detecting-cuts`, `rendering-output`);
how a pytest test fakes this boundary (`writing-tests`).

## Two spawners are sanctioned, not one — AGENTS.md's module-tree comment is stale

`_ffmpeg.py`'s own module docstring says it is "the only module allowed to spawn
ffmpeg / ffprobe subprocesses" — note the qualifier is *ffmpeg / ffprobe*, not
*subprocesses in general*. `AGENTS.md`'s module-tree diagram currently reads
`_ffmpeg.py       # The only module allowed to spawn subprocesses`, which drops
that qualifier and is being corrected elsewhere in this effort to name two
spawners. The accurate rule, enforced by
`tests/test_project.py::TestSubprocessIsolation.test_no_module_but_the_wrapper_imports_subprocess`,
is: `SPAWNERS = ("_ffmpeg.py", "doctor.py")`, and the test walks every `*.py`
under `SRC_ROOT`, asserting `offenders == []` where `offenders` is any file
outside `SPAWNERS` whose text contains the string `"subprocess"`. The class
docstring names the requirement directly: "REQ-030: pipeline stages spawn
processes only through the wrapper." Reject in review: a stage module reaching
for `subprocess` directly instead of routing through `_ffmpeg.run`.

## `doctor.py` is the second spawner because it cannot afford to raise

`_ffmpeg._execute` turns a non-zero exit, a timeout, or a missing binary into a
raised `UsageError`/`ExecutionFailedError`/`FfmpegError` — exactly wrong for
`doctor`, whose entire job is to report "this binary is broken or missing" as
data rather than crash on the first broken check. `doctor.py`'s own module
docstring states this: "Every check answers one question — 'can the pipeline
rely on this?' — and records the answer instead of raising, so a broken
environment is reported in full rather than one failure at a time." Its
`_run_command` wraps `subprocess.run` directly, catching `subprocess.TimeoutExpired`
and `OSError` and returning a `_Completed(False, error=...)` instead of raising,
with a docstring that reinforces the same point: "A doctor that dies on a broken
binary is useless, so every failure mode — missing, not executable, hanging,
exiting non-zero — comes back as data (REQ-022)." `TestSubprocessIsolation`'s
`SPAWNERS` comment says the same thing in one line: "`doctor` probes external
tools that are not ffmpeg — and must record a failure rather than raise on
one — so it spawns its own processes."

## `_ffmpeg._execute`'s `subprocess.run` call is the one with the `noqa`

`_execute` calls `subprocess.run(command, capture_output=True, text=True,
check=False, timeout=timeout)  # noqa: S603`, directly preceded by the comment
"Fixed argument vector, shell=False: no interpolation into a shell." —
`doctor._run_command` carries the identical comment over its own call, reusing
the wording because the reasoning is the same call shape: both build a `list[str]`
argument vector from constants and resolved paths, never from a shell string, so
bandit's S603 (subprocess call without shell equals true, check for execution of
untrusted input) is a false positive for both, argued explicitly rather than
suppressed blind.

## `run()` returns stdout, `run_analysis()` returns stderr — and `run()` covers more than ffmpeg

`_ffmpeg.run` is for commands whose job is to produce a file, or for ffprobe to
print one value; its docstring extends the boundary beyond ffmpeg itself: "the
sibling audio tools vidprep shells out to — such as DeepFilterNet — go through
here too, so no stage module owns a subprocess." `run_analysis` exists because
ffmpeg filters that report numbers — `loudnorm`, `silencedetect`, `astats` —
"print them to stderr along with the rest of the log, so an analysis pass is
read from there rather than from stdout," per its own docstring. Both are thin
wrappers over the private `_execute`, which does the actual `subprocess.run` call
and raises on failure; nothing outside `_ffmpeg.py` calls `_execute` directly.

## `shutil.which` is PATH lookup, not execution — it is fine to call from a third place

Resolving *where* a binary lives leaks outside the two spawners in a way that is
deliberate, not a gap: `grep -n "shutil.which" src/vidprep/*.py` finds it in
`doctor.py` (three call sites — the generic `_check_version` lookup shared by
the ffmpeg/ffprobe/auto-editor checks, plus the whisper.cpp and DeepFilterNet
binary checks) and in `_asr.py::resolve`, which looks up a whisper.cpp
binary from `doctor.WHISPER_BINARIES` and, when the backend is mlx-whisper, the
`MLX_BINARY` name — raising `UsageError` itself when neither is found, before
any subprocess would ever be started. The distinction that keeps this from
violating `TestSubprocessIsolation`: locating a path on disk touches no process
and needs no wrapper; only the act of executing that path is confined to
`_ffmpeg.py` and `doctor.py`.

## Timeouts turn a hung subprocess into a reported failure, not a bound on normal runtime

`_ffmpeg.DEFAULT_TIMEOUT_SECONDS = 3600.0` carries the comment "A full transcode
of a long recording is legitimately slow, so the timeout is only there to turn a
hung subprocess into a reported failure." `doctor.COMMAND_TIMEOUT_SECONDS = 5.0`
is two orders of magnitude shorter, with its own comment explaining why the
gap is intentional rather than an oversight: "External commands are probed, not
used for work, so they must answer fast." Both feed a `timeout=` kwarg on
`subprocess.run` and both convert `subprocess.TimeoutExpired` into a non-raising
or mapped result — `_execute` raises `ExecutionFailedError`, `_run_command`
returns `_Completed(False, error=f"timed out after {COMMAND_TIMEOUT_SECONDS:g}s")`.

## `--dry-run` walks the same command-building code without calling `_execute`

A stage module's `plan()` function — e.g. `audio.plan`, `detect.plan`,
`render.plan` — constructs the identical argument vectors `_ffmpeg.run` would
receive (`audio.plan` builds `chain.extract_command(...)`, `chain.denoise_command(...)`,
etc., the same helpers `run_audio_fix` calls) and returns a `dict[str, Any]`
shaped with a `"commands"` list of argument vectors and a `"writes"` list of
target paths, never invoking `_execute`. `cli.py`'s `_plan_lines(plan)` renders
that dict for a human: `"dry-run: nothing was written"` followed by
`f"  would run: {' '.join(command)}"` per entry in `plan["commands"]` and
`f"  would write: {target}"` per entry in `plan["writes"]`. This is how a stage
answers "what ffmpeg command would run" without spawning anything — the plan is
data built from the same command-construction functions the real run uses, not
a separate hand-maintained description of them.

## Errors raised at this boundary

`_execute` raises `UsageError` when the executable is not on PATH,
`ExecutionFailedError` when the command exceeds its timeout, and `FfmpegError`
when it exits non-zero — the message carries `completed.stderr.strip()[-STDERR_TAIL_CHARS:]`,
where `STDERR_TAIL_CHARS = 2000`, because a full ffmpeg stderr dump can run to
thousands of lines and only the tail is usually diagnostic. `designing-errors`
owns the rest of the hierarchy and the exit-code mapping; this boundary only
decides which of the three to raise and how much stderr to keep.
