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
├── verify.py        # --verify-asr: read the finished render back
├── _ffmpeg.py       # The only module allowed to spawn subprocesses
├── _asr.py          # whisper.cpp / mlx-whisper behind transcribe
├── _dictionary.py   # The misconversion dictionary and its two passes
├── _text.py         # The comparison form text measurements share
├── _autoeditor.py   # auto-editor's v3 timeline -> cut intervals
├── _intervals.py    # Interval arithmetic detection and rendering share
├── _fillers.py      # Filler scanning and the cuts it justifies
├── _reencode.py     # The renderer protocol and the v1 re-encode
├── _subtitles.py    # Line breaking, readability limits, the SRT file
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
- Update `docs/reference.md` and README examples whenever you change the public API

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

- Document non-obvious behavior, architecture decisions, and trade-offs
- Do NOT document what is obvious from the code or already expressed by the type system
- Code examples in docs must be valid Python that works with the current API
- Use admonitions (note, warning, tip) for important callouts in MkDocs pages

## Project configuration conventions: `pyproject.toml`

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

### Design

- Keep modules under 300 lines; one logical concern per module
- Keep functions under 40 lines; prefer 3 or fewer parameters (group related params with dataclass or TypedDict)
- Google-style docstrings (Args/Returns/Raises) on all public functions; document *why*, not what the type signature already says; don't document obvious code

### Error Handling

- Define a package-level base exception; derive all specific errors from it
- Catch the most specific exception possible
- Use `logging.exception()` in catch blocks (auto-includes traceback), never `logger.error(str(e))`
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
- Use `enum.Enum` for fixed sets of values instead of string constants
- Use `walrus operator` (:=) for assign-and-test when it improves clarity
- Use structural pattern matching (`match/case`) for complex dispatch
- Use `*args` unpacking and `**kwargs` deliberately; avoid passing them blindly through call chains

### Security

- Sanitize file paths to prevent directory traversal (`pathlib.Path.resolve()` then check prefix)
- Ruff's bandit rules (`S`) cover eval/exec/pickle/random misuse — do not suppress them with `noqa` without a written justification

### Constants and Naming

- Use `UPPER_SNAKE_CASE` named constants instead of magic numbers/strings
- Boolean variables/params: prefix with `is_`, `has_`, `can_`, `should_`
- Private helpers: prefix with `_`; reserve `__` (name mangling) only for avoiding conflicts in subclass hierarchies

## Test conventions: `tests/**/*.py`

### Structure and Organization

- File structure mirrors source: `tests/test_<module>.py`
- Shared fixtures go in `tests/conftest.py`; use the narrowest fixture scope possible
- Function names: `test_<what>_<scenario>_<expected_result>` (e.g., `test_parse_config_empty_string_raises_value_error`)
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
