---
name: managing-the-project-dir
description: >
  Covers src/vidprep/project.py — the on-disk project layout (vidprep.json,
  profile.json, audio/processed.wav, out/, report/), the atomic_write_text /
  atomic_replace / write_json primitives, the per-module WORKSPACE_PREFIX
  convention, the manifest's StageRecord, the four declarative stage tables
  (ARTIFACT_MODELS, STAGE_PROFILE_SECTIONS, STAGE_INPUTS, STAGE_UPSTREAM), and
  the split between stale_inputs (blocks/re-runs) and stale_upstream_warnings
  (only warns). Use when a stage needs to read or write a project artifact,
  when deciding whether a new stage dependency belongs in STAGE_INPUTS or
  STAGE_UPSTREAM, or when touching record_stage, stale_inputs, or
  stale_upstream_warnings.
metadata:
  platforms: claude-code, codex
---

# Managing The Project Dir

**Owns:** `src/vidprep/project.py` — the on-disk project layout, the
`atomic_write_text`/`atomic_replace`/`write_json` primitives, the
`WORKSPACE_PREFIX` convention, the manifest (`vidprep.json`), `StageRecord`,
the four declarative stage tables, and the distinction between `stale_inputs`
(triggers a re-run) and `stale_upstream_warnings` (only warns) — including why
one blocks and the other doesn't. **Does not own:** the pydantic model
definitions themselves for the manifest, profile, and other artifacts
(`modelling-artifacts`); how staleness warnings are rendered to the user in
`--json`/human output (`writing-cli-commands`).

## The on-disk layout is a handful of named constants, not a convention to remember

`project.py` defines `MANIFEST_NAME = "vidprep.json"`, `PROFILE_NAME =
"profile.json"`, `SOURCE_DIR = "source"` (only populated by `init
--copy-source`). Everything past that is stage output with no constant of
its own in `project.py`: `audio/processed.wav`, `transcript.json`,
`cuts.json`, `telops.json`/`styles.json` (optional), `out/output.mp4`,
`out/subtitles.srt`/`out/subtitles.nowrap.srt`, `out/transcript.txt`,
`out/telops.ass` and `out/preview.mp4` (the last two only with `--preview`),
and `report/stats.json`, `report/vad.json`, `report/noise_floor.json`,
`report/boundaries/*.png`, `report/boundary_digest.mp4`. README.md's "The
project directory" section mirrors this tree and is the doc to update
alongside a layout change (`updating-docs`).

## Two write primitives, and the per-module `WORKSPACE_PREFIX` scratch directory they publish from

`atomic_write_text` writes to a sibling `.{name}.tmp` file, then `os.replace`s
it into place inside a `try`/`finally` that unlinks the temp file
(`missing_ok=True`) regardless of outcome — a crash before the replace leaves
the previous version of the target untouched. Both `os.replace` call sites in
this module carry the identical comment `# noqa: PTH105 — pathlib has no
atomic replace`. `atomic_replace(produced, target)` publishes a stage's
finished output from its scratch workspace into the real project location the
same way — it requires both paths on the same filesystem, which is why every
stage builds its workspace *inside* the project root rather than in a system
temp directory. `write_json(path, model)` calls `model.model_dump(mode=
"json")`, then `json.dumps(payload, indent=2, ensure_ascii=False) + "\n"`
through `atomic_write_text` — every artifact vidprep writes goes through this
one function.

Six modules each define their own `WORKSPACE_PREFIX` constant for that
scratch directory, verified by grep, rather than importing one shared
constant: `audio.py` (`".audio-fix-"`), `transcribe.py` (`".transcribe-"`),
`_reencode.py` (`".render-"` — `render.py` itself defines none, the
re-encoder does), `verify.py` (`".verify-"`), `_preview.py` (`".preview-"`)
and `_boundaries.py` (`".report-"`, which `report.py` reuses via
`_boundaries.WORKSPACE_PREFIX` rather than declaring its own). Treat this as
current, verified reality — six independent definitions — not something to
silently "fix" by extracting a shared constant.

## Four declarative tables, all keyed by the same six stage names

`audio_fix`, `transcribe`, `correct`, `detect`, `render`, `report` key every
one of the four tables near the top of `project.py`. `ARTIFACT_MODELS` maps a
filename (`"transcript.json"`, `"report/noise_floor.json"`,
`"report/vad.json"`, `"cuts.json"`, `"telops.json"`, `"styles.json"`) to its
pydantic model — the module comment calls this "in the order they are
reported," and `validate_artifacts` validates whichever of those files exist,
in that order. `STAGE_PROFILE_SECTIONS` names which `profile.json` sections
change a stage's output (e.g. `render: ("render", "subtitle")`), feeding
`stage_params_sha256`. `STAGE_INPUTS` names which artifacts' *content* a
stage reads and must be re-hashed to catch an out-of-band edit (e.g. `render:
("transcript.json", "cuts.json")`). `STAGE_UPSTREAM` is the stage dependency
graph proper (e.g. `render: ("audio_fix", "transcribe", "detect")`).

