"""Tests for :mod:`vidprep._subtitles`, focused on ``to_text``.

Line breaking, readability and the SRT itself are exercised through a real
render in ``tests/test_render.py`` (``TestSubtitles``, ``TestLineBreaking``);
what belongs here is the paragraph rule ``to_text`` applies on top of the same
entries, which is cheaper to probe directly on hand-built ``Entry`` values than
by driving a whole render for every width and gap combination.
"""

from __future__ import annotations

from vidprep._subtitles import (
    MAX_PARAGRAPH_WIDTH,
    MIN_PARAGRAPH_WIDTH,
    PARAGRAPH_PAUSE,
    Entry,
    Subtitles,
)


def entry(segment_id: str, start: float, end: float, text: str) -> Entry:
    """Build an entry with *text* as its one unbroken line."""
    return Entry(segment_id, start, end, (text,))


class TestEmptyAndSingleParagraph:
    def test_no_entries_render_to_an_empty_document(self):
        assert Subtitles((), ()).to_text() == ""

    def test_a_single_paragraph_is_timestamped_at_its_first_entrys_start(self):
        subtitles = Subtitles((entry("s1", 3.0, 4.0, "こんにちは"),), ())

        assert subtitles.to_text() == "[00:03] こんにちは\n"

    def test_paragraphs_are_separated_by_a_blank_line(self):
        long_a = "あ" * MIN_PARAGRAPH_WIDTH + "。"
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 5.0, long_a),
                entry("s2", 5.1, 6.0, "つぎ"),
            ),
            (),
        )

        assert subtitles.to_text().count("\n\n") == 1


class TestTimestampFormat:
    def test_under_an_hour_is_minutes_and_seconds(self):
        subtitles = Subtitles((entry("s1", 125.4, 126.0, "あ"),), ())

        assert subtitles.to_text().startswith("[02:05] ")

    def test_exactly_an_hour_gains_the_hour_field(self):
        subtitles = Subtitles((entry("s1", 3600.0, 3601.0, "あ"),), ())

        assert subtitles.to_text().startswith("[1:00:00] ")

    def test_past_an_hour_keeps_minutes_and_seconds_zero_padded(self):
        subtitles = Subtitles((entry("s1", 3661.0, 3662.0, "あ"),), ())

        assert subtitles.to_text().startswith("[1:01:01] ")


class TestBreakingRules:
    def test_a_sentence_end_below_the_minimum_width_does_not_break(self):
        below_minimum = "あ" * (MIN_PARAGRAPH_WIDTH - 2) + "。"
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 5.0, below_minimum),
                entry("s2", 5.1, 6.0, "つぎ"),
            ),
            (),
        )

        assert subtitles.to_text().count("[") == 1

    def test_a_sentence_end_at_the_minimum_width_breaks(self):
        at_minimum = "あ" * (MIN_PARAGRAPH_WIDTH - 1) + "。"  # width == minimum
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 5.0, at_minimum),
                entry("s2", 5.1, 6.0, "つぎ"),
            ),
            (),
        )

        assert subtitles.to_text().count("[") == 2

    def test_a_long_enough_pause_breaks_without_punctuation(self):
        no_punctuation = "あ" * MIN_PARAGRAPH_WIDTH
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 5.0, no_punctuation),
                entry("s2", 5.0 + PARAGRAPH_PAUSE, 6.0, "つぎ"),
            ),
            (),
        )

        assert subtitles.to_text().count("[") == 2

    def test_a_shorter_gap_does_not_break_without_punctuation(self):
        no_punctuation = "あ" * MIN_PARAGRAPH_WIDTH
        gap = PARAGRAPH_PAUSE - 0.01
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 5.0, no_punctuation),
                entry("s2", 5.0 + gap, 6.0, "つぎ"),
            ),
            (),
        )

        assert subtitles.to_text().count("[") == 1

    def test_the_hard_cap_breaks_regardless_of_punctuation_or_pause(self):
        no_signal = "あ" * MAX_PARAGRAPH_WIDTH
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 5.0, no_signal),
                entry("s2", 5.1, 6.0, "つぎ"),  # gap well under PARAGRAPH_PAUSE
            ),
            (),
        )

        assert subtitles.to_text().count("[") == 2


class TestJoining:
    def test_two_half_width_entries_are_joined_with_a_space(self):
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 1.0, "hello"),
                entry("s2", 1.1, 2.0, "world"),
            ),
            (),
        )

        assert "hello world" in subtitles.to_text()

    def test_two_full_width_entries_are_joined_with_no_separator(self):
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 1.0, "こんにちは"),
                entry("s2", 1.1, 2.0, "さようなら"),
            ),
            (),
        )

        assert "こんにちはさようなら" in subtitles.to_text()

    def test_a_full_width_side_gets_no_space_even_next_to_half_width(self):
        subtitles = Subtitles(
            (
                entry("s1", 0.0, 1.0, "abc"),
                entry("s2", 1.1, 2.0, "です"),
            ),
            (),
        )

        assert "abcです" in subtitles.to_text()
