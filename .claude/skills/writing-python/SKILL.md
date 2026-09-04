---
name: writing-python
description: >
  Covers how one module or function under src/vidprep/**/*.py and
  scripts/**/*.py is written: mypy-strict typing and Any, the choice between
  @dataclass(frozen=True, slots=True), pydantic BaseModel, TypedDict and
  Protocol, `from __future__ import annotations` plus TYPE_CHECKING, EAFP,
  keyword-only boolean parameters, Literal over Enum, UPPER_SNAKE_CASE
  constants, the 300-line/40-line review triggers, Google-style docstrings,
  and ruff's bandit (S) rules. Use when writing or reviewing a function or
  class body inside src/vidprep, choosing between a dataclass and a pydantic
  model, adding an `Any`, or deciding whether a module or function has grown
  too large.
metadata:
  platforms: claude-code, codex
---

# Writing Python

**Owns:** how one module or function under `src/vidprep/**/*.py` and `scripts/**/*.py` is written — mypy-strict typing and `Any`, `@dataclass(frozen=True, slots=True)` versus pydantic `BaseModel` versus `TypedDict` versus `Protocol`, `from __future__ import annotations` plus `TYPE_CHECKING`, EAFP, keyword-only boolean parameters, `Literal` over `Enum`, `UPPER_SNAKE_CASE` named constants, the 300-line/40-line review triggers, Google-style docstrings, and ruff's bandit (`S`) rules. **Does not own:** the exception hierarchy and exit codes (`designing-errors`); what a module may export (`public-api-contract`); how a test is written (`writing-tests`); the CLI's own shape (`writing-cli-commands`); artifact JSON schemas (`modelling-artifacts`); the subprocess/process-execution boundary (`running-subprocesses`).

## mypy runs `strict = true`; a bare, uncommented `Any` is a review rejection

`pyproject.toml`'s `[tool.mypy]` sets `strict = true` for `python_version = "3.12"`, plus `warn_return_any`, `warn_unused_configs`, `warn_unused_ignores`, `warn_redundant_casts`, `warn_unreachable`, and `enable_error_code = ["ignore-without-code", "redundant-cast", "truthy-bool"]` — an `# type: ignore` with no error code, or a redundant cast, fails the same way a type error does. The `tests.*` override relaxes `disallow_untyped_defs`/`disallow_untyped_calls` for test bodies; `sudachipy.*` gets `ignore_missing_imports = true` because, as the comment above it says, "SudachiPy ships no py.typed marker and has no stubs on typeshed." That same sentence, verbatim, is the comment `_dictionary.py` puts above its own two sanctioned `Any` uses — `_open_tokenizer`'s `dictionary: Any` parameter and `_SudachiReader.tokenize: Any` — which is the shape a raw `Any` needs to survive review: a one-line reason, not silence. The other, much larger use of `Any` needs no such comment because it names a real house convention rather than an escape hatch: 45 occurrences of `dict[str, Any]` across `src/vidprep` (44 lines — `_ffmpeg.py:192` carries it twice on one line) are the de facto shape of anything crossing the `--json` boundary — every `to_dict()` method (`doctor.Report.to_dict`, `prep.Result.to_dict`, `correct.Plan.to_dict`) and every `plan()` function returns it, because the document being built is the contract and `Any` is the honest type for values whose shape differs per key.

## `@dataclass(frozen=True, slots=True)` is the default; a mutable one is a named exception

58 of the 60 `@dataclass` declarations in `src/vidprep` are `frozen=True, slots=True` — `project.Project`, `_ffmpeg.ProbeResult`, `cli.CommonOptions` and `cli.Output` among them. The two that are not frozen still keep `slots=True`, and each says why in its own docstring: `timeline._Entry` is "a segment being mapped" that `Timeline.map_segments` mutates in place as it walks the list, and `_ass._Resolution` is "the plan being assembled, plus the complaints collected on the way" — a document `_ass.py` builds field by field. `timeline.Timeline` itself is not a dataclass at all; it hand-writes `__slots__` because its `__init__` derives state (`_starts`, `_removed`, `_images`) a generated `__init__` cannot express. A mutable dataclass earns its place the same way those two do — name what mutates it and why — rather than being reached for out of habit.

## pydantic `BaseModel` is for a document parsed from JSON at a boundary, not a general value type

`models.py`'s `_Strict(BaseModel)` sets `model_config = ConfigDict(extra="forbid")` once and every schema in `models.py` inherits it, so a typo in a hand-edited `profile.json` fails loudly instead of silently vanishing. Five other modules parse their own JSON file each and re-declare `model_config = ConfigDict(extra="forbid")` inline rather than importing `_Strict` — `_autoeditor.Timeline`/`_Clip` (auto-editor's own v3 export, not a vidprep artifact), `_dictionary.DictionaryEntry`/`AsrDictionary`, `_fillers.FillerDictionary`, `correct.PatchEdit`/`Patch`, and `transcribe._Hallucinations` — because each is a document belonging to that module's own concern, not the intermediate-artifact schema `models.py` owns. One model opts out of strictness on purpose: `models.Word` sets `ConfigDict(extra="allow")` because word-level timing "Not produced in v1; accepted so backends may add it" — a schema for data vidprep does not yet write itself has no business rejecting fields it does not yet know about.

## `Protocol` over ABC — three of them, all structural, none inherited from

`_reencode.Renderer`, `prep.Reported`, and `_dictionary.Reader` are the only three in `src/`, and there are no ABCs anywhere in the package. Each exists so a second implementation can be swapped in without the caller noticing: `_reencode.Renderer` is what lets the cut-without-re-encode renderer design.md §8 describes replace `ReencodeRenderer` later, and `render.run_render` reaches it "through the protocol, not through the concrete class" for exactly that reason. A `Protocol` earns its place when a caller genuinely needs to accept more than one shape — reach for a plain function parameter first.