`correct` is deliberately absent as a *value* anywhere in `STAGE_UPSTREAM` —
its own entry is `("transcribe",)`, not empty, but no other stage's tuple
names `correct` as something it depends on — the docstring on `STAGE_INPUTS`
explains why: `correct`
"rewrites `transcript.json` in place rather than producing an output of its
own, and it is reachable outside a pipeline run as `correct --apply-patch`.
Tracking the artifact rather than the stage catches the edit whoever made it,
and re-runs nothing when a pass changed nothing." The same docstring explains
why `audio/processed.wav` is excluded from `STAGE_INPUTS` even though
`render` reads it: "A changed `audio/processed.wav` always arrives with
`audio_fix` having run, which an invocation already tracks, so hashing
hundreds of megabytes on every staleness check would cost far more than it
catches."

## `stale_inputs` blocks; `stale_upstream_warnings` only warns — and each is wired to a different caller

`stale_upstream_warnings(project, stage)` compares each upstream stage's
recorded `params_sha256` against `stage_params_sha256(project.profile,
upstream)` computed fresh, and returns one warning string per mismatch. Its
docstring states the rationale plainly: "Running on stale inputs is allowed
on purpose (design.md §3.2): the user is told, not blocked." It is called
only from `cli.py`'s `_prepare`, which every single-stage subcommand goes
through — so `vidprep render` alone gets the `⚠` warning line but takes no
automatic action on it.

`stale_inputs(project, stage)` instead recomputes `stage_inputs_sha256`
against the CURRENT file contents and returns the names of any recorded
inputs whose digest moved. It has two escape hatches, both real: `[]` when
`project.manifest.stages.get(stage)` is `None` (the stage never ran), and
`[]` when `record.inputs_sha256` is falsy — an older manifest written before
input hashing existed, and "such a record heals itself the next time the
stage runs for any other reason," per the docstring. It is called only from
`prep.py`'s `_needs_run`, used only by `prep` — `prep.py` has its own,
narrower `_prepare` (load, `verify_source`, `validate_artifacts`) that does
**not** call `stale_upstream_warnings`, so `prep` drops the profile-drift
warning entirely while gaining the automatic re-run a single-stage command
never gets. This is a real, verified asymmetry, not a bug to fix in passing:
`vidprep render` warns but never force-reruns itself; `vidprep prep` reruns
but never warns about upstream profile drift.

`stale_inputs` is what commit `a5e0aa3` added, and it is worth reading before
touching input tracking further: before it, nothing detected an out-of-band
rewrite of a stage's tracked input — `correct --apply-patch` rewriting
`transcript.json` between two `prep` runs left `render` reporting "up to
date" and shipping stale subtitles at exit 0, silently. The fix added
`inputs_sha256` to `StageRecord`, `stage_inputs_sha256`/`stale_inputs` to
`project.py`, and the final line of `prep.py`'s `_needs_run` (`return bool(
project_module.stale_inputs(loaded, stage))`), reached only after the
upstream-ran-this-invocation check and the `params_sha256` check both come up
clean. Any change to what a stage reads as input belongs in `STAGE_INPUTS`
first — an omitted entry means an out-of-band edit to that file goes
undetected the same way `transcript.json` did before this fix.

## `StageRecord`, `record_stage`, and the one stage that never calls it

`StageRecord` (in `models.py`) carries `done_at: AwareDatetime`,
`params_sha256: str` (pattern-checked as a sha256 hex digest),
`inputs_sha256: dict[str, str]` (default `{}`), and `tool_versions: dict[str,
str]` (default `{}`). `record_stage(project, stage, tool_versions=None)`
builds one from `stage_params_sha256` and `stage_inputs_sha256` computed
fresh, does `project.manifest.model_copy(update={"stages":
{**project.manifest.stages, stage: record}}, deep=True)`, writes that
manifest through `write_json`, and returns `dataclasses.replace(project,
manifest=manifest)` — `Project` is a frozen `@dataclass(slots=True)`, so this
is the only way to hand the caller an in-memory `Project` reflecting the
just-written manifest without reloading from disk.

Grep confirms `record_stage` is called from `audio.py`, `transcribe.py`,
`correct.py`, `detect.py`, and `render.py` — never from `report.py`. `report`
still keys every one of the four stage tables (an empty tuple in
`STAGE_PROFILE_SECTIONS`, `("cuts.json",)` in `STAGE_INPUTS`, `("detect",
"render")` in `STAGE_UPSTREAM`), but `prep.py` tracks its completion
separately through `UNRECORDED_OUTPUT = {report.STAGE:
Path(report.STATS_NAME)}` — `_needs_run` falls back to checking whether that
file exists on disk exactly when `loaded.manifest.stages.get(stage) is None`.
