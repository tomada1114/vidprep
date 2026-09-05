# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `audio-fix` now reads the JSON report from loudnorm's second pass and warns
  when the filter falls back from the requested `linear` normalization to
  `dynamic`. The output is still produced — dynamic normalization is a valid
  loudnorm result — but the change in gain behavior is no longer silent.

- `render` now verifies the encoded output's true peak against the profile's
  `target TP` before publishing it. An output above the limit is rejected and
  the previous `out/output.mp4` remains in place, just as it does for a failed
  loudness or length check.

- A correction applied between two `prep` runs no longer misses the output.
  `correct --apply-patch` rewrites `transcript.json` without running a pipeline
  stage, so nothing landed in the invocation's ran-set and no `profile.json`
  value moved: `prep` reported `render: up to date` and delivered subtitles and
  `transcript.txt` with the old wording, silently and with a zero exit code.
  Stage records now carry `inputs_sha256`, the digests of the artifacts the
  stage read, and a stage whose inputs have moved runs again. Records written
  before this change carry no digests and are read as fresh, so an existing
  project does not re-run every stage once on upgrade.

- `doctor`'s remedy for a missing SudachiDict now names the interpreter to
  install into. A bare `uv pip install sudachidict_core` lands the dictionary in
  whatever environment is active, which is not the one an installed `vidprep`
  runs from, so the check kept failing after following its own advice.

- The `skipped` list of the dictionary pass reports one entry per decision. A
  katakana spelling that is also its own reading — every model name in a real
  dictionary — matched in both the surface and the reading stage and was listed
  twice, doubling the work list the `correct-transcript` skill reads.

### Changed

- New projects now leave denoising, silence detection and filler detection off
  unless `profile.json` opts into them. `audio-fix` still applies the 80 Hz
  high-pass and two-pass loudnorm by default. Auto-editor and DeepFilterNet are
  optional doctor checks; selecting one while it is unavailable now stops with
  an actionable error instead of silently changing the requested processing.
  Profiles created before these switches existed retain their old cutting
  behavior when loaded.

- `audio-fix` now collects before/after loudness and noise-floor statistics by
  default. Use `--no-stats` when only the processed audio is needed; `--stats`
  remains available as an explicit spelling of the default. `report` also warns
  when the source needs more than 12 dB of gain to reach its loudness target, so
  an unusually quiet recording is visible before later processing raises its
  noise floor as well.

- `--verify-asr` no longer fails the run on a missing hunk made only of
  interjections. A 「はい」 spoken into the pause before a clause abuts the next
  words once that pause is cut, and the second pass may fold it in rather than
  emit a token for it — the audio is still in the render, so this is a false
  positive, and a gate that fires on it teaches the reviewer to override the
  gate. Such flags are still reported, marked `negligible` in `--json` and
  `(interjection, ignored)` in the log; `near_boundary_flags` still counts every
  flag and the new `gating_flags` counts the ones that fail the run.

### Added

- `out/transcript.txt`, a readable counterpart to `out/subtitles.srt`: the same
  cut-timeline entries as `[MM:SS] body` paragraphs (`[H:MM:SS]` past an hour)
  instead of individually-timed captions. `render` writes it unconditionally,
  next to the video and the SRT, and `vidprep prep` delivers it beside the
  source as `<name>.txt`, so no flag is needed either way. Paragraph breaks are
  a mechanical rule over what the data already carries — vidprep stays
  AI-free, so it cannot tell where a topic turns — never under 100 full-width
  characters, then a sentence-ending mark or a pause of 0.6s or more, and
  always at 300 characters regardless of either signal.

