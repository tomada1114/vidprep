---
name: correcting-transcripts
description: >
  Covers src/vidprep/correct.py and src/vidprep/_dictionary.py — the
  `plan_dictionary`/`plan_patch`/`apply` pipeline behind `vidprep correct`,
  the `Patch`/`PatchEdit` pydantic models `--apply-patch` reads, the
  `_verify_shape`/`_check_patch` re-validation that must pass before
  transcript.json is rewritten, and the `PatchInvalidError` (`patch_invalid`)
  rejection contract. Use when changing how a correction is planned or
  applied, adding a new reason to reject a patch, or deciding why
  transcript.json can only change through `vidprep correct --apply-patch`.
metadata:
  platforms: claude-code, codex
---

# Correcting Transcripts

**Owns:** `src/vidprep/correct.py` and `src/vidprep/_dictionary.py` — the dictionary
correction pass, the verified-patch application path behind `vidprep correct
--apply-patch`, why `transcript.json` is CLI-owned once transcription completes, and
the `patch_invalid` rejection contract. **Does not own:** the packaged dictionary's
file format and its override mechanics (`packaging-data-files`); the LLM-facing
workflow that produces `patch.json` in the first place (`correct-transcript`).

## Two passes, one shared invariant

`correct.py`'s module docstring states the invariant both passes answer to: "only the
text may change" (design.md §5.3). `plan_dictionary` runs the deterministic,
idempotent dictionary pass (design.md §3.7); `plan_patch`, gated behind
`--apply-patch`, runs the LLM-patch pass. Both return a `Plan` — nothing is written
until `apply(loaded, plan)` is called, and `apply` itself calls `_verify_shape` before
touching disk. `cli.py`'s `correct` command calls whichever `plan_*` function
`--apply-patch` selects; `plan_dictionary` has a second caller outside tests too —
`prep.py:253`'s dictionary pass calls it directly as part of `vidprep prep`'s
composite pipeline, bypassing `cli.py` entirely. `plan_patch` is reached only
through `cli.py`.

## The dictionary pass scans and replaces, `_dictionary.py` decides what counts as a match

`plan_dictionary` loads the `AsrDictionary` (via `_dictionary.load_dictionary`, whose
`DictionaryEntry`/`AsrDictionary` pydantic schema and override resolution belong to
`packaging-data-files`) and calls `_dictionary.correct_text` once per segment. That
function runs two stages in order: `_apply_surface`, a literal left-to-right scan
against each entry's `misrecognized` spellings (longest match wins, via
`_surface_patterns`'s `sorted(..., key=lambda pattern: -len(pattern[0]))`), then
`_apply_readings`, which tokenizes the (already-corrected) text with a `Reader` and
compares each run of tokens' joined reading against `normalise_reading(entry.yomi)`.
Only `confidence: "always"` entries are replaced in either stage; a `confidence:
"context"` match is recorded as a non-applied `Hit` and surfaces in `Plan.skipped` for
`correct-transcript`'s LLM pass to decide.

`normalise_reading` is the piece that lets one `yomi` match both katakana and
hiragana spellings: it NFKC-normalizes, folds hiragana to katakana by adding
`_KANA_OFFSET = 0x60` to any codepoint in `_HIRAGANA_FIRST`-`_HIRAGANA_LAST`
(`0x3041`-`0x3096`), then keeps only characters in `_KATAKANA_FIRST`-`_KATAKANA_LAST`
(`0x30A1`-`0x30FA`) or in `_READING_EXTRAS` ("ーヽヾ") — everything else, including the
"・" that can sit between words in a compound term, is dropped. `_is_joiner` is what
lets a reading run cross that dropped "・": a token with no reading is still allowed
inside a match when its surface is entirely punctuation/separator/symbol Unicode
categories (`_JOINER_CATEGORIES = frozenset({"P", "Z", "S"})`), so "クロード・コード" matches
while "クロードXコード" does not.

`default_reader()` builds the `Reader` from SudachiPy (dependency group details belong
to `managing-dependencies`; the package names are `sudachipy` and `sudachidict-core`).
`_open_tokenizer` catches bare `Exception` and returns `None` on any failure to open a
dictionary flavour — **BACKGROUND:** `designing-errors` documents why this specific
bare `except Exception:` is deliberate rather than a swallow (every failure means the
same thing to this caller: try the next flavour). When no flavour opens,
`plan_dictionary` degrades rather than fails: surface replacement still runs, and a
warning ("no usable SudachiDict: matching by reading is skipped") is added to
`Plan.warnings` only if the dictionary actually has entries to miss.

## The verified patch path is the core contract: re-check before every write

