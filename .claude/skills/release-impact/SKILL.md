---
name: release-impact
description: >
  Covers deciding whether a change is breaking and at which semver level,
  how that lands in the PR title and `CHANGELOG.md`, and
  `.github/workflows/release.yml`'s tag-to-version gate. Use when a PR
  touches `__all__`, a CLI flag or subcommand, an artifact JSON schema
  (`vidprep.json`, `transcript.json`, `cuts.json`, `telops.json`,
  `styles.json`, `profile.json`), an exit code, or a `--json` payload key,
  or when choosing a Conventional Commits PR title, writing a `CHANGELOG.md`
  entry, or tagging a release (`v<version>`).
metadata:
  platforms: claude-code, codex
---

# Release Impact

**Owns:** deciding whether a change is breaking and at which semver level,
how that decision lands in the PR title and `CHANGELOG.md`, and
`.github/workflows/release.yml`'s tag/version gate — plus the fact that
vidprep's "public surface" is wider than the Python API: it includes CLI
flags, artifact JSON schemas, exit codes, and `--json` payload keys. **Does
not own:** what may be exported at the Python level in the first place
(`public-api-contract`); which doc surface a change lands on
(`updating-docs`).

## The tag *is* the release trigger, and it must equal the version

`release.yml` fires on `push: tags: ["v*"]` — nothing else starts a release.
Its `build` job's first step, "Validate tag matches project version", reads
`pyproject.toml`'s `project.version` with `tomllib` and compares it against
`GITHUB_REF_NAME`: if the pushed tag isn't exactly `f"v{version}"` it raises
`SystemExit(f"Tag {tag!r} does not match project version {version!r}. "
f"Expected {expected!r}.")` before `uv build` ever runs. This is the
mechanical enforcement of "bump the version first" — there is no separate
release-please or version-bump bot in this repo. Tagging `v0.2.0` while
`pyproject.toml` still says `version = "0.1.0"` fails the build job outright,
and no PyPI publish or GitHub Release follows. As of this writing
`pyproject.toml` is still at `version = "0.1.0"` and no `v*` tag has ever
been pushed (`git tag -l` is empty) — the semver level chosen for the first
tagged release is the first real exercise of this gate.

## Job chain after the gate: test, build, publish, release

`test` (plain `uv run pytest`, no coverage flags) gates `build` (`uv build`,
then `uv run python scripts/smoke_test.py dist/*.whl`, then
`actions/attest-build-provenance` over `dist/*`), which gates `publish`
(`uv publish --trusted-publishing always`), which gates `release`
(`softprops/action-gh-release` with `generate_release_notes: true`). Compare
this `test` job to `ci.yml`'s, which runs
`uv run pytest --cov=vidprep --cov-branch --cov-report=term-missing:skip-covered --cov-fail-under=80 --cov-report=xml`
— the release pipeline's own test run carries none of that, so a coverage
regression that would fail `ci.yml` does not by itself block a tagged
release from publishing to PyPI. `changing-gates` is where the coverage-gate
mechanics themselves live; the fact worth carrying here is only the
consequence for this path.

## PR title, label, and release-notes category are one chain

`check-pr-title.yml` enforces Conventional Commits titles via
`amannn/action-semantic-pull-request` (ignoring PRs labeled `dependencies`).
`pr-label.yml` reads the same title and maps its type to a label:
`feat`→`enhancement`, `fix`→`bug`, `docs`→`documentation`, `ci`→`ci` (any
other type is left unlabeled with a notice, and the label write is
best-effort since fork PRs carry a read-only token). `.github/release.yml`
then maps those same labels to `generate_release_notes: true`'s section
titles: `enhancement`→Features, `bug`→Bug Fixes, `documentation`→
Documentation, `ci`→CI. Getting the PR title's Conventional Commits type
right is therefore not just style — it decides both the applied label and
which auto-generated release-notes heading the PR's entry lands under.

