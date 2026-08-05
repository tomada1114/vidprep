# Project Guide

## Overview

This is a Python library built with [uv](https://docs.astral.sh/uv/) and
[hatchling](https://hatch.pypa.io/). It uses a strict `src/` layout with
comprehensive type checking and linting.

## Quick Reference

```bash
just install   # Install dependencies and git hooks when .git/ is present
just fmt       # Format code (ruff check --fix + ruff format)
just lint      # Lint (ruff check) + type check (mypy)
just test      # Run tests with coverage
just smoke     # Build and verify the wheel in a temp virtual environment
just check     # Run all checks: fmt → lint → test
just docs      # Serve docs locally
just build     # Build distribution packages
```

Without Just: replace `just <cmd>` with the corresponding `uv run` commands
in the `justfile`. Run a single test with
`uv run pytest tests/test_<module>.py::test_<name>`.

## Architecture

```
src/vidprep/
├── __init__.py      # Public API — export everything users need here
├── py.typed         # PEP 561 marker for typed package
├── cli.py           # typer app: one thin subcommand per pipeline stage
├── errors.py        # Exception hierarchy and the 0/1/2/3 exit codes
├── models.py        # pydantic schemas for the intermediate JSON (design.md §3)
├── project.py       # Project directory: init, load, verify, stage records
├── timeline.py      # Original <-> cut timeline mapping (design.md §4)
├── doctor.py        # doctor: inspect the tools the stages shell out to
├── audio.py         # audio-fix: denoise, high-pass, two-pass loudnorm
├── transcribe.py    # transcribe: Silero VAD in front of the recogniser
├── correct.py       # correct: dictionary replacement, verified patches
├── detect.py        # detect: silence and filler cut candidates
├── render.py        # render: apply the cuts, write the video and the SRT
├── report.py        # report: the review gate and the numbers behind it
├── verify.py        # --verify-asr: read the finished render back
├── _ffmpeg.py       # The only module allowed to spawn subprocesses
├── _asr.py          # whisper.cpp / mlx-whisper behind transcribe
├── _dictionary.py   # The misconversion dictionary and its two passes
├── _text.py         # The comparison form text measurements share
├── _autoeditor.py   # auto-editor's v3 timeline -> cut intervals
├── _intervals.py    # Interval arithmetic detection and rendering share
├── _fillers.py      # Filler scanning and the cuts it justifies
├── _reencode.py     # The renderer protocol and the v1 re-encode
├── _subtitles.py    # Line breaking, readability limits, the SRT file
├── _ass.py          # Telops as an ASS subtitle track (design.md §3.5)
├── _preview.py      # Burning the telop track into the preview video
├── _boundaries.py   # Waveform stills and the boundary digest video
├── _review.py       # The `report --cuts` listing
├── _retranscribe.py # The arithmetic behind the re-transcription check
├── dictionaries/    # Packaged ASR misconversions, fillers, hallucinations
├── profiles/        # Packaged templates (default.json) copied by `vidprep init`
└── styles/          # Packaged telop style presets (default.json)
```

- Keep the public API surface small — export via `__init__.py.__all__`
- Internal modules can use a leading underscore (`_internal.py`)
- Separate concerns: one module per logical unit
- Update `docs/reference.md` and README examples whenever you change the public API

## Review Checklist

Before submitting a PR:

1. `just check` passes (format, lint, type check, tests)
2. New public APIs have type annotations and docstrings
3. Tests cover the new functionality
4. No unnecessary dependencies added

## Important Reminders

- All code, published docs, commits, and PRs must be written in English.
  Design notes (`docs/design-input.md`, `docs/design.md`,
  `docs/verification-plan.md`, `docs/research/`) stay in Japanese, and so does
  `README.ja.md` — the Japanese counterpart of `README.md`. Keep the two
  READMEs in step: a change to one belongs in the other in the same commit
- Do what has been asked; nothing more, nothing less
- NEVER create files unless absolutely necessary
- ALWAYS prefer editing an existing file to creating a new one
- NEVER proactively create documentation files unless explicitly requested
- Dependencies should always be added to the appropriate group in pyproject.toml
