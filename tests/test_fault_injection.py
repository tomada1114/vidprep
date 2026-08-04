"""Run every fault injection of verification-plan.md §10 as part of the suite.

The cases are scripts first — each one runs on its own and prints what it broke
and what caught it — but they are also the regression test of the checkers
themselves: if a check is weakened, the case that proves it fires stops passing
here, without anybody having to remember to run the scripts.
"""

from __future__ import annotations

import pytest

from tests.fault_injection import (
    case01_skipped_audio_fix,
    case02_midword_cut,
    case03_srt_drop,
    case04_bad_patch,
    case05_overlapping_cuts,
    case06_swapped_source,
)

CASES = (
    case01_skipped_audio_fix,
    case02_midword_cut,
    case03_srt_drop,
    case04_bad_patch,
    case05_overlapping_cuts,
    case06_swapped_source,
)


@pytest.mark.parametrize(
    "case", CASES, ids=[module.__name__.rsplit(".", 1)[-1] for module in CASES]
)
def test_the_broken_input_is_refused(case):
    assert case.main() == 0


def test_every_case_of_the_table_is_covered():
    assert len(CASES) == 6, "verification-plan.md §10 lists six ways to break it"