## `CHANGELOG.md` is the human-curated record; release notes are supplementary

`CONTRIBUTING.md`'s Changelog Policy section is explicit about the split:
`CHANGELOG.md` (Keep a Changelog format, semver) is what users should read
to understand a release, and GitHub's auto-generated notes from
`.github/release.yml` are "supplementary — useful for a quick PR-by-PR
diff." Every user-facing change adds an entry under `## [Unreleased]` in the
same PR that makes the change — there are no per-version headers in the file
yet (only `## [Unreleased]`, consistent with no tag having shipped), broken
into `### Added` / `### Changed` / `### Fixed` subsections. Entries run
several sentences of prose per change, naming the exact flag, field, or
behavior and the reasoning behind it (e.g. the `--verify-asr` entry explains
*why* an interjection-only miss is a false positive, not just that one was
added) — match that granularity and tone, not a terse one-liner.

## Deciding the semver level: ground it in this repo's actual surface

**MAJOR/breaking:** removing or renaming an `__all__` export, or the
`vidprep` console script itself — `public-api-contract` owns what belongs in
`__all__`, this skill just notes that *changing* the exported set is what
matters for semver; renaming or removing a CLI flag or subcommand; changing
what a `--json` payload's top-level keys mean; changing what exit code a
given failure now produces (`designing-errors` owns the exit-code
vocabulary); changing an existing field's meaning or removing a field from
any artifact JSON — `vidprep.json` (`MANIFEST_NAME`), `profile.json`
(`PROFILE_NAME`), or any of `project.py`'s `ARTIFACT_MODELS` table entries
(`transcript.json`, `report/noise_floor.json`, `report/vad.json`,
`cuts.json`, `telops.json`, `styles.json`).

**MINOR:** adding a new CLI flag, a new optional field to an artifact schema
that existing consumers can ignore, a new `__all__` export.

**PATCH:** bug fixes that change no documented contract, internal refactors,
dependency bumps with no behavior change.

**A schema change that looks purely internal can still break a skill's
contract.** vidprep's artifacts are consumed by two audiences: the CLI's own
stages, and the three pipeline agent skills that read and write them
directly per their documented contracts — `correct-transcript` writes
`patch.json` and reads `transcript.json`, `review-cuts` reads `cuts.json`
and `report --cuts --json`, `place-telops` writes `telops.json`. A field
rename in `cuts.json` that no CLI stage cares about because it always reads
the whole object can still silently break `review-cuts`'s field-level
expectations. Treat these three skills as first-class consumers when judging
whether an artifact schema change is breaking, not just `project.py`'s own
readers.

## What doesn't need a release, or even a `CHANGELOG.md` entry

Pure test additions, internal refactors with identical observable behavior,
doc-only changes, and CI/gate-only changes don't move the semver needle.
`CHANGELOG.md` has no "Internal" or "Chore" category in this file's actual
structure (only `Added`/`Changed`/`Fixed` appear) — a pure gate or CI change
typically gets no `CHANGELOG.md` line at all rather than being shoehorned
into one of those three headings.

## Branch-and-PR is the stated flow; direct-to-main commits do happen

`CONTRIBUTING.md`'s Pull Request Process says fork, branch from `main`, open
a PR, and the last several merged changes (`#39` `2b14e1b` down through
`#28` `dad6567`) came in through that flow. But the four commits currently
at the tip of `main` — `a5e0aa3`, `7f85fff`, `85a5df5`, `d1508b0` — sit ahead
of `#39`, the most recent merged PR, with no corresponding pull request
(`gh pr list --state merged` has no entry for them): they landed straight on
`main`. Don't assume a PR is mandatory for every change in this repo — check
`gh pr list --state merged` against `git log` when it matters whether a
specific change went through review, and treat the branch-and-PR path in
`CONTRIBUTING.md` as the default for external and most internal
contributions rather than an absolute rule this repository enforces
mechanically.
