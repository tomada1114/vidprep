---
name: writing-tests
description: >
  Covers how one pytest test is written in vidprep: naming style, asserting
  through behaviour instead of implementation, hand-worked expected values,
  `pytest.raises(XError, match=...)`, the `tests/conftest.py` fixtures
  (`source_video`, `fake_probe`, `project_dir`, `run_cli`), and the
  fakes-over-mocks pattern built on `src/vidprep/_ffmpeg.py`. Use when writing
  or reviewing a test in `tests/test_*.py`, choosing a fixture, deciding what a
  test should assert, or replacing a real tool call in a test.
metadata:
  platforms: claude-code, codex
---

# Writing Tests

**Owns:** how one pytest test is written — naming, asserting through behaviour rather
than implementation, why an expected value should be hand-worked rather than
recomputed the way the implementation computes it, `pytest.raises(XError,
match=...)`, factory-vs-static fixtures, fakes-over-mocks and the `_ffmpeg`
monkeypatch fake pattern, and `tests/conftest.py`'s four fixtures. **Does not
own:** where a test file lives and which coverage floor applies
(`placing-tests`); the fault-injection and golden-run regression protocol
(`verifying-renders`); the error classes under assertion (`designing-errors`).

## Test names are prose sentences describing behaviour, not `test_<what>_<scenario>_<expected>` templates

AGENTS.md's naming rule (`test_<what>_<scenario>_<expected_result>`) does not match
what the suite actually does. Read any real test file and the dominant style is a
sentence that states the expected behaviour, usually inside a `class TestX:` that
supplies the subject so the sentence doesn't have to repeat it — 19 of the 27
`tests/test_*.py` files use `class TestX:` grouping (`test_asr_backends.py`,
`test_asr_bench.py`, `test_bench_metrics.py`, `test_bootstrap.py`, `test_cer.py`,
`test_fault_injection.py`, `test_golden_sample.py`, and `test_timeline.py` are the
eight that stay module-level). Real names from `tests/test_render.py`:
`test_only_approved_cuts_are_removed`, `test_a_proposed_and_a_rejected_cut_stay_in_the_output`,
`test_the_output_ends_on_a_fade_to_black`, `test_a_manufactured_tail_is_reported_as_a_warning`.
From `tests/test_detect.py`: `test_punctuation_and_held_vowels_do_not_hide_a_filler`,
`test_a_silence_shorter_than_min_duration_is_never_detected`. Write the next test
name the same way: a full sentence, present tense, that would still make sense
read aloud without the class name — never a `test_<method>_<condition>` slug.

## Cite the requirement or design section a test locks down

Test docstrings frequently anchor a class to a requirement ID or a design
document section so the traceability survives a rename. `tests/test_project.py`
has `class TestSubprocessIsolation:` docstringed `"""REQ-030: pipeline stages
spawn processes only through the wrapper."""`, and other classes in the same
file carry `REQ-010 / REQ-011`, `REQ-012`, `REQ-013 / REQ-014`, `REQ-025`.
`tests/test_render.py`'s module docstring points at `verification-plan.md §8`
for the completion conditions under test, and its `SEGMENTS` constant is
commented "one per mapping rule of design.md §4". Add this citation whenever a
test exists specifically because a numbered requirement or a design section
demands it — it tells the next reader why the test may not be deleted just
because it looks redundant with another one.

## Fakes at the `_ffmpeg` boundary, never `unittest.mock.Mock`

There is no `unittest.mock.Mock` or `unittest.mock.patch` anywhere under
`tests/test_*.py` — every process boundary is faked by hand. The one chokepoint
worth faking is `src/vidprep/_ffmpeg.py`; tests patch it with a recording class
via `monkeypatch.setattr`, then assert on what was actually built rather than
how many times something was called. `tests/test_render.py`'s `FakeFfmpeg`
keeps `self.commands: list[list[str]]`, exposes `install(monkeypatch)`, and
answers ffprobe queries from the `trim=start=...:end=...` intervals it finds in
the filter graph it was handed — `VIDEO_TRIM`, `AUDIO_TRIM`, `HELD`, and
`FADE_OUT` are compiled regexes that pull those intervals back out of
`tools.graph` for assertions. `tests/test_audio.py`'s `FakeTools` does the same
for ffmpeg, ffprobe and DeepFilterNet, keying canned loudnorm/astats reports by
*which file* a command reads (`audio.EXTRACTED_NAME`, `audio.DENOISED_DIR`,
`audio.RENDERED_NAME`) so "measure the source" and "measure the result" stay
distinguishable. `conftest.py`'s `fake_probe` fixture is the smallest version:
it replaces `_ffmpeg.probe` with a closure returning a fixed
`_ffmpeg.ProbeResult`. Prefer a fake like these over a mock at the `_ffmpeg`
boundary — it lets the assertion read back the real `-af` chain or `-i`
inputs, which is the thing that actually matters here.

## `tests/conftest.py` has exactly four fixtures, all function-scoped

