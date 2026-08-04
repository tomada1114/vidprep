# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `vidprep detect`: converts the `auto-editor --export v3` timeline into padded
  silence cuts (`approved`), proposes filler-word cuts from the transcript and
  the speech regions behind it (`proposed`), and merges the result into any
  existing `cuts.json` so an approved, rejected or hand-written cut keeps its
  verdict and its note across runs — identifiers are never reused. A v3 export
  in a shape vidprep does not recognise fails with exit `2` instead of being
  guessed at, and a run whose silence cut would remove more than 0.2s of
  speech fails with exit `3` and writes nothing
- the filler dictionary (`vidprep/dictionaries/fillers.json`, shipped with the
  package): six strong words used by default and three weak ones used only
  when `profile.json` sets `filler.enable_weak`. A project may add its own in
  `<project>/dictionaries/fillers.json`
- `vidprep transcribe`: runs Silero voice activity detection in front of the
  recogniser — whisper.cpp or mlx-whisper, chosen by the new `asr` section of
  `profile.json` — and writes `transcript.json` in original-timeline seconds
  plus `report/vad.json` with the speech regions. Detection cannot be skipped:
  there is no flag and no profile value that turns it off. A transcript whose
  segments start where no speech was detected, or that repeats a known
  hallucination over silence, is refused with exit `3` and never written;
  material with no speech at all fails with exit `2` rather than producing an
  empty transcript
- the known-hallucination phrase list (`vidprep/dictionaries/hallucinations.json`,
  shipped with the package), used by the transcript verification and reusable
  by the re-transcription check
- `vidprep correct`: replaces the misconversion dictionary's terms in the
  transcript — literally, then by SudachiPy reading so unseen spellings are
  caught too — replacing only `confidence: always` entries and reporting the
  homophones it deliberately left to LLM correction. `--apply-patch` verifies
  a patch against the transcript before applying any of it (unknown ids,
  duplicate ids, schema), rejects it whole with exit `3` otherwise, shows the
  diff and asks unless `--yes` is given. Nothing but segment text can change,
  checked again after the correction and before the file is written
- the misconversion dictionary (`vidprep/dictionaries/asr-dict.json`, shipped
  with the package), seeded from the iobsidian `fix-transcriptions` dictionary
  with a `yomi` reading added to every entry
- `scripts/cer.py`: character error rate against the human reference, with the
  normalisation rules (NFKC, punctuation and whitespace removal, lower-casing,
  no number folding) exposed as an importable `normalize()`
- `scripts/asr_bench.py`: the ASR benchmark harness — runs each candidate twice
  under `/usr/bin/time -l`, counts hallucinated segments against ffmpeg
  `silencedetect` output, compares VAD on/off, and writes the result matrix
  plus the selection rationale
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
