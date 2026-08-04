"""The Claude Code skills that drive the CLI (design.md §7).

The skills are prose, so what can be checked mechanically is their contract:
each one is discoverable, ends in a CLI verification command, knows the payload
the CLI rejects it with, and never tells anyone to edit the package.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parents[1] / ".claude" / "skills"

#: The skills of the v1 pipeline (design.md §7).
PIPELINE_SKILLS = ("correct-transcript", "review-cuts", "place-telops")

#: The CLI command each skill must run to have its own output verified.
VERIFICATION_COMMAND = {
    "correct-transcript": "vidprep correct --apply-patch",
    "review-cuts": "vidprep report --json",
    "place-telops": "vidprep render --preview",
}

#: The ``--json`` error payload each skill must document a recovery for.
REJECTION_PAYLOAD = {
    "correct-transcript": "patch_invalid",
    "review-cuts": "schema_invalid",
    "place-telops": "telop_invalid",
}

#: The part of the contract each skill has to state in its own words.
REQUIRED_PHRASES = {
    "correct-transcript": (
        '{"edits": [{"id": "s0001"',  # the patch schema, and nothing else
        "Never edit it directly",  # transcript.json is the CLI's to write
    ),
    "review-cuts": (
        "`status` and `note` **only**",
        "`id`, `start`, `end`, `reason`",  # the fields that stay untouched
    ),
    "place-telops": (
        "must name a preset that actually exists",
        "Prefer `segment_id`",
    ),
}

#: Every skill refuses to change the package it drives.
PACKAGE_GUARD = "Never modify `src/vidprep/**`"


def read_skill(name: str) -> str:
    """Return the text of ``.claude/skills/<name>/SKILL.md``."""
    return (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a skill into its frontmatter fields and its body.

    Folded values (``description: >`` continued on the following lines) are
    joined back into one string, which is all the fields are inspected for.
    """
    assert text.startswith("---\n"), "a skill must open with its frontmatter"
    _, block, body = text.split("---\n", 2)
    fields: dict[str, str] = {}
    key = ""
    for line in block.splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, _, value = line.partition(":")
            fields[key] = value.strip().removeprefix(">").strip()
        elif key and line.strip():
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    return fields, body


@pytest.mark.parametrize("name", PIPELINE_SKILLS)
class TestSkillContract:
    """REQ-042 and the invariants shared by the three pipeline skills."""

    def test_skill_file_exists(self, name):
        assert (SKILLS_DIR / name / "SKILL.md").is_file()

    def test_frontmatter_names_the_skill_after_its_directory(self, name):
        fields, _ = parse_frontmatter(read_skill(name))

        assert fields["name"] == name

    def test_frontmatter_describes_when_to_use_the_skill(self, name):
        fields, _ = parse_frontmatter(read_skill(name))

        assert "Use PROACTIVELY when:" in fields["description"]

    def test_body_runs_the_cli_verification(self, name):
        _, body = parse_frontmatter(read_skill(name))

        assert VERIFICATION_COMMAND[name] in body

    def test_body_documents_the_recovery_from_a_rejection(self, name):
        _, body = parse_frontmatter(read_skill(name))

        assert REJECTION_PAYLOAD[name] in body

    def test_body_forbids_editing_the_package(self, name):
        _, body = parse_frontmatter(read_skill(name))

        assert PACKAGE_GUARD in body

    def test_body_states_its_own_contract(self, name):
        _, body = parse_frontmatter(read_skill(name))

        assert [phrase for phrase in REQUIRED_PHRASES[name] if phrase not in body] == []


class TestFrontmatterParser:
    """The helper the contract tests rely on."""

    def test_folded_value_is_joined_into_one_line(self):
        fields, body = parse_frontmatter(
            "---\nname: demo\ndescription: >\n  first\n  second\n---\n\n# Body\n"
        )

        assert fields == {"name": "demo", "description": "first second"}
        assert body.strip() == "# Body"

    def test_text_without_frontmatter_is_rejected(self):
        with pytest.raises(AssertionError, match="frontmatter"):
            parse_frontmatter("# Body\n")
