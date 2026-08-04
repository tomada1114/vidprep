"""Case 2: a hand-written `manual` cut through the middle of a sentence.

The one this whole check exists for (verification-plan.md §10). Two things must
react: the re-transcription flag of §8.1, because the words the cut removed are
gone from the second pass, and the speech-collision check of §7, because the cut
overlaps 0.4s of speech against a limit of 0.2s.

§7 scopes its check to `reason: silence`, so a `manual` cut is deliberately out
of its reach — the overlap is measured and printed here, and `detect.verify_speech`
is then shown to refuse the very same interval once it is a cut it owns.

The clean run at the end is the other half of the measurement: with the injected
cut removed and a faithful second pass, the flag count over the same boundaries
is the false-positive rate (REQ-013) that gate promotion is judged on.

!!! warning "What this case does and does not prove"

    The second pass is a fake that returns the expected text with *LOST* cut out
    of it, so the recogniser is not what is under test: what is proved is that a
    deletion is turned into a flag, blamed on the right cut, and placed back on
    the original timeline within the two-second window. That the words really do
    disappear from a real render, and that a real second pass over an untouched
    render flags nothing, is measured on the golden material instead — the table
    in verification-plan.md §8.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vidprep import detect, render
from vidprep._intervals import Span
from vidprep.errors import InvariantViolationError
from vidprep.models import Cut

from ._harness import (
    CUTS,
    SEGMENTS,
    FakeMedia,
    build_project,
    refusal,
    workspace,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vidprep.verify import VerifyResult

#: The sentence the cut crosses, and where it crosses it.
DAMAGED = SEGMENTS[1]
MIDWORD_CUT = ("c0003", 12.0, 12.4, "manual", "approved")

#: Characters of that sentence the cut takes with it.
LOST = "作業状況"


def _reasr_text() -> str:
    """Return what a second pass hears once the cut has removed *LOST*."""
    return "".join(
        text.replace(LOST, "") if identifier == DAMAGED[0] else text
        for identifier, _, _, text in SEGMENTS
    )


def _collision(cuts: tuple[tuple[str, float, float, str, str], ...]) -> float:
    """Return how much speech the injected cut removes, in seconds."""
    spoken = [Span(start, end) for _, start, end, _ in SEGMENTS]
    injected = Span(MIDWORD_CUT[1], MIDWORD_CUT[2])
    measured = sum(span.overlap(injected) for span in spoken)
    as_silence = [
        Cut(
            id=identifier,
            start=start,
            end=end,
            reason="silence",
            status="approved",
        )
        for identifier, start, end, _, _ in cuts
    ]
    reported = refusal(
        lambda: detect.verify_speech(as_silence, spoken),
        InvariantViolationError,
        "a silence cut through the middle of a sentence",
    )
    assert MIDWORD_CUT[0] in reported, (
        f"the wrong cut was blamed for the collision: {reported}"
    )
    assert "0.2s of speech" in reported, (
        f"the §7 limit was not the one reported: {reported}"
    )
    return measured


def _verify(
    root: Path,
    cuts: Sequence[tuple[str, float, float, str, str]],
    reasr: str | None,
) -> VerifyResult:
    """Render *cuts* with a second pass returning *reasr*, and return its result."""
    loaded = build_project(root, cuts)
    media = FakeMedia(reasr=reasr)
    with media.installed(root.parent):
        result = render.run_render(loaded, verify_asr=True)
    assert result.verified is not None
    return result.verified


def main() -> int:
    """Run the injection.

    Returns:
        ``0`` when the mid-sentence cut was flagged.

    Raises:
        AssertionError: If either check let the cut through.
    """
    with workspace() as root:
        injected = (*CUTS, MIDWORD_CUT)
        print(
            f"[setup] cuts.json gains a manual cut "
            f"({MIDWORD_CUT[1]}, {MIDWORD_CUT[2]}) → crosses "
            f"{DAMAGED[0]}「{DAMAGED[3]}」"
        )
        overlap = _collision(injected)
        print(
            f"[assert] speech collision: manual cuts are outside §7 (silence "
            f"only); the overlap is {overlap:.3f}s against a "
            f"{detect.MAX_SPEECH_OVERLAP:.1f}s limit, and the check refuses "
            f"the same interval as a silence cut ✔"
        )
        print("[run]   vidprep render --verify-asr")
        flagged = _verify(root / "damaged", injected, _reasr_text())
        for flag in flagged.flags:
            print(f"  {flag.to_dict()}")
        assert flagged.flags, (
            "the re-transcription check found nothing where a sentence was cut "
            "in half; verification-plan.md §8.1 has no detection power"
        )
        (flag,) = flagged.flags
        assert flag.cut_id == MIDWORD_CUT[0], (
            f"the flag blamed {flag.cut_id}, not the injected cut"
        )
        assert flag.missing == LOST, (
            f"the flag reports {flag.missing!r}, not the removed {LOST!r}"
        )
        print(f"[assert] boundary flags: {len(flagged.flags)} → detected ✔")

        clean = _verify(root / "clean", CUTS, None)
        rate = clean.false_positive_rate
        assert rate is not None, "the clean plan cut nothing, so there is no rate"
        assert not clean.flags, (
            f"an untouched render was flagged {len(clean.flags)} times; the "
            "check cannot be promoted to a gate while that happens"
        )
        print(
            f"[measure] false positives on normal cuts: {len(clean.flags)}/"
            f"{clean.boundaries} boundaries = {rate:.3f} (REQ-013)"
        )
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
