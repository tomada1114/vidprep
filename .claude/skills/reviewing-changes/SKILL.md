---
name: reviewing-changes
description: >
  Covers routing a diff's changed paths to the review skill that owns them, and
  the checks every diff owes regardless of path — matching `src/vidprep/<mod>.py`
  to `tests/test_<mod>.py`, the `just check` gate ladder, and treating "tests
  pass" as something actually run, not assumed. Use when a change touches
  multiple `src/vidprep/` modules and it's unclear which knowledge skill to load,
  before declaring any change complete, or when deciding what a PR still owes
  before opening it.
metadata:
  platforms: claude-code, codex
---

# Reviewing Changes

**Owns:** pre-completion review routing — given a diff's changed paths, which review applies and which skill owns it — plus the checks every diff owes regardless of path: matching implementation to test to doc, the gate ladder, and execution evidence. **Does not own:** any individual review's content, each named skill below owns its own; PR mechanics (`create-pr`); commit grouping (`smart-commit`).

This skill is deliberately thin — it is a lookup table, not a second copy of every other skill's rules. Load the target skill it names before actually reviewing the diff.

## Path → review → owner

| Changed path | Owning skill |
|---|---|
| `src/vidprep/cli.py` | `writing-cli-commands` (its `_prepare` at line 174 calls `project.py`'s `stale_upstream_warnings` — a change there touches `managing-the-project-dir` too) |
| `src/vidprep/project.py` | `managing-the-project-dir` |
| `src/vidprep/models.py` | `modelling-artifacts` |
| `src/vidprep/timeline.py` | `mapping-timelines` |
| `src/vidprep/doctor.py` | `running-subprocesses` (the subprocess-inspection boundary) |
| `src/vidprep/audio.py` | `processing-audio` |
| `src/vidprep/transcribe.py`, `src/vidprep/_asr.py` | `transcribing-speech` |
| `src/vidprep/correct.py`, `src/vidprep/_dictionary.py` | `correcting-transcripts` |
| `src/vidprep/detect.py`, `src/vidprep/_autoeditor.py`, `src/vidprep/_fillers.py`, `src/vidprep/_intervals.py` | `detecting-cuts` |
| `src/vidprep/render.py`, `src/vidprep/_reencode.py`, `src/vidprep/_subtitles.py`, `src/vidprep/_ass.py`, `src/vidprep/_preview.py` | `rendering-output` |
| `src/vidprep/report.py`, `src/vidprep/_review.py`, `src/vidprep/_boundaries.py`, `src/vidprep/verify.py`, `src/vidprep/_retranscribe.py` | `verifying-renders` (also owns `tests/fault_injection/**` and `just golden`) |
| `src/vidprep/prep.py` | `writing-cli-commands` (the composite `prep` subcommand — its own `_needs_run` calls `project.py`'s `stale_inputs`, a separate staleness overlap with `managing-the-project-dir`) |
| `src/vidprep/_ffmpeg.py` | `running-subprocesses` |
| `src/vidprep/_text.py` | `correcting-transcripts` (the comparison-form text measurements `correct.py` and `_dictionary.py` share) |
| `src/vidprep/errors.py` | `designing-errors` |
| `src/vidprep/__init__.py` | `public-api-contract` |
| `src/vidprep/dictionaries/**`, `src/vidprep/profiles/**`, `src/vidprep/styles/**` | `packaging-data-files` |
| any `src/vidprep/**/*.py` for general style, typing, docstrings | `writing-python` (layer under whichever module skill above applies) |
| `tests/**` | `writing-tests` (how) + `placing-tests` (where — including `tests/fault_injection/caseNN_*.py` naming) |
| `.github/workflows/**`, `.agents/hooks/**`, `justfile`, `[tool.ruff]`/`[tool.mypy]`/`[tool.pytest.ini_options]`/`[tool.coverage.*]` in `pyproject.toml`, `.pre-commit-config.yaml`, `.github/zizmor.yml` | `changing-gates` |
| `[project] dependencies`, `[dependency-groups]`, `uv.lock`, `[tool.uv] exclude-newer` | `managing-dependencies` |
| `docs/**`, `README.md`, `README.ja.md`, `mkdocs.yml` | `updating-docs` |
| `.claude/skills/**`, `.agents/skills/**` | `authoring-skills` |
| PR title, `CHANGELOG.md`, anything semver-visible | `release-impact` |
| GitHub issue text/labels, not code | `triaging-issues` |

A module with no row above (`src/vidprep/py.typed`) carries no review beyond the ladder in the next section. `scripts/smoke_test.py` belongs to `public-api-contract` (it is what `just smoke` runs against the built wheel); `scripts/golden_run.py` and `scripts/compare_stats.py` belong to `verifying-renders` (they back `just golden` and `just golden-diff`). Other `scripts/**` changes fall to `writing-python`'s general scope. A diff spanning several rows — say `detect.py` plus its test plus `docs/reference.md` — loads every skill its paths touch, not just the first match; this table routes each path independently, it does not pick one skill per diff.

## The gate ladder every diff owes regardless of path

`.agents/hooks/format.py` already re-formats every edited `.py` file automatically after each edit — never manually re-run `ruff format` just to catch what the hook already applied. Before calling any change complete, `just check` (`justfile`'s `check: fmt lint test` recipe) is the standard bar: format, lint plus mypy, then pytest with the 80% branch-coverage floor. This repo has no split between a fast and a full local gate the way some projects do — `.agents/hooks/stop_check.py` runs a lighter subset (ruff + mypy, no tests) only as an end-of-turn safety net, not as an alternative gate to reach for instead of `just check`. **REQUIRED:** `changing-gates` for the full three-layer breakdown (hooks < `just check` < CI) and exactly what each layer skips.

## Matching implementation, test, and doc in the same change

A change to `src/vidprep/<module>.py` owes an update to its test file — `tests/test_<module>.py` for a public module, or the private-module file `placing-tests` names for a leading-underscore module — in the same diff, not a follow-up. A change that alters the public CLI surface or `__init__.py`'s `__all__` owes a doc surface per `updating-docs` in the same commit, not "later."

## "Tests pass" means the command ran, not that it looks like it should

Report a passing gate only after actually seeing `just check` (or at minimum `just test`) exit clean in this session — never infer it from the diff looking correct. This repo's CI (`.github/workflows/ci.yml`) runs the same lint and test steps across a three-version Python matrix (`3.12`, `3.13`, `3.14`) plus jobs nothing local runs (`spell-check`, `build`, `zizmor`); a local pass is necessary but not sufficient proof the branch is clean. `changing-gates` owns the coverage-floor mechanics and the CI job list — cite it rather than re-deriving the numbers here.

This skill is why "finishing any change" resolves to a single stop before declaring anything done: whatever path a diff touches, it still owes this table's routing plus this ladder — the two things every other skill in this set assumes already happened by the time its own rules apply.
