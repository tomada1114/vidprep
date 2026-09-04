---
name: detecting-cuts
description: >
  Covers src/vidprep/detect.py, _autoeditor.py, _fillers.py, and _intervals.py —
  auto-editor's `--export v3` timeline as silence-detection input, the
  Span/Candidate interval arithmetic, the MERGE_IOU/MAX_SPEECH_OVERLAP/
  SILENCE_CONFIDENCE constants, the speech-collision guard `verify_speech`
  enforces before cuts.json is written, filler-cut boundaries snapped to VAD
  regions, the SILENCE/FILLER/MANUAL reason constants, and the `_ID_FORMAT =
  "c{:04d}"` cut-id scheme. Use when changing how a silence or filler cut is
  proposed, touching `merge`/`_resolve_overlaps`/`verify_speech` in detect.py,
  parsing auto-editor's timeline, or deciding why a candidate's interval moved.
metadata:
  platforms: claude-code, codex
---

# Detecting Cuts

**Owns:** `detect.py`, `_autoeditor.py`, `_fillers.py`, and `_intervals.py` —
interval arithmetic and disjointness, mid-word cut safety, the `reason`/`status`
values this stage actually produces, cut id assignment, and auto-editor's v3
timeline as vidprep's silence-detection input. **Does not own:** actually
applying cuts to produce a rendered video (`rendering-output`); the
original-to-cut timestamp mapping math (`mapping-timelines`); the human/LLM
judgement pass that sets a cut's `status` and `note` (`review-cuts`).

## auto-editor's v3 timeline is the input — and its `Timeline` collides in name

`_autoeditor.py` defines its own pydantic `_Clip` and `Timeline` models to parse
auto-editor's `--export v3` JSON output; this `Timeline` is a different class
from the exported `Timeline` in `timeline.py` (the original↔cut mapping that
`mapping-timelines` owns) — a real naming collision worth knowing about before
grepping for "Timeline" and reading the wrong hit. `command(audio, silence)`
builds the actual CLI invocation (`--edit audio:threshold=...`, `--margin 0s`,
`--export v3`, `-o -`, `--no-open`, `--quiet`, `--progress none`); `detect.py`'s
`_silence_candidates` is what actually runs it, via `_ffmpeg.run` (**BACKGROUND:**
`running-subprocesses` for how that call is made and its output captured).
`parse_timeline(raw, version)` accepts only the shape vidprep knows and raises
`TimelineSchemaError` on anything else — malformed JSON, a wrong `version`, or a
field pydantic's `extra="forbid"` rejects — because a v3 export whose shape
changed is a timeline whose meaning may have changed, and the conversion layer
would rather stop than guess at cut positions (**BACKGROUND:** `designing-errors`
for the class itself; `TimelineSchemaError` sits under `ExecutionFailedError`,
exit 2). `kept_spans` turns the parsed timeline into the intervals auto-editor
kept, and `silence_spans` takes their complement against the material's
duration, dropping gaps shorter than `min_duration` — `--margin 0s` is
deliberate: letting auto-editor apply padding would make the padding
undetectable in the detector's own output, so `pad_spans` applies `pad_pre`/
`pad_post`/`tail_pad` in vidprep instead (design.md §5.4).

## `_intervals.py`'s `Span`/`Candidate` are the shared vocabulary — `Interval` is not this module's

Despite the name, the type alias `type Interval = tuple[float, float]` lives in
`_reencode.py`, not here — `_intervals.py` instead defines `Span` (a
`start`/`end` dataclass with `.duration` and `.overlap()`) and `Candidate` (an
unassigned cut proposal, `status: Literal["proposed", "approved"]` — never
`"rejected"`, because only a human review can reject one). `merge_spans` sorts
and joins touching-or-overlapping spans into a disjoint ordered list;
`complement` returns the gaps between spans inside `[0, duration]`; `subtract`
removes a sequence of spans from one span, used to trim a filler candidate
against the silence cuts detected in the same run; `intersection_over_union`
compares two spans at millisecond resolution so a threshold like 0.500 is never
a float coin toss. Three constants in `detect.py` drive these primitives:
`MERGE_IOU = 0.5` is how much of their union two intervals must share for
`_pair_up` to treat a fresh candidate as the same cut an existing (possibly
already-reviewed) entry represents; `MAX_SPEECH_OVERLAP = 0.2` (seconds) is how
much real speech one `silence` cut may graze before the whole run is refused;
`SILENCE_CONFIDENCE = 0.95` is the fixed confidence every silence candidate
carries, because silence detection is "the reliable half of this stage" (its
own comment, design.md §1 decision 8) — which is also why silence candidates
are built with `status="approved"` directly in `run_detect`, not `"proposed"`.

## Mid-word safety is two different mechanisms, not one shared guard

