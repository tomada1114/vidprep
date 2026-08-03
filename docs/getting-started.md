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

Create a working directory for one video. The material is referenced by
absolute path and sha256 — it is never modified, and only copied into the
project when you ask for it with `--copy-source`.

```bash
vidprep init ./work/talk01 --source ~/Movies/VID_20260507_144024.mp4
```

This writes `vidprep.json` (the manifest: source specs, hash, stage records)
and `profile.json` (the processing parameters, copied from the packaged
defaults). Every subcommand accepts `--project/-p`, `--json` and `--dry-run`.

!!! note

    The processing stages (`audio-fix`, `transcribe`, `correct`, `detect`,
    `render`, `report`) and `doctor` are registered but not implemented yet;
    they currently report that and exit `1`.

## What's Next?

See the [API Reference](reference.md) for the complete API documentation.