## `TypedDict` is reserved for a genuinely fixed-but-partial shape; `dict[str, Any]` covers the rest

`timeline.SegmentWarning` is the one real `TypedDict` in `src/vidprep`, and it uses `NotRequired[float]` for `value` and `threshold` because those two fields apply only to a `min_display` warning, never to `dropped_by_cut`. `doctor.py`'s `Check = dict[str, Any]` is the case where a `TypedDict` was on the table and lost, and the module says why in a comment: "the keys differ per check — ffmpeg reports `libass`, the ASR check nests a result per backend — because the document this becomes is the contract, so `Any` is the honest type." A `TypedDict` earns its place when every instance shares the same keys; once the keys themselves vary by caller, forcing a `TypedDict` on it would just relocate the untyped part into a `total=False` sprawl.

## `Literal` plus a loose string constant does what `Enum` would, without the ceremony

`grep -rn "enum\.\|from enum\|Enum" src/vidprep/` returns nothing — the package has zero `Enum` usage. A fixed set of values inside a pydantic model is `Literal`: `models.Cut.status: Literal["proposed", "approved", "rejected"]`, `models.AsrProfile.backend: Literal["whisper.cpp", "mlx-whisper"]`. The same values used outside a model are `UPPER_SNAKE_CASE` string constants rather than an `Enum` member — `detect.SILENCE = "silence"`, `_asr.WHISPER_CPP = "whisper.cpp"` — so a stage that compares `cut.reason == detect.SILENCE` is comparing plain strings the schema already constrains, with no enum-to-string conversion at the JSON boundary to get wrong.

## Boolean parameters are keyword-only and named as adjectives, not `is_`/`has_`/`can_`/`should_`

AGENTS.md still asks for an `is_`/`has_`/`can_`/`should_` prefix on every boolean; the codebase itself does something different and, on the evidence, better. Every boolean reads as a bare noun or adjective — `CommonOptions.json_output`, `CommonOptions.dry_run`, `FillerProfile.enable_weak` — and every boolean *parameter* is forced keyword-only behind a bare `*,`: `audio.run_audio_fix(loaded: Project, *, with_stats: bool = False)`, `render.run_render(loaded, *, no_wrap=False, preview=False, verify_asr=False)`. Reject in review: a positional `bool` parameter — the keyword-only convention is what makes `run_render(loaded, True, False, True)` a `TypeError` instead of a call nobody can read back correctly.

## `from __future__ import annotations` is mechanical, not a habit to remember

`[tool.ruff.lint.isort] required-imports = ["from __future__ import annotations"]` inserts and enforces the import — ruff adds it on `--fix` and flags a file missing it, so it is never a judgment call. Pair it with a `TYPE_CHECKING` block for anything that appears only in an annotation: `project.py` imports `Mapping` and `BaseModel` under `if TYPE_CHECKING:` (they are used only as return-type and field annotations), keeping pydantic's own `BaseModel` out of the runtime import graph of a module that otherwise never touches it directly.

## `UPPER_SNAKE_CASE` constants are the default home for a literal; `PLR2004` makes the alternative fail lint

Module constants carry a `#:` doc-comment above them when the number needs justifying — `_ffmpeg.STDERR_TAIL_CHARS = 2000`, `project.HASH_CHUNK_BYTES = 1024 * 1024`. Ruff's `PLR2004` (magic-value-comparison) is in the default `select` list and is waived only for `tests/**` in `[tool.ruff.lint.per-file-ignores]`, so a bare literal dropped into a comparison inside `src/` fails lint rather than merely reading badly.

## 300-line modules and 40-line functions are a review trigger, not a law

21 of the 29 modules in `src/vidprep` already exceed 300 lines — `audio.py` leads at 804, followed by `report.py` (639), `render.py` (636), `detect.py` (619) and `cli.py` (586) — so the threshold names when to *look*, not when to split automatically; a module earns a split when it holds two concerns that could be reasoned about separately, not when `wc -l` crosses a number. A 40-line function is rarer but real: `render.run_render` runs 545–636, kept in one function because its docstring states the reason — "everything that can fail cheaply fails first" — and splitting the checks out would scatter that ordering across several functions a reader would have to re-assemble.

## Google-style docstrings are enforced by ruff's `D` rules; `Raises:` sections are not

`[tool.ruff.lint.pydocstyle] convention = "google"` applies to `src/**` with no per-file exemption (only `tests/**` and `scripts/**` waive `D1`), so every public function and class carries at least a one-line docstring mechanically. A full `Args:`/`Returns:`/`Raises:` section is not mechanically required, and the gaps are real: `project.write_json` and `project.atomic_write_text` both do I/O that can raise `OSError`, and neither carries a `Raises:` section — acceptable under "document why, not what the signature already says" only when the omission is deliberate, not just unnoticed.

## Bandit's `S` rules stay on; a `noqa` needs a comment that argues the specific check, not a blanket excuse

`_ffmpeg._execute`'s `subprocess.run` call carries `# noqa: S603`, directly preceded by "Fixed argument vector, shell=False: no interpolation into a shell." — the comment names exactly what the check is worried about (shell injection through a partial path) and why this call site is safe from it. `doctor._run_command` repeats the identical comment over its own `subprocess.run`, which is the same argument made twice rather than two separate justifications — reuse the wording when the reasoning genuinely is the same call shape, don't paraphrase it into something vaguer.
