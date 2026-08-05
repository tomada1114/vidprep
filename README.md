# vidprep

**English** | [日本語](README.ja.md)

A CLI pipeline that prepares a recorded talk for YouTube: clean up the audio,
transcribe it, propose the cuts, apply the approved ones and write subtitles
that match the result — then hand the files to an editor such as Filmora.

Every stage writes plain JSON you can read and edit, and every stage refuses to
proceed when what it is about to write would be wrong. That is the whole design:
the pipeline is fast enough to re-run, so a stage that is unsure stops and asks
instead of guessing.

## The pipeline

| Stage | Command | Writes |
|---|---|---|
| Audio repair | `vidprep audio-fix` | `audio/processed.wav` |
| Transcription | `vidprep transcribe` | `transcript.json`, `report/vad.json` |
| Correction | `vidprep correct` | `transcript.json` (in place) |
| Cut detection | `vidprep detect` | `cuts.json` |
| Render | `vidprep render` | `out/output.mp4`, `out/subtitles.srt` |
| Report | `vidprep report` | `report/stats.json`, waveforms, cut digest |

`audio-fix` denoises with DeepFilterNet — or ffmpeg's `afftdn` when it is not
installed — then applies a high-pass at 80 Hz and a two-pass `loudnorm` to
-14 LUFS with a true peak of -1.0 dBTP. `transcribe` puts Silero voice
activity detection in front of whisper.cpp and timestamps everything in
original-timeline seconds. `detect` takes the silences from auto-editor and the
filler words from the transcript. `render` applies only what you approved.

## What it refuses to do

The checks are the point, so they are worth stating before the install steps.

- **No cut may remove speech.** Each `silence` candidate is checked against both
  the transcript and the detected speech regions; a run that would delete a word
  is refused rather than written.
- **Voice activity detection has no off switch.** Without it whisper invents
  sentences in the silences, and those come back later as subtitles. A
  transcript whose segments do not line up with detected speech is refused.
- **The video and its subtitles come from the same cut plan**, so the two cannot
  drift apart.
- **The output is measured before it replaces anything.** Its length must match
  the cut list to within one frame, its streams must agree to within 50 ms and
  its loudness must still be on target. A failed render leaves the previous
  `out/output.mp4` in place.
- **`render --verify-asr` reads the finished file back.** It transcribes
  `out/output.mp4` a second time with the same backend, model and detector — so
  both passes make the same mistakes and those mistakes cancel out — and reports
  any text the second pass never heard near a cut boundary. This is a gate: one
  flag exits `3`.

## Requirements

Python 3.12 or newer, plus a few external tools. `vidprep doctor` checks all of
them and prints what to install for the ones that are missing.

| Tool | Needed for | Notes |
|---|---|---|
| ffmpeg / ffprobe | every stage | must be built with libass for `render --preview` |
| auto-editor | `detect` | `uv tool install auto-editor`; needs `--export v3` |
| whisper.cpp or mlx-whisper | `transcribe` | plus a ggml model in `~/.cache/whisper.cpp` |
| Silero VAD weights | `transcribe` | `ggml-silero-v5.1.2.bin`, same directory |
| SudachiPy dictionary | `correct` | `uv pip install sudachidict_core` |
| DeepFilterNet | `audio-fix` | optional — falls back to ffmpeg's `afftdn` |

`doctor` exits `3` when a required tool is missing and `0` when only
DeepFilterNet is absent.

## Installation

vidprep is not on PyPI yet. Install it from the repository:

```bash
git clone https://github.com/tomada1114/vidprep.git
cd vidprep
just install          # or: uv sync --all-groups
uv run vidprep doctor
```

To use it outside the checkout, install the CLI as a tool:

```bash
uv tool install --from . vidprep
```

## Quickstart

```bash
vidprep doctor          # check the external tools first
vidprep init ./work/talk01 --source ~/Movies/talk01.mp4

vidprep audio-fix --stats   # denoise -> high-pass 80 Hz -> loudnorm, before/after numbers
vidprep transcribe          # Silero VAD -> ASR -> transcript.json (original timeline)
vidprep correct --dry-run   # the misconversion dictionary's diff, nothing written
vidprep detect              # silence + filler candidates -> cuts.json

vidprep report --cuts       # what each candidate deletes, with the transcript around it
# edit the `status` of each candidate in cuts.json: approved / rejected

vidprep render              # approved cuts -> out/output.mp4 + out/subtitles.srt
vidprep report              # stats.json + boundary waveforms + boundary_digest.mp4
```

The source video is referenced by absolute path and sha256. It is never
modified, and only copied into the project if you ask with `--copy-source`.

Every subcommand takes `--project/-p`, `--json` and `--dry-run`. `detect` can be
re-run as often as you like: it updates the intervals of candidates you already
judged, keeps their status and notes, and never reuses an identifier.

## Where the review happens

vidprep decides nothing that a human should decide. Three places are built for
that:

- `vidprep report --cuts` lists every candidate with the speech it would remove
  and the transcript around it. You set `status` in `cuts.json`.
- `report/boundary_digest.mp4` plays every cut boundary back to back, so a
  flagged boundary can be listened to instead of argued about.
- `vidprep render --preview` burns `telops.json` into `out/preview.mp4` through
  libass, so on-screen captions are checked before they are committed to.

The repository also ships three Claude Code skills for the LLM-assisted parts —
`correct-transcript`, `review-cuts` and `place-telops`. Each one reads the
intermediate JSON, writes exactly one artifact and hands it back to the CLI to
be verified.

## The project directory

```
work/talk01/
├── vidprep.json       # manifest: source path, sha256, stage records
├── profile.json       # processing parameters (copied from the packaged defaults)
├── audio/
│   └── processed.wav  # audio-fix output; the render's audio comes from here
├── transcript.json    # segments in original-timeline seconds
├── cuts.json          # cut candidates and the status you gave them
├── out/
│   ├── output.mp4
│   ├── subtitles.srt  # and subtitles.nowrap.srt with --no-wrap
│   ├── telops.ass     # with --preview
│   └── preview.mp4    # with --preview
└── report/
    ├── stats.json
    ├── vad.json
    ├── noise_floor.json        # written by audio-fix --stats
    ├── boundaries/             # one waveform PNG per boundary
    └── boundary_digest.mp4
```

## Regression runs

```bash
just golden        # the whole pipeline over the fixed sample, archived under fixtures/runs/<date>/
just golden-diff   # what changed between the two most recent runs
```

Both are local-only: they need the material, ffmpeg, whisper.cpp and
auto-editor, so they are not part of `just check`. `tests/fault_injection/` is
the other half — deliberately broken inputs, each asserting that the check meant
to catch it does.

## Development

```bash
just install   # dependencies and git hooks
just check     # format, lint, type check, tests
just docs      # serve the documentation locally
```

## Documentation

- [Getting Started](docs/getting-started.md) — setup and a walk through the stages
- [API Reference](docs/reference.md) — the public API
- [Design notes](docs/design.md) — the architecture and the decisions behind it (Japanese)
- [Verification plan](docs/verification-plan.md) — how each requirement is checked (Japanese)
- [Feasibility research](docs/research/feasibility-report.md) — the evidence the design rests on (Japanese)
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute, and [CHANGELOG.md](CHANGELOG.md)

## License

MIT. Built on [uv-template](https://github.com/tomada1114/uv-template).
