"""Tests for scripts/cer.py — the §3.1 measurement rules (REQ-001..003)."""

from __future__ import annotations

import pytest

import cer


def test_measure_counts_a_single_dropped_character():
    result = cer.measure("あいうえお", "あいうえ")

    assert result.cer == pytest.approx(0.2)
    assert (result.substitutions, result.deletions, result.insertions) == (0, 1, 0)
    assert (result.reference_chars, result.hypothesis_chars) == (5, 4)


def test_measure_counts_an_added_character():
    result = cer.measure("あいう", "あいえう")

    assert result.cer == pytest.approx(1 / 3)
    assert (result.substitutions, result.deletions, result.insertions) == (0, 0, 1)


def test_identical_text_scores_zero():
    assert cer.measure("こんにちは", "こんにちは").cer == 0.0


# Look-alike characters are the subject of these cases, so RUF001 (which warns
# that they are easy to confuse with their ASCII twins) is exactly what is
# being exercised here rather than a mistake.
@pytest.mark.parametrize(
    ("reference", "hypothesis"),
    [
        pytest.param("Claude Code。", "claude code", id="case-and-full-stop"),
        pytest.param("はい、そうです！", "はいそうです", id="comma-and-bang"),  # noqa: RUF001
        pytest.param("えーっと…", "えーっと", id="ellipsis"),
        pytest.param("ＡＢＣ", "abc", id="full-width-letters"),  # noqa: RUF001
        pytest.param("ｶﾀｶﾅ", "カタカナ", id="half-width-katakana"),
        pytest.param("テスト です", "テストです", id="whitespace"),
    ],
)
def test_normalisation_hides_case_width_and_punctuation(reference, hypothesis):
    assert cer.measure(reference, hypothesis).cer == 0.0


def test_number_spellings_are_not_normalised_away():
    result = cer.measure("3つあります", "三つあります")

    assert result.cer > 0
    assert result.substitutions == 1


def test_empty_hypothesis_loses_every_character():
    assert cer.measure("あいうえお", "").cer == 1.0


def test_empty_reference_is_rejected():
    with pytest.raises(ValueError, match="reference is empty"):
        cer.measure("。。。", "なにか")


def test_format_result_reports_every_field():
    rendered = cer.format_result(cer.measure("あいうえお", "あいうえ"))

    assert rendered.splitlines() == [
        "ref chars (normalized): 5",
        "hyp chars (normalized): 4",
        "CER: 0.2000  (20.00%)",
        "substitutions=0 deletions=1 insertions=0",
    ]


def test_cli_prints_the_comparison(tmp_path, capsys):
    reference = tmp_path / "ref.txt"
    hypothesis = tmp_path / "hyp.txt"
    reference.write_text("Claude Code。", encoding="utf-8")
    hypothesis.write_text("claude code", encoding="utf-8")

    exit_code = cer.main([str(reference), str(hypothesis)])

    assert exit_code == 0
    assert "CER: 0.0000  (0.00%)" in capsys.readouterr().out


def test_cli_reports_a_missing_file(tmp_path):
    hypothesis = tmp_path / "hyp.txt"
    hypothesis.write_text("なにか", encoding="utf-8")

    with pytest.raises(SystemExit, match="cannot read"):
        cer.main([str(tmp_path / "missing.txt"), str(hypothesis)])
