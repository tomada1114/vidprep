---
name: authoring-skills
description: >
  Covers how a SKILL.md is authored inside this repository — the
  `.claude/skills/<name>/SKILL.md` layout, the `metadata.platforms:
  claude-code, codex` + `.agents/skills/` symlink mirror, the machine-enforced
  assertions in `tests/test_skills.py`, the pipeline-skill JSON contract
  (`correct-transcript`, `review-cuts`, `place-telops`), and the boundary
  between AGENTS.md and a skill. Use when adding a new skill under
  `.claude/skills/`, deciding whether it is a workflow or a knowledge skill,
  wiring its `.agents/skills/` symlink, or checking it against
  `tests/test_skills.py`.
metadata:
  platforms: claude-code, codex
---

# Authoring Skills

**Owns:** how a SKILL.md in this repository is authored — the
`.claude/skills/<name>/SKILL.md` layout, the `metadata.platforms: claude-code,
codex` plus `.agents/skills/` symlink-mirror contract, `tests/test_skills.py`'s
machine-enforced assertions, the pipeline-skill JSON contract (a skill writes
only its own artifact, verifies through the CLI, documents its rejection error
payload, and never modifies `src/vidprep/**`), the workflow-skill vs
knowledge-skill distinction, and the boundary between AGENTS.md and a skill.
**Does not own:** the general-purpose Agent Skills format mechanics and the
validator script (the user-level `creating-agent-skills` skill); the content
of any other individual skill.

## Two classes of skill live side by side, and only one needs a platform annex

`correct-transcript`, `create-pr`, `place-telops`, `review-cuts`,
`shipping-issues` and `smart-commit` are workflow skills — each drives a
procedure end-to-end and reads like a runbook with numbered "first do X, then
Y" steps. `merge-dependabot` looks like a seventh but is a leftover stub: its
directory holds only an empty `assets/`, no SKILL.md, so it is invisible to
`tests/test_skills.py` and to any host. Every knowledge skill (`writing-python`,
`placing-tests`, `designing-errors`, `authoring-skills` itself, and the rest of
this batch) owns a body of convention that is pulled in when a matching file or
decision is touched, not run start-to-finish — it reads as a review checklist
with reasons, not a procedure. That distinction has a real filesystem
consequence: all six workflow skills carry a `references/platform-notes.md`
annex (each opens with a `<!-- platform-annex -->` marker comment, then a
short "Tool mapping" / "Codex constraints" pair — see
`correct-transcript/references/platform-notes.md`) because a workflow skill
makes host-specific tool calls that need documenting per host. A knowledge
skill makes none — pure convention prose has nothing to annex — so it skips
`references/` entirely, per the house style this batch follows.

## Frontmatter is exactly three keys, and the dual-platform string is machine-checked

Every skill's frontmatter is `name`, `description`, and a nested
`metadata: platforms: claude-code, codex` — no `allowed-tools`, no `license`,
no other keys. `tests/test_skills.py`'s `test_all_agent_skills_declare_both_supported_hosts`
asserts the literal string `"metadata:\n  platforms: claude-code, codex"`
appears in every skill's raw text, parametrized over `ALL_SKILLS` —
`tuple(sorted(path.parent.name for path in SKILLS_DIR.glob("*/SKILL.md")))`, a
scan that auto-discovers every skill directory carrying a `SKILL.md`, so a
newly added skill is covered with no edit to the test file itself. A skill
missing that exact two-line block fails this test regardless of which class it
belongs to.

## `.claude/skills/` is the source; `.agents/skills/` is a generated mirror

`.claude/skills/<name>/SKILL.md` is the only real file. `.agents/skills/`
holds a symlink per skill so Codex reads the same content — e.g.
`.agents/skills/correct-transcript -> ../../.claude/skills/correct-transcript`
— and its `.gitignore` names every linked skill under the comment "Generated
Codex bridge symlinks (managed by bridge_symlink.sh). Real Codex-only skills
are NOT listed here and remain committable." No `bridge_symlink.sh` exists
anywhere in this repository today, so a new skill's symlink is created by
hand: `ln -s ../../.claude/skills/<name> .agents/skills/<name>`, then add
`<name>` to `.agents/skills/.gitignore` alongside the others. Skipping the
symlink leaves the skill invisible on the Codex side even though its
SKILL.md is otherwise correct.

## `tests/test_skills.py`'s pipeline contract is stricter than the dual-platform check

`PIPELINE_SKILLS = ("correct-transcript", "review-cuts", "place-telops")` gets
its own `TestSkillContract` class, asserting: the file exists; frontmatter
`name` equals the directory name; `description` contains the literal
`"Use PROACTIVELY when:"`; the body contains `VERIFICATION_COMMAND[name]`
(`vidprep correct --apply-patch`, `vidprep report --json`,
`vidprep render --preview`); the body contains `REJECTION_PAYLOAD[name]`
(`patch_invalid`, `schema_invalid`, `telop_invalid`); the body contains the
literal guard `"Never modify \`src/vidprep/**\`"`; and body-specific
`REQUIRED_PHRASES`, e.g. correct-transcript must contain
`'{"edits": [{"id": "s0001"'` and `"Never edit it directly"`. This is the
mechanical bar a fourth pipeline skill would have to clear — none of the
knowledge skills in this batch are in `PIPELINE_SKILLS`, so they are exempt
from all of it, but every skill, pipeline or not, still owes the dual-platform
string above.

## design.md §7 defines the pipeline contract only — knowledge skills get no row there

`docs/design.md` §7 states the CLI stays AI-free and skills only read/write
intermediate JSON plus call the CLI, then gives one read/write/contract row
per pipeline skill: `correct-transcript` reads `transcript.json` and
`dictionaries/`, writes `patch.json`, and the contract is the patch format
applied only through `vidprep correct --apply-patch`; `review-cuts` reads
`cuts.json`, `report --cuts` output and `transcript.json`, writes `cuts.json`
status only, and may never change an interval or id; `place-telops` reads
`transcript.json` and `styles.json`, writes `telops.json`, and defers
validation to `render --preview`. A knowledge skill has no JSON artifact and
no CLI verification step, so it earns no row in that table — it is a
different kind of thing, convention rather than a pipeline contract.

## AGENTS.md keeps the one-line rule; a skill keeps the reasoning

The rule this whole knowledge layer follows: AGENTS.md holds every
enforceable, always-loaded invariant, because a skill only fires
conditionally — a rule moved entirely into a skill can be silently violated
in a session where that skill never loads. A skill carries the deep version —
the reasoning, real examples, thresholds and anti-patterns that would bloat
AGENTS.md if inlined everywhere it applies. Never restate a rule's full text
in both places: AGENTS.md gets the one-liner, the skill gets the "why."

## Validate a new skill before shipping it

The user-level `creating-agent-skills` skill ships a `validate_skill.py`
script that mechanically checks frontmatter shape, length and structure; run
it with the target skill's path, `.claude/skills/<name>`, as its one
argument. That script belongs to `creating-agent-skills`, not to this skill,
so this skill only points at it rather than re-implementing the check.
