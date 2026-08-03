"""Triage open GitHub issues for the shipping-issues skill.

Parses each open issue's Dependencies sections (Depends On / Blocks /
Can Parallel With), resolves readiness against closed issues, and emits
a JSON report the orchestrating agent uses to pick the next batch.

Requires the ``gh`` CLI to be authenticated. Run from anywhere inside
the repository::

    python3 .claude/skills/shipping-issues/scripts/triage.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from typing import Any

HUMAN_KEYWORDS = ("人手", "手作業", "実機", "試聴", "目視", "リファレンス作成")
ISSUE_REF = re.compile(r"#(\d+)")
FOUNDATION_LABEL = "foundation"


def gh_json(args: list[str]) -> Any:
    """Run a ``gh`` command with fixed arguments and parse its JSON output."""
    gh = shutil.which("gh")
    if gh is None:
        msg = "gh CLI not found on PATH"
        raise RuntimeError(msg)
    out = subprocess.run(  # noqa: S603 — fixed argv built from literals below
        [gh, *args], check=True, capture_output=True, text=True
    ).stdout
    return json.loads(out)


def section_refs(body: str, heading: str) -> list[int]:
    """Collect #N references inside one ``### <heading>`` section."""
    pattern = re.compile(
        rf"^###\s+{re.escape(heading)}\b.*?$(.*?)(?=^#{{2,3}}\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(body)
    if not match:
        return []
    return sorted({int(n) for n in ISSUE_REF.findall(match.group(1))})


def main() -> None:
    """Build the triage report and write it to stdout as JSON."""
    open_issues = gh_json(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,title,labels,body",
        ]
    )
    closed = {
        i["number"]
        for i in gh_json(
            [
                "issue",
                "list",
                "--state",
                "closed",
                "--limit",
                "500",
                "--json",
                "number",
            ]
        )
    }
    open_prs = gh_json(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,title,headRefName,isDraft",
        ]
    )

    report: list[dict[str, Any]] = []
    for issue in open_issues:
        body: str = issue["body"] or ""
        deps = section_refs(body, "Depends On")
        deps_open = [d for d in deps if d not in closed]
        report.append(
            {
                "number": issue["number"],
                "title": issue["title"],
                "labels": [label["name"] for label in issue["labels"]],
                "depends_on": deps,
                "deps_open": deps_open,
                "ready": not deps_open,
                "blocks": section_refs(body, "Blocks"),
                "can_parallel_with": section_refs(body, "Can Parallel With"),
                "human_keywords": {
                    kw: count for kw in HUMAN_KEYWORDS if (count := body.count(kw)) > 0
                },
            }
        )

    report.sort(
        key=lambda i: (
            not i["ready"],
            FOUNDATION_LABEL not in i["labels"],
            -len(i["blocks"]),
            i["number"],
        )
    )
    sys.stdout.write(
        json.dumps(
            {"issues": report, "open_prs": open_prs}, ensure_ascii=False, indent=2
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
