---
name: transcribing-speech
description: >
  Covers src/vidprep/transcribe.py and src/vidprep/_asr.py — Silero VAD
  running mandatorily in front of the ASR recognizer, whisper.cpp/mlx-whisper
  backend selection in `_asr.resolve`, the `_ID_FORMAT = "s{:04d}"` segment id
  scheme, and `hallucination_phrases()` filtering known Whisper hallucinations
  out of `transcript.json`. Use when touching `run_transcribe`, `_recognise`,
  `_build_segments`, `_anchor_starts`, `Backend.resolve`, `vad_model`, or the
  packaged `dictionaries/hallucinations.json` list.
metadata:
  platforms: claude-code, codex
---

# Transcribing Speech

**Owns:** `transcribe.py` and `_asr.py` — Silero VAD running in front of the ASR
recognizer, backend selection (whisper.cpp vs mlx-whisper), segment id assignment,
and hallucination-phrase filtering. **Does not own:** process spawning and tool
resolution mechanics (`running-subprocesses`); the dictionary-based correction pass
that runs after transcription (`correcting-transcripts`).

## Silero VAD runs before the recognizer, and no profile setting or flag turns it off

`Backend._vad_options()` in `_asr.py` always adds `["--vad", "--vad-model",
str(self.vad_model)]` to both `whisper_command` and `detect_command`; `vad_model()`
resolves the Silero weights by globbing `doctor.VAD_MODEL_GLOB` (`"ggml-silero-*.bin"`,
defined once in `doctor.py` and re-exported in `_asr.py` as `VAD_MODEL_GLOB`) next to
the whisper.cpp models directory, raising `UsageError` when nothing matches.
`AsrProfile` in `models.py` fixes `vad: Literal["silero-v5"] = "silero-v5"` as a
single value rather than a switch, and its docstring states why: the Step 1 bench
measured 6 hallucinated segments without VAD and 0 with it
(verification-plan.md §12.2). `transcribe.py`'s module docstring frames the
consequence directly — a sentence invented over silence does not stay a
transcription problem, it comes back as a subtitle — which is why detection is
mandatory rather than a quality knob. Recognition always runs on
`audio/processed.wav`, never on anything already cut, because timestamps are only
meaningful while the timeline they were measured on is still intact.

## VAD is not a separate tool — whisper.cpp hosts Silero for both backends

`_asr.py`'s module docstring is explicit: whisper.cpp's `--vad` detects speech,
transcribes each region, and maps the timestamps back onto the original timeline in
one process, so the `whisper.cpp` backend reads `vad_segment_info` lines straight out
of the run that used them (`_SPEECH_REGION`, parsed by `parse_speech`). mlx-whisper
has no detection front-end of its own, so it borrows whisper.cpp's: a detection-only
run (`detect_command`, `-d` set to `_PROBE_DURATION_MS = "1"`) supplies the regions,
each one is sliced with ffmpeg (`slice_command`) and handed to `mlx_command`
individually, and `transcribe.py`'s `_shift` adds the region's offset back in. This is
why whisper.cpp must be installed even when the profile selects `mlx-whisper` —
`resolve()` looks up a whisper.cpp binary unconditionally. `Backend.mlx_command`'s
docstring records why regions are transcribed one at a time rather than in one
`--clip-timestamps` pass: on the golden sample, 23 of 106 segments landed outside the
speech they were meant to cover.

## `resolve()` probes installed binaries and weights, not just the profile field

`_asr.resolve(settings: AsrProfile) -> Backend` first finds a whisper.cpp binary via
`shutil.which` over `doctor.WHISPER_BINARIES`, raising `UsageError` naming
`brew install whisper-cpp` if none is on `PATH`. When `settings.backend ==
MLX_WHISPER` it additionally resolves `shutil.which(MLX_BINARY)`
(`"mlx_whisper"`), raising `UsageError` toward `uv sync --group asr` if missing —
mlx-whisper is Apple-Silicon-only, matching `managing-dependencies`'s note that the
`asr` dependency group is platform-gated. Model resolution differs by backend: mlx
only needs `_detection_host_model()`, the cheapest installed `ggml-*.bin` used purely
to host Silero, while whisper.cpp requires the exact `ggml-{settings.model}.bin` via
`_require()`. Either path also calls `vad_model()` unconditionally, since Silero is
loaded regardless of which recognizer transcribes. The two backend name constants,
`WHISPER_CPP = "whisper.cpp"` and `MLX_WHISPER = "mlx-whisper"`, are exactly the two
values `AsrProfile.backend: Literal["whisper.cpp", "mlx-whisper"]` accepts in
`models.py`.

