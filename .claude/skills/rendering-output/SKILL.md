---
name: rendering-output
description: >
  Covers src/vidprep/render.py, _reencode.py, _subtitles.py, _ass.py and
  _preview.py — the render stage that applies approved cuts and writes
  out/output.mp4, subtitles.srt, subtitles.nowrap.srt, transcript.txt,
  telops.ass and preview.mp4. Use when touching the Renderer protocol,
  ReencodeRenderer, MAX_AV_DELTA_MS, LOUDNESS_TOLERANCE_LUFS, the closing-tail
  fade, Subtitles.to_text, TelopInvalidError ("telop_invalid"), or the
  --no-wrap/--preview/--verify-asr flags on `vidprep render`.
metadata:
  platforms: claude-code, codex
---

# Rendering Output

**Owns:** `render.py`, `_reencode.py`, `_subtitles.py`, `_ass.py`, `_preview.py` — the
`Renderer` protocol, the A/V-sync and loudness-tolerance invariants
(`MAX_AV_DELTA_MS`, `LOUDNESS_TOLERANCE_LUFS`), the SRT/`transcript.txt`/ASS telop-track
outputs, telop validation and the `telop_invalid` rejection, and the closing-tail fade.
**Does not own:** which cuts get applied (`detecting-cuts`); the timestamp-mapping math
the render pass consumes (`mapping-timelines`); style-preset packaging and its override
mechanics (`packaging-data-files`); the review gate that runs after render to check its
output (`verifying-renders`); the LLM-facing workflow that authors `telops.json`
(`place-telops`).

## `Renderer` is the seam for a future smart-cut renderer, not an abstraction to grow now

`_reencode.py` defines `Renderer` as a structural `Protocol` with one method,
`render(self, job: RenderJob) -> RenderResult` — no ABC, the general convention
`writing-python` covers. `render.py` depends on it through the annotation `renderer:
Renderer = encoder`, not through `ReencodeRenderer` directly, specifically so design.md
§5.5's `SmartCutRenderer` can drop in later "同一プロトコルで差し替える" (§8 lists the same
seam again as a future extension point) without
`cuts.json`'s schema or `timeline.py`'s mapping changing at all. `ReencodeRenderer` is
the only implementation today: it re-encodes every kept interval with `trim`/`atrim` +
`concat`, video through `libx264` at the profile's CRF/preset, audio from
`audio/processed.wav` rather than the container (§2.1) so nothing downstream of
`audio-fix` ever times against un-normalised sound.

## Two tolerances gate `InvariantViolationError`, checked together before either is reported

`_reencode.py` defines `MAX_AV_DELTA_MS = 50.0` and `LOUDNESS_TOLERANCE_LUFS = 0.5`.
`_verify()` (called from `ReencodeRenderer.render` before the workspace file is moved
into place) checks three things in one pass — the output length against
`Σkeep + closing.pad` within one frame, `video_duration` vs `audio_duration` within
`MAX_AV_DELTA_MS`, and `integrated_lufs` vs the loudnorm target within
`LOUDNESS_TOLERANCE_LUFS` — and raises `InvariantViolationError` listing every problem
found, not just the first: "a render is expensive enough that 'and this is also wrong'
is worth knowing in one go." A failed check leaves the previous `output.mp4` untouched;
the workspace directory (`.render-*`, `project.atomic_replace`) is only swapped in on
success. `_reencode.py` rounds these comparisons with its own `DELTA_DECIMALS = 3` and
`LUFS_DECIMALS = 2`. `render.py` does **not** import either — it separately declares its
own `LUFS_DECIMALS = 2` (same name, same value, not shared) and a `SECONDS_DECIMALS = 3`
used only to round the numbers `--json` prints, coincidentally equal to
`_reencode.py`'s `DELTA_DECIMALS`. This is a real, verified duplication, not a bug to
fix here — the two modules round for different purposes (an invariant check vs. a
display value) and happen to agree.

## The closing fade (commit `85a5df5`) replaced a hard cut with `fade`/`tpad`/`apad`

Before `85a5df5`, the output ended the instant the last kept interval did. Now
`Closing.plan(fade_out, tail)` (in `_reencode.py`) computes `pad = max(0, fade_out -
tail)`, where `tail` is `RenderJob.tail` — how long the cut timeline runs past the last
mapped transcript segment, computed by `render._closing_tail()` because the renderer is
only ever handed kept intervals and cannot tell which part of one is speech.
`Closing.chains()` appends a `fade=t=out`/`afade=t=out` pair to the `concat` output when
there is enough silent tail to fade over; when there is not (`tail < fade_out`), it first
pads with `tpad=stop_mode=clone` (holding the last frame, never black) and `apad`.
`ReencodeRenderer.closing()` rounds `pad` up to a whole frame against `self.fps` before
`filtergraph()` ever sees it — the same `math.ceil(round(..., ALIGNMENT_DECIMALS))`
pattern `align_to_frames()` uses for cut boundaries — so `expected_duration = Σkeep +
pad` matches the file exactly instead of being up to a frame short, and a `fps` filter
is forced in front of `tpad` — a comment on `_reencode.py`'s `RATE_FILTER` constant
records the ffmpeg 7.1.1 finding that `tpad` behind `concat` otherwise reads no frame
duration and silently adds nothing to the video stream while `apad` still pads the
audio, which is exactly the silent AV drift the tolerance above exists to catch. `render.fade_out = 0` disables both
the fade and the padding, reproducing the pre-`85a5df5` behaviour.

