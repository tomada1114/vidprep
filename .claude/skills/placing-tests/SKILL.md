---
name: placing-tests
description: >
  Covers where a new test file goes in `tests/`, when a `_private.py` module
  earns its own `tests/test_<module>.py` versus being folded into the test file
  of the public module that exercises it, the placement rule for new
  `tests/fault_injection/caseNN_<name>.py` modules, the two import styles used
  across `tests/`, and the 80% branch-coverage floor enforced by
  `--cov-fail-under=80`. Use when adding a new source module and deciding what
  its test file should be named, adding a fault-injection case, importing a
  helper from another test file, or running a single test with
  `uv run pytest tests/test_<module>.py::test_<name>`.
metadata:
  platforms: claude-code, codex
---

# Placing Tests

**Owns:** where a new test file goes — the flat `tests/test_<module>.py` mirror
of `src/vidprep/<module>.py` — plus when a `_private.py` module earns its own
test file, the `tests/fault_injection/` placement rule, the two import styles
used across test files, which command runs a test, and the 80% branch-coverage
floor. **Does not own:** what a test asserts and how it's named
(`writing-tests`); changing the coverage floor or the pytest/coverage config
tables themselves (`changing-gates`).

## `tests/` mirrors `src/vidprep/` flatly, one file per public module

Both directories are flat — no subpackages inside `src/vidprep/` and, with one
exception below, none inside `tests/`. `src/vidprep/` has 14 public
(non-underscore) modules: `audio.py`, `cli.py`, `correct.py`, `detect.py`,
`doctor.py`, `errors.py`, `models.py`, `prep.py`, `project.py`, `render.py`,
`report.py`, `timeline.py`, `transcribe.py`, `verify.py`. Thirteen of them
follow the exact name mirror (`detect.py` → `tests/test_detect.py`,
`render.py` → `tests/test_render.py`, and so on); `errors.py` is the one with
no dedicated test file — its exceptions are exercised from whichever module's
test file raises and catches them (`tests/test_correct.py`,
`tests/test_detect.py`, `tests/test_render.py`, and others each import from
`vidprep.errors` directly), since that's where each error actually gets
raised. Separately, the package root `__init__.py` — dunder-named, so outside
the 14 above — is also tested, via `tests/test_package.py`
(`TestPackageMetadata.test_public_exports` against `vidprep.__all__`) rather
than a `test___init__.py`. A new public module follows the exact-name mirror
unless it is, like `errors.py`, a cross-cutting concern with no behavior of
its own to assert in isolation.

## A `_private.py` module earns its own test file only when it has independent surface area

Three private modules get a dedicated `tests/test_<name>.py` (dropping the
leading underscore): `_ffmpeg.py` → `tests/test_ffmpeg.py` (172 lines),
`_preview.py` → `tests/test_preview.py` (735 lines), `_subtitles.py` →
`tests/test_subtitles.py` (160 lines). All three are large enough, and
self-contained enough, to justify testing in isolation — `_ffmpeg.py` is the
sole subprocess boundary in the package (234 lines) and is tested against a
faked subprocess without any of the stages that call it.

Every other `_private.py` module is tested inside the test file of the public
module that exercises it, not on its own: `_dictionary.py` (369 lines) is
tested from `tests/test_correct.py` (`from vidprep._dictionary import
AsrDictionary, ReadingToken, normalise_reading`); `_autoeditor.py` and
`_intervals.py` from `tests/test_detect.py`; `_boundaries.py` and
`_review.py` from `tests/test_report.py`; `_retranscribe.py` and `_text.py`
from `tests/test_verify.py`; `_reencode.py` from `tests/test_render.py`. The
heuristic is real surface area, not file length alone — a private module
earns its own file when it stands in for an external boundary or is complex
enough that testing it through its caller would smuggle two concerns into one
test file; otherwise it belongs in the public test file, because that public
module is how the private one is actually used.

The "test through the public interface" framing elsewhere in this repo
doesn't mean test files avoid importing private symbols — most of the
public-module test files above import their related private module directly
by name (`from vidprep import _autoeditor, _ffmpeg, _fillers, _intervals,
cli` in `tests/test_detect.py`; `from vidprep import _boundaries, _ffmpeg,
_review, audio, report` in `tests/test_report.py`). What distinguishes the
three standalone-tested modules is only that they get a whole file to
themselves; direct private imports are the norm across the test suite, not an
exception reserved for those three.