Silence cuts are protected by refusal, not by boundary-snapping:
`verify_speech` measures every `reason == SILENCE` cut against `spoken_spans()`
— the intersection of transcript segments and Silero VAD speech regions, not
raw segment timestamps, because a VAD-anchored segment's `end` can stretch
across tens of seconds of real silence (design.md §5.4 cites a golden-sample
segment spanning 55s) — and raises `InvariantViolationError` if any cut exceeds
`MAX_SPEECH_OVERLAP`, leaving `cuts.json` untouched. Filler cuts are protected
by snapping instead: `_leading_span`/`_trailing_span` in `_fillers.py` only
propose a candidate when the segment holds at least `_MIN_REGIONS = 2` VAD
regions, and the cut boundary lands on a real region boundary inside the
segment — there is no honest word-level end without word timestamps, so a
segment the VAD never split is silently dropped, not proposed as a
candidate at all (unlike a mid-sentence filler, which is at least counted
even though it is never cut). Neither mechanism
reaches a `reason: "manual"` cut: `speech_overlaps` filters to `reason ==
SILENCE` explicitly, so a hand-written cut through the middle of a word is
outside this stage's guard entirely — `tests/fault_injection/case02_midword_cut.py`
injects exactly that (`MIDWORD_CUT = ("c0003", 12.0, 12.4, "manual",
"approved")`, crossing 0.4s of speech against the 0.2s limit) to prove
`verify_speech` would refuse the same interval as a `silence` cut, while a
`manual` one only gets caught later by re-transcription (owned by
`verifying-renders`, not this skill).

## Filler detection reads the dictionary and proposes only two shapes of cut

`_fillers.py` matches the merged packaged-plus-project dictionary
(**BACKGROUND:** `packaging-data-files` for the additive-merge mechanism
`load_dictionary` implements) against segment text, but only ever proposes a
cut for a segment that is nothing but filler (`Reading.is_whole`) or a filler
sitting at a segment's edge next to enough silence (`require_adjacent_silence`)
— a mid-sentence filler (`Reading.inside`) is counted and reported, never cut,
because there is no honest way to say where inside a sentence it starts.
`REASON = "filler"` is the module's own constant; `detect.py` imports it as
`FILLER = _fillers.REASON` rather than re-declaring the string. `prep.py`
does not follow that pattern: it independently redeclares `FILLER_REASON =
"filler"` from scratch (`src/vidprep/prep.py:68`) instead of importing either
`detect.FILLER` or `_fillers.REASON` — a real duplication, not something this
skill fixes, just worth knowing when `"filler"` needs to change.

## What this stage actually writes into `reason` and `status`

`detect.py` defines `SILENCE = "silence"` and `MANUAL = "manual"` alongside the
imported `FILLER`, but `Cut.reason` in `models.py` is typed as a plain
`str = Field(min_length=1)`, not a `Literal` — the schema deliberately leaves
`reason` open for a future detector to add a value without a schema change
(design.md §8; the closed vocabulary lives on `Cut.status: Literal["proposed",
"approved", "rejected"]` instead, which `modelling-artifacts` owns). This stage
never writes `"rejected"` — that is exclusively a `review-cuts` decision — and
it writes `"approved"` only for fresh `silence` candidates; every fresh
`filler` candidate is born `"proposed"`. An `approved` cut can still come back
out of `merge` as `"proposed"`: `_resolve_overlaps` demotes the later of two
clashing approved cuts (REQ-040) so the "approved cuts never overlap" invariant
survives a re-run without discarding a human's earlier review. `report.py`
separately restates `REASONS = ("silence", "filler", "manual")` and `STATUSES =
("approved", "proposed", "rejected")` as untyped tuples (`src/vidprep/report.py`
lines 58-59) — a known duplication of what these constants and `Cut.status`'s
`Literal` already express, not something in scope to fix here.

## Cut ids are assigned once, never reused

`_ID_FORMAT = "c{:04d}"` produces ids like `c0001`; `_next_id` scans existing
cuts for the highest number in use and `merge` hands out identifiers above it
one at a time, capped at `_MAX_CUT_ID = 9999` — past that, `merge` raises
`InvariantViolationError` rather than reusing a withdrawn proposal's number for
a different interval (REQ-023).

## Errors this stage raises

`UsageError` when `audio-fix` has not produced `audio/processed.wav` yet, or
auto-editor is missing or unusable; `TimelineSchemaError` when auto-editor's v3
export does not match the shape `_autoeditor.py` expects; `InvariantViolationError`
when a silence cut would remove too much speech, or when cut ids would exceed
`_MAX_CUT_ID`; `SchemaInvalidError` when the merged cuts fail `Cuts.model_validate`
before `_publish` writes them (**BACKGROUND:** `designing-errors` for the
exit-code tiers these fall under).