## `transcript.txt` (commit `7f85fff`) is prose built from the same entries the SRT already verified

`_write_transcript_text()` in `render.py` calls `Subtitles.to_text()` (`_subtitles.py`)
and writes it to `out/transcript.txt` with no read-back — unlike `_write_subtitles()`,
which re-parses `subtitles.srt` and raises `InvariantViolationError` on any missing
entry, `to_text()` is derived from the same `Subtitles` object that check already passed,
so a second verification would prove nothing new. `to_text()` groups entries into
paragraphs via `_paragraphs()`/`_breaks()`: a paragraph never ends under
`MIN_PARAGRAPH_WIDTH` (100 full-width characters), breaks past that width on a
sentence-ending mark (`SENTENCE_ENDINGS`) or a gap ≥ `PARAGRAPH_PAUSE` (0.6s), and always
breaks at `MAX_PARAGRAPH_WIDTH` (300) regardless of either signal. Each paragraph is
prefixed `[MM:SS]` (`[H:MM:SS]` past an hour, via `_timestamp()`), timed on its first
entry's start. These thresholds are named constants in `_subtitles.py`, deliberately not
`profile.json` keys — `render`'s stage hash covers the `render`/`subtitle` profile
sections, and a paragraph-width knob there would force a full video re-encode just to
reflow a text file.

## `_subtitles.py` owns line breaking and the readability warnings; `pysubs2` owns the file format

`build()` wraps each mapped segment's text with `wrap()`, which packs BudouX phrases
(`phrases()`, via the cached `_parser()`) greedily into at most `profile.max_lines`
lines, never splitting a phrase and never truncating — an entry too wide for
`max_chars_per_line` is left overlong and reported as a `line_overflow` warning rather
than cut. `Subtitles.to_srt(wrapped=...)` builds a `pysubs2.SSAFile` and renders it
through `SRT_FORMAT = "srt"`; `wrapped=False` joins each entry onto one line, which is
what `render`'s `--no-wrap` flag (see `writing-cli-commands` for the flag's own CLI
shape) writes to `out/subtitles.nowrap.srt` alongside the normal file, so a reviewer can
compare BudouX's line breaks against the unbroken text. `tests/test_render.py`'s
`test_the_srt_survives_a_pysubs2_round_trip` is the check that the written SRT parses
back through `pysubs2` unchanged — the round-trip test lives with the render-stage tests,
not `tests/test_subtitles.py`, because it is exercising what `render` writes to disk.

## `_ass.py` builds `telops.ass`; `TelopInvalidError` fires on an unresolvable name, not on style

`_ass.document()` turns a resolved `TelopPlan` plus the merged style presets
(`packaging-data-files` owns how `merge_styles()` does the field-by-field merge) into an
ASS document: every preset becomes a `pysubs2.SSAStyle` via `to_style()`, and colours
declared as `&HAABBGGRR` are parsed by `parse_colour()` using `COLOUR_DIGITS = 8` and
`HEX_BASE = 16` — the module comment states the format is `&HAABBGGRR`, alpha first,
because that is the order pysubs2's `Color` expects it decomposed into. `resolve()` is
what raises `TelopInvalidError` (`code="telop_invalid"`, `EXIT_VALIDATION`, from
`designing-errors`): specifically an `unknown segment_id: … (telops[N])` when a telop
names a `segment_id` `transcript.json` never had, or `unknown style_preset: … (telops[N])`
when a telop names a preset absent from the merged styles — both checked for every telop
before either is raised, so one preview run reports every bad name at once. A
`segment_id` a cut later removed is not an error: it increments `dropped_by_cut` and adds
a warning instead, since the transcript once had it.

## `_preview.py` only ever reads `output.mp4`; `require_libass()` and the length check are its two failure modes

`_write_preview()` in `render.py` calls `_ass.document()` to write `out/telops.ass`, then
`_preview.burn()` to burn it into `out/preview.mp4` via the `subtitles` ffmpeg filter,
gated behind `render --preview` (see `writing-cli-commands` for the flag). Video is
re-encoded to match the render's profile but audio is copied, never re-encoded, "so a
copy cannot move the length the telops were timed against." Two failure modes: `render.
_resolve_telops()` calls `_preview.require_libass()` first and raises `UsageError` when
this ffmpeg build has no `subtitles` filter; `_preview.burn()`'s internal `_verify()`
raises `InvariantViolationError` when the burned preview's duration drifts from
`output.mp4`'s by more than one frame — the same class of check as the render's own
length tolerance, applied to a second file that must never desynchronise from the first.

## The real output list under `out/`

`render.py`'s path constants: `VIDEO_NAME = out/output.mp4`, `SUBTITLES_NAME =
out/subtitles.srt`, `NOWRAP_NAME = out/subtitles.nowrap.srt` (only with `--no-wrap`),
`TEXT_NAME = out/transcript.txt`, `TELOPS_ASS_NAME = out/telops.ass` and `PREVIEW_NAME =
out/preview.mp4` (both only with `--preview`). `modelling-artifacts` owns the pydantic
schemas these outputs are validated against upstream; this skill is about what actually
gets written and the checks it survives on the way out.