- `vidprep prep <video>` — a composite subcommand that runs the whole pipeline
  over one file, with what comes out copied next to the recording as
  `<name>.edited.mp4`, `<name>.srt` and `<name>.txt`. It is the same command
  whether installed with `uv tool install --force --from . vidprep` or run
  against the working tree as `uv run vidprep prep`. The project lands beside
  them as `<name>.vidprep/`, which is what makes a second run cheap: a stage
  whose result is already there and whose `profile.json` parameters have not
  moved is skipped, and a stage downstream of one that did run is redone, so
  tuning a threshold re-does exactly what the change reaches. The run stops once
  after the dictionary pass — on the invocation that produced the transcript,
  so never twice — for the `correct-transcript` skill to proofread it, because
  the CLI itself stays AI-free (design.md §7); `--yes` skips that for an
  unattended run. The filler candidates `detect` leaves for a reviewer are
  approved on the reviewer's behalf, since the point of the command is
  something publishable without a review pass, but only while
  `filler.enable_weak` is off: the tier a candidate came from is not recorded in
  `cuts.json`, so with the weak tier enabled there is no way to approve the
  strong ones alone and the approval is declined instead. `--keep-fillers` cuts
  only the silences, `--no-verify-asr` drops the second ASR pass, and
  `--dry-run` lists the stages a run would perform without doing any of them

- A closing fade to black, and a tail for it to happen over. `detect` now cuts
  the silence that reaches the end of the material all the way to that end
  rather than stopping `pad_post` short of it — after the last word there is no
  following word for `pad_post` to protect, and stopping short left the
  recording's final fraction of a second stranded on the far side of the
  removed stretch — and leaves `silence.tail_pad` seconds of quiet behind the
  last word instead. `render` fades that tail to black, picture and sound, over
  `render.fade_out` seconds. Both default to `2.0`, so the fade covers the tail
  exactly and never starts while somebody is still talking. A recording stopped
  the moment the sentence ended has no tail to fade: the render holds its last
  frame (`tpad=stop_mode=clone`) for the missing part and darkens from the
  picture it ended on rather than cutting to black, reports the held seconds
  under `closing.held_sec`, and says so in the human output. The held frames
  are the only thing in the renderer that changes a length: they are rounded up
  to a whole frame and counted into the length check of verification-plan.md
  §8, and the frame rate is pinned with `fps` before them, because `tpad`
  behind a `concat` adds nothing at all and reports nothing about it
  (ffmpeg 7.1.1) — which would have padded the sound and not the picture. Set
  `render.fade_out` to `0` for the previous behaviour, where the video ends the
  moment the material does

- `vidprep correct --dict <path>` and `correct.dictionary_path` in
  `profile.json`: read the misconversion dictionary from outside the package,
  so several projects can share one maintained file instead of each forking
  `dictionaries/asr-dict.json`. `--dict` wins over the profile's
  `dictionary_path`, which wins over the packaged default; both expand `~`,
  and a relative `dictionary_path` resolves against the project directory
  rather than the shell's cwd, since `profile.json` is per-project. A path
  that does not exist is a usage error naming it rather than a silent
  fallback to the packaged dictionary, and a schema violation is reported the
  same way `load_dictionary` already reports one. Both the diff summary and
  `--json` output name the dictionary that was used, under `dictionary_source`
- The three agent skills of design.md §7 —
  `.claude/skills/correct-transcript`, `review-cuts` and `place-telops`. Each
  one reads the intermediate JSON, writes exactly one artifact
  (`patch.json`, the `status`/`note` of `cuts.json`, `telops.json`) and hands it
  to the CLI to be verified: `vidprep correct --apply-patch`,
  `vidprep report --json` and `vidprep render --preview`. A rejection is
  answered by fixing the artifact — never by editing the transcript, the cut
  intervals or the package. `correct-transcript` also carries what the golden
  sample showed: the speaker dictates CLI commands and option names out loud,
  so katakana renderings of them are restored from context
- `dictionaries/asr-dict.json`: `resume` (「リズーム」) and `claude -c`
  (「クロード-C」), the two dictated CLI terms the golden sample misrecognises
  reproducibly. Context-dependent ones such as 「半額スペース」 stay out of the
  dictionary and are left to LLM correction
