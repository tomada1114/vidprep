"""Case 3: one entry deleted from the SRT (verification-plan.md §10).

The "no missing entries" condition of §9 says every kept segment reaches
``out/subtitles.srt``; the mapping's own warning list accounts for the ones that
do not. Deleting an entry by hand leaves a file the mapping cannot explain, and
:func:`vidprep.verify.missing_subtitle_entries` must name the segment that went
missing.
"""

from __future__ import annotations

import pysubs2

from vidprep import render, verify
from vidprep.timeline import Timeline

from ._harness import CUTS, DURATION, build_project, workspace

DROPPED_INDEX = 1


def main() -> int:
    """Run the injection.

    Returns:
        ``0`` when the deleted entry was named.

    Raises:
        AssertionError: If the check passed over a file with an entry missing.
    """
    with workspace() as root:
        loaded = build_project(root / "project")
        timeline = Timeline(
            [(start, end) for _, start, end, _, status in CUTS if status == "approved"],
            DURATION,
        )
        subtitles = render.build_subtitles(loaded, timeline)
        path = loaded.root / render.SUBTITLES_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(subtitles.to_srt(), encoding="utf-8")
        assert not verify.missing_subtitle_entries(path, subtitles.entries), (
            "the freshly written SRT was already reported as incomplete"
        )
        print(f"[setup] out/subtitles.srt written ({len(subtitles.entries)} entries)")

        events = pysubs2.SSAFile.from_string(
            path.read_text(encoding="utf-8"), format_="srt"
        )
        removed = events[DROPPED_INDEX]
        del events[DROPPED_INDEX]
        path.write_text(events.to_string("srt"), encoding="utf-8")
        print(
            f"[setup] entry {DROPPED_INDEX + 1} deleted by hand "
            f"({len(subtitles.entries)} → {len(events)})"
        )

        print("[run]   missing-entry check (verification-plan.md §9)")
        missing = verify.missing_subtitle_entries(path, subtitles.entries)
        assert missing, (
            "the missing-entry check accepted an SRT with an entry deleted; "
            "verification-plan.md §9 is not being enforced"
        )
        expected = subtitles.entries[DROPPED_INDEX].segment_id
        assert missing == [expected], (
            f"the check named {missing}, not the deleted {expected}"
        )
        print(
            f"[assert] expected: fail (1 entry missing at {removed.start}ms); "
            f"actual: fail ✔ ({', '.join(missing)})"
        )
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
