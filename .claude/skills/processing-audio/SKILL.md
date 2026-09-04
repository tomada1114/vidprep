---
name: processing-audio
description: >
  Covers src/vidprep/audio.py — the audio-fix stage's processing order
  (denoise, then highpass, then two-pass loudnorm), the DeepFilterNet-vs-afftdn
  fallback in resolve_chain(), the length-invariance machinery around
  MAX_DELTA_MS, and the noise-floor measurement --stats writes to
  report/noise_floor.json. Use when touching audio.py's Chain, resolve_chain,
  _produce, _compare, or noise_floor, when deciding what audio.denoise,
  highpass_hz, or loudnorm targets in profile.json should be, or when a change
  to the filter chain's order needs justifying.
metadata:
  platforms: claude-code, codex
---

# Processing Audio

**Owns:** `src/vidprep/audio.py` — the `audio-fix` CLI stage, its
`denoise -> highpass -> loudnorm` filter-graph order and why that order is not
arbitrary, the DeepFilterNet-vs-`afftdn` denoiser choice, and the noise-floor
statistics the stage computes. **Does not own:** the subprocess mechanics that
actually run these ffmpeg/DeepFilterNet commands (`running-subprocesses`); the
loudness and audio/video sync checks that happen later, at render time
(`rendering-output`).

## The chain is `denoise -> highpass 80Hz -> loudnorm`, and the order encodes a real constraint

