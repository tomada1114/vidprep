---
name: modelling-artifacts
description: >
  Covers src/vidprep/models.py — the _Strict base with extra="forbid", the
  Literal closed vocabularies (Cut.status, Segment.source, Edit.tool,
  AsrProfile.backend), the Seconds/SegmentId/CutId-style type aliases,
  MS_PER_SECOND, to_ms, describe_validation_error, and the rule that a
  pydantic BaseModel defined outside models.py exists only to parse one
  external JSON file format. Use when adding or changing a field on Manifest,
  Transcript, Cuts, Telops, Styles, or Profile, adding a new Literal
  vocabulary, writing a validation context={"duration": ...} check, or
  deciding whether a new small parser (like _autoeditor.py's Timeline or
  correct.py's Patch) should define its own BaseModel.
metadata:
  platforms: claude-code, codex
---

# Modelling Artifacts

**Owns:** `models.py` and every artifact schema it defines — the `_Strict` base,
the `Literal` closed vocabularies used instead of `enum.Enum`, the
`Seconds`/`SegmentId`/`CutId`-style type aliases, `to_ms`, `describe_validation_error`,
and the rule that pydantic `BaseModel` usage outside `models.py` exists only to
parse one specific JSON file, never as a general modelling tool. **Does not own:**
where artifacts live on disk and how they're atomically written
(`managing-the-project-dir`); the original-to-cut timeline mapping
(`mapping-timelines`); the packaged dictionaries/profiles/styles and their
override mechanics (`packaging-data-files`).

The module docstring states the premise the rest of this skill follows from:
"the intermediate JSON *is* the design: every stage reads JSON and writes JSON,
so the invariants that matter (identifier shape, interval sanity, approved cuts
never overlapping) are enforced here rather than in each stage."

## `_Strict` closes every schema against typos, with one deliberate exception

`_Strict(BaseModel)` sets `model_config = ConfigDict(extra="forbid")` and its
docstring says why: "rejecting unknown keys so typos fail loudly." Every schema
class in `models.py` — `Manifest`, `Source`, `StageRecord`, `Segment`,
`Transcript`, `Cut`, `Cuts`, `Telop`, `Telops`, `StylePreset`, `Styles`, every
`*Profile` — inherits it, enforced by `tests/test_models.py`'s
`test_unknown_key_is_rejected`. `Word` is the one class in the file that plugs
`BaseModel` directly with `ConfigDict(extra="allow")` instead, and its docstring
says exactly why: "Word-level timing. Not produced in v1; accepted so backends
may add it." `test_word_timestamps_are_accepted_when_a_backend_adds_them` feeds
a `words` entry carrying an extra `"prob": 0.9` key that no `Word` field
declares, and it validates — the one place vidprep tolerates a field it has no
use for yet, because a future ASR backend may emit it and rejecting it would
mean rejecting the whole segment over a field nobody reads.

## `Literal` is vidprep's enum — `enum.Enum` appears nowhere in `src/vidprep/`

`grep -rn "enum\.\|from enum\|Enum" src/vidprep/` returns nothing. Every closed
vocabulary is instead a `Literal` on the field itself: `Cut.status:
Literal["proposed", "approved", "rejected"]`, `Segment.source: Literal["asr",
"dict", "llm"]`, `Edit.tool: Literal["dict", "llm", "manual"]`,
`AsrProfile.backend: Literal["whisper.cpp", "mlx-whisper"]`,
`AsrProfile.vad: Literal["silero-v5"]`, `VerifyAsrMode = Literal["advisory",
"gate"]`. A `Literal` wins over `enum.Enum` here for reasons an `Enum` cannot
match at this layer: it is validated by the schema itself the moment a JSON
payload is parsed — no separate `try`/`except` on an `Enum` constructor — it
serializes straight into the JSON Schema `vidprep`'s own artifacts describe
themselves with, and it needs no import beyond `typing.Literal`, which every
other type alias in the file already uses. `AsrProfile.vad` is a `Literal` with
exactly one member rather than a plain `str` for the same enforcement reason:
its docstring explains "there is no profile setting, and no flag, that turns it
off (design.md §5.2)" — a single-value `Literal` still rejects any other string
a hand-edited `profile.json` might try.

## Type aliases centre every interval check on integer milliseconds

`Seconds` and `PositiveSeconds` are `Annotated[float, Field(ge=0.0 | gt=0.0),
PlainSerializer(...)]` pairs that round to three decimals on the way out;
`SegmentId`/`CutId` pin the `s0000`/`c0000` shape; `AssColour` and `PresetName`
pin the ASS field syntax those values round-trip through. `to_ms`'s docstring —
"Return *seconds* as whole milliseconds, the unit all comparisons use" — states
the reason the rest of the file follows: comparing stored `float` seconds
directly is unreliable for frame-level cut and telop boundaries, so
`_check_interval`, `VadReport`'s disjointness check, and `Cuts`' approved-overlap
check all convert through `to_ms` before comparing, even though the field itself
stays a `float` in seconds for storage and display. `MS_PER_SECOND = 1000` (an
`int` here) backs `to_ms`. Worth knowing: the same name is redeclared
independently in four other modules, each as a `float` `1000.0` rather than
imported from `models.py` — `_preview.py`, `_reencode.py`, `audio.py` all define
`MS_PER_SECOND = 1000.0`, and `_asr.py` defines `_MS_PER_SECOND = 1000.0`. That
is a real duplication (int in `models.py`, float everywhere else) adjacent to
this skill's territory, not something this skill fixes.

Interval checks that depend on the source duration (`_check_interval`,
`Telop._validate_timing`) read it from `ValidationInfo.context` rather than from
a field on the model being validated, because the duration lives on `Manifest`,
not on `Transcript`/`Cuts`/`Telops` — callers pass
`Cuts.model_validate(payload, context={"duration": manifest.source.duration})`,
and the bound is silently unchecked when a caller omits the context.

## `describe_validation_error` turns pydantic's errors into vidprep's message shape

Its docstring: "Render a pydantic error as a single line naming location and
constraint." It joins every `ValidationError` entry's dotted `loc` and stripped
`msg` with `"; "`, which is what lets `test_field_errors_name_their_location`
assert `describe_validation_error(caught.value).startswith("cuts.0.id: ")`.
Every module that hand-parses its own JSON calls it at the `except
ValidationError` site to build a `VidprepError` message or `--json` payload
entry: `project.py`'s artifact loader, `detect.py` and `transcribe.py` reading
their own dictionaries, `correct.py`'s `patch.json` loader (feeding
`PatchInvalidError`), `verify.py`, and the five external-format parsers below.
It exists so a pydantic `ValidationError` — verbose, nested, meant for a
developer — never reaches a user or a `--json` consumer verbatim.

