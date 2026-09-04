---
name: verifying-renders
description: >
  Covers src/vidprep/report.py, _review.py, _boundaries.py, verify.py and
  _retranscribe.py, plus the local regression protocol built on top of them —
  the review gate and report/stats.json, `report --cuts`, `--verify-asr`'s
  advisory-vs-gate modes, the six tests/fault_injection/ cases, and `just
  golden`/`just golden-diff`. Use when changing what stats.json measures,
  wiring a new --cuts listing field, touching _boundaries.py's waveform or
  digest commands, deciding what VerifyResult.gating_flags should count,
  adding a tests/fault_injection/caseNN_*.py, or running a golden-run
  regression.
metadata:
  platforms: claude-code, codex
---

# Verifying Renders

**Owns:** `report.py`, `_review.py`, `_boundaries.py`, `verify.py`,
`_retranscribe.py`, plus the local regression protocol — the review gate and
`report/stats.json`, `report --cuts`, `--verify-asr`'s advisory-vs-gate modes,
the six `tests/fault_injection/` cases, and `just golden`/`just golden-diff`.
**Does not own:** how a pytest unit test is written or placed (`writing-tests`,
`placing-tests`); the rendering pass itself (`rendering-output`).

## `report.py` has two jobs, and neither writes back to the project

`run_report()` aggregates every prior stage into `report/stats.json`
(`STATS_NAME`, `STATS_VERSION = "2"`), and `run_review()` — a separate entry
point behind `report --cuts` — lists cut candidates for a reviewer, with no
overlap between the two beyond reading the same `Inputs`. `STATS_VERSION`
exists to be bumped whenever a section changes shape rather than value, the
way `noise_floor` did. The stage is read-only by design (REQ-040): it never
touches `cuts.json`, `transcript.json` or `vidprep.json`, and a missing input
— running `report` before `detect` or before `render` is normal — leaves a
section `null` or empty plus a line in `Inputs.warnings` rather than raising,
so the exit code stays `0`. **BACKGROUND:** `review-cuts` is the human/LLM
workflow that consumes `report --cuts --json`'s output; this skill only
covers what `_review.py` computes and emits, not how a reviewer acts on it.

## `_review.py` pairs every cut with the transcript it would delete

`review()` sorts cuts by `(start, end, id)` and, for each, finds the segments
it swallows whole (`removed`), plus the last surviving segment before it and
the first after (`Entry.before`/`Entry.after`). `removed=None` means no
transcript was available to check against; `removed=()` means it was checked
and removes no speech — the module's docstring calls this the REQ-020
boundary, and the two are rendered differently (`NO_TRANSCRIPT` vs
`NO_SPEECH`). `_review.py` only reads: deciding a candidate's `status` is
somebody else's job, and this module never writes `cuts.json`.

## `_boundaries.py` renders what a cut would remove, one waveform and one digest

Every cut gets a `Boundary` window — the cut plus `BOUNDARY_MARGIN = 2.0`
seconds on each side, clamped to the material — from which
`waveform_command()` draws a `showwavespic` still into `report/boundaries/`
named by `Boundary.image_name` (`{cut_id}.png`), and `clip_command()` cuts
the same window out of the *source* video, not the rendered output, because
"a boundary is only reviewable while the part about to be removed is still in
front of you." Clips are staged under a workspace as
`CLIP_FORMAT = "clip-{index:04d}.mp4"`, separated by `SEPARATOR_SECONDS = 0.5`
of black silence, and stitched with the concat demuxer's `-c copy` — every
piece shares `DIGEST_VIDEO_CODEC`/`DIGEST_AUDIO_CODEC` for exactly that
reason — into `report/boundary_digest.mp4`, published with
`project_module.atomic_replace`.

## Both modules define their own `_RECOVERABLE` tuple to survive a bad ffmpeg call

