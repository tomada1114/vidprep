"""Deliberately broken inputs, and the checks that must reject them.

verification-plan.md §1 principle 3: a machine check is only worth trusting once
it has been seen to fail. Each ``caseNN_*.py`` here breaks one thing from the
table of verification-plan.md §10, runs the check that should catch it, and
fails if the check passes — the assertion is inverted on purpose.

Every case runs on its own, printing what it broke and what caught it::

    uv run python -m tests.fault_injection.case02_midword_cut

and all six are also collected by ``tests/test_fault_injection.py``, so a change
that quietly disables a check breaks the suite. Nothing here needs ffmpeg,
whisper.cpp or auto-editor: the material is faked at the process boundary by
:mod:`tests.fault_injection._harness`, which is what lets the cases run on CI.
"""

from __future__ import annotations
