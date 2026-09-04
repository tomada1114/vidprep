---
name: managing-dependencies
description: >
  Covers `[project] dependencies` and `[dependency-groups]` (dev, docs, asr) in
  pyproject.toml, SemVer ranges vs exact pins, `uv.lock` regeneration, the
  `[tool.uv] exclude-newer` supply-chain cooldown and its bump procedure, and
  `.github/workflows/pip-audit.yml`. Use when adding or bumping a dependency,
  editing `exclude-newer`, running `uv lock` or `uv sync --all-groups`, or
  investigating a `pip-audit` failure.
metadata:
  platforms: claude-code, codex
---

# Managing Dependencies

**Owns:** `[project] dependencies` and `[dependency-groups] dev`/`docs`/`asr` in
pyproject.toml, the SemVer-range-vs-exact-pin convention, `uv.lock`
regeneration, the `[tool.uv] exclude-newer` supply-chain cooldown and its bump
procedure, and `.github/workflows/pip-audit.yml`. **Does not own:** which
dependency groups CI installs (`changing-gates`); the semver consequence a
bump has for this package's own consumers (`release-impact`).

## Three groups, and one of them never reaches CI at all

`dev` carries the tooling `just check` runs — jiwer, mypy, pre-commit, pytest,
pytest-cov, ruff, sudachidict-core. The sudachidict-core entry has a comment
worth reading literally: "SudachiPy ships no dictionary; `doctor` reports
which one is installed, so the flavour stays a deliberate choice rather than a
runtime pin." `docs` is mkdocs-material plus mkdocstrings[python]. A third
group, `asr`, holds `mlx-whisper>=0.4; sys_platform == 'darwin' and
platform_machine == 'arm64'` — its comment states why it exists at all:
"Benchmark-only ASR backend (verification-plan.md §12.2). Apple Silicon only,
so it never resolves into the dev or docs environments CI installs." The
marker isn't decoration — on any non-arm64-darwin resolver this group
resolves to nothing, so `uv sync --all-groups` on Linux CI silently skips it.

## Ranges everywhere, no pin earns an exception

AGENTS.md's rule is "Use version ranges (`>=X.Y`) for runtime dependencies --
never pin exact versions in a library." Checked against the actual six
entries — `budoux>=0.7`, `click>=8.1`, `pydantic>=2.11`, `pysubs2>=1.8`,
`sudachipy>=0.6.10`, `typer>=0.15` — none uses `==`, and neither does any
`dev`/`docs`/`asr` entry. AGENTS.md's rule is stated for runtime dependencies
only, and dev-group tools that don't bind library consumers could reasonably
use tighter constraints, but nothing in this repo currently exercises that
allowance — every dependency in every group is an open lower bound. Reject in
review: a
new entry pinned with `==` anywhere in `[project] dependencies` — that is
exactly the constraint AGENTS.md forbids for a library.

## `exclude-newer` is presently the only supply-chain cooldown that runs

`[tool.uv] exclude-newer = "2026-07-20T00:00:00Z"` makes `uv lock`/`uv sync`
ignore any package version published after that timestamp — a freshly
published (or freshly compromised) release can't enter `uv.lock` until it has
aged past the cutoff. There is no `.github/dependabot.yml` and no Renovate
config anywhere in this repo (verified: `.github/` holds `release.yml`,
`zizmor.yml`, `ISSUE_TEMPLATE/`, `PULL_REQUEST_TEMPLATE.md`, and `workflows/`,
none of them a bot config, and `find . -iname "dependabot*" -o -iname "renovate*"` matches nothing
outside `.venv`'s bundled Material-for-MkDocs icon assets). So this isn't one
layer of a two-layer scheme — it's the only automated defense currently
sitting between a compromised release and this lockfile; nothing else files a
bump PR or nudges a stale range forward. That makes the cutoff's own
freshness load-bearing: dependencies here are updated by hand, so a stale
`exclude-newer` is a stale defense, not just a missed convenience.

The stated cadence (AGENTS.md) is to move the cutoff to roughly "today minus
14 days" whenever dependencies are updated, and at least monthly regardless.
Measured against the working date, `2026-07-20` is 46 days behind — past both
the monthly floor and well short of where a routine "today minus 14 days"
bump would put it (`2026-08-21`).

## The bump touches two files in one commit, never `uv.lock` alone

Moving the cutoff forward means editing `exclude-newer` in pyproject.toml and
then running `uv lock` to regenerate `uv.lock` against the new date — the two
files land in the same commit, because a `pyproject.toml` change with a stale
`uv.lock` is a lockfile that doesn't reflect its own cooldown. The same
applies to any ordinary dependency bump, not just the cadence-driven one:
edit the range in `pyproject.toml`, regenerate, commit together.

## `uv.lock` is tracked — nothing hand-edits it

`.gitignore` carries the comment "uv.lock is tracked — do not ignore it" —
this repo commits the lockfile rather than treating it as derived-and-ignored.
**BACKGROUND:** `changing-gates` for the `.agents/hooks/guard.py` check that
blocks direct writes to `uv.lock` outside a tool invocation. The only
sanctioned way to change it is `uv lock`, `uv add`, or `uv sync` — never an
editor.

## `pip-audit.yml` and `exclude-newer` catch different failure modes

`.github/workflows/pip-audit.yml` runs on `cron: "0 6 * * 1"` (Mondays,
06:00 UTC) plus `workflow_dispatch`, exports the resolved lockfile with
`uv export --no-emit-project --format requirements-txt`, then runs `uvx
pip-audit --disable-pip -r requirements.txt` against it. This catches known
CVEs disclosed *after* a package version was already locked — `exclude-newer`
can't catch that, since the vulnerable version may have aged well past any
cutoff before the CVE was published. `exclude-newer` catches the opposite
case: a version too newly published to have accumulated any track record yet,
CVE or not. Neither one substitutes for the other.

## Before a new dependency enters the repo at all

AGENTS.md's gate for adding one: verify active maintenance, a compatible
license (MIT/BSD/Apache), and minimal transitive dependencies. This is the
actual judgment call this skill exists for — a package failing any of the
three doesn't get added even if it solves the immediate problem, because the
cost lands on every future `uv lock` and every future license audit, not just
this PR.

## After any dependency change

Run `uv sync --all-groups` so the local environment matches the new lock, and
commit `uv.lock` alongside the `pyproject.toml` change in the same commit —
AGENTS.md states both as MUSTs, not suggestions, because a dependency change
without its regenerated lockfile is a change CI and every other clone will
resolve differently than the one that was tested locally.
