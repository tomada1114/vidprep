---
name: updating-docs
description: >
  Covers which documentation surface a change lands on in vidprep — README.md,
  README.ja.md, docs/getting-started.md, docs/reference.md, docs/design.md and
  the other Japanese design docs, or just a docstring — the README.md /
  README.ja.md same-commit parity rule, the English/Japanese split across
  docs/, the `mkdocs.yml` nav vs. `mkdocs build --strict`, and docs/design.md
  as the canonical §-numbered source for design questions. Use when editing
  README.md, README.ja.md, any file under docs/, or `mkdocs.yml`, or when
  deciding whether a code change owes a documentation update at all.
metadata:
  platforms: claude-code, codex
---

# Updating Docs

**Owns:** which documentation surface a change lands on — README.md,
README.ja.md, docs/getting-started.md, docs/reference.md, docs/design.md and
the other Japanese design docs, or just a docstring — the README.md ↔
README.ja.md same-commit parity rule, the English/Japanese language split
across the repository's docs, the mkdocs nav and `mkdocs build --strict`, and
docs/design.md as the canonical §-numbered source for design questions.
**Does not own:** the semver level and CHANGELOG wording for a change
(`release-impact`); what may be exported into the public API
(`public-api-contract`); how a skill's own SKILL.md is written
(`authoring-skills`).

## The language split is per-file, not per-directory

AGENTS.md's "Important Reminders" states it plainly: "All code, published
docs, commits, and PRs must be written in English. Design notes
(`docs/design-input.md`, `docs/design.md`, `docs/verification-plan.md`,
`docs/research/`) stay in Japanese, and so does `README.ja.md`." `docs/`
actually holds both languages side by side: `docs/index.md`,
`docs/getting-started.md`, `docs/reference.md` and `docs/contributing.md` are
English; `docs/design-input.md`, `docs/design.md`, `docs/verification-plan.md`
and `docs/research/feasibility-report.md` are Japanese. There is no
`docs/en/` or `docs/ja/` split to lean on — the language of a doc change is
decided file by file, and a new file under `docs/` needs an explicit call on
which side of that list it joins.

## README.md and README.ja.md change together, in the same commit

AGENTS.md's rule — "Keep the two READMEs in step: a change to one belongs in
the other in the same commit" — is enforced only by discipline, not tooling.
The two files mirror each other section for section (`## The pipeline` /
`## パイプライン`, `## Quickstart` / `## クイックスタート`, `## The project directory` /
`## プロジェクトディレクトリ`, and so on down to `## License` / `## ライセンス`), including
their `## Documentation` / `## ドキュメント` sections, which both list the same five
docs in the same order — README.md marking `docs/design.md`,
`docs/verification-plan.md` and `docs/research/feasibility-report.md` as
"(Japanese)", README.ja.md marking `docs/getting-started.md`,
`docs/reference.md` and `CONTRIBUTING.md` as "（英語）". Editing one README's
project-tree diagram, quickstart transcript, or option list without the other
is the exact drift this rule exists to prevent — check the mirrored section
exists and matches before considering the change done.

## `mkdocs.yml`'s nav lists four pages; the site builds and deploys all eight

`mkdocs.yml`'s `nav:` has exactly four entries — Home (`index.md`), Getting
Started (`getting-started.md`), API Reference (`reference.md`), Contributing
(`contributing.md`) — all English. The four Japanese docs
(`design-input.md`, `design.md`, `verification-plan.md`,
`research/feasibility-report.md`) are absent from `nav:`, but `mkdocs.yml` has
no `exclude_docs` or per-page `draft` setting, so mkdocs still builds every
`.md` file under `docs/` and gives it a URL — they're just unreachable by
clicking through the site's navigation. `.github/workflows/docs.yml` then runs
`uv run mkdocs build --strict` followed by `uv run mkdocs gh-deploy --force`
on every push to `main` that touches `docs/**`, `mkdocs.yml`, `src/**` or a
few other paths — so those Japanese pages genuinely ship to the public site at
`https://tomada1114.github.io/vidprep`, reachable by direct URL, even though
AGENTS.md's rule reads as "published docs must be English." This is a real
tension between the nav and the deploy step worth knowing about, not something
this skill resolves by itself — flag it rather than silently adding the
Japanese docs to `nav:` or silently leaving them out of a build-affecting
change.