- `vidprep render --verify-asr`: transcribes the finished `out/output.mp4` a
  second time — with the backend, model and detector `transcript.json` records,
  so both passes make the same mistakes and cancel out — and compares it with
  what the kept segments should say, `reason: filler` cuts taking their word out
  of the expectation. A run of two characters or more that the second pass never
  heard, placed back on the original timeline through the cut mapping and
  landing within two seconds of a cut boundary, is reported as a boundary flag
  with the cut, the second and the missing text. The global CER is recorded as a
  reference figure and decides nothing. The check is a gate — one flag exits
  `3` — and the new `render.verify_asr_mode` set to `"advisory"` in
  `profile.json` reports the same flags with the exit code left at `0`. It was
  introduced as advisory and promoted once the recogniser's repeatability had
  been measured: three comparisons of one render returned identical numbers,
  and no normal boundary has been flagged in 364 of them
  (verification-plan.md §8.1). The render is only read; nothing about it
  changes either way
- `vidprep render` now reads `out/subtitles.srt` back after writing it and
  refuses with exit `3` if an entry the mapping produced is not in the file, at
  its timing and with its text (verification-plan.md §9)
- `just golden` and `scripts/golden_run.py`: the whole pipeline over the fixed
  sample, archiving `report/stats.json`, every warning and each stage's result
  under `fixtures/runs/<date>/`; a stage that fails stops the run and is
  recorded with its reason, whatever it raised, and the archive is written
  either way. `just golden-diff` (`scripts/compare_stats.py`) diffs two runs —
  every number, warning lists by their length, and text values such as the
  `verify_asr` backend and model — and reports "first run" and exits `0` when
  there is nothing to compare against. Both are local-only. `--skip <stage>`
  leaves one stage out of an otherwise identical run, which is how a number is
  attributed to that stage rather than to the pipeline; the run says what it
  skipped and records it in `summary.json`, because a run missing a stage is
  not a baseline
- `tests/fault_injection/`: the six deliberately broken inputs of
  verification-plan.md §10, each asserting that the check meant to catch it
  fails. Every case runs on its own
  (`uv run python -m tests.fault_injection.case02_midword_cut`) and all six run
  in the suite
- `vidprep render`: applies the `approved` cuts of `cuts.json` and writes
  `out/output.mp4` (kept intervals joined with `trim`/`concat`, video
  re-encoded at CRF 18 / preset slow with the source resolution and frame
  rate, audio taken from `audio/processed.wav` with a length-preserving 10ms
  fade at every boundary and encoded as AAC at 320 kbps) together with
  `out/subtitles.srt`, timed by the same timeline as the video and broken into
  lines at BudouX phrase boundaries. `--no-wrap` adds an unbroken
  `out/subtitles.nowrap.srt` to compare the breaks against. Cuts are snapped
  inwards onto the frame grid first, so the output length matches the cut list
  to within a frame however many boundaries there are; the finished file is
  then checked for length, audio/video synchronisation and loudness before it
  replaces anything, and a result that fails leaves the previous render in
  place. Entries dropped by a cut, shown for less than `min_display`, read
  faster than `max_cps` or too wide for `max_chars_per_line` are reported
  rather than silently fixed
- `Timeline.keeps`: the intervals the cuts leave behind, so rendering and
  subtitle mapping read the same interval table
