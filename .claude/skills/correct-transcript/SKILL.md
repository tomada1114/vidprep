---
name: correct-transcript
description: >
  Proofread a vidprep transcript.json with an LLM and apply the result through
  the CLI's verified patch path. Reads transcript.json and the packaged
  misconversion dictionary, writes patch.json only, and applies it with
  `vidprep correct --apply-patch`. Use PROACTIVELY when: transcript correction,
  proofread the transcript, LLM 校正, patch.json, fix the ASR output,
  context dictionary entries, `vidprep correct` follow-up.
---

# Correct Transcript

LLM proofreading for a vidprep project. The CLI stays AI-free: this skill only
reads intermediate JSON, writes `patch.json`, and calls the CLI (design.md §7).

## Contract

| Reads | Writes | Never touches |
|---|---|---|
| `transcript.json`, the packaged `dictionaries/asr-dict.json` | `patch.json` | `transcript.json` (hand edits), `cuts.json`, `telops.json`, `src/vidprep/**` |

- The patch schema is `{"edits": [{"id": "s0001", "text": "…"}]}` and nothing
  else. `extra` keys are forbidden — no `version`, no `start`, no `end`, no
  `note`. Timing, segment count and order are unrepresentable on purpose.
- The only way transcript.json changes is
  `vidprep correct --apply-patch <patch.json>`. Never edit it directly, not even
  to "fix up" a rejected patch.
- Never modify `src/vidprep/**`. If a term is misrecognised reproducibly, report
  it as a dictionary candidate instead of editing the dictionary mid-run.

## Step 1: Collect the material

Run from the project directory (the one holding `vidprep.json`).

```bash
vidprep correct --json                 # dictionary pass; idempotent, safe to re-run
```

The result's `segments` are what the dictionary already replaced
(`confidence: always`). The `skipped` list is the real work list: each entry is
`{"id", "stage", "matched", "correct"}` for a `confidence: context` dictionary
hit that was deliberately *not* replaced, because the term is a homophone of an
everyday word. Deciding those from context is this skill's main job.

List the context entries with their notes so you know what each one means:

```bash
python -c "import json,importlib.resources as r; d=json.loads(r.files('vidprep').joinpath('dictionaries/asr-dict.json').read_text('utf-8')); print(json.dumps([e for e in d['entries'] if e['confidence']=='context'], ensure_ascii=False, indent=2))"
```

Use whichever interpreter has vidprep installed (inside this repo:
`uv run python -c …`). Then read `transcript.json` itself — the segment `text`
values in order — so every decision is made with the neighbouring segments in
view.

## Step 2: Decide the edits

### The speaker dictates commands out loud

These projects are programming tutorials. The speaker **says CLI commands,
option flags and technical terms out loud**, and Japanese ASR writes them back
as katakana or as a same-sounding everyday word. Restoring them from context is
a requirement of this skill, not an optional polish.

| Heard | ASR wrote | Correct as | Why |
|---|---|---|---|
| resume | 「リズーム」 | `resume` | Already in the dictionary (`always`) — should arrive fixed |
| claude -c | 「クロード-C」 | `claude -c` | Already in the dictionary (`always`) |
| 半角スペース | 「半額スペース」 | 「半角スペース」 | 「半額」 is a real word, so it stays out of the dictionary — decide it here |
| ハイフンC | 「配分し」 | 「ハイフンC」 | Pure context: only the surrounding sentence says an option is being read out |

Rules that follow from that:

- Restore katakana-ised English commands and option names
  (「ハイフン◯◯」, 「ダブルハイフン◯◯」) to the spelling the speaker means.
- Keep the *spoken* form when the speaker is reading an option aloud as
  narration — 「半角スペースにハイフンC」 stays as it is written, because that is
  what was said. Write the command form (`claude -c`) when the sentence refers
  to the command itself, e.g. 「claude -cでいきなり最後のセッションに戻れます」.
- A `context` dictionary entry is a judgement call, not a rename: 「クラウドに
  デプロイ」 in an infrastructure sentence stays 「クラウド」; 「クラウドコードで」
  becomes `Claude Code`.
- Leave a `context` hit alone when the text is already right for a command:
  `claude` inside `claude -c` is reported as a candidate for `Claude`, but a
  command name stays lowercase.

### What not to change

- Do not rewrite speech into written Japanese. Word order, particles, repeated
  phrasing and spoken rhythm all stay. A reviewer must be able to read the diff
  and see only mistranscriptions being fixed.
- Do not delete fillers (「えーと」「なんか」). Those belong to `detect` and
  `review-cuts`, and removing them here breaks the cut timing.
- Do not merge, split, reorder or renumber segments. One edit fixes the text of
  one existing id.
- Do not touch punctuation or spacing except where the ASR clearly broke a word.

Keep the patch small — roughly one edit per genuine mistranscription. If a
segment needs no change, leave it out.

## Step 3: Write patch.json

```json
{
  "edits": [
    {"id": "s0003", "text": "まるで会話ができる"},
    {"id": "s0088", "text": "Geminiと比べてみます"}
  ]
}
```

Only `id` (matching `^s\d{4}$`, and present in transcript.json) and `text`.
Duplicate ids are rejected. Write it as UTF-8 with the Japanese unescaped.

**Nothing to fix**: write `{"edits": []}` — it applies cleanly and changes
zero segments — or skip the patch entirely and say so in the report. If
`transcript.json` has no segments at all, report "nothing to correct" and stop.

## Step 4: Apply through the CLI

```bash
vidprep correct --apply-patch patch.json --json
```

The command prints the plan, asks `Apply N changes?`, and writes only after the
confirmation. Pass `--yes` only when the user has already approved the diff.
Add `--dry-run` first if you want the plan without any prompt.

On success the JSON is `{"action": "correct", "tool": "llm", "changed": N,
"applied": N, …}` and the exit code is `0`. The CLI re-checks that every
segment's `id`, `start` and `end` are unchanged before writing, so a successful
apply is proof that only text moved.

## Step 5: When the CLI rejects the patch

Exit code `3` with:

```json
{"error": "patch_invalid", "detail": ["unknown segment id: s0215"], "applied": 0}
```

Nothing was written — the transcript is untouched and the whole complaint list
is in `detail`. Common causes and the only allowed response:

| `detail` says | Fix in patch.json |
|---|---|
| `unknown segment id: sXXXX` | The id does not exist. Re-read transcript.json for the real id (ids stop at the last segment) and drop or correct the edit |
| `duplicate segment id: sXXXX` | Merge the two edits for that segment into one |
| a schema message | Remove the extra key, or fix the `id` pattern / missing `text` |
| `cannot read <path>` | Fix the path or the JSON syntax |

Fix `patch.json` and re-run step 4. **Never** hand-edit `transcript.json` to
make a patch fit — that is exactly the accident the verified path exists to
prevent. An `invariant_violated` (also exit `3`) means the apply would have
changed the shape of the transcript; stop and report it rather than working
around it.

## Step 6: Report

Summarise for the user:

- how many edits were applied, grouped by kind (dictionary `context` decisions
  vs. plain mistranscriptions);
- every `context` hit you deliberately left alone, with the reason;
- any reproducible mistranscription worth adding to
  `src/vidprep/dictionaries/asr-dict.json` — as a *suggestion*, since this skill
  does not edit the package.

Inside this repository, the third measurement point of the CER comparison
(verification-plan.md §6 — raw ASR → after dictionary → after LLM correction) is
recorded with:

```bash
uv run python scripts/cer.py fixtures/expected/golden.reference.txt <hypothesis>
```
