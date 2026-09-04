---
name: changing-gates
description: >
  Covers editing a file that enforces rather than implements: `.github/workflows/*.yml`,
  `.agents/hooks/guard.py`, `.agents/hooks/format.py`, `.agents/hooks/stop_check.py`,
  `.claude/settings.json` and `.codex/hooks.json`, `justfile` recipes, the
  `[tool.ruff]`/`[tool.mypy]`/`[tool.pytest.ini_options]`/`[tool.coverage.*]` tables
  in `pyproject.toml`, `.pre-commit-config.yaml`, and `.github/zizmor.yml`. Use when
  adding or loosening a ruff rule, changing `--cov-fail-under=80`, editing a hook
  script or its matcher/timeout, adding a CI job, or bumping a pinned tool version
  in `.pre-commit-config.yaml`.
metadata:
  platforms: claude-code, codex
---

# Changing Gates

**Owns:** editing a file that enforces rather than implements — the three `.agents/hooks/*.py` scripts, `.claude/settings.json` / `.codex/hooks.json`, `.github/workflows/*.yml`, `justfile` recipes, the `[tool.ruff]` / `[tool.mypy]` / `[tool.pytest.ini_options]` / `[tool.coverage.*]` tables in `pyproject.toml`, `.pre-commit-config.yaml`, and `.github/zizmor.yml`. **Does not own:** which tests exist or where they live (`writing-tests`; `placing-tests`); whether a dependency may be added at all (`managing-dependencies`); what a gate change breaks for consumers (`release-impact`).

A gate file is reviewed at the weight of "this defines what done means for every future change," not at the weight of an ordinary code edit — a loosened rule or a skipped check here silently lowers the bar for every PR after it, not just the one that touched the file.

## Three layers, each stricter than the last: hooks < `just check` < CI

The hooks in `.agents/hooks/` are agent-facing and deliberately incomplete — `stop_check.py`'s own docstring says it runs "the same ruff lint + format checks and mypy as `just lint` (no tests — those stay in `just check` and CI)". `just check` (`justfile`'s `check: fmt lint test` recipe) is the full local gate a human runs before a PR. CI in `.github/workflows/ci.yml` is authoritative: it runs everything `just check` runs, on a Python version matrix, plus jobs nothing local runs at all (`spell-check`, `build`, `zizmor`). A change to one layer that isn't reflected in the others creates a gap an agent can pass through locally and still fail in CI, or vice versa.

## `.agents/hooks/*.py`: what each hook checks, and what it deliberately skips

`guard.py` (PreToolUse, matcher `Edit|Write|Bash`, 30s timeout) blocks writes to `uv.lock` ("run `uv lock` or `uv add` instead"), to `.env`/`.env.<stage>` files unless the name ends `.example`/`.sample`/`.template`, and to any path with `secrets` among its parent segments — the comment calls this the "write-side counterpart of the `Read(secrets/**)` deny in settings.json." For Bash it splits the command on `SEGMENT_SPLIT` (`\|\||&&|;|\||&|\n`) and inspects each segment's argv independently, so a flag on one command in a pipeline can't excuse another; it blocks `git commit --no-verify` (or a clustered `-n`) and a forced `git push` (bare `--force` or clustered `-f`) unless `--force-with-lease` is present. `format.py` (PostToolUse, matcher `Edit|Write`, 60s timeout) runs `uv run ruff check --fix <file>` then `uv run ruff format <file>` on each edited `.py` file; its exit 2 does not block — the edit already happened — it only feeds remaining violations back as context. `stop_check.py` (Stop, 180s timeout) runs only when `git status --porcelain -uall` shows a changed `.py` file or `pyproject.toml`, then runs `ruff check .`, `ruff format --check .`, `mypy src scripts tests` in that order and blocks (exit 2) on the first failure; it checks `event.stop_hook_active` so a hook-driven continuation never loops. None of the three hooks runs pytest — that omission is intentional, not a gap to close here.

## `.claude/settings.json` and `.codex/hooks.json` carry the identical hooks block

Both files wire the same three commands (`uv run --script "$(git rev-parse --show-toplevel)/.agents/hooks/{guard,format,stop_check}.py"`) at the same matchers and timeouts (PreToolUse 30s, PostToolUse 60s, Stop 180s) — a change to a hook's matcher or timeout belongs in both files, never just one. `.claude/settings.json` additionally carries permissions with no counterpart in `.codex/hooks.json`: `allow` lists `Bash(just fmt)`, `Bash(just lint)`, `Bash(just test)`, `Bash(just check)`, `Bash(just build)`, `Bash(just smoke)`, `Bash(just install)`, `Bash(uv run pytest:*)`, `Bash(uv run ruff:*)`, `Bash(uv run mypy:*)`, `Bash(uv run pre-commit:*)`, `Bash(uv sync:*)`, `Bash(uv lock:*)`, `Bash(uv build)`; `deny` lists `Read(.env)`, `Read(.env.*)`, `Read(secrets/**)`, `Edit(secrets/**)`, `Write(secrets/**)`.

## `justfile`: `check` is the composition, not a fourth independent gate

`fmt` runs `ruff check --fix .` then `ruff format .` ("lint fixes first so the formatter has the last word"). `lint` runs `ruff check .`, `ruff format --check .`, `mypy src scripts tests`. `test` runs `pytest --cov=vidprep --cov-branch --cov-report=term-missing:skip-covered --cov-fail-under=80`. `check` is a Just dependency chain, `check: fmt lint test` — editing what "passing `just check`" means is editing one of those three recipes, not `check` itself. `build` (`uv build`) and `smoke: build` (`uv build` then `scripts/smoke_test.py`) are separate; `golden`/`golden-diff` are explicitly local-only, needing real ffmpeg, whisper.cpp, and auto-editor (`verifying-renders` owns that protocol).