## `BaseModel` outside `models.py` is legitimate only at a real parsing boundary

Five modules define their own small `BaseModel` subclasses, each to parse
exactly one external JSON format, and none of them import `models._Strict` —
each re-declares `model_config = ConfigDict(extra="forbid")` inline instead:
`_autoeditor.py`'s `_Clip`/`Timeline` (auto-editor's own `--export v3` document
— note this `Timeline` is a different class from the exported, public
`timeline.py`'s `Timeline`, a real naming collision worth knowing about before
grepping for one and finding the other), `_dictionary.py`'s
`DictionaryEntry`/`AsrDictionary` (the misconversion dictionary format),
`_fillers.py`'s `FillerDictionary` (the filler-word dictionary format),
`correct.py`'s `PatchEdit`/`Patch` (the `patch.json` format), and
`transcribe.py`'s `_Hallucinations` (the hallucination-phrase dictionary
format). `project.py` also imports `BaseModel`, but only under
`TYPE_CHECKING` to type `ARTIFACT_MODELS: Mapping[str, type[BaseModel]]` — it
declares no model of its own, so it is not a sixth.

The rule this pattern implies going forward: a `BaseModel` in a non-`models.py`
module earns its place only when it is parsing an external file format at a
real serialization boundary — general internal typing is `writing-python`'s
`dataclass`/`TypedDict` territory, not this. When adding a new such parser,
prefer `class MyDoc(_Strict):` over re-declaring the same
`ConfigDict(extra="forbid")` a sixth time — the five existing ones predate this
observation and are not being asked to refactor, but a new one has no excuse to
repeat it.

## Which artifact each schema validates

`Manifest` is `vidprep.json`, `Profile` is `profile.json`, `Transcript` is
`transcript.json`, `Cuts` is `cuts.json`, `Telops` is `telops.json`, `Styles` is
`styles.json`, and `NoiseFloorReport`/`VadReport` are the `report/` JSON files —
`project.py`'s `ARTIFACT_MODELS` table lists the subset validated before a stage
runs. Where these files live on disk, how they're written atomically, and how
staleness is tracked across them is `managing-the-project-dir`'s territory, not
this one.
