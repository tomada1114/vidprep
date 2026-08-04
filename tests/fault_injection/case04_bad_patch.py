"""Case 4: an LLM patch naming a segment that does not exist, twice.

A correction patch is written by a language model, so §6 checks it against the
transcript before a single segment is touched: an unknown identifier and a
repeated one are both refusals, and every complaint is reported at once. The
patch here carries both faults, and `correct` must apply nothing.
"""

from __future__ import annotations

import json

from vidprep import correct
from vidprep.errors import PatchInvalidError

from ._harness import SEGMENTS, build_project, refusal, workspace

UNKNOWN_ID = "s9999"
DUPLICATED_ID = SEGMENTS[0][0]


def main() -> int:
    """Run the injection.

    Returns:
        ``0`` when the patch was refused for both faults.

    Raises:
        AssertionError: If the patch was accepted, or refused for only one of
            the two faults it carries.
    """
    with workspace() as root:
        loaded = build_project(root / "project")
        patch = root / "patch.json"
        patch.write_text(
            json.dumps(
                {
                    "edits": [
                        {"id": UNKNOWN_ID, "text": "存在しないセグメント"},
                        {"id": DUPLICATED_ID, "text": "一度目"},
                        {"id": DUPLICATED_ID, "text": "二度目"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(
            f"[setup] patch.json names {UNKNOWN_ID} (absent) and {DUPLICATED_ID} twice"
        )
        print("[run]   vidprep correct --apply-patch patch.json")
        transcript = loaded.root / "transcript.json"
        before = transcript.read_bytes()
        details = refusal(
            lambda: correct.plan_patch(loaded, patch),
            PatchInvalidError,
            "a patch naming a segment that does not exist",
        )
        assert UNKNOWN_ID in details, f"the unknown id went unreported: {details}"
        assert DUPLICATED_ID in details, f"the duplicate went unreported: {details}"
        assert transcript.read_bytes() == before, (
            "a refused patch must leave the transcript exactly as it was"
        )
        print(f"[assert] patch validation: fail ✔ ({details})")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
