---
name: mapping-timelines
description: >
  Covers src/vidprep/timeline.py — the original/cut timestamp mapping
  specification (docs/design.md §4), the module's machine-enforced purity
  invariant (tests/test_timeline.py's test_timeline_module_imports_nothing_impure,
  REQ-044), Timeline.forward/inverse, Timeline.map_segments, the
  SegmentWarning TypedDict and TimedSegment NamedTuple, and the bare
  ValueError contract every caller of Timeline() must decide how to handle.
  Use when reading or changing timeline.py, calling Timeline() or
  map_segments from a new module, or deciding whether a ValueError out of
  it needs to be caught.
metadata:
  platforms: claude-code, codex
---

# Mapping Timelines

**Owns:** `timeline.py` — the original↔cut timestamp mapping specification (docs/design.md
§4), the module's deliberate purity constraint that `tests/test_timeline.py` enforces via
an AST-based forbidden-imports check, `Timeline.map_segments`, the `SegmentWarning`
TypedDict, and the bare `ValueError` contract every caller of this module must guard
against. **Does not own:** producing the cut list this module maps against
(`detecting-cuts`); applying the mapping to actually render output (`rendering-output`).

## `timeline.py` is machine-enforced pure — no I/O, no subprocess, no filesystem

`tests/test_timeline.py::test_timeline_module_imports_nothing_impure` parses the module
with `ast.parse`, walks every `Import`/`ImportFrom` node, and asserts none of
`FORBIDDEN_IMPORTS` — `asyncio`, `io`, `multiprocessing`, `os`, `pathlib`, `shutil`,
`socket`, `subprocess`, `tempfile`, `urllib` — appear, then separately asserts no bare
`open(` call exists in the tree. The test's own docstring names the requirement:
"REQ-044: the mapping must not reach for processes or the filesystem." This is not a
style preference enforced by review — it is a test that fails the build if violated. The
reason is that `Timeline` is exported public API (`from .timeline import Timeline` and
`"Timeline"` in `__all__`, `src/vidprep/__init__.py`), and video rendering and subtitle
output must agree on where a moment in the source ends up after cuts, so both share this
one implementation (the module's own docstring: "if they ever disagree, the subtitles
drift"). Keeping it pure means it can be unit-tested with plain floats and tuples and
reasoned about independently of ffmpeg, ASR, or the project directory layout. Never add an
import to `timeline.py` without checking it against `FORBIDDEN_IMPORTS` first — even a
seemingly harmless one like `pathlib` for a type hint fails the test.

## `Timeline` hand-writes `__slots__` instead of using `@dataclass` — the one outlier

Unlike the dataclass convention `writing-python` documents for internal value objects,
`Timeline` is a plain class with `__slots__ = ("_cuts", "_images", "_removed", "_starts",
"duration")` and a hand-written `__init__`. The reason is visible in the body: only
`duration` is stored as given; `_cuts` is `normalize_cuts(cuts, duration)` (merged, not the
raw input), and `_starts`, `_removed`, `_images` are all derived sequences built from
`_cuts` via `itertools.accumulate` and a `zip` — none of the five slots mirrors an
`__init__` parameter unchanged the way a dataclass field normally would. A dataclass
buys nothing here because there is no 1:1 field-to-parameter mapping to generate.

## `map_segments`: five mapping cases from design.md §4, table `## 4.`

`Timeline.map_segments(self, segments: Sequence[tuple[str, float, float]], min_display:
float = DEFAULT_MIN_DISPLAY) -> tuple[list[TimedSegment], list[SegmentWarning]]` takes
`(segment_id, start, end)` triples in original-timeline seconds and returns the mapped
segments in cut-timeline order (rounded to milliseconds) plus warnings in input order.
`DEFAULT_MIN_DISPLAY = 0.8` (seconds). Docs/design.md §4's table, paraphrased against
what the code actually does:

- A segment a cut swallows whole (or matches exactly) is dropped from the output and
  recorded as a `"dropped_by_cut"` warning — a signal of "a cut that deletes speech."
- A segment whose end overlaps a cut is clipped to that cut's boundary before mapping
  (`Timeline._clip`), not dropped.
- A segment that contains a cut entirely is **not split** — it stays one entry, whose
  display time is simply shortened by the forward mapping.
- An entry whose final display time falls under `min_display` is still emitted, with a
  `"min_display"` warning — mapping never auto-deletes on this basis.
- Entries left touching after mapping are separated by `SEPARATION_MS` (1 ms), trimming
  the earlier entry's end so the result stays strictly increasing (`_separate`).

`TimedSegment(NamedTuple)` has fields `segment_id: str`, `start: float`, `end: float`.
`SegmentWarning(TypedDict)` has `segment_id: str`, `kind: Literal["dropped_by_cut",
"min_display"]`, and two `NotRequired[float]` fields, `value` and `threshold`, present
only when `kind == "min_display"` — the docstring on the class says this explicitly.

## `forward`/`inverse` implement design.md §4's `f(t) = t - removed(t)`

`removed(t)` is the total length cut out of `[0, t)`; a `t` inside a cut lands where the
cut's end lands, keeping the mapping continuous at every boundary — boundary fades never
overlap (design.md §1, decision 9), which is what lets the mapping be a per-interval
translation instead of anything more elaborate. `inverse` undoes this from the same
interval table so `report.py`'s boundary display and the re-transcription check
(verification-plan.md §8.1) read identical boundaries. Both raise `ValueError` when the
argument falls outside `[0, duration]` / `[0, cut_duration]`.

## The `ValueError` contract: no caller gets a `VidprepError` for free

`Timeline.__init__`, `_check_interval` (used by `normalize_cuts` and `map_segments`),
`forward`, and `inverse` all raise bare `ValueError` — never a `VidprepError` subclass —
for a non-positive duration, an inverted/out-of-range interval, or an out-of-range
timestamp. Anyone calling into `timeline.py` must decide explicitly what happens to that
exception; there is no default net. `report.py`'s `_subtitles()` shows the deliberate
choice: it wraps `Timeline([(cut.start, cut.end) for cut in inputs.approved], duration)`
in `except ValueError as exc:` and turns it into `_empty_subtitles(), [f"the subtitles
could not be mapped ({exc})"]` — a warning line in the report output, not a crash.
`render.py`'s `_timeline()` builds the same kind of `Timeline(...)` call (from
`_reencode.align_to_frames(approved, ...)` and `loaded.manifest.source.duration`) with no
surrounding `try`/`except` at all, and neither of its two call sites (`render.py:510`,
`render.py:589`) adds one either — a `ValueError` there sails past `cli.py`'s `_run`
funnel uncaught and surfaces as an unhandled traceback instead of a clean `--json` payload
or a `✖ ` line. Any new code that constructs a `Timeline` should copy `report.py`'s
pattern — catch `ValueError` locally and translate it, or accept and document that an
uncaught one will crash the command outright. **BACKGROUND:** `designing-errors` covers
this same gap from the exit-code funnel's side, alongside the analogous bare `IndexError`
in `_retranscribe.py`.
