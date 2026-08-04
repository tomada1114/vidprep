"""Case 5: two `approved` cuts written by hand so that they overlap.

`cuts.json` is edited by a human and by a skill, so the schema of §7 is what
stands between a slip of the hand and a render whose length nobody can predict.
Two approved cuts sharing a second must be refused when the file is read, before
any stage acts on it.
"""

from __future__ import annotations

from vidprep import project as project_module
from vidprep.errors import SchemaInvalidError
from vidprep.models import Cuts

from ._harness import DURATION, build_project, refusal, workspace, write_cuts

OVERLAPPING = (
    ("c0001", 4.5, 9.5, "silence", "approved"),
    ("c0002", 8.0, 12.0, "silence", "approved"),
)


def main() -> int:
    """Run the injection.

    Returns:
        ``0`` when the overlap was refused.

    Raises:
        AssertionError: If the file was accepted, which would let two cuts
            remove the same second twice.
    """
    with workspace() as root:
        loaded = build_project(root / "project")
        write_cuts(loaded.root, OVERLAPPING)
        print(
            f"[setup] cuts.json holds {OVERLAPPING[0][0]}"
            f"({OVERLAPPING[0][1]}-{OVERLAPPING[0][2]}) and "
            f"{OVERLAPPING[1][0]}({OVERLAPPING[1][1]}-{OVERLAPPING[1][2]}), "
            "both approved"
        )
        print("[run]   cuts.json schema validation")
        reported = refusal(
            lambda: project_module.load_artifact(
                loaded.root / "cuts.json", Cuts, DURATION
            ),
            SchemaInvalidError,
            "two overlapping approved cuts",
        )
        assert "approved cuts overlap" in reported, (
            f"the refusal was not about the overlap: {reported}"
        )
        for identifier, _, _, _, _ in OVERLAPPING:
            assert identifier in reported, f"{identifier} went unnamed: {reported}"
        print(f"[assert] schema validation: fail ✔ ({reported})")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
