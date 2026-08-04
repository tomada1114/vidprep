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

    Every stage and every flag of the pipeline is implemented.

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
vidprep render --preview    # and out/telops.ass + out/preview.mp4
vidprep render --verify-asr # and transcribe the result again to look for lost words
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

## Telops

`--preview` reads `telops.json`, dresses each caption with a preset from
`styles.json`, writes the track to `out/telops.ass` and burns it into
`out/preview.mp4` with libass. A telop that names a `segment_id` is shown for
exactly as long as that segment's subtitle; one with a `start` in
original-timeline seconds and a `duration` is put through the same cut
mapping. Naming a segment or a preset that does not exist stops the run before
anything is encoded, and `out/output.mp4` is only ever read.

The presets ship with vidprep; a project `styles.json` overrides them field by
field, so stating a `fontsize` keeps the packaged `fontname` — which is how
weight is asked for at all. macOS renders libass through CoreText, where
`Bold: 1` was measured to change nothing, so the presets name a weighted
family such as `Hiragino Sans W6` instead (design.md §3.5).

## Verifying the Render

`--verify-asr` reads the finished file back. It transcribes `out/output.mp4`
again — with the backend, model and detector `transcript.json` records, so the
two passes make the same mistakes and those mistakes cancel out — and compares
it with what the kept segments should say. Text the second pass never heard,
two characters or more, within two seconds of a cut boundary, is a word the cut
took with it.

```bash
vidprep render --verify-asr --json   # the comparison under "verify_asr"
```

!!! note

    The check is advisory: it reports its flags and leaves the exit code at
    `0`, because a recogniser run twice does not return the same string twice.
    Set `render.verify_asr_mode` to `"gate"` in `profile.json` to make one flag
    exit `3`. The global CER is reported as a reference figure and decides
    nothing.

A flagged boundary is a question, not a verdict: seek to `src_time` in
`report/boundary_digest.mp4` and listen.

## Regression Runs

The whole pipeline over the fixed sample of `docs/verification-plan.md` §2 is
one command, and it archives what it measured so the next run can be compared
against it:

```bash
just golden        # six stages, then fixtures/runs/<date>/
just golden-diff   # what changed between the two most recent runs
```

Both are local-only: they need the material, ffmpeg, whisper.cpp and
auto-editor. `tests/fault_injection/` is the other half — six deliberately
broken inputs, each asserting that the check meant to catch it does.

## What's Next?

See the [API Reference](reference.md) for the complete API documentation.