`Chain.filters()` joins `cleanup_filters()` (denoise, when it runs inside the
same ffmpeg process, plus `highpass=f={highpass_hz}`) with the `loudnorm`
filter, in that order — matching the module docstring's `denoise -> highpass
80Hz -> loudnorm` and design.md §5.1. `cleanup_filters()`'s own docstring
states the reason denoising and the high-pass both have to finish before
`loudnorm` runs: "everything meant to remove noise has run, and the makeup
gain that would lift the floor back up has not." `loudnorm` applies gain to
hit its integrated-loudness target, and that gain lifts the whole signal,
noise floor included — running it before denoising would mean denoising a
signal whose noise has already been amplified toward the target level, and the
carefully-measured "did the floor go down" comparison (REQ-007, tracked as
#33) would have nowhere clean to sit. What the code does *not* document is
why the high-pass specifically sits between denoise and `loudnorm` rather than
before denoising — only the denoise/highpass-before-`loudnorm` half of the
ordering has a documented rationale; treat the high-pass's position as
observed fact, not something to invent a justification for.

## `resolve_chain()` picks the denoiser — a profile field, resolved against what's installed

`DEEPFILTERNET = "deepfilternet"` and `AFFTDN = "afftdn"` are the two entries
of `DENOISERS`; `resolve_chain()` raises `UsageError` if `profile.json`'s
`audio.denoise` names anything else. When `deepfilternet` is requested, it
calls `doctor.check_deepfilternet()` (which looks for `deep-filter` or
`deepFilter` on `PATH`); on success `Chain.uses_deepfilternet` is `True` and
DeepFilterNet runs as its own subprocess (`denoise_command()`) before ffmpeg
ever opens the file, so `cleanup_filters()` omits `afftdn` from the ffmpeg
chain entirely. On failure `resolve_chain()` appends a warning
(`f"{check['error']}; falling back to {AFFTDN}"`) and silently switches
`denoise` to `afftdn`, which folds into the same single `-af` chain as the
high-pass and `loudnorm` instead of running separately.

## `loudnorm` runs twice: a measuring pass over stderr, then a linear-mode apply pass

`analysis_command()` runs the whole chain with `_loudnorm_filter(loudnorm,
None)` — no `measured_*` options — through `_ffmpeg.run_analysis`, because
`loudnorm`'s report is JSON printed to stderr, not stdout. `_loudnorm_report()`
pulls that JSON out with the `_LOUDNORM_REPORT` regex and `_measured_options()`
maps its fields onto the pass-2 filter options via `MEASURED_KEYS`
(`measured_I<-input_i`, `measured_TP<-input_tp`, `measured_LRA<-input_lra`,
`measured_thresh<-input_thresh`, `offset<-target_offset`). `render_command()`
then builds the pass-2 `-af` chain with those measured values plus
`linear=true` — design.md §5.1 gives the reason as avoiding the pumping that
`dynamic` mode would otherwise produce mid-recording. Targets (`I`, `TP`,
`LRA`) come from `profile.json`'s `audio.loudnorm`, modelled as
`models.Loudnorm` (`i=-14.0`, `tp=-1.0`, `lra=11.0` by default) — not a
constant in `audio.py`.

## Length invariance is machinery, not luck — two chain steps quietly move the tail

The module docstring names the two culprits: DeepFilterNet trims its own
STFT/lookahead delay off the end of the audio, and `loudnorm` labels its
output with timestamps offset by its own lookahead. `render_command()`
appends `LENGTH_TAIL = "asetpts=N/SR/TB,apad"` to rebuild timestamps from the
sample count and pad with silence, then cuts at the source's own duration with
`-t`. `_check_duration()` re-probes the result and raises
`InvariantViolationError` if the delta exceeds `MAX_DELTA_MS = 1.0` — the
message says "`audio/processed.wav` was left untouched," matching `_produce()`,
which only calls `project_module.atomic_replace()` after that check passes.

## The noise floor is measured at the one point in the chain that exists only transiently

`--stats` writes `report/noise_floor.json` (`NOISE_FLOOR_NAME`) because the
comparison REQ-007 needs — the floor right after denoising, before
`loudnorm`'s makeup gain — is only ever materialized while `audio-fix` is
running; `report` quotes this file rather than re-measuring, calling
`audio.py`'s own `floor_section()` (`report.py` never redefines it). `_compare()` reuses `detect_silence()`'s own
`silencedetect=noise=-40dB:d=0.5` (`SILENCE_NOISE`, `SILENCE_MIN_SECONDS`) —
the module's own comment gives the reason as keeping this measurement and
`report.py`'s later reuse of the same `detect_silence()` call in agreement
about which parts of the take are quiet (the `detecting-cuts` stage proposes
silence cuts through auto-editor's own threshold mechanism instead, and
never calls `detect_silence()`). `floor_intervals()` then shaves
`SILENCE_GUARD_SECONDS = 0.1` off each end of every silent stretch before
`astats` reads it, because the speech ramp and whatever frame `aselect` pulls
in at a boundary both read far louder than the true floor — a comment cites a
measured -45.87dB with the ends included versus -60.48dB without, on the
golden sample (#33). `LEVEL_DECIMALS = 2` rounds every dB field this stage
reports; `report.py` independently defines its own `LEVEL_DECIMALS = 2` at
the same value — a real duplicate, not something this stage needs to resolve.
`_publish_floor()` deletes a stale `noise_floor.json` on a run without
`--stats`, rather than leaving it to describe audio that's since changed.

## `--stats` is what turns on measurement — the chain itself runs the same either way

The `--stats` CLI flag (wired in `cli.py`, `run_audio_fix(with_stats=True)`)
does not change what `Chain` renders; it makes `_produce()` call `_compare()`,
which runs `measure()` (LUFS/TP/LRA via `loudnorm`'s analysis pass, plus the
noise floor when there's silence to read it over) against both the extracted
source and the finished output, and separately reads the denoised-but-not-yet-
normalised floor for the REQ-007 comparison. Without `--stats`, `audio-fix`
still denoises, high-passes, and normalises — it just produces no
`Measurement`, no `NoiseFloorReport`, and no `report/noise_floor.json`.

## Tuning this stage means editing `profile.json`'s `audio` section, modelled as `AudioProfile`

`AudioProfile` (`models.py`) carries the four fields that steer this module:
`denoise` (which of `DENOISERS` to prefer), `deepfilternet_atten_lim_db`
(`Field(default=12.0, ge=0.0, le=100.0)`, DeepFilterNet's attenuation ceiling),
`highpass_hz` (`Field(default=80, ge=0)`), and `loudnorm` (the `Loudnorm`
targets above). Deep ownership of the schema belongs to `modelling-artifacts`;
this is only where those fields feed into `Chain`.

## `WORKSPACE_PREFIX = ".audio-fix-"` follows the repo's per-module convention

`run_audio_fix()` creates its scratch directory with
`tempfile.mkdtemp(dir=loaded.root, prefix=WORKSPACE_PREFIX)` and removes it in
a `finally` with `shutil.rmtree(..., ignore_errors=True)`, publishing only on
success via `atomic_replace()`. The general convention each stage module picks
its own prefix under is `managing-the-project-dir`'s.
