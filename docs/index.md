# vidprep

A CLI pipeline that prepares recorded talks for YouTube: silence cutting,
Japanese transcription, subtitle generation and Filmora handoff.

## Installation

vidprep is not published to PyPI yet. Install it from the repository with
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/tomada1114/vidprep.git
cd vidprep
uv sync --all-groups
uv run vidprep doctor
```

`doctor` reports the external tools the stages shell out to — ffmpeg,
auto-editor, an ASR backend and the Silero VAD weights among them — and what to
install for the ones that are missing.

## Quick Example

```bash
vidprep init ./work/talk01 --source ~/Movies/talk01.mp4
vidprep audio-fix --stats
vidprep transcribe
vidprep detect
vidprep report --cuts   # decide the status of each candidate in cuts.json
vidprep render          # out/output.mp4 + out/subtitles.srt
```

## Next Steps

- [Getting Started](getting-started.md) — setup and first steps
- [API Reference](reference.md) — full API documentation
- [Contributing](contributing.md) — how to contribute