## Segment ids follow `_ID_FORMAT = "s{:04d}"`

`transcribe.py` defines `_ID_FORMAT = "s{:04d}"` and `_build_segments` calls
`_ID_FORMAT.format(len(segments) + 1)` to produce `s0001`, `s0002`, and so on. This
numbering is what later gives `correcting-transcripts`'s `patch.json` a stable
address for each segment — that patch format is owned there, not here, but the id
scheme it references is assigned in this module.

## Hallucination filtering is a substring match gated by low speech coverage

`hallucination_phrases()` is `@cache`d and reads the packaged
`dictionaries/hallucinations.json` (`HALLUCINATION_RESOURCE`) through
`importlib.resources`, validating it against the local `_Hallucinations` pydantic
model (`extra="forbid"`) and raising `SchemaInvalidError` if the packaged list itself
is malformed — the packaging and override mechanics belong to
`packaging-data-files`, not here. `_hallucinations()` then keeps a segment only when
`phrase in segment.text` (a plain substring check, not exact equality) **and**
`_coverage(segment, speech) < MIN_SPEECH_COVERAGE` (0.5) — coverage is what tells a
truly spoken sign-off from an invented one, since the same phrase said for real sits
inside a detected region. A hit here is not dropped quietly: `_verify()` folds it into
`InvariantViolationError` and refuses to publish `transcript.json` at all rather than
silently deleting the offending segments.

## `_build_segments` assembles `Segment`s and enforces monotonic order; `_anchor_starts` is a separate correction pass

`_build_segments(raw, duration)` turns the recognizer's raw entries into numbered
`Segment` objects, skipping any entry with empty text or a start at or past its
(duration-clamped) end, and raises `AsrFailedError` if consecutive raw entries run
backwards in time or if constructing a `Segment` fails pydantic validation. It does
not correct anything — that is `_anchor_starts`'s separate job: it pulls a segment's
start back onto the speech region it actually transcribes when whisper.cpp's
region-separator interpolation strands that start in silence, and records one warning
string per correction rather than failing the stage. `run_transcribe` calls
`_anchor_starts(_build_segments(raw, duration), speech)` as two distinct steps, and
only the result of both goes to `_verify`.

## Errors: `UsageError` for a missing tool, `AsrFailedError` for a broken run, `InvariantViolationError` for a transcript that disagrees with the VAD

`UsageError` covers preconditions that stop the stage before it starts: `_audio_path`
when `audio-fix` has not produced `audio/processed.wav`, and every missing-binary or
missing-weights branch inside `_asr.resolve`/`vad_model`. `AsrFailedError` covers a
recognizer that ran but produced something unusable — a non-zero exit from `_asr.run`,
`parse_speech` finding no `whisper_vad` marker at all, `read_transcript`/`_read_segment`
meeting a document shape neither backend produces, `_build_segments` seeing
out-of-order timestamps, or `run_transcribe` seeing zero detected speech regions.
`InvariantViolationError` is `_verify`'s alone: a segment starting outside every
detected region, or a hallucination hit over silence — both discard the whole
transcript rather than partially writing it. `SchemaInvalidError` covers the packaged
hallucination list failing its own schema and `_check_bounds`/`_publish` finding an
interval that runs past the manifest's recorded duration. The full hierarchy and
exit-code mapping are `designing-errors`'s territory.

## `STAGE`, `TRANSCRIPT_NAME`, `VAD_REPORT_NAME`, `WORKSPACE_PREFIX` follow the per-module constant convention

`transcribe.py` defines `STAGE = "transcribe"`, `TRANSCRIPT_NAME = "transcript.json"`,
`VAD_REPORT_NAME = Path("report") / "vad.json"`, and `WORKSPACE_PREFIX =
".transcribe-"` at module scope, the same shape every other stage module uses. How
`STAGE` feeds `project_module.record_stage` and how `WORKSPACE_PREFIX` interacts with
staleness detection is `managing-the-project-dir`'s territory; how `TRANSCRIPT_NAME`
surfaces through the CLI's `--json` output is `writing-cli-commands`'s.
