---
name: packaging-data-files
description: >
  Covers the packaged JSON resources under src/vidprep/dictionaries/,
  src/vidprep/profiles/, and src/vidprep/styles/ — the {"version", "note",
  <payload>} file shape, loading through importlib.resources.files(__package__),
  and the three different override philosophies (resolve_dictionary_path's
  whole-file replacement, _fillers.py's load_dictionary(project_root) additive
  merge, and _ass.py's merge_styles per-field merge). Use when adding a new
  packaged JSON resource, deciding how a project should be able to override it,
  or touching asr-dict.json, fillers.json, hallucinations.json, or
  styles/default.json and their loaders.
metadata:
  platforms: claude-code, codex
---

# Packaging Data Files

**Owns:** `src/vidprep/dictionaries/`, `src/vidprep/profiles/`, and `src/vidprep/styles/` —
the packaged JSON resource format, loading via `importlib.resources`, and the three
different override philosophies vidprep uses for its three kinds of packaged data.
**Does not own:** the correction pass that actually consumes the misconversion
dictionary (`correcting-transcripts`); telop and style rendering at render time
(`rendering-output`); the pydantic schema definitions themselves (`modelling-artifacts`).

## Every packaged file is `{"version", "note", <payload>}` — but the two fields disagree with each other across files

`asr-dict.json` uses `"version": "1.0.0"`; `fillers.json`, `hallucinations.json`,
`profiles/default.json`, and `styles/default.json` all use `"version": "1"`. Live
with this rather than silently normalising it in an unrelated change — it is a real,
verified discrepancy, not a typo to "fix" as a side effect. The `note` field has the
same kind of split: `asr-dict.json`'s note is written in Japanese ("音声入力で崩れ
やすい固有名詞・専門用語の正式表記リスト…"), while `fillers.json`'s and
`hallucinations.json`'s notes are in English. `AGENTS.md` requires English in code,
docs, commits and PRs but is silent on packaged data payloads, which is exactly how
this drifted.

## Loading always goes through `importlib.resources.files(__package__).joinpath(...)` — except one caller that inlines its path

Four of the five loaders name their resource path as a module-level constant before
calling `resources.files(__package__).joinpath(...)`: `_dictionary.py`'s
`DICTIONARY_RESOURCE = "dictionaries/asr-dict.json"`, `_fillers.py`'s
`FILLER_RESOURCE = "dictionaries/fillers.json"`, `transcribe.py`'s
`HALLUCINATION_RESOURCE = "dictionaries/hallucinations.json"`, and `_ass.py`'s
`PACKAGED_STYLES = "styles/default.json"`. `project.py`'s `default_profile()` is the
odd one out — it calls `resources.files(__package__).joinpath("profiles/default.json")`
with the literal string inline, no named constant. Follow the four-loader pattern
for a new resource; the profile loader's shortcut is a minor inconsistency, not the
convention.

## `asr-dict.json` — whole-file replacement, resolved `--dict` > `profile.json` > packaged

`correct.py`'s `resolve_dictionary_path()` picks one file, in full, to replace the
packaged dictionary outright: `--dict` on the command line wins over
`profile.correct.dictionary_path` in `profile.json`, which wins over the packaged
default (`None` from both means "use the packaged one"). The two override sources
resolve relative paths against different bases, and the docstring says why: "a
relative `--dict` resolves against the current directory, like any other CLI path
argument; a relative `dictionary_path` resolves against the project directory, since
`profile.json` is per-project and must not depend on the shell's cwd." Commit
`2b14e1b feat(correct): read the misconversion dictionary from outside the package
(#39)` is what added this override entirely — before it, `correct` always used the
packaged dictionary, with no `--dict` flag and no `correct.dictionary_path` field on
`CorrectProfile` in `models.py`.

## `fillers.json` — additive merge from a fixed project-relative path, no flag or profile field

`_fillers.py`'s `load_dictionary(project_root)` reads `<project_root>/dictionaries/fillers.json`
(the `PROJECT_DICTIONARY` constant) and adds its `strong`/`weak` entries to the
packaged ones rather than replacing them: `FillerDictionary(strong=[*packaged.strong,
*extra.strong], weak=[*packaged.weak, *extra.weak])`. The docstring states the reason
directly — "its entries are added to the packaged ones rather than replacing them, so
a project only has to write down what is missing." There is no `--dict`-equivalent
flag and no `profile.json` field for this one; the project path is a fixed
convention, not a resolved option.

## `styles/default.json` — per-field merge, and an unknown preset name is added, not rejected

`_ass.py`'s `merge_styles(packaged, override)` walks `override.presets` and, for each
preset the project's `styles.json` mentions, calls `preset.model_dump(exclude_unset=True)`
so only the fields the project actually stated overwrite the packaged default —
every field the project stays silent about keeps the packaged value. A preset name
the packaged file has never heard of is still merged in, per the docstring: "a preset
the packaged file has never heard of is added rather than refused." `load_styles()`
reports which happened through `PACKAGED_SOURCE = "packaged default"` or
`OVERRIDE_SOURCE = "project override"`, surfaced through `render --json` so a user
can see whether a preset came from the package or their own file.

## `hallucinations.json` — no override path at all, cached process-wide

`transcribe.py`'s `hallucination_phrases()` is `@cache`d and reads only the packaged
`dictionaries/hallucinations.json`; there is no CLI flag, no profile field, and no
project-relative file convention for it, unlike the other two dictionaries.

## Picking a philosophy for a new overridable resource

The three real examples pull apart on one axis: how much of the packaged content a
project still wants. Whole-file replacement (`asr-dict.json`) suits a resource the
user fully owns and wants total control over — a personal misconversion list has no
reason to inherit anyone else's entries. Additive merge (`fillers.json`) suits a
resource where the packaged defaults are almost always still wanted, and a project
only needs to add its own exceptions on top. Per-field merge (`styles/default.json`)
suits a resource with several independent named presets where a project typically
wants to nudge one or two fields of one preset without restating the whole preset,
let alone the whole file. There is no shared helper function between the three
mechanisms — `resolve_dictionary_path`, `load_dictionary(project_root)`, and
`merge_styles` are each implemented independently in their own module, so a fourth
resource does not inherit an override mechanism for free; whichever philosophy fits
gets written out again at the point where the new resource is loaded.