- `vidprep report`: writes `report/stats.json` (source and rendered length,
  reduction ratio, cuts broken down by reason and status, loudness before and
  after with a level-matched noise floor, and the subtitle warnings — dropped
  by a cut, under `min_display`, over `max_cps`), draws a `showwavespic` still
  per cut into `report/boundaries/<cut_id>.png` and stitches
  `report/boundary_digest.mp4` from the source material: every cut plus two
  seconds of context on each side, separated by half a second of silent black.
  `--cuts` lists each candidate with the transcript it would delete and the
  segments around it, as text or as JSON. Every input is optional — running
  before `detect` or before `render` leaves those sections empty or `null` and
  still exits `0` — and nothing outside `report/` is written, not even the
  manifest
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
  the ASR backends (whisper.cpp / mlx-whisper), the Silero VAD weights both
  backends need, DeepFilterNet and the SudachiPy dictionary, prints the report
  as JSON with `--json`, and exits `3` when a required dependency is missing.
  The VAD weights are a required check, not an informational one: `transcribe`
  resolves `ggml-silero-*.bin` for either backend and refuses to start without
  it, so the model directory is reported per role — the Silero file no longer
  counts as a transcription model for the ASR check
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
- `vidprep render --preview`: reads `telops.json` and `styles.json`, writes
  the telop track to `out/telops.ass` and burns it into `out/preview.mp4` with
  libass. A telop naming a `segment_id` is shown for exactly as long as that
  segment's subtitle — both go through the one `Timeline` mapping — while a
  `start` in original-timeline seconds plus a `duration` is mapped the same
  way. Style presets ship with the package and a project `styles.json`
  overrides them field by field, so a stated `fontsize` keeps the packaged
  `fontname`; weight is asked for by family name (`Hiragino Sans W6`) because
  `Bold: 1` was measured to do nothing through CoreText on macOS. A telop
  naming a segment or a preset that is not there stops the run with exit `3`
  before anything is encoded, a telop whose segment a cut removed is reported
  rather than drawn, and `out/output.mp4` is only ever read
- `vidprep transcribe`: a segment start left in the silence between two
  detected regions is moved onto the speech it transcribes instead of failing
  the stage. whisper.cpp recognises the regions concatenated with 0.2s of
  silence between them and maps the timestamps back by interpolating across
  each separator, so a boundary it placed inside one returns stretched over
  the whole original pause — on the golden sample 0.119s of separator came
  back as 1.300s past the end of a region. Only a segment at least half of
  whose length lies inside detected speech is moved, and the move is reported
  as a warning and counted as `anchored_starts` in `--json`; nothing is
  dropped, and a segment that covers no speech is still refused with exit `3`
- `vidprep detect`: the auto-editor progress bar is switched off with
  `--progress none`. The `--export v3` timeline is read from stdout, which is
  also where auto-editor draws that bar, and `--quiet` silences its messages
  but not the bar — so an analysis slow enough to draw one prepended
  `Analyzing audio volume | ... ETA ...` to the document and the stage stopped
  with `timeline_schema` (exit `2`). Whether the bar appears depends on
  auto-editor's audio-level cache being warm, which is why the same command
  parsed one run and failed the next. The timeline is still validated strictly
- `vidprep audio-fix --stats` measures the noise floor where denoising can be
  seen: on the denoised audio *before* `loudnorm`, against the same silence in
  the material. Both earlier forms of the comparison failed on the golden
  sample — the absolute floor of the finished file because the makeup gain
  lifts the floor with everything else, and the level-matched
  `below_programme_db` for the same reason plus a measurement fault. Each
  silent stretch is now read with 0.1s held back from either end and with
  `aselect` deciding on 1024-sample frames, because the speech ramp at the
  boundary and whatever a long frame drags in with it are tens of dB above the
  floor and were dominating the average: the same audio read -45.87 dB with
  the ends included and -60.48 dB without, which hid a 9 dB improvement behind
  a 0.75 dB one. The comparison is written to the new `report/noise_floor.json`
  — the point of the chain it describes exists only while the stage runs — and
  `report/stats.json` quotes it as `noise_floor.denoise`, which is what REQ-007
  is decided on. `noise_floor.output` keeps the absolute and level-matched
  floor of the finished audio as reference figures. The statistics document is
  version `2`

### Changed

- `Renderer.render` takes a `RenderJob` instead of the five arguments that
  built one, which is how the closing fade reaches it
