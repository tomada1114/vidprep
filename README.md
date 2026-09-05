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
| Render | `vidprep render` | `out/output.mp4`, `out/subtitles.srt`, `out/transcript.txt` |
| Report | `vidprep report` | `report/stats.json`, waveforms, cut digest |

`audio-fix` applies a high-pass at 80 Hz and a two-pass `loudnorm` to -14 LUFS
with a true peak of -1.0 dBTP. Denoising is opt-in: set
`audio.denoise` to `deepfilternet` or `afftdn` when you want it. `transcribe`
puts Silero voice activity detection in front of whisper.cpp and timestamps
everything in original-timeline seconds. `correct` fixes known ASR
misconversions with a bundled dictionary — swap in your own with `--dict
<path>` or `correct.dictionary_path` in `profile.json`, e.g. to share one
dictionary across several projects. `detect` runs silence detection only when
`silence.enabled` is true and filler detection only when `filler.enabled` is
true; both are off in a new project. `render` applies only what you approved.

The video does not stop dead when the talking does. `detect` leaves
`silence.tail_pad` seconds of the recording behind the last word instead of
stranding its final fraction of a second behind the removed silence, and
`render` fades that tail to black over `render.fade_out` seconds — two seconds
of each by default. A recording that was stopped the moment the sentence ended
has no tail to fade, so the render holds its last frame for the missing part
and darkens from there rather than cutting to black; it says so when it does.
Set `render.fade_out` to `0` to end the video the moment the material does.

For a more natural voice, opt into DeepFilterNet and use its
`deepfilternet_atten_lim_db` setting. Lower it to leave more of the original
voice and background ambience, or raise it when stronger denoising is more
important.

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
  its loudness and decoded AAC true peak must stay within their configured
  limits (the default AAC peak limit is the profile's -1.0 dBTP target plus a
  bounded 0.5 dB encoder allowance). A failed render leaves the previous
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
| auto-editor | `detect` when `silence.enabled=true` | optional; `uv tool install auto-editor`; needs `--export v3` |
| whisper.cpp or mlx-whisper | `transcribe` | plus a ggml model in `~/.cache/whisper.cpp` |
| Silero VAD weights | `transcribe` | `ggml-silero-v5.1.2.bin`, same directory |
| SudachiPy dictionary | `correct` | `uv pip install sudachidict_core` |
| DeepFilterNet | `audio-fix` when `audio.denoise=deepfilternet` | optional; install with `uv tool install deepfilternet` or put the official `deep-filter` binary on `PATH` |

`doctor` exits `3` when a required tool is missing. Missing auto-editor and
DeepFilterNet are warnings, so it exits `0` when only those optional tools are
absent. Selecting an unavailable optional tool makes the corresponding stage
stop with an actionable error.

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

vidprep audio-fix          # high-pass 80 Hz -> loudnorm, with stats
vidprep transcribe          # Silero VAD -> ASR -> transcript.json (original timeline)
vidprep correct --dry-run   # the misconversion dictionary's diff, nothing written
vidprep detect              # optional silence/filler candidates -> cuts.json

vidprep report --cuts       # what each candidate deletes, with the transcript around it
# edit the `status` of each candidate in cuts.json: approved / rejected