## `tests/fault_injection/` is the one real subdirectory, and its cases are wired by hand

`tests/fault_injection/` has no `src/` counterpart — it holds a shared
`_harness.py` plus six case modules, `case01_skipped_audio_fix.py` through
`case06_swapped_source.py`, each exposing a `main()` that returns an exit
code. A new fault-injection case is a new `caseNN_<name>.py` in that
directory, and it does not run on its own: `tests/test_fault_injection.py`
imports every case module explicitly and lists it in the `CASES` tuple, which
drives both `test_the_broken_input_is_refused` (parametrized over `CASES`)
and `test_every_case_of_the_table_is_covered`, which hard-asserts
`len(CASES) == 6` against verification-plan.md §10. Adding a case means
updating both the import list and the `CASES` tuple, and bumping that
assertion's expected count — the file will fail loudly rather than silently
skip the new case.

## Two import styles coexist — relative within a directory, `tests.` across one

Test files import each other's helpers with a plain relative import inside
the same directory: `tests/test_project.py` does `from .conftest import
SAMPLE_DURATION`, `tests/test_preview.py` does `from .test_render import
DURATION, FRAME, FakeFfmpeg, write_cuts, write_transcript`, and each
`tests/fault_injection/caseNN_*.py` does `from ._harness import ...`.
Reaching from `tests/` into the `tests/fault_injection/` subpackage instead
uses the absolute `tests.` form — `tests/test_verify.py` does `from
tests.fault_injection._harness import (...)`, and
`tests/test_fault_injection.py` does `from tests.fault_injection import
(case01_skipped_audio_fix, ...)`. Both are real, accepted patterns: relative
for same-directory helpers, `tests.` when crossing into the one subdirectory.

## No `src/` counterpart at all: golden-sample, fault-injection runner, and the skills contract

`tests/test_fault_injection.py`, `tests/test_golden_sample.py`, and
`tests/test_skills.py` test cross-cutting properties rather than one module.
`tests/test_golden_sample.py` carries `pytestmark =
[pytest.mark.skipif(not GOLDEN.is_file(), ...), pytest.mark.skipif(...)]` —
it only runs when a real golden-sample fixture and `ffprobe` are both
present, so it is silent in ordinary CI. `tests/test_skills.py` is a text
contract over `.claude/skills/*/SKILL.md`, not this skill's concern.
Separately, `scripts/smoke_test.py` is the one file under `scripts/` with no
`tests/test_smoke_test.py` counterpart — every other `scripts/*.py`
(`asr_backends.py`, `asr_bench.py`, `bench_metrics.py`, `bootstrap.py`,
`cer.py`, `compare_stats.py`, `golden_run.py`) has one. That asymmetry is a
fact to know when placing a new test near `scripts/`, not something to fix
here.

## Running a test and the coverage floor

A single test runs as `uv run pytest tests/test_<module>.py::test_<name>` (or
`::TestClass::test_name` for a class-scoped one); `just test` runs the full
suite with `uv run pytest --cov=vidprep --cov-branch
--cov-report=term-missing:skip-covered --cov-fail-under=80`, and `just check`
chains `fmt` → `lint` → `test`. `pyproject.toml`'s `[tool.pytest.ini_options]`
sets `testpaths = ["tests"]` and `pythonpath = ["scripts"]` with the comment
"scripts/ is not a package, so tests import the bench helpers by name." —
that pythonpath entry is why `tests/test_asr_bench.py` can `import asr_bench`
directly instead of reaching through `scripts.asr_bench`.

The 80% branch-coverage floor is not a number in `pyproject.toml` — only
`[tool.coverage.run] branch = true` lives there. The `80` itself is only on
the CLI, duplicated in `justfile`'s `test` recipe and in
`.github/workflows/ci.yml`'s test job (`--cov-fail-under=80`).
`.github/workflows/release.yml` runs plain `uv run pytest` with no coverage
flags at all, so the release path does not enforce the floor — a test placed
correctly still needs `just test` or CI's own job to prove it clears 80%, not
the release workflow.