- `source_video(tmp_path)` — writes placeholder bytes to `tmp_path/material/clip.mp4`; its contents are never decoded, only its path matters.
- `fake_probe(monkeypatch)` — patches `_ffmpeg.probe` to answer with the golden sample's specs (`SAMPLE_DURATION = 298.92`, `SAMPLE_VIDEO`, `SAMPLE_AUDIO`, all module constants in `conftest.py`).
- `project_dir(tmp_path, source_video, fake_probe)` — calls `project.init_project` and returns the resulting `.root`, so most tests start from an already-initialised project.
- `run_cli(monkeypatch, capsys)` — a **factory fixture**: it returns a callable `_run(*args) -> CliResult` that sets `sys.argv`, calls `cli.main()`, catches the `SystemExit`, and strips ANSI colour codes (`ANSI_ESCAPE`) from the captured output before handing back the frozen `CliResult(exit_code, stdout, stderr)` dataclass.

No fixture anywhere in `tests/` declares a non-default `scope=` — grepping
`pytest.fixture(` for a `scope` argument across the whole suite comes back
empty, so every fixture, including these four, is function-scoped. That is the
narrowest-scope default working as intended, not an oversight to fix.

## Assert on the real inputs, hand-work the expected value

`tests/test_render.py`'s `test_only_approved_cuts_are_removed` is the model to
copy: the `CUTS` tuple at module level spells out four cut candidates by hand
(`("c0001", 10.0, 12.0, "approved")` … `("c0004", 100.0, 140.0, "approved")`),
and the test's assertion is the literal, hand-worked result of applying only
the approved ones to `DURATION` — `[(0.0, 10.0), (12.0, 100.0), (140.0,
DURATION)]` — typed out, not produced by calling vidprep's own interval
arithmetic a second time. The module-level `REMOVED = 42.0` constant is the
same idea one level up: "the two approved cuts remove 42 seconds" (2 + 40,
worked by hand from `CUTS`). In a codebase this thick with duration and
loudness arithmetic, recomputing the expected value with the implementation's
own formula only proves the formula agrees with itself — reject in review a
test whose "expected" value is a call into `_intervals`, `_reencode`, or
similar production arithmetic rather than a literal.

## `pytest.raises(XError, match=...)` always carries a message pattern

Never assert only the exception type. Real examples: `tests/test_render.py`
has `pytest.raises(InvariantViolationError, match=r"41\.0ms > 40\.0ms")` and
`pytest.raises(UsageError, match="run \`vidprep audio-fix\` first")`;
`tests/test_audio.py` has `pytest.raises(UsageError, match=r"audio\.denoise
must be one of")`. The pattern is what proves the error is being raised for
the *reason* the test claims, not merely that some exception of the right
class happened to surface.

## Parametrize for input/output variation, name each case with `pytest.param(..., id=...)`

Roughly half the test files use `@pytest.mark.parametrize`. `tests/test_cer.py`
gives every case a readable id — `pytest.param("Claude Code。", "claude code",
id="case-and-full-stop")`, `pytest.param("えーっと…", "えーっと", id="ellipsis")` — and
`tests/test_audio.py` does the same for millisecond-boundary cases:
`pytest.param(SOURCE_SECONDS - 0.001, 1.0, id="one-millisecond-short")`. Prefer
an `id` whenever the raw parameter values wouldn't read as a test name on their
own in a failure report.

## `tmp_path` and `monkeypatch` are standard; `tests/fault_injection/_harness.py` is a deliberate exception

Every ordinary test uses `tmp_path` and `monkeypatch`, matching AGENTS.md.
`tests/fault_injection/_harness.py` instead uses `tempfile.TemporaryDirectory`
and `unittest.mock.patch.dict(os.environ, ...)` — this is not a lapse. Each
`caseNN_*.py` under `tests/fault_injection/` carries `if __name__ ==
"__main__":` and is documented in `tests/fault_injection/__init__.py` to be
runnable standalone (`uv run python -m tests.fault_injection.case02_midword_cut`)
as well as collected by `tests/test_fault_injection.py` — pytest-only fixtures
aren't available outside a pytest run, so the shared harness sticks to
stdlib mechanisms that work in both places.

## `slow` is a registered marker with zero uses; golden-sample tests gate on `skipif` instead

`pyproject.toml` registers `slow: marks tests as slow (deselect with '-m "not
slow"')`, but no test anywhere carries `@pytest.mark.slow` — an unused
facility, not a convention to apply speculatively. For conditional skips,
follow `tests/test_golden_sample.py` instead: it stacks
`pytest.mark.skipif(not GOLDEN.is_file(), reason="golden sample is not
available")` with a second `skipif` on `shutil.which("ffprobe") is None`.

## Coverage is a floor here, not proof a test is good

`[tool.coverage.run]` sets `branch = true` in `pyproject.toml`, but the 80%
floor itself is enforced on the command line via `--cov-fail-under=80` in the
justfile's `test` recipe, not in `pyproject.toml`. `placing-tests` owns the
exact numbers and where a new test file belongs; treat a green coverage run
here only as a floor cleared, never as confirmation the assertions above are
meaningful.

**BACKGROUND:** `tests/test_skills.py` parses each `SKILL.md`'s frontmatter as
text and asserts contract properties across the pipeline skills — an example of
testing prose as a contract rather than code; `authoring-skills` owns that file
in depth.