vidprep render              # approved cuts -> out/output.mp4 + out/subtitles.srt + out/transcript.txt
vidprep report              # stats.json + boundary waveforms + boundary_digest.mp4
```

The source video is referenced by absolute path and sha256. It is never
modified, and only copied into the project if you ask with `--copy-source`.

Every subcommand takes `--project/-p`, `--json` and `--dry-run`. `detect` can be
re-run as often as you like: it updates the intervals of candidates you already
judged, keeps their status and notes, and never reuses an identifier.

`audio-fix` collects before/after loudness statistics by default. It also
measures the denoise noise floor when `audio.denoise` is enabled. Use
`--no-stats` when those measurements are not needed; `--stats` remains
available as an explicit spelling of the default.

The packaged profile is deliberately conservative. After `init`, opt into the
extra processing by setting `audio.denoise`, `silence.enabled` and/or
`filler.enabled` in `profile.json`; keep the other fields when editing the
file. DeepFilterNet is needed only for `audio.denoise=deepfilternet`, and
auto-editor only for `silence.enabled=true`.

## One command for one video

The stages above are separate because each of them is worth stopping at. When
there is nothing to stop for, `vidprep prep` runs all six over one file and
puts what comes out beside the recording — the same command whether it is
installed as a tool or run from inside the checkout:

```bash
vidprep prep ~/Movies/talk01.mp4
```

```
~/Movies/
├── talk01.mp4          the source; read and hashed, never written
├── talk01.edited.mp4   approved cuts, -14 LUFS, faded out
├── talk01.srt          subtitles on the cut timeline
├── talk01.txt          the same transcript as timestamped, paragraphed prose
└── talk01.vidprep/     the project, kept so the next run is cheap
```

The project directory is what makes the second run cheap: a stage whose result
is already there, and whose parameters in `profile.json` have not moved since,
is skipped, and a stage downstream of one that did run is redone. Tuning a
threshold and running the same command again re-does exactly what the change
reaches.

The first run stops after the dictionary pass so the transcript can be
proofread by an LLM — `vidprep`'s own CLI stays AI-free, so that work belongs to
the `correct-transcript` skill, which writes `patch.json` and applies it through
`vidprep correct --apply-patch`. Running the same command again continues from
the transcript that left behind; `--yes` skips the pause for an unattended run.

New projects leave both `silence.enabled` and `filler.enabled` off, so `prep`
does not cut the material unless those profile switches are enabled. When
fillers are enabled, `detect` leaves them proposed for review and `prep`
approves them too when `filler.enable_weak` is off, because it was asked for
something publishable without a review pass. The candidate tier is not
recorded in `cuts.json`, so with the weak tier enabled the approval is
declined rather than guessed at. `--keep-fillers` leaves every enabled filler
proposal alone;
`--no-verify-asr` drops the second ASR pass over the finished render, which is
the slowest thing in the run.

## Where the review happens

vidprep decides nothing that a human should decide. Three places are built for
that:

- `vidprep report --cuts` lists every candidate with the speech it would remove
  and the transcript around it. You set `status` in `cuts.json`.
- `report/boundary_digest.mp4` plays every cut boundary back to back, so a
  flagged boundary can be listened to instead of argued about.
- `vidprep render --preview` burns `telops.json` into `out/preview.mp4` through
  libass, so on-screen captions are checked before they are committed to.

The repository also ships agent skills for the LLM-assisted parts. The three
pipeline skills — `correct-transcript`, `review-cuts` and `place-telops` — each
read the intermediate JSON, write exactly one artifact and hand it back to the
CLI to be verified. `create-pr`, `shipping-issues` and `smart-commit` cover
repository workflows. The canonical definitions live in `.claude/skills/`,
and Codex reads the same definitions through generated symlinks in
`.agents/skills/`.

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
│   ├── transcript.txt # the same entries as timestamped, paragraphed prose
│   ├── telops.ass     # with --preview
│   └── preview.mp4    # with --preview
└── report/
    ├── stats.json
    ├── vad.json
    ├── noise_floor.json        # written by stats when denoising is enabled
    ├── boundaries/             # one waveform PNG per boundary
    └── boundary_digest.mp4
```

## Regression runs

```bash
just golden        # the whole pipeline over the fixed sample, archived under fixtures/runs/<date>/
just golden-diff   # what changed between the two most recent runs
```

Both are local-only: they need the material, ffmpeg, whisper.cpp and the
profile-enabled optional tools, so they are not part of `just check`.
`tests/fault_injection/` is
the other half — deliberately broken inputs, each asserting that the check meant
to catch it does.

## Development

```bash
just install   # dependencies and git hooks
just check     # format, lint, type check, tests
just docs      # serve the documentation locally
```

Before opening a pull request, run `just check` to verify the formatting,
linting, type checks and test suite together.

## Documentation

- [Getting Started](docs/getting-started.md) — setup and a walk through the stages
- [API Reference](docs/reference.md) — the public API
- [Design notes](docs/design.md) — the architecture and the decisions behind it (Japanese)
- [Verification plan](docs/verification-plan.md) — how each requirement is checked (Japanese)
- [Feasibility research](docs/research/feasibility-report.md) — the evidence the design rests on (Japanese)
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute, and [CHANGELOG.md](CHANGELOG.md)

## License

MIT. Built on [uv-template](https://github.com/tomada1114/uv-template).
