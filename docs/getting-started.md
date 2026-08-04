# Getting Started

## Installation

```bash
pip install vidprep
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add vidprep
```

## Basic Usage

Start by checking the machine. `vidprep doctor` inspects the external tools the
pipeline shells out to — ffmpeg (including whether it was built with libass),
ffprobe, auto-editor, an ASR backend, DeepFilterNet and a SudachiPy dictionary —
and tells you what to install for the ones that are missing.

```bash
vidprep doctor          # readable report
vidprep doctor --json   # the same report as JSON on stdout
```

It exits `3` when a required dependency is missing, and `0` when only the
optional DeepFilterNet is absent — `audio-fix` falls back to ffmpeg's `afftdn`
in that case.

Then create a working directory for one video. The material is referenced by
absolute path and sha256 — it is never modified, and only copied into the
project when you ask for it with `--copy-source`.

```bash
vidprep init ./work/talk01 --source ~/Movies/VID_20260507_144024.mp4
```

This writes `vidprep.json` (the manifest: source specs, hash, stage records)
and `profile.json` (the processing parameters, copied from the packaged
defaults). Every subcommand accepts `--project/-p`, `--json` and `--dry-run`.

!!! note

    Every stage is implemented. What is not built yet lives behind flags of
    `render`: `--preview` (burnt-in captions) and `--verify-asr` (comparing a
    re-transcription of the output against the cut plan).

## Transcribing

`transcribe` runs Silero voice activity detection in front of the recogniser
and writes `transcript.json` in original-timeline seconds, plus
`report/vad.json` with the speech regions it found.

```bash
vidprep transcribe          # whisper.cpp by default; see profile.json's `asr`
vidprep transcribe --json   # segment count, speech duration, realtime factor
```

Detection has no off switch: without it whisper invents sentences in the
silences, and those come back as subtitles later. A transcript whose segments
do not line up with the detected speech is refused rather than written.

## Detecting cuts

`detect` writes `cuts.json`: the silences auto-editor found, padded and
proposed as `approved`, plus the filler words the transcript and the speech
regions justify cutting, proposed as `proposed`.

```bash
vidprep detect              # silence + filler candidates
vidprep detect --json       # counts and seconds per reason, and what merged
```

Run it as often as you like. Re-running after changing `profile.json` updates
the intervals of the candidates you already judged and keeps their status and
notes; only untouched proposals are withdrawn, and identifiers are never
reused. No cut vidprep proposes may remove speech: each `silence` cut is
checked against the transcript and the speech regions behind it, and the run
is refused rather than written if one would.

## Rendering

`render` applies the `approved` cuts and nothing else, and writes both the
video and the subtitles from the same cut plan, so the two cannot drift apart.

```bash
vidprep render              # out/output.mp4 + out/subtitles.srt
vidprep render --no-wrap    # and out/subtitles.nowrap.srt, without line breaks
vidprep render --dry-run    # the ffmpeg command it would run, filters included
```

The video is re-encoded at the CRF and preset in `profile.json`, keeping the
source resolution and frame rate; the audio comes from `audio/processed.wav`
rather than from the container, with a 10ms fade in and out at every boundary.
The fades do not overlap, so no boundary changes a length.

!!! note

    The output is measured before it replaces anything: its length must match
    the cut list to within one frame, its two streams must agree to within
    50ms, and its loudness must still be on target. A render that fails leaves
    the previous `out/output.mp4` in place.

Subtitles are broken at BudouX phrase boundaries into at most `max_lines`
lines of `max_chars_per_line` full-width characters. Nothing is truncated:
text that does not fit, entries shown for less than `min_display` and entries
read faster than `max_cps` are reported in the result rather than changed.

## What's Next?

See the [API Reference](reference.md) for the complete API documentation.