`Patch` (`edits: list[PatchEdit]`) and `PatchEdit` (`id: SegmentId`, `text: str`) are
both pydantic `BaseModel`s with `model_config = ConfigDict(extra="forbid")` —
`patch.json` cannot carry a `start`, `end`, `version`, or any other key, so the schema
itself makes it impossible for a patch to ask for anything but segment text. `edits`
has no default, so a patch that lost its payload is a validation failure rather than a
silent no-op; an explicit `{"edits": []}` still parses and applies cleanly.

`plan_patch` runs the checks in a fixed order, all before a single segment is rewritten:
`_read_patch` parses the file (an `OSError` reading it, or a pydantic `ValidationError`
on the schema, both become `PatchInvalidError`); `_check_patch` then walks every edit
against `{segment.id for segment in transcript.segments}`, collecting every unknown id
and every duplicate id into one `details` list and raising `PatchInvalidError(details)`
once, rather than stopping at the first problem — the class's own docstring in
`errors.py` explains why: a patch is written by a language model, and fixing one
complaint at a time would mean one round trip per mistake. After the edits are
provisionally applied to build the corrected `Transcript`, `apply()` calls
`_verify_shape(plan.original, plan.corrected)`, which compares `_shape()` (the
`(id, start_ms, end_ms)` tuple of every segment) before and after; a `Transcript`
Pydantic model's structure already makes most of this unrepresentable in the patch
schema, so `_verify_shape` failing means a bug in vidprep itself, not a bad patch —
and it still raises `InvariantViolationError` (not `PatchInvalidError`) rather than
writing.

`PatchInvalidError` (`src/vidprep/errors.py`, `code = "patch_invalid"`, exit code
3) overrides `payload()` to return `{"error": "patch_invalid", "detail": self.details,
"applied": 0}` — the `applied: 0` is hardcoded in the payload, not computed, because
raising this exception always means nothing was written. **BACKGROUND:**
`designing-errors` covers the `VidprepError` hierarchy and the `PatchInvalidError`/
`TelopInvalidError` "list of complaints" pattern in general; the specific complaints
this stage can raise are exactly the two `_check_patch` details (`unknown segment id:
…`, `duplicate segment id: …`) plus whatever `describe_validation_error` renders for a
schema violation or unreadable file.

## Why `transcript.json` is CLI-owned once transcription completes

An LLM-produced `patch.json` is never trusted to overwrite `transcript.json` directly.
It must go through `vidprep correct --apply-patch`, which re-validates every edit
against the *current* file content — not the content the patch author last saw —
before writing anything, so a stale patch (the transcript changed since the patch was
generated) or a hand-edited one (a segment id that never existed) fails loudly with
`patch_invalid` instead of silently corrupting the transcript every later stage joins
on. This is exactly the invariant `correct-transcript`'s own contract states as "Never
edit it directly, not even to 'fix up' a rejected patch" — **BACKGROUND:**
`authoring-skills` covers how `tests/test_skills.py` machine-enforces that phrasing for
the workflow skill; here it is enough to know the CLI-side half of the guarantee is
`_check_patch` plus `_verify_shape`, not a convention anyone has to remember by hand.

## `TRANSCRIPT_NAME` is defined twice, independently

`correct.py:49` and `transcribe.py:60` each define their own module-level
`TRANSCRIPT_NAME = "transcript.json"` — verified duplicate, not a shared import. Both
name the same file and nothing currently drifts them apart, but a rename of the
transcript filename would need editing both constants; there is no single source of
truth for the literal string.

## `correct` does not use the shared `_plan_lines`/`{"commands","writes"}` dry-run shape

Most stages' `--dry-run` path returns a `dict` with `"commands"` and `"writes"` keys
that `cli.py`'s `_plan_lines(plan)` renders generically (`writing-cli-commands` owns
that helper). `correct`'s `action()` in `cli.py` does not call `_plan_lines` at all: it
prints `plan.lines(verbose=options.dry_run)` — `Plan`'s own renderer, which lists
warnings, the dictionary source, verification checks, and one line per `SegmentChange`
— and returns `Output(plan.to_dict(applied=0), ["dry-run: nothing was written"])`
instead. `Plan.to_dict()` is shaped for this stage specifically: `{"action", "tool",
"changed", "applied", "segments", "skipped", "warnings", "dictionary_source"}`, not
`{"commands", "writes"}`. `--dict` (`resolve_dictionary_path`, wins over
`profile.json`'s `correct.dictionary_path`, which wins over the packaged dictionary)
and `--dry-run` still follow the CLI's usual option-wiring conventions
(`writing-cli-commands`) and the packaged-resource override order
(`packaging-data-files`) — only the plan payload's shape is stage-specific here.