## `[tool.ruff]`: every ignored rule and per-file exemption carries its own reason

`target-version = "py312"`, `line-length = 88`. The enabled rule prefixes span core (`E`, `W`, `F`, `I`), modernization (`UP`), bug/security prevention (`B`, `S`, `A`, `ISC`, `ICN`), quality (`C4`, `SIM`, `PIE`, `RET`, `ARG`, `ERA`, `PL`), performance (`PERF`, `FURB`), typing (`TC`), style (`COM`, `PTH`, `SLF`, `T20`, `LOG`), docs (`D`), tests (`PT`), naming (`N`), error handling (`TRY`, `EM`), datetime safety (`DTZ`), exception hygiene (`RSE`), and `PGH`/`RUF`. The global `ignore` list is three codes, each commented with why: `E501` ("line too long — handled by formatter"), `COM812` ("missing trailing comma — conflicts with formatter"), `ISC001` ("implicit string concatenation — conflicts with formatter") — removing one without checking it against the formatter's own behavior reintroduces exactly the conflict the comment describes. `[tool.ruff.lint.per-file-ignores]` relaxes `tests/**` (`S101` "assert is fine in tests", `ARG001`/`ARG002` fixture arguments, `PLR2004` magic numbers, `T20` print, `D1` "test names are self-documenting") and `scripts/**` (`D1` "scripts are not a public API", `T20` "print is the expected output channel for CLI scripts"). Never widen a per-file ignore without the same one-line justification the existing entries carry.

## `[tool.mypy]`: `strict = true` plus explicit extras, and two named overrides

Beyond `strict = true`: `warn_return_any`, `warn_unused_configs`, `warn_unused_ignores`, `warn_redundant_casts`, `warn_unreachable`, `show_error_codes`, and `enable_error_code = ["ignore-without-code", "redundant-cast", "truthy-bool"]`. Two `[[tool.mypy.overrides]]` blocks exist: one relaxes `disallow_untyped_defs`/`disallow_untyped_calls` for `tests.*`; the other sets `ignore_missing_imports` for `sudachipy.*`, commented "SudachiPy ships no py.typed marker and has no stubs on typeshed."

## The coverage exclusion mechanism is configured but unused, and the 80% floor lives off the table

`[tool.coverage.run]` sets `branch = true`. `[tool.coverage.report]`'s `exclude_lines` starts with `"pragma: no cover"` alongside the usual `__repr__`/`NotImplementedError`/`TYPE_CHECKING` entries — but that literal string appears zero times across `src/`, `scripts/`, and `tests/`, so the exclusion mechanism is wired and currently exercised by none of it; know this rather than "fix" it unprompted. The `80` in `--cov-fail-under=80` is not in `pyproject.toml` at all — it's duplicated on the command line in `justfile`'s `test` recipe and in `.github/workflows/ci.yml`'s `test` job, and again in prose in `CONTRIBUTING.md` ("Maintain or improve test coverage (minimum 80%)"). `.github/workflows/release.yml`'s own `test` job runs plain `uv run pytest` with no coverage flags at all — the release path never proves the floor. **BACKGROUND:** `placing-tests` owns the fuller story of which floor applies where; this skill owns editing the number and the tables it's duplicated across.

## CI is five workflow files, and some checks exist nowhere else

`ci.yml` has five jobs: `lint` (`ruff check .`, `ruff format --check .`, `mypy src scripts tests`); `test` (matrix `python-version: ["3.12", "3.13", "3.14"]`, `fail-fast: false`, pytest with coverage plus `--cov-report=xml`, Codecov upload gated to `matrix.python-version == '3.12'`); `spell-check` (the `crate-ci/typos` action against `typos.toml`); `build` (`uv build` then `scripts/smoke_test.py dist/*.whl`); `zizmor` (`uvx zizmor .github/workflows/`). `check-pr-title.yml` runs `amannn/action-semantic-pull-request` to enforce a Conventional Commits PR title, ignoring the `dependencies` label. `pr-label.yml` maps a PR title's `feat`/`fix`/`docs`/`ci` prefix to the `enhancement`/`bug`/`documentation`/`ci` label, best-effort — it deliberately doesn't fail the job when a fork PR's read-only token can't apply the label.

## `.pre-commit-config.yaml` pins its own tool versions, independently of CI

`pre-commit-hooks` `v5.0.0` (trailing-whitespace, end-of-file-fixer, check-toml, check-yaml, check-merge-conflict, check-added-large-files `--maxkb=500`), `ruff-pre-commit` `v0.15.7`, `crate-ci/typos` `v1.44.0`, `zizmor-pre-commit` `v1.26.1`, plus a local `mypy` hook (`entry: uv run mypy src scripts tests`, `language: system`, `pass_filenames: false`). This drifts from CI already: `ci.yml`'s `spell-check` job pins `crate-ci/typos` at `v1.48.0`, four minor versions ahead of the `v1.44.0` pre-commit pin — a local `pre-commit run` and CI's spell-check can disagree on which typo dictionary flags a word. Bumping one without the other is how that gap opens further, not something to silently reconcile without checking which side the change was meant for. `.github/zizmor.yml` carries two named suppressions, each commented: `artipacked` ignores `docs.yml:27:9` ("`docs.yml`'s deploy job needs persisted credentials so `mkdocs gh-deploy` can push the built site"), and `superfluous-actions` ignores `release.yml:134:15` ("`softprops/action-gh-release` is a deliberate, SHA-pinned choice ... a raw `gh release create` script step is not a meaningful security improvement here").
