# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `vidprep audio-fix`: denoises with DeepFilterNet (falling back to `afftdn`
  when it is not installed), high-passes at 80 Hz and normalises loudness with
  a two-pass linear `loudnorm`, writing `audio/processed.wav` as PCM 16 bit at
  the source sample rate. The length is held to within 1 ms of the source and
  verified before the file is published; `--stats` reports loudness, true
  peak, LRA and the noise floor of the silent stretches, before and after
- `vidprep doctor`: checks ffmpeg (including libass), ffprobe, auto-editor,
  the ASR backends (whisper.cpp / mlx-whisper), DeepFilterNet and the SudachiPy
  dictionary, prints the report as JSON with `--json`, and exits `3` when a
  required dependency is missing
- pydantic v2 schemas for the intermediate JSON files (manifest, transcript,
  cuts, telops, styles, profile) enforcing identifier shapes, interval bounds
  and the "approved cuts never overlap" invariant
- `vidprep` CLI skeleton with the eight subcommands, the common
  `--project/-p` / `--json` / `--dry-run` flags and the `0/1/2/3` exit codes;
  `init` is implemented, the processing stages are still skeletons
- `Timeline`: the original/cut timeline mapping (`forward`, `inverse`,
  `map_segments`) that video rendering and subtitle output share, so both use
  the same cut boundaries
- 実現可能性調査レポートと設計インプットを `docs/` に追加（設計フェーズ、実装なし）
- [uv-template](https://github.com/tomada1114/uv-template) ベースのプロジェクト雛形