## `docs/reference.md` needs no manual editing — it renders `__all__`

The whole file is a heading and one `mkdocstrings` directive: `# API
Reference` followed by `::: vidprep`. `mkdocs.yml`'s `mkdocstrings` handler is
configured with `docstring_style: google` and `show_source: true`, so the page
is generated from whatever `src/vidprep/__init__.py.__all__` exports and each
export's Google-style docstring — changing what a function does or exports is
a `public-api-contract` and docstring-writing concern, not a reason to touch
this file. Adding or removing something from `__all__` doesn't need a
`docs/reference.md` edit; it changes what the next `mkdocs build` renders
automatically.

## `docs/contributing.md` includes the root `CONTRIBUTING.md`; a broken include fails the build

The file is one line: `--8<-- "CONTRIBUTING.md"`, `pymdownx.snippets`'
include syntax. `mkdocs.yml` configures that extension with `check_paths:
true`, so if the referenced file moves or is renamed, `mkdocs build --strict`
fails rather than silently rendering an empty page — the contributor-facing
content lives in the root `CONTRIBUTING.md` alone, never duplicated into
`docs/`.

## `docs/design.md` is the canonical, §-numbered source — dozens of docstrings cite it by section

`docs/design.md`'s headings are numbered `## 1.` through `## 8.` with `###`
subsections (`## 3. プロジェクトとデータ設計` with `### 3.5 telops.json /
styles.json`, `## 4. タイムライン写像仕様（timeline.py）`, `## 7. Agent連携（Claude Code /
Codex）`, and so on). Source docstrings across `src/vidprep/` cite these
section numbers directly and rely on them staying stable —
`src/vidprep/models.py`'s module docstring points at §3, `timeline.py` at §4,
`errors.py` at §6, `prep.py`'s "AI-free" claim at §7, `_reencode.py`'s v1
re-encode decision at "§1, decision 2." Renumbering or splitting a `design.md`
section is not a docs-only edit — grep `design.md §` across `src/` before
renumbering anything, because every hit is a citation that would go stale.
When a design question needs an answer (should this be a new stage record
field, what does the manifest schema look like), `design.md` is where the
answer is recorded, in Japanese, and the English docs only ever restate a
derived summary of it.

## Routing a change to its doc surface

| Change | Where it lands |
|---|---|
| CLI flag, subcommand, or a stage's observable behavior | `docs/getting-started.md`, plus README.md and README.ja.md together |
| On-disk project layout or an intermediate JSON schema | `docs/design.md` (Japanese, canonical) first; README's `## The project directory` / `## プロジェクトディレクトリ` tree is the English derivative and must be kept in step with it |
| A single function or class's contract | Its own docstring is enough — `docs/reference.md` picks it up automatically; no separate doc file is owed |
| Anything that changes `__all__` | The docstring plus `public-api-contract`'s review, not a `docs/reference.md` edit |

## Self-check a doc change with `just docs`, then confirm with `mkdocs build --strict`

`just docs` runs `uv run mkdocs serve` for a local preview. That does not
catch what CI catches: `.github/workflows/docs.yml` runs `uv run mkdocs build
--strict`, which turns every mkdocs warning — a broken internal link, a
`pymdownx.snippets` include that can't resolve under `check_paths: true`, a
`mkdocstrings` reference to a symbol that no longer exists — into a build
failure. Run `uv run mkdocs build --strict` locally before calling a docs
change done; a green `mkdocs serve` preview is not the same guarantee.

**BACKGROUND:** `.github/PULL_REQUEST_TEMPLATE.md` carries a `- [ ] CHANGELOG
updated (if user-facing change)` checklist line alongside `- [ ] Documentation
updated (if applicable)` — `release-impact` owns what that CHANGELOG entry
should say and whether the change is user-facing at all.
