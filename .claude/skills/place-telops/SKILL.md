---
name: place-telops
description: >
  Place on-screen telops for a vidprep project. Reads transcript.json and the
  style presets, writes telops.json only, and lets `vidprep render --preview`
  do the validation. Use PROACTIVELY when: place telops, add captions,
  テロップ配置, telops.json, emphasis text on screen, chapter titles,
  `render --preview` follow-up.
---

# Place Telops

Chooses what appears on screen and when. Validation is not this skill's job —
`vidprep render --preview` owns it (design.md §7).

## Contract

| Reads | Writes | Never touches |
|---|---|---|
| `transcript.json`, `styles.json` (and the packaged style presets) | `telops.json` | `transcript.json`, `cuts.json`, `styles.json`, `src/vidprep/**` |

- `style_preset` must name a preset that actually exists. Never invent one.
- Prefer `segment_id`; direct `start` + `duration` is for text that is not tied
  to a spoken segment (chapter titles, a closing card).
- Never modify `src/vidprep/**`, including the packaged
  `styles/default.json`.

## Step 1: Read the inputs

From the project directory (the one holding `vidprep.json`):

```bash
# the segments you can attach a telop to
python -c "import json,pathlib; print(json.dumps(json.loads(pathlib.Path('transcript.json').read_text('utf-8'))['segments'], ensure_ascii=False, indent=2))"

# the presets that exist: the packaged defaults …
python -c "import json,importlib.resources as r; print(list(json.loads(r.files('vidprep').joinpath('styles/default.json').read_text('utf-8'))['presets']))"
```

… plus every preset in the project's own `styles.json`, if it has one. A
project `styles.json` is merged field-by-field over the packaged defaults, so
both sets of names are usable. The packaged presets are `default` (bottom
centre, body text), `emphasis` (top centre, larger) and `chapter` (centred,
largest, yellow).

If a design needs a preset that does not exist, stop and ask the user — adding
it to `styles.json` is a styling decision, not a placement one.

## Step 2: Choose the placements

- Emphasise sparingly. A telop on every segment is noise; pick the sentences
  that carry the point of the section.
- Keep the text short enough to read at a glance and shorter than the segment it
  sits on. Telop text is a headline, not a subtitle — `subtitles.srt` already
  carries the full transcript.
- Attach to a spoken segment with `segment_id` so the timing follows the segment
  even after cuts are applied.
- Use `start` + `duration` only for text with no segment behind it, e.g. a
  chapter title over a pause.

## Step 3: Write telops.json

```json
{
  "version": "1",
  "telops": [
    {"segment_id": "s0012", "text": "ここが重要", "style_preset": "emphasis",
     "start": null, "duration": null},
    {"segment_id": null, "text": "第1章 セットアップ", "style_preset": "chapter",
     "start": 45.0, "duration": 3.0}
  ]
}
```

Schema rules the file must satisfy:

| Field | Rule |
|---|---|
| `version` | `"1"` (the string) |
| `text` | required, non-null |
| `style_preset` | an existing preset name; defaults to `default` if omitted |
| `segment_id` | `^s\d{4}$` and present in `transcript.json`, or `null` |
| `start` | seconds ≥ 0, not past the source duration, or `null` |
| `duration` | seconds **> 0**, or `null` |

- With `segment_id` set, leave `start` and `duration` `null`. Supplying both
  forms is not an error but `segment_id` wins and the render warns — so do not
  do it.
- With `segment_id` null, **both** `start` and `duration` are required.
- No other keys are accepted anywhere in the file.

**Nothing to place**: write `{"version": "1", "telops": []}`. `--preview` passes
with no burn-in. If `transcript.json` has no segments and there are no
standalone titles to add, report "nothing to place" and stop.

## Step 4: Verify through the CLI

```bash
vidprep render --preview --json
```

Exit `0` means the file is accepted. The result carries the two blocks worth
reporting:

```json
{"telops": {"total": 12, "by_segment_id": 9, "by_start_duration": 3,
            "dropped_by_cut": 0, "warnings": []},
 "styles": {"presets": ["default", "emphasis", "chapter"],
            "source": "packaged default"}}
```

`dropped_by_cut` counts telops whose segment an approved cut removed — not an
error, but re-check whether that text should move to a surviving segment.

`render --preview` needs ffmpeg built with libass, and it re-renders the video.
When ffmpeg is unavailable, or for a quick check while iterating, any stage
command validates `telops.json` against the schema first:

```bash
vidprep report --cuts --json    # loads and validates telops.json, writes nothing
```

That catches schema errors but **not** unknown `segment_id` or unknown
`style_preset`, which are only resolved by `--preview`. A successful
`--preview` is what closes the loop.

## Step 5: When validation fails

Exit code `3`:

| Payload | Meaning | Fix in telops.json |
|---|---|---|
| `{"error": "telop_invalid", "detail": ["unknown segment_id: s0012 (telops[0])"]}` | the id is not in transcript.json | Re-read the transcript and point at a real segment |
| `{"error": "telop_invalid", "detail": ["unknown style_preset: X (telops[0])"]}` | the preset does not exist | Use an existing preset, or ask the user before adding one to `styles.json` |
| `{"error": "schema_invalid", "detail": "telops.json: …"}` | shape violation — missing `text`, `duration` of `0`, an extra key, `segment_id` null with no `start`/`duration` | Fix the offending entry |

Exit `1` with `{"error": "usage", …}` means the environment, not the file:
`telops.json` missing, or ffmpeg without the `subtitles` (libass) filter — run
`vidprep doctor`.

Always fix `telops.json` and re-run the verification. Never edit
`transcript.json`, `cuts.json` or the packaged styles to make a telop fit.

## Step 6: Report

List what was placed — how many by `segment_id`, how many by `start`/`duration`,
which presets were used — and point the user at `out/preview.mp4` to check that
the text lands on the words it belongs to.
