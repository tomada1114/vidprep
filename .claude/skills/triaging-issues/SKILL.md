---
name: triaging-issues
description: >
  Covers this repository's GitHub issue and label vocabulary (`foundation`,
  `parallel`, `has-dependency`, `integration`, plus the template-applied `bug`
  and `enhancement`), the `### Depends On` / `### Blocks` / `### Can Parallel
  With` sections `.claude/skills/shipping-issues/scripts/triage.py` parses,
  and what an issue body needs (REQ-xxx IDs, `design.md §n.n` citations,
  `close #N`) to survive being picked up in a session with no memory of the
  triage conversation. Use when filing, labeling, or re-reading a GitHub
  issue, deciding whether one issue depends on or blocks another, or writing
  the Dependencies section of an issue body.
metadata:
  platforms: claude-code, codex
---

# Triaging Issues

**Owns:** this repository's GitHub issue and label vocabulary, what an issue
body must contain to survive being picked up in a different session, and
dependency ordering between issues. **Does not own:** implementing an issue
through to a PR (`shipping-issues`); PR mechanics generally (`create-pr`);
what a resulting change breaks or which semver level it implies
(`release-impact`).

## The label vocabulary is the GitHub default set plus four triage labels — no `priority:` scheme exists

`gh label list` returns the nine labels every new GitHub repo ships with
(`bug`, `documentation`, `duplicate`, `enhancement`, `good first issue`,
`help wanted`, `invalid`, `question`, `wontfix`) plus four added for this
repo's dependency-driven triage: `foundation` ("Must complete first",
`#5319E7`), `parallel` ("Can work in parallel", `#0E8A16`), `has-dependency`
("Waiting on dependencies", `#B60205`), and `integration` ("Connects
components", `#FBCA04`). There is no `priority: P0`–`P3` label scheme, no
`blocked:` prefix family, and nothing named `triage` — do not invent one.
The only label with machine-readable meaning is `foundation`:
`scripts/triage.py`'s `FOUNDATION_LABEL` constant sorts ready issues
foundation-first, then by `len(blocks)` descending, then by issue number, so
`foundation` is a real scheduling signal and the other three are
human-readable classification only.

## Issue templates fix the required fields and the auto-applied label

`.github/ISSUE_TEMPLATE/bug_report.yml` requires five fields — Description,
Steps to Reproduce, Expected Behavior, Actual Behavior, Version — plus
Python Version, and auto-applies `labels: ["bug"]`; Operating System is the
one optional field. `.github/ISSUE_TEMPLATE/feature_request.yml` requires
Problem and Proposed Solution, leaves Alternatives Considered optional, and
auto-applies `labels: ["enhancement"]`. `.github/ISSUE_TEMPLATE/config.yml`
sets `blank_issues_enabled: false` and points security reports at
`https://github.com/tomada1114/vidprep/security/advisories/new` instead of a
public issue — there is no third template to reach for.

## `pr-label.yml` maps a merged PR's Conventional Commits type onto the same labels an issue can carry

`.github/workflows/pr-label.yml` reads the PR title's `type` prefix and maps
`feat`→`enhancement`, `fix`→`bug`, `docs`→`documentation`, `ci`→`ci` — the
same `bug`/`enhancement`/`documentation` vocabulary the issue templates
apply, so an issue and the PR that closes it usually end up carrying
matching labels. `ci` does not appear in `gh label list`'s current output
because no `ci:` PR has merged yet; the workflow's `gh label create "$label"
>/dev/null 2>&1 || true` step creates it lazily on first use rather than it
being pre-provisioned. `.github/workflows/check-pr-title.yml` sets
`ignoreLabels: dependencies` for `amannn/action-semantic-pull-request`, but
`dependencies` is likewise absent from `gh label list` and `.github/` has no
`dependabot.yml` — the exemption exists in config for a Dependabot-style
producer that has no automated source right now.

## `### Depends On` / `### Blocks` / `### Can Parallel With` sections are parsed by regex, not read by a human first

`shipping-issues`' `scripts/triage.py` extracts dependency edges straight
from the issue body: `section_refs()` matches `^###\s+{heading}\b.*?$(.*?)
(?=^#{2,3}\s|\Z)` per `### Depends On`, `### Blocks`, and `### Can Parallel
With` heading, then collects every `#N` inside that section with `ISSUE_REF
= re.compile(r"#(\d+)")`. An issue is `ready` when every number under its
`### Depends On` heading is a closed issue. Closed issue #9 (`render — カット
適用 + SRT 出力`) shows the real shape:

```markdown
## Dependencies

### Depends On
- #2 — スキーマ、`project.py`、`_ffmpeg.py`、CLI 骨格
- #4 — `timeline.py` の写像関数と字幕写像規則
- #5 — `audio/processed.wav`
- #8 — `cuts.json`（approved 判定済み）

### Blocks
- #11 — `--verify-asr` / ゴールデンランが `out/output.mp4` を前提にする
- #12 — `--preview` が render の基盤に載る

### Can Parallel With
- #10 — report は `report/` 配下のみを触る（ただし目視 AC は #10 のマージ後に実施）
```

A dependency mentioned only in prose ("this builds on #4's mapping") outside
a `### Depends On` heading is invisible to the regex — `deps_open` will miss
it, and `shipping-issues`' Phase 0 falls back to a human `gh issue view <N>`
read precisely because this parser is conservative, not exhaustive. Writing
or editing an issue's Dependencies section means using these three exact
`###` headings with `#N` references inside them, or the automation silently
treats the issue as more ready than it is.

## An issue body must let a fresh session act without re-deriving the triage conversation

Every closed issue in this repo (see #9, #10, #11, #12, #13) follows the
same shape: a User Story, a Source Requirements table with columns 節 /
参照 / 仕様 whose 参照 column cites `design.md §n.n` and
`verification-plan.md §n` for every line, a Functional Requirements table of
EARS-format `REQ-NNN` rows, a Not In Scope list naming the issues that pick
up the deferred work, a Technical Notes → Suggested Files list naming the
concrete module(s) to touch (`src/vidprep/render.py`, `tests/test_render.py`
for #9), and a `## PR Instructions` line spelling out `close #N` verbatim.
That `REQ-xxx` / `design.md §n.n` pairing is not specific to issue-writing —
it is the same citation style used throughout the source and test suite
(`tests/test_models.py`'s `test_...` docstring `"REQ-001: every model
accepts the sample JSON from design.md §3."`, `src/vidprep/timeline.py`'s
module docstring citing `design.md §4`), so an issue that cites a §-number
is citing something a later session can independently re-open and check. An
issue with no §-citation and no named file forces whoever picks it up to
reconstruct the design decision from scratch — reject that at triage time
by adding the citation and the file list before filing, not after.

## Human-blocker keywords and title tags flag what triage.py can't infer from labels alone

`scripts/triage.py`'s `HUMAN_KEYWORDS` tuple — `人手`, `手作業`, `実機`,
`試聴`, `目視`, `リファレンス作成` — is counted per issue body and surfaced
to `shipping-issues` as a signal to read the issue by hand rather than
auto-batch it; it is not a label, so it only works if the issue body
actually uses one of these words for a step that needs a person (Filmora
hardware ingest checks, a listen-through). Closed issues also carry a
bracketed title tag ahead of the label — `【基盤】` for `foundation`,
`【並列可/worktree:<slug>】` for `parallel` (the slug becomes the worktree
branch suffix `shipping-issues` uses), `【依存あり】` for `has-dependency` —
which is a convention visible in issue titles, not something any workflow
enforces, so a mislabeled issue and an untagged title can drift apart.

## Issue bodies are written in Japanese — AGENTS.md's English rule doesn't reach them

AGENTS.md requires English for "code, published docs, commits, and PRs" and
carves out only `docs/design-input.md`, `docs/design.md`,
`docs/verification-plan.md`, `docs/research/`, and `README.ja.md` for
Japanese — it does not mention issues either way. In practice every issue
body observed in this repo (bug/feature templates aside) is written in
Japanese, matching the design docs it cites rather than the English code and
PRs it produces; triaging a new issue in Japanese is consistent with
existing practice, not a rule violation to flag.
