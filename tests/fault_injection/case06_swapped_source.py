"""Case 6: the source file replaced with a different video after `init`.

Every timestamp in the project is measured against one recording. Swap the file
and the cuts still parse, the render still runs, and the result is nonsense — so
the manifest's sha256 is re-checked before every stage, and a mismatch has to
stop the run rather than warn about it.
"""

from __future__ import annotations

from vidprep import project as project_module
from vidprep.errors import HashMismatchError

from ._harness import build_project, refusal, workspace

REPLACEMENT = b"a completely different recording"


def main() -> int:
    """Run the injection.

    Returns:
        ``0`` when the swap was detected.

    Raises:
        AssertionError: If verification passed over replaced material.
    """
    with workspace() as root:
        loaded = build_project(root / "project")
        recorded = loaded.manifest.source.sha256
        project_module.verify_source(loaded)
        loaded.source_path.write_bytes(REPLACEMENT)
        actual = project_module.sha256_file(loaded.source_path)
        print(f"[setup] source replaced: {recorded[:12]}… → {actual[:12]}…")
        print("[run]   manifest sha256 verification")
        reported = refusal(
            lambda: project_module.verify_source(
                project_module.load_project(loaded.root)
            ),
            HashMismatchError,
            "material replaced after `init`",
        )
        assert recorded in reported, f"the recorded hash went unnamed: {reported}"
        assert actual in reported, f"the actual hash went unnamed: {reported}"
        print(f"[assert] sha256 verification: fail ✔ ({reported})")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