`report.py` and `_boundaries.py` each independently define
`_RECOVERABLE = (ExecutionFailedError, UsageError)` — the pattern
`designing-errors` documents generally — and catch it at every
measurement/drawing call site, turning a failure into a warning string
instead of aborting: `report.py`'s `_measure` returns
`(None, [f"{path.name} could not be measured ({exc})"])`, and
`_boundaries.py`'s `_waveforms`/`_clips` each append
`f"{boundary.cut_id}: waveform not drawn ({exc})"` (or the clip equivalent)
and move on to the next window — one missing PNG or one failed clip costs
only that window. `_digest` fails differently: it has no per-window
identity to attach a message to, so any recoverable failure while
concatenating the pieces aborts the whole digest with a single
non-`cut_id`-prefixed warning ("the boundary digest was not built (…)" or,
when nothing survived to concatenate, "no boundary clip could be cut; the
digest was not built") rather than skipping just one piece.

## `--verify-asr` re-transcribes the render and checks it against what the cuts should have kept

`verify.run_verify_asr(subject)` builds the expectation with
`_retranscribe.build_expected()` — the kept segments from
`Timeline.map_segments`, with any filler an approved `reason: filler` cut
removed stripped back out — transcribes `out/output.mp4` a second time with
the exact settings `transcript.json` recorded, and diffs the two texts with
`difflib` to find `missing_hunks()`. `flag_boundaries()` keeps the hunks that
land within `BOUNDARY_WINDOW = 2.0` seconds of a cut edge as `BoundaryFlag`s.
A hunk made only of interjections (`NEGLIGIBLE_TOKENS`, e.g. `はい`) is still
reported but marked `negligible=True`, and `VerifyResult.gating_flags`
excludes it — `VerifyResult.passed` is `not gating_flags`.

The mode is typed `VerifyAsrMode = Literal["advisory", "gate"]` in
`models.py`, with `ADVISORY`/`GATE` string constants in `verify.py`, and
`Profile.render.verify_asr_mode` **defaults to `"gate"`**, not advisory — a
project has to opt *out* by setting `render.verify_asr_mode = "advisory"` in
`profile.json`. `cli.py`'s `render` and `prep` commands each check
`output.result["verify_asr"]["mode"] == verify_module.GATE` and
`gating_flags` after their own `_run` call, raising
`typer.Exit(EXIT_VALIDATION)` directly — not through a raised
`VidprepError`. **BACKGROUND:** `designing-errors` covers why this sits
outside the `_run`/`VidprepError` funnel and what `EXIT_VALIDATION` means.

## `_retranscribe.build_expected()` shares a known gap with two other skills

`ExpectedText.locate()` raises a bare `IndexError` when the expected text is
empty, and `character_error_rate()` raises a bare `ValueError` for the same
reason — neither is a `VidprepError` subclass. `designing-errors` and
`mapping-timelines` both already document this as the same underlying gap
from their own angles; this skill doesn't re-derive it, only confirms
`_retranscribe.py` is one of the modules where it lives.

## The six `tests/fault_injection/` cases prove vidprep's own checks catch six real failure modes

`case01_skipped_audio_fix.py` renders from raw, un-normalised audio and
expects the loudness check to raise `InvariantViolationError`.
`case02_midword_cut.py` writes a hand-made `manual` cut through the middle of
a sentence and checks both that `--verify-asr`'s comparison flags the lost
words and that `detect.verify_speech` separately refuses the same overlap
once it is a cut it owns. `case03_srt_drop.py` deletes one entry from
`out/subtitles.srt` by hand and expects `verify.missing_subtitle_entries()`
to name the missing segment. `case04_bad_patch.py` hands `correct` a patch
naming an unknown segment id and a duplicated one, expecting
`PatchInvalidError` with both complaints reported at once.
`case05_overlapping_cuts.py` writes two `approved` cuts that overlap and
expects `Cuts` schema loading to raise `SchemaInvalidError` before any stage
runs. `case06_swapped_source.py` replaces the source file after `init` and
expects the manifest's sha256 check to raise `HashMismatchError`. Each case
is a script that also
runs standalone (`uv run python -m tests.fault_injection.case02_midword_cut`)
outside pytest — **BACKGROUND:** `placing-tests` covers why fault-injection
cases keep this dual shape. `tests/test_fault_injection.py` collects all six
into `CASES` and asserts `case.main() == 0` for each, plus
`len(CASES) == 6` to assert verification-plan.md §10's table stays fully
covered. The shared `tests/fault_injection/_harness.py` fakes the process
boundary — `FakeMedia`, `build_project`, `workspace`, `refusal` — so every
case runs without ffmpeg, whisper.cpp or auto-editor; **BACKGROUND:**
`writing-tests` covers that fake-over-mock mechanics from the pytest angle.
What these cases prove is narrower than "the pipeline is correct" — each one
proves that one specific way a run can go wrong is caught, nothing more.

## `just golden` and `just golden-diff` are the slow, real-media check nothing else substitutes for

`just golden` runs `scripts/golden_run.py`, which runs the whole pipeline
over a fixed real sample and archives the result under
`fixtures/runs/<date>/`. `just golden-diff` runs `scripts/compare_stats.py`,
which flattens every leaf of `report/stats.json` between the two most recent
runs — a list contributes its length rather than its items, so a growing
warning list shows up directly — plus the `verify_asr` section, and calls
out any path whose name contains `warning`, `flag` or `error` (`_ALERTING`)
when it grows. README.md states the reason both are excluded from CI
directly: "Both are local-only: they need the material, ffmpeg, whisper.cpp
and auto-editor, so they are not part of `just check`."

## Four layers, each catching what the one before it cannot

`report.py`'s gate operates on already-computed cut and render results
before a human approves anything; `--verify-asr` operates after render by
re-checking the actual output file; `tests/fault_injection/` is a permanent
regression suite proving each known failure mode stays caught; and golden
runs are the slow, human-triggered, real-media check of the whole pipeline
together that none of the other three substitute for.
