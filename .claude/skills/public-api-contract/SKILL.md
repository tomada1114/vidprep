---
name: public-api-contract
description: >
  Covers `src/vidprep/__init__.py`'s `__all__`, the `vidprep` console-script
  entry point in `pyproject.toml`, `src/vidprep/py.typed`, and the `just
  smoke` / `scripts/smoke_test.py` check, and what counts as "public" for a
  project whose `docs/reference.md` renders straight from `__all__`. Use when
  adding or removing a name from `__all__`, deciding whether a new
  `models.py`/`errors.py`/`project.py` symbol should be exported, changing
  `[project.scripts]`, or explaining why `tests/test_package.py` asserts an
  exact `__all__` set.
metadata:
  platforms: claude-code, codex
---

# Public API Contract

**Owns:** `src/vidprep/__init__.py`'s `__all__`, the `vidprep` console-script
entry point, `py.typed`, `just smoke`, and what "public" means for a project
whose `docs/reference.md` is entirely auto-generated from `__all__`. **Does
not own:** the semver level and CHANGELOG wording for a surface change
(`release-impact`); which doc surface a change lands on (`updating-docs`);
the artifact JSON schemas themselves (`modelling-artifacts`).

## `docs/reference.md` is two lines — there is no doc list to keep in sync

The whole file is a heading and a mkdocstrings directive: `# API Reference`
followed by `::: vidprep`. That directive renders straight from
`vidprep.__all__` and each exported symbol's own docstring at build time —
there is no hand-maintained list of classes to edit. "Update the docs when
you change the public API" therefore means exactly one thing here: edit
`__all__` in `src/vidprep/__init__.py` and make sure the symbol's own
docstring is accurate. Editing `docs/reference.md` itself is never the fix.

## What's exported today, and where each name actually lives

`__all__` currently lists exactly twelve names:
`Cuts, Manifest, NoiseFloorReport, Profile, Project, Styles, Telops,
Timeline, Transcript, VadReport, VidprepError, __version__`. Ten of them are
imported into `__init__.py` from three modules — `Cuts`, `Manifest`,
`NoiseFloorReport`, `Profile`, `Styles`, `Telops`, `Transcript`, `VadReport`
from `models.py`; `Project` from `project.py`; `Timeline` from `timeline.py`
— plus `VidprepError` from `errors.py` and the runtime-computed
`__version__`. `tests/test_package.py::TestPackageMetadata::test_public_exports`
asserts `set(__all__)` against this exact literal set, and
`test_every_export_is_reachable_on_the_package` asserts every name in
`__all__` actually resolves on the `vidprep` module — together these are the
enforcement for "the declared surface is real and complete," not a doc page.

## The real gap: public-looking classes that never made it into `__all__`

`models.py` defines twenty-eight public classes (past its private `_Strict`
base); only eight of them — `Manifest`, `NoiseFloorReport`, `Transcript`,
`VadReport`, `Cuts`, `Telops`, `Styles`, `Profile` — are exported. `Cut`,
`Segment`, `Word`, `Edit`, and the seven `*Profile` component classes
(`AudioProfile`, `AsrProfile`, `CorrectProfile`, `SilenceProfile`,
`FillerProfile`, `RenderProfile`, `SubtitleProfile`) are all plain public
classes — no leading underscore — but are reachable only by importing
`vidprep.models` directly. A caller who receives a `Cuts.cuts[i]` gets a
`Cut` instance typed with a class they cannot import from the top-level
package. Same story in `errors.py`: only `VidprepError` is exported, so none
of `UsageError`, `NotAProjectError`, `ExecutionFailedError`, `FfmpegError`,
`AsrFailedError`, `TimelineSchemaError`, `SchemaInvalidError`,
`InvariantViolationError`, `PatchInvalidError`, `TelopInvalidError`, or
`HashMismatchError` are part of the declared surface, and neither are the
`EXIT_OK` / `EXIT_USAGE` / `EXIT_EXECUTION` / `EXIT_VALIDATION` exit-code
constants that `errors.py`'s own module docstring points to (design.md §6).

## Tests already reach past `__all__` for exactly the exit-code / subclass gap

`tests/test_cli.py` imports `EXIT_EXECUTION, EXIT_OK, EXIT_USAGE,
EXIT_VALIDATION, FfmpegError` straight from `vidprep.errors`, and
`tests/test_ffmpeg.py` imports `ExecutionFailedError, FfmpegError,
UsageError` the same way — never from the top-level `vidprep` package,
because those names aren't there. That is not sloppiness; it is what an
undeclared surface looks like from the inside. Anything that inspects a
`VidprepError.exit_code` or wants to catch a specific subclass (a caller
mirroring the CLI's own exit-code handling) is in the same position: reject
in review a *thirteenth* export added to satisfy one call site without first
asking whether the whole exit-code/subclass family belongs in `__all__`
together — exporting `AsrFailedError` alone and leaving `FfmpegError` behind
would just move the inconsistency rather than close it.

## `README.md` documents the CLI and the on-disk layout, not the Python API

`grep -n "import vidprep\|from vidprep" README.md README.ja.md docs/*.md`
returns nothing — there is no "using vidprep as a library" section anywhere
in the shipped docs. That's a fact about the current state, not something
this skill fixes; a change that adds real Python-API-consumer documentation
lands in `README.md`/`README.ja.md` together, per the existing rule that the
two READMEs move in the same commit, and which section it belongs in is
`updating-docs`'s call.

## Two `Timeline` classes exist — always import the one off the package

`timeline.py` defines the exported `class Timeline` (line 109) that maps
original-to-cut positions. `_autoeditor.py` separately defines its own
`class Timeline(BaseModel)` (line 63) for auto-editor's v3 export document —
an unrelated shape, private to that module. Never assume which module
"Timeline" means from context; import it as `vidprep.Timeline` (or
`from vidprep import Timeline`) so it resolves to the exported one rather
than whichever module happened to be imported nearby.

## Verifying a surface change: `just smoke`, not just `just test`

`just smoke` runs the `build` recipe (`uv build`) and then
`uv run python scripts/smoke_test.py` with no wheel argument, which
auto-detects the single wheel under `dist/`; CI and the release workflow
instead call `uv run python scripts/smoke_test.py dist/*.whl` explicitly —
same wheel, resolved at a different point, not a real behavioral gap. The
script installs that wheel into an isolated temp venv and imports every
module the distribution ships, asserting each one's `__file__` resolves
under the venv's `site-packages` rather than under the repo tree — it is
checking that packaging (`py.typed`, the `vidprep = "vidprep.cli:main"`
entry point in `pyproject.toml`'s `[project.scripts]`, the `Typing :: Typed`
classifier) actually ships correctly, not that `__all__` itself is intact.
`test_package.py` (via `just test`) is what guards `__all__`'s content; run
both before calling a public-surface change verified.
