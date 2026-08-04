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

    `audio-fix`, `transcribe` and `correct` are implemented. The remaining
    stages (`detect`, `render`, `report`) are registered but not implemented
    yet; they currently report that and exit `1`.

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

## What's Next?

See the [API Reference](reference.md) for the complete API documentation.
