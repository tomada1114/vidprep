"""The comparison form every text measurement uses (verification-plan.md §3.1).

CER against the human reference, the ASR bench and the re-transcription check of
``render --verify-asr`` all compare Japanese text, and they only mean the same
thing if they reduce it the same way first. The rule lives here, in the package,
so ``scripts/cer.py`` and :mod:`vidprep.verify` share one definition rather than
two that drift apart.
"""

from __future__ import annotations

import unicodedata

#: Punctuation dropped before comparison (verification-plan.md §3.1 lists the
#: ideographic comma and full stop, the full-width exclamation and question
#: marks, and the ellipsis). Normalisation runs NFKC first, which already folds
#: the full-width marks and the ellipsis into ASCII, so only the two ideographic
#: marks survive to be named here — the rest are matched in their ASCII form.
PUNCTUATION = frozenset(
    "、"  # ideographic comma
    "。"  # ideographic full stop
    "!?.,"  # what NFKC leaves of the full-width marks and the ellipsis
)


def normalize(text: str) -> str:
    """Return *text* reduced to the form CER is measured on.

    NFKC folding, then whitespace and punctuation removal, then lower-casing.
    Number spellings ("3つ" vs "三つ") are left alone on purpose: getting them
    right is part of what the dictionary and the proofreading pass are measured
    on, so normalising them away would hide the very errors we care about.
    """
    folded = unicodedata.normalize("NFKC", text)
    kept = (char for char in folded if not char.isspace() and char not in PUNCTUATION)
    return "".join(kept).lower()
