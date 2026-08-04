---
name: review-cuts
description: >
  Review the cut candidates of a vidprep project and set each one's status.
  Reads cuts.json, `vidprep report --cuts --json` and transcript.json, changes
  only `status` and `note`, and re-validates through the CLI. Use PROACTIVELY
  when: review cuts, approve cuts, カット候補のレビュー, filler review, cuts.json
  status, decide what to delete, `report --cuts` follow-up.
---

# Review Cuts

Judgement pass over `cuts.json`. The CLI decides nothing about *whether* a cut
is a good idea; this skill does, and writes the decision back into the two
fields it is allowed to touch (design.md §7).

## Contract

| Reads | Writes | Never touches |
|---|---|---|
| `cuts.json`, `vidprep report --cuts --json`, `transcript.json` | `cuts.json` — `status` and `note` **only** | `id`, `start`, `end`, `reason`, `confidence`, the number and order of cuts, `transcript.json`, `src/vidprep/**` |

- `status` is one of `proposed` / `approved` / `rejected`. `render` applies
  **`approved` only**.
- Every cut whose status you change gets a `note` saying why.
- Never add a cut, never delete a cut, never move a boundary. If a candidate is
  half right, reject it and say so in the note — do not trim `start`/`end` to
  rescue it.
- Never modify `src/vidprep/**`.

## Step 1: Take a baseline

From the project directory (the one holding `vidprep.json`):

```bash
cp cuts.json cuts.before.json          # baseline for the diff check in step 4
```

`cuts.before.json` is scratch: delete it once step 4 passes.

## Step 2: Read the review material

```bash
vidprep report --cuts --json           # the review listing; writes nothing
```

Each element of `cuts` carries what you need to judge it:

| Field | Meaning |
|---|---|
| `id`, `start`, `end`, `duration` | the interval, immutable here |
| `reason` | `silence` / `filler` / `manual` |
| `confidence`, `status`, `note` | the detector's certainty and the current decision |
| `removed` | the transcript segments the cut would delete (`null` when there is no transcript) |
| `before`, `after` | the neighbouring segments — the context the decision hangs on |

Read `transcript.json` as well whenever `before`/`after` is not enough to tell
whether the sentence still joins up after the deletion.

## Step 3: Decide

**Filler candidates** (`reason: filler`) are the ones that need real judgement.
The target is precision, not recall: at least 8 in 10 approved filler cuts must
read as obviously deletable (verification-plan.md §7).

- Approve when the filler stands alone and the surrounding sentences join up
  cleanly without it — 「えーと」 on its own between two complete sentences.
- Reject when the word carries the speaker's rhythm — a sentence-initial
  「なんか」, a 「まあ」 that softens what follows. Removing those makes the
  delivery abrupt.
- Reject when `removed` contains anything besides the filler.

**Silence candidates** (`reason: silence`): approve unless `removed` shows real
speech inside the interval, or `before`/`after` shows the cut would clip the
start of a word. A breath before an important sentence is worth keeping.

**Manual cuts** (`reason: manual`) were written by a human; leave them unless
you are asked to re-check them.

Rules that hold regardless of reason:

- **Approved cuts must never overlap** — that is a schema invariant, and the
  next CLI command will refuse the file if you break it. Approve the better of
  two overlapping candidates and reject the other.
- Rejecting everything is fine (note the reason on each). So is demoting an
  already-approved cut back to `proposed` or `rejected`: `detect` merges by id
  and keeps reviewed statuses, so a decision survives a re-run.
- **Zero candidates**: change nothing and report that there was nothing to
  review.

## Step 4: Edit and verify

Edit `cuts.json` in place, changing only `status` and `note` on the cuts you
decided. Write the note in the language of the transcript (Japanese for these
projects), one line, saying what is being removed and why that is safe:

```json
{"id": "c0052", "start": 61.2, "end": 61.9, "reason": "filler",
 "confidence": 0.9, "status": "approved",
 "note": "独立フィラー「えーと」。前後の文がつながる"}
```

Then run both checks:

```bash
# 1. schema validation — cut intervals, unique ids, approved cuts not overlapping
vidprep report --json

# 2. nothing but status/note moved
jq -S '[.cuts[] | {id, start, end, reason, confidence}]' cuts.before.json > /tmp/cuts-a.json
jq -S '[.cuts[] | {id, start, end, reason, confidence}]' cuts.json        > /tmp/cuts-b.json
diff /tmp/cuts-a.json /tmp/cuts-b.json   # must print nothing
```

`vidprep report --json` validates every project artifact before it does
anything, so exit `0` means `cuts.json` is accepted. Its `cuts.by_reason` block
is also the summary to report:
`{"filler": {"count": 18, "sec": 12.64, "approved": 14, "proposed": 0,
"rejected": 4}, …}`.

`vidprep report --json` also regenerates the waveforms and the boundary digest,
so it needs ffmpeg. Where that is not available, `vidprep report --cuts --json`
alone already loads and validates `cuts.json` — every stage command validates
the project's artifacts before it does anything.

## Step 5: When validation fails

Exit code `3`:

| Payload | Meaning | Fix |
|---|---|---|
| `{"error": "schema_invalid", "detail": "cuts.json: approved cuts overlap: c0001(…) x c0002(…)"}` | two approved cuts share time | Put one of them back to `proposed`/`rejected` and re-decide. **Do not** move `start`/`end` |
| `{"error": "schema_invalid", "detail": "cuts.json: …"}` (other) | a field was damaged while editing | Restore from `cuts.before.json` and redo the status edits only |

If the `diff` in step 4 is not empty, an interval, id, reason or confidence
changed: restore `cuts.json` from `cuts.before.json` and reapply only the
`status`/`note` changes. Never reconcile the difference by editing the other
direction — the detector owns those fields.

## Step 6: Report

For the user: how many cuts were approved / rejected / left proposed per reason,
the total approved seconds, and the notes for every decision that was not
obvious. Mention the boundary digest (`report/boundary_digest.mp4`) as the way
to listen to the approved boundaries before rendering.
