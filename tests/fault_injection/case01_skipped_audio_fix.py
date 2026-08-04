"""Case 1: render material `audio-fix` never touched (verification-plan.md §10).

The loudness check of §8 exists because normalisation can be undone by the cuts
without anything else looking wrong. Rendering from the raw recording — which
measures -22.24 LUFS on the golden sample, more than 8 dB under the -14 target —
must therefore be refused.
"""

from __future__ import annotations

from vidprep import render
from vidprep.errors import InvariantViolationError

from ._harness import (
    NORMALISED_LUFS,
    UNPROCESSED_LUFS,
    FakeMedia,
    build_project,
    refusal,
    workspace,
)


def main() -> int:
    """Run the injection.

    Returns:
        ``0`` when the loudness check refused the render.

    Raises:
        AssertionError: If the render was accepted, which means the check of
            verification-plan.md §8 is not doing anything.
    """
    with workspace() as root:
        print(
            f"[setup] audio/processed.wav is the raw recording "
            f"({UNPROCESSED_LUFS} LUFS, target -14.0 ± 0.5)"
        )
        loaded = build_project(root / "project")
        media = FakeMedia(integrated=UNPROCESSED_LUFS)
        print("[run]   vidprep render")
        with media.installed(root):
            reported = refusal(
                lambda: render.run_render(loaded),
                InvariantViolationError,
                f"a render at {UNPROCESSED_LUFS} LUFS",
            )
        # §8 reports its three conditions together, so the message has to say
        # loudness: a case that accepted any refusal would pass even if the
        # loudness clause were deleted and the length clause fired instead.
        assert "LUFS" in reported, f"the refusal was not about loudness: {reported}"
        assert "off by 8.24" in reported, f"the drift was misreported: {reported}"
        print(f"[assert] loudness check: fail ✔ ({reported})")
        assert not (loaded.root / "out" / "output.mp4").exists(), (
            "a refused render must leave no output behind"
        )

        # The control: the same fixture at the normalised level must pass, or
        # the case above proves only that this project never renders.
        control = build_project(root / "control")
        with FakeMedia(integrated=NORMALISED_LUFS).installed(root):
            render.run_render(control)
        print(f"[control] the same render at {NORMALISED_LUFS} LUFS: accepted ✔")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
