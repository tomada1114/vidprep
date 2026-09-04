# Project Guide

## Overview

This is a Python library built with [uv](https://docs.astral.sh/uv/) and
[hatchling](https://hatch.pypa.io/). It uses a strict `src/` layout with
comprehensive type checking and linting.

## Quick Reference

```bash
just install   # Install dependencies and git hooks when .git/ is present
just fmt       # Format code (ruff check --fix + ruff format)
just lint      # Lint (ruff check) + type check (mypy)
just test      # Run tests with coverage
just smoke     # Build and verify the wheel in a temp virtual environment
just check     # Run all checks: fmt → lint → test
just docs      # Serve docs locally
just build     # Build distribution packages
```

Without Just: replace `just <cmd>` with the corresponding `uv run` commands
in the `justfile`. Run a single test with
`uv run pytest tests/test_<module>.py::test_<name>`.

The whole pipeline over one video is `vidprep prep <video>` — installed via
`uv tool install --force --from . vidprep`, or `uv run vidprep prep <video>`
against the working tree without installing.

## Architecture

```
src/vidprep/
├── __init__.py      # Public API — export everything users need here
├── py.typed         # PEP 561 marker for typed package
├── cli.py           # typer app: one thin subcommand per pipeline stage
├── errors.py        # Exception hierarchy and the 0/1/2/3 exit codes
├── models.py        # pydantic schemas for the intermediate JSON (design.md §3)
├── project.py       # Project directory: init, load, verify, stage records
├── timeline.py      # Original <-> cut timeline mapping (design.md §4)
├── doctor.py        # doctor: inspect the tools the stages shell out to
├── audio.py         # audio-fix: denoise, high-pass, two-pass loudnorm
├── transcribe.py    # transcribe: Silero VAD in front of the recogniser
├── correct.py       # correct: dictionary replacement, verified patches
├── detect.py        # detect: silence and filler cut candidates
├── render.py        # render: apply the cuts, write the video and the SRT
├── report.py        # report: the review gate and the numbers behind it
├── prep.py          # prep: the whole pipeline over one file, resumable
├── verify.py        # --verify-asr: read the finished render back
├── _ffmpeg.py       # ffmpeg/ffprobe subprocess wrapper (doctor.py is the other sanctioned spawner)
├── _asr.py          # whisper.cpp / mlx-whisper behind transcribe
├── _dictionary.py   # The misconversion dictionary and its two passes
├── _text.py         # The comparison form text measurements share
├── _autoeditor.py   # auto-editor's v3 timeline -> cut intervals
├── _intervals.py    # Interval arithmetic detection and rendering share
├── _fillers.py      # Filler scanning and the cuts it justifies
├── _reencode.py     # The renderer protocol and the v1 re-encode
├── _subtitles.py    # Line breaking, readability limits, the SRT and text files
├── _ass.py          # Telops as an ASS subtitle track (design.md §3.5)
├── _preview.py      # Burning the telop track into the preview video
├── _boundaries.py   # Waveform stills and the boundary digest video
├── _review.py       # The `report --cuts` listing
├── _retranscribe.py # The arithmetic behind the re-transcription check
├── dictionaries/    # Packaged ASR misconversions, fillers, hallucinations
├── profiles/        # Packaged templates (default.json) copied by `vidprep init`
└── styles/          # Packaged telop style presets (default.json)
```

- Keep the public API surface small — export via `__init__.py.__all__`
- Internal modules can use a leading underscore (`_internal.py`)
- Separate concerns: one module per logical unit
- Update `__all__` and the new symbol's docstring whenever you change the public API —
  `docs/reference.md` renders straight from `__all__` via mkdocstrings and needs no
  manual edit; see `public-api-contract`

## Skills

`.claude/skills/` holds two kinds of skill. **Workflow skills** drive a procedure end
to end: `correct-transcript`, `create-pr`, `place-telops`, `review-cuts`,
`shipping-issues`, `smart-commit`. **Knowledge skills** own a body of convention and
carry the reasoning, boundary cases, and anti-patterns this file states only as bare
rules — load the matching one before changing that layer.

| Touching | Load |
|---|---|
| `src/vidprep/**/*.py`, `scripts/**/*.py` general style | `writing-python` |
| any test | `writing-tests`, `placing-tests` |
| an error class or an exit code | `designing-errors` |
| `__init__.py`'s `__all__` | `public-api-contract` |
| `docs/**`, `README*.md` | `updating-docs` |
| CI workflows, `.agents/hooks/**`, `justfile`, gate config in `pyproject.toml` | `changing-gates` |
| `pyproject.toml` dependency tables, `uv.lock` | `managing-dependencies` |
| whether a change is breaking, `CHANGELOG.md`, a PR title | `release-impact` |
| a GitHub issue | `triaging-issues` |
| a `SKILL.md` | `authoring-skills` |
| `_ffmpeg.py`, `doctor.py`'s subprocess boundary | `running-subprocesses` |
| `cli.py` | `writing-cli-commands` |
| `project.py`, the manifest, staleness | `managing-the-project-dir` |
| `models.py`, an artifact JSON schema | `modelling-artifacts` |
| `timeline.py` | `mapping-timelines` |
| `dictionaries/`, `profiles/`, `styles/` | `packaging-data-files` |
| `audio.py` | `processing-audio` |
| `transcribe.py`, `_asr.py` | `transcribing-speech` |
| `correct.py`, `_dictionary.py` | `correcting-transcripts` |
| `detect.py`, `_autoeditor.py`, `_fillers.py`, `_intervals.py` | `detecting-cuts` |
| `render.py`, `_reencode.py`, `_subtitles.py`, `_ass.py`, `_preview.py` | `rendering-output` |
| `report.py`, `verify.py`, `_retranscribe.py`, `tests/fault_injection/`, `just golden` | `verifying-renders` |
| finishing any change | `reviewing-changes` |

## Review Checklist

Before submitting a PR:

1. `just check` passes (format, lint, type check, tests)
2. New public APIs have type annotations and docstrings
3. Tests cover the new functionality
4. No unnecessary dependencies added

## Important Reminders

- All code, published docs, commits, and PRs must be written in English.
  Design notes (`docs/design-input.md`, `docs/design.md`,
  `docs/verification-plan.md`, `docs/research/`) stay in Japanese, and so does
  `README.ja.md` — the Japanese counterpart of `README.md`. Keep the two
  READMEs in step: a change to one belongs in the other in the same commit
- Do what has been asked; nothing more, nothing less
- NEVER create files unless absolutely necessary
- ALWAYS prefer editing an existing file to creating a new one
- NEVER proactively create documentation files unless explicitly requested
- Dependencies should always be added to the appropriate group in pyproject.toml

## Agent hooks

The shared hooks under `.agents/hooks/` enforce the following behavior for
agent sessions:

- Before a tool call, `guard.py` blocks writes to `uv.lock`, `.env*`, and
  `secrets/**`, as well as `git commit --no-verify` and plain force-pushes.
- After an edit, `format.py` runs Ruff's fix and format passes for edited
  Python files.
- Before a session ends, `stop_check.py` runs Ruff's lint and format checks
  plus mypy when Python files or `pyproject.toml` changed.

The hook definitions are wired by each supported agent's project
configuration. Start sessions at the repository root so the project hooks are
loaded, and review the hook definitions when the host asks for trust.

## Documentation conventions: `docs/**/*.md`, `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`

Depth: `updating-docs` (which doc surface a change lands on, the README/README.ja.md
parity rule, and the mkdocs nav vs. `--strict` build).

- Document non-obvious behavior, architecture decisions, and trade-offs
- Do NOT document what is obvious from the code or already expressed by the type system
- Code examples in docs must be valid Python that works with the current API
- Use admonitions (note, warning, tip) for important callouts in MkDocs pages

## Project configuration conventions: `pyproject.toml`

Depth: `managing-dependencies` (whether a package may enter at all, the two ranges-vs-pins
rule, and the `exclude-newer` bump procedure), `changing-gates` (the ruff/mypy/coverage
tables this section's gate-related rules protect).

- Runtime dependencies go under `[project] dependencies`
- Dev dependencies go under `[dependency-groups] dev`; docs under `[dependency-groups] docs`
- Before adding a dependency: verify active maintenance, compatible license (MIT/BSD/Apache), and minimal transitive dependencies
- Use version ranges (`>=X.Y`) for runtime dependencies -- never pin exact versions in a library
- NEVER remove existing ruff rules without explicit user approval
- NEVER lower the coverage threshold (currently 80%)
- After modifying dependencies, run `uv sync --all-groups`
- The `uv.lock` file MUST be committed alongside dependency changes

### `[tool.uv] exclude-newer`

`exclude-newer` is a supply-chain cooldown: `uv lock` and `uv sync` ignore any
package version published after the given timestamp, so a dependency cannot be
resolved until it has survived in the wild for a while. Dependencies are
updated by hand in this repo, so this cutoff is the only thing keeping freshly
published packages out of the lockfile.

Bump cadence: whenever dependencies are updated, move the `exclude-newer`
timestamp forward to roughly "today minus 14 days"; do this at least monthly
even if no dependency changed, so the cutoff doesn't drift too far behind.

Procedure:

1. Edit the `exclude-newer` date in `pyproject.toml`.
2. Run `uv lock` to regenerate `uv.lock` against the new cutoff.
3. Commit `pyproject.toml` and `uv.lock` together in the same commit.

## Python conventions: `src/**/*.py`, `scripts/**/*.py`

Depth: `writing-python` (typing, value-object shape, the size triggers, docstrings,
performance and Pythonic idioms), `designing-errors` (the exception hierarchy and how a
domain error becomes an exit code), `public-api-contract` (what may be exported).

### Design

- A module past 300 lines or a function past 40 lines is a trigger to consider a split,
  not a hard cap — most of `src/vidprep/` already exceeds 300 lines, so treat this as a
  review prompt rather than a rule to enforce literally
- Prefer 3 or fewer parameters (group related params with dataclass or TypedDict); typer
  CLI commands are the sanctioned exception (`# noqa: PLR0913`), since one parameter per
  flag is typer's own contract
- Google-style docstrings (Args/Returns/Raises) on all public functions; document *why*,
  not what the type signature already says; don't document obvious code

### Error Handling

- Define a package-level base exception (`VidprepError`); derive all specific errors from it
- Catch the most specific exception possible
- There is no `logging` module in `src/vidprep/`; a recoverable failure is caught into a
  reusable exception tuple and turned into a user-visible warning string, never logged
  and dropped
- Never swallow exceptions silently; if catching, handle meaningfully or re-raise
- Never use exceptions for control flow
- Return `None` or a sentinel only when the caller expects it; prefer raising for true errors

### Type System

- Prefer `@dataclass(frozen=True, slots=True)` for internal value objects
- Use Pydantic (`BaseModel`) only at serialization/deserialization boundaries
- Use `TypedDict` for structured dict shapes (API responses, config dicts)
- Use `Protocol` for structural subtyping instead of ABC when possible
- Avoid `Any`; when unavoidable, add a comment explaining why (e.g., `# Any: third-party lib has no stubs`)

### Performance

- Use generator expressions and `itertools` for large sequences; avoid materializing unnecessary lists
- Use `__slots__` on frequently instantiated classes (dataclass `slots=True`)
- Use `functools.lru_cache` or `functools.cache` for expensive pure functions
- Prefer `str.join()` over `+=` concatenation in loops
- Use `collections.defaultdict`, `Counter`, `deque` instead of hand-rolled equivalents
- Avoid repeated attribute lookups in tight loops; bind to local variable
- Use `dict`/`set` for O(1) membership tests instead of lists
- Lazy-import heavy optional dependencies inside functions to reduce import time

### Pythonic Patterns

- EAFP (try/except) over LBYL (if-check) when dealing with duck typing or I/O
- Use context managers (`with`) for all resource management (files, connections, locks)
- Prefer comprehensions over `map()`/`filter()` for readability
- Use a pydantic `Literal` field for a fixed set of values that lives on a schema
  (`Cut.status: Literal["proposed","approved","rejected"]`), paired with a
  module-level string constant for each value; `enum.Enum` is not used anywhere in
  `src/vidprep/` and there's no reason to introduce it
- Use `walrus operator` (:=) for assign-and-test when it improves clarity
- Use structural pattern matching (`match/case`) for complex dispatch
- Use `*args` unpacking and `**kwargs` deliberately; avoid passing them blindly through call chains

### Security

- Sanitize file paths to prevent directory traversal (`pathlib.Path.resolve()` then check prefix)
- Ruff's bandit rules (`S`) cover eval/exec/pickle/random misuse — do not suppress them with `noqa` without a written justification

### Constants and Naming

- Use `UPPER_SNAKE_CASE` named constants instead of magic numbers/strings
- Boolean parameters are keyword-only (`*, with_stats: bool = False`) rather than
  prefixed with `is_`/`has_`/`can_`/`should_` — this codebase has no such prefixes and
  relies on the keyword-only call site to stay self-documenting instead
- Private helpers: prefix with `_`; reserve `__` (name mangling) only for avoiding conflicts in subclass hierarchies

## Test conventions: `tests/**/*.py`

Depth: `writing-tests` (naming, what to assert, fakes over mocks, the `conftest.py`
fixtures), `placing-tests` (where a test file goes and which command runs it).

### Structure and Organization

- File structure mirrors source: `tests/test_<module>.py`, with a `_private.py` module
  usually folded into the test file of the public module that exercises it rather than
  getting its own file — see `placing-tests` for when a private module earns one
- Shared fixtures go in `tests/conftest.py`; use the narrowest fixture scope possible
- Test names are a prose sentence describing the expected behaviour, grouped inside a
  `class TestX:` (e.g. `test_a_stream_without_a_duration_is_reported`), not the
  `test_<what>_<scenario>_<expected_result>` template — see `writing-tests`
- Follow Arrange-Act-Assert: set up data, execute the behavior, verify the outcome
- One logical assertion per test; multiple `assert` statements are fine if they verify one behavior

### What to Test

- Test *behavior and contracts*, not implementation details
- Never test private methods directly; test through the public interface
- Always test the happy path AND the error path for every public function
- If a function can raise, test that it raises the right exception with the right message

### Edge Cases (always consider these)

- **Empty inputs**: empty string, empty list, empty dict, None where optional
- **Boundary values**: 0, 1, -1, max int, min int, float('inf'), float('nan')
- **Type boundaries**: very long strings, unicode/emoji, mixed encodings
- **Collection boundaries**: single element, duplicate elements, max expected size
- **Concurrent scenarios**: if the code is async, test cancellation and timeout behavior
- **State transitions**: initial state, after one operation, after repeated operations, after error recovery

### Error and Exception Testing

- Use `pytest.raises(XError, match=r"expected message")` -- always verify the message pattern
- Test that errors propagate correctly through the call chain
- Test that cleanup/teardown runs even when exceptions occur (context managers, finally blocks)
- Test invalid argument combinations that should be rejected
- Test error recovery: after an error, does the object remain in a consistent state?

### Parametrize and Data-Driven Tests

- Use `@pytest.mark.parametrize` for input/output variations; don't copy-paste test bodies
- Group related parametrize cases with `pytest.param(..., id="descriptive-name")`
- For complex parametrized data, use a helper function or fixture to build test cases
- Consider property-based testing with `hypothesis` for functions with well-defined invariants (add to dev deps if using)

### Fixtures

- Prefer factory fixtures over static fixtures: `def make_user(**overrides)` returns a customizable object
- Use `tmp_path` for filesystem tests; never write to real directories
- Use `monkeypatch` for environment variables, not direct `os.environ` manipulation
- Fixtures that open resources must clean up with `yield` + teardown or `addfinalizer`
- Scope fixtures appropriately: `function` (default) for isolation, `session` only for truly expensive setup

### Mocking Strategy

- Mock at boundaries only: I/O, network, clock (`freezegun`/`time_machine`), external services
- Never mock the unit under test
- Prefer fakes (in-memory implementations) over mocks for repositories/stores
- Use `unittest.mock.AsyncMock` for async callables
- Assert on behavior and outputs, not on how many times a mock was called
- If you need to mock more than 2 things in one test, the code under test may have too many dependencies

### Test Independence and Reliability

- Tests must be independent: no shared mutable state, no ordering dependency
- Each test must pass when run alone (`pytest tests/test_foo.py::test_specific`)
- No `@pytest.mark.skip` or TODO tests on main -- delete them or fix them
- No `time.sleep` in tests; design tests to be event-driven or use deterministic fakes for time
- Flaky tests must be fixed immediately, not ignored

### Coverage Philosophy

- Coverage is a *floor*, not a *ceiling* -- 80% minimum, but aim for meaningful coverage not percentage
- Branch coverage matters more than line coverage; always test both sides of conditionals
- Missing coverage should prompt "is this code reachable?" -- if not, delete it
- Don't write trivial tests just to hit numbers; cover edge cases and error paths instead

### Anti-Patterns

- Don't test getters/setters while missing business logic edge cases
- Don't use `assert True` or `assert result is not None` when a specific value should be checked
- Don't share mutable test data between tests (use fresh fixtures)
- Don't test that a dependency's library works (e.g., testing that `json.loads` parses JSON)
- Don't mock everything -- integration tests with real dependencies catch real bugs
