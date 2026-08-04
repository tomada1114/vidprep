"""Character error rate against the human reference (verification-plan.md §3.1).

Usage:
    uv run python scripts/cer.py <reference> <hypothesis>

``normalize`` is deliberately importable: the ASR bench (``scripts/asr_bench.py``)
and the re-transcription check planned for ``render --verify-asr`` must all
compare text under exactly the same rules, and the only way to guarantee that is
to share this one function.
"""

from __future__ import annotations

import argparse
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import jiwer

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


@dataclass(frozen=True, slots=True)
class CerResult:
    """One reference/hypothesis comparison.

    Attributes:
        cer: Character error rate as a fraction of the reference length.
        substitutions: Characters read as a different character.
        deletions: Reference characters the hypothesis dropped.
        insertions: Characters the hypothesis added.
        reference_chars: Reference length after normalisation.
        hypothesis_chars: Hypothesis length after normalisation.
    """

    cer: float
    substitutions: int
    deletions: int
    insertions: int
    reference_chars: int
    hypothesis_chars: int


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


def measure(reference: str, hypothesis: str) -> CerResult:
    """Compare two transcripts under the §3.1 normalisation rules.

    Raises:
        ValueError: If the reference is empty once normalised, which leaves the
            error rate undefined rather than merely large.
    """
    normalized_reference = normalize(reference)
    normalized_hypothesis = normalize(hypothesis)
    if not normalized_reference:
        msg = "reference is empty after normalisation"
        raise ValueError(msg)
    output = jiwer.process_characters(normalized_reference, normalized_hypothesis)
    return CerResult(
        cer=output.cer,
        substitutions=output.substitutions,
        deletions=output.deletions,
        insertions=output.insertions,
        reference_chars=len(normalized_reference),
        hypothesis_chars=len(normalized_hypothesis),
    )


def format_result(result: CerResult) -> str:
    """Render a comparison the way the command line reports it."""
    return "\n".join(
        (
            f"ref chars (normalized): {result.reference_chars}",
            f"hyp chars (normalized): {result.hypothesis_chars}",
            f"CER: {result.cer:.4f}  ({result.cer * 100:.2f}%)",
            f"substitutions={result.substitutions} "
            f"deletions={result.deletions} insertions={result.insertions}",
        )
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read {path}: {exc}"
        raise SystemExit(msg) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reference", type=Path, help="human reference transcript")
    parser.add_argument("hypothesis", type=Path, help="transcript under test")
    arguments = parser.parse_args(argv)
    try:
        result = measure(_read(arguments.reference), _read(arguments.hypothesis))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
