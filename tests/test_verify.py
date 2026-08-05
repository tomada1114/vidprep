"""Tests for the re-transcription check and the SRT read-back.

The recogniser and the media tools are the fakes the fault injections use
(:mod:`tests.fault_injection._harness`), so what is exercised here is the
comparison itself: which differences count, where they are placed on the
original timeline, and what the CLI does about them. The suite therefore runs
on CI, where neither whisper.cpp nor ffmpeg is installed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pysubs2
import pytest

from tests.fault_injection._harness import (
    CUTS,
    DURATION,
    SEGMENTS,
    FakeMedia,
    build_project,
    transcript_text,
)
from vidprep import _fillers, _retranscribe, verify
from vidprep import project as project_module
from vidprep import render as render_module
from vidprep._retranscribe import ExpectedSegment, ExpectedText, MissingHunk
from vidprep._text import normalize
from vidprep.errors import (
    EXIT_OK,
    EXIT_VALIDATION,
    AsrFailedError,
    InvariantViolationError,
    UsageError,
)
from vidprep.models import Cut, Segment
from vidprep.timeline import Timeline

if TYPE_CHECKING:
    from pathlib import Path

    from vidprep._fillers import Tier
    from vidprep.project import Project

#: A hand-written cut straight through the middle of ``s0002``.
MIDWORD_CUT = ("c0003", 12.0, 12.4, "manual", "approved")

APPROVED = tuple(
    Cut.model_validate(
        {
            "id": identifier,
            "start": start,
            "end": end,
            "reason": reason,
            "status": status,
        }
    )
    for identifier, start, end, reason, status in CUTS
)


def timeline() -> Timeline:
    """The cut plan the miniature project renders with."""
    return Timeline([(cut.start, cut.end) for cut in APPROVED], DURATION)


def segments() -> list[Segment]:
    """The transcript of the miniature project."""
    return [
        Segment(id=identifier, start=start, end=end, text=text)
        for identifier, start, end, text in SEGMENTS
    ]


# --------------------------------------------------------------------------- #
#  Building the expectation
# --------------------------------------------------------------------------- #


class TestExpectedText:
    def test_kept_segments_are_concatenated_in_cut_timeline_order(self):
        expected = _retranscribe.build_expected(segments(), timeline(), APPROVED)

        assert expected.text == normalize(transcript_text())
        assert [entry.segment_id for entry in expected.segments] == [
            "s0001",
            "s0002",
            "s0003",
        ]

    def test_a_segment_a_cut_swallowed_is_left_out(self):
        swallowing = (
            Cut(id="c0009", start=9.5, end=15.0, reason="silence", status="approved"),
        )
        plan = Timeline([(9.5, 15.0)], DURATION)

        expected = _retranscribe.build_expected(segments(), plan, swallowing)

        assert [entry.segment_id for entry in expected.segments] == ["s0001", "s0003"]
        assert "コマンドを実行すると" not in expected.text

    def test_locate_places_a_character_inside_its_segment(self):
        expected = _retranscribe.build_expected(segments(), timeline(), APPROVED)
        offset = expected.offsets[1]

        segment_id, moment = expected.locate(offset)

        assert segment_id == "s0002"
        assert moment == pytest.approx(expected.segments[1].start)

    def test_locate_refuses_an_empty_expectation(self):
        with pytest.raises(IndexError, match="empty"):
            ExpectedText((), "", ()).locate(0)


class TestFillerRemoval:
    """REQ-002: an approved ``reason: filler`` cut takes its word with it."""

    WORDS: tuple[tuple[str, Tier], ...] = (("えーと", "strong"),)

    def _spoken(self) -> list[Segment]:
        return [Segment(id="s0001", start=1.0, end=4.0, text="えーとこれが本題です")]

    def _filler_cut(self, status: str) -> tuple[Cut, ...]:
        return (
            Cut.model_validate(
                {
                    "id": "c0001",
                    "start": 1.0,
                    "end": 1.6,
                    "reason": _fillers.REASON,
                    "status": status,
                }
            ),
        )

    def test_an_approved_filler_cut_removes_the_word(self):
        approved = self._filler_cut("approved")
        plan = Timeline([(cut.start, cut.end) for cut in approved], DURATION)

        expected = _retranscribe.build_expected(
            self._spoken(), plan, approved, self.WORDS
        )

        assert expected.text == "これが本題です"

    def test_a_proposed_filler_cut_leaves_the_word_alone(self):
        proposed = self._filler_cut("proposed")
        plan = Timeline([], DURATION)

        expected = _retranscribe.build_expected(
            self._spoken(), plan, proposed, self.WORDS
        )

        assert expected.text == "えーとこれが本題です"

    def test_a_trailing_filler_cut_removes_the_word_it_ends_with(self):
        spoken = [Segment(id="s0001", start=1.0, end=4.0, text="これが本題ですえーと")]
        approved = (
            Cut.model_validate(
                {
                    "id": "c0001",
                    "start": 3.4,
                    "end": 4.0,
                    "reason": _fillers.REASON,
                    "status": "approved",
                }
            ),
        )
        plan = Timeline([(3.4, 4.0)], DURATION)

        expected = _retranscribe.build_expected(spoken, plan, approved, self.WORDS)

        assert expected.text == "これが本題です"

    def test_a_segment_that_normalises_to_nothing_drops_out(self):
        spoken = [
            Segment(id="s0001", start=1.0, end=4.0, text="。。"),
            Segment(id="s0002", start=5.0, end=8.0, text="本題です"),
        ]

        expected = _retranscribe.build_expected(spoken, Timeline([], DURATION), ())

        assert [entry.segment_id for entry in expected.segments] == ["s0002"]
        assert expected.text == "本題です"

    def test_a_filler_cut_inside_the_segment_removes_what_it_covers(self):
        """A hand-written interior filler cut must not become a false positive."""
        spoken = [Segment(id="s0001", start=0.0, end=10.0, text="これはえーと本題です")]
        approved = (
            Cut.model_validate(
                {
                    "id": "c0001",
                    "start": 3.0,
                    "end": 6.0,
                    "reason": _fillers.REASON,
                    "status": "approved",
                }
            ),
        )
        plan = Timeline([(3.0, 6.0)], DURATION)

        expected = _retranscribe.build_expected(spoken, plan, approved, self.WORDS)

        assert expected.text == "これは本題です"

    def test_a_filler_cut_covering_no_filler_at_all_removes_nothing(self):
        spoken = [Segment(id="s0001", start=0.0, end=10.0, text="これはえーと本題です")]
        approved = (
            Cut.model_validate(
                {
                    "id": "c0001",
                    "start": 8.0,
                    "end": 9.0,
                    "reason": _fillers.REASON,
                    "status": "approved",
                }
            ),
        )
        plan = Timeline([(8.0, 9.0)], DURATION)

        expected = _retranscribe.build_expected(spoken, plan, approved, self.WORDS)

        assert expected.text == "これはえーと本題です"

    def test_a_silence_cut_over_the_same_words_removes_nothing(self):
        silence = (
            Cut(id="c0001", start=1.0, end=1.6, reason="silence", status="approved"),
        )
        plan = Timeline([(1.0, 1.6)], DURATION)

        expected = _retranscribe.build_expected(
            self._spoken(), plan, silence, self.WORDS
        )

        assert expected.text == "えーとこれが本題です"


# --------------------------------------------------------------------------- #
#  Finding the differences
# --------------------------------------------------------------------------- #


class TestMissingHunks:
    @pytest.mark.parametrize(
        ("expected", "actual", "found"),
        [
            pytest.param("あいうえお", "あいうえお", [], id="identical"),
            pytest.param("あいうえお", "あいえお", [], id="one-character-ignored"),
            pytest.param("あいうえお", "あいお", ["うえ"], id="two-characters-flagged"),
            pytest.param(
                "あいうえお", "あいかきお", [], id="replacement-is-not-a-deletion"
            ),
            pytest.param("あいうえお", "", ["あいうえお"], id="everything-gone"),
        ],
    )
    def test_only_deletions_of_two_or_more_characters_count(
        self, expected, actual, found
    ):
        assert [
            hunk.text for hunk in _retranscribe.missing_hunks(expected, actual)
        ] == found

    def test_a_hunk_records_where_it_starts(self):
        (hunk,) = _retranscribe.missing_hunks("あいうえお", "あいお")

        assert hunk.index == 2


class TestCharacterErrorRate:
    def test_identical_texts_measure_zero(self):
        assert _retranscribe.character_error_rate("あいうえお", "あいうえお") == 0.0

    def test_every_kind_of_edit_is_counted(self):
        # one deletion out of five characters
        assert _retranscribe.character_error_rate(
            "あいうえお", "あいえお"
        ) == pytest.approx(0.2)

    def test_an_insertion_is_counted(self):
        assert _retranscribe.character_error_rate(
            "あいうえお", "あいうかきえお"
        ) == pytest.approx(0.4)

    def test_a_replacement_counts_the_longer_side(self):
        assert _retranscribe.character_error_rate(
            "あいうえお", "あいかきくけお"
        ) == pytest.approx(0.8)

    def test_an_empty_expectation_has_no_rate(self):
        with pytest.raises(ValueError, match="empty"):
            _retranscribe.character_error_rate("", "あいうえお")


class TestBoundaryWindow:
    """REQ-005: 2.00s from a boundary is flagged, 2.01s is not."""

    #: One second of the cut timeline per hundred characters.
    TEXT = "あ" * 9000

    def _expected(self) -> ExpectedText:
        return ExpectedText(
            (ExpectedSegment("s0001", self.TEXT, 0.0, 90.0),), self.TEXT, (0,)
        )

    def _flags(self, index: int) -> list[_retranscribe.BoundaryFlag]:
        cut = Cut(id="c0001", start=10.0, end=20.0, reason="silence", status="approved")
        return _retranscribe.flag_boundaries(
            self._expected(),
            [MissingHunk("かき", index)],
            Timeline([(10.0, 20.0)], 100.0),
            [cut],
        )

    def test_exactly_two_seconds_away_is_flagged(self):
        (flag,) = self._flags(800)

        assert flag.cut_id == "c0001"
        assert flag.src_time == pytest.approx(8.0)

    def test_a_hundredth_of_a_second_further_is_not(self):
        assert self._flags(799) == []

    def test_nothing_is_flagged_when_nothing_was_cut(self):
        assert (
            _retranscribe.flag_boundaries(
                self._expected(), [MissingHunk("かき", 800)], Timeline([], 100.0), []
            )
            == []
        )


# --------------------------------------------------------------------------- #
#  The stage
# --------------------------------------------------------------------------- #


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A miniature project ready to be rendered."""
    return build_project(tmp_path / "project")


def render_with_verification(
    project: Project, tmp_path: Path, media: FakeMedia
) -> render_module.Result:
    """Render *project* with the check switched on, under *media*."""
    with media.installed(tmp_path):
        return render_module.run_render(project, verify_asr=True)


class TestRunVerifyAsr:
    def test_a_faithful_second_pass_flags_nothing(self, project, tmp_path):
        result = render_with_verification(project, tmp_path, FakeMedia())

        assert result.verified is not None
        assert result.verified.passed
        assert result.verified.global_cer == 0.0
        assert result.verified.expected_chars == result.verified.reasr_chars
        assert result.verified.false_positive_rate == 0.0

    def test_the_second_pass_repeats_the_settings_of_the_transcript(
        self, project, tmp_path
    ):
        media = FakeMedia()
        result = render_with_verification(project, tmp_path, media)

        whisper = next(command for command in media.commands if "--vad" in command)
        assert "ggml-large-v3-turbo.bin" in whisper[whisper.index("-m") + 1]
        assert result.verified is not None
        assert result.verified.model == project.profile.asr.model
        assert result.verified.vad == "silero-v5"

    def test_the_render_is_not_modified_by_reading_it_back(self, project, tmp_path):
        media = FakeMedia()
        render_with_verification(project, tmp_path, media)
        video = project.root / render_module.VIDEO_NAME
        before = project_module.sha256_file(video)

        subject = render_module.subject(project, list(APPROVED), timeline())
        with media.installed(tmp_path):
            verify.run_verify_asr(subject)

        assert project_module.sha256_file(video) == before

    def test_a_second_pass_that_returns_nothing_is_a_failed_stage(
        self, project, tmp_path
    ):
        with pytest.raises(AsrFailedError, match="returned no text"):
            render_with_verification(project, tmp_path, FakeMedia(reasr=""))

    def test_a_transcript_from_an_unknown_backend_is_refused(self, project, tmp_path):
        path = project.root / "transcript.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["asr"]["backend"] = "whisper-in-a-teacup"
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        reloaded = project_module.load_project(project.root)

        with pytest.raises(UsageError, match="cannot repeat"):
            render_with_verification(reloaded, tmp_path, FakeMedia())

    def test_a_missing_recogniser_stops_the_run_before_the_encode(
        self, project, tmp_path
    ):
        media = FakeMedia()
        # Installed, then emptied of the binary directory again: what a machine
        # that never built whisper.cpp looks like.
        with pytest.MonkeyPatch.context() as patched, media.installed(tmp_path):
            patched.setenv("PATH", "")
            with pytest.raises(UsageError, match="whisper"):
                render_module.run_render(project, verify_asr=True)

        assert not any("-filter_complex" in command for command in media.commands)

    def test_a_render_with_nothing_left_to_say_cannot_be_compared(self, tmp_path):
        swallowed = build_project(
            tmp_path / "swallowed",
            (
                ("c0001", 0.8, 4.4, "silence", "approved"),
                ("c0002", 9.6, 14.4, "silence", "approved"),
                ("c0003", 19.6, 24.4, "silence", "approved"),
            ),
        )

        with pytest.raises(AsrFailedError, match="nothing to compare"):
            render_with_verification(swallowed, tmp_path, FakeMedia())

    def test_the_false_positive_rate_is_undefined_without_cuts(self, tmp_path):
        uncut = build_project(tmp_path / "uncut", ())

        result = render_with_verification(uncut, tmp_path, FakeMedia())

        assert result.verified is not None
        assert result.verified.boundaries == 0
        assert result.verified.false_positive_rate is None


class TestMissingSubtitleEntries:
    def test_a_complete_file_reports_nothing(self, project, tmp_path):
        render_with_verification(project, tmp_path, FakeMedia())
        subtitles = render_module.build_subtitles(project, timeline())

        assert (
            verify.missing_subtitle_entries(
                project.root / render_module.SUBTITLES_NAME, subtitles.entries
            )
            == []
        )

    def test_an_entry_whose_text_was_emptied_counts_as_missing(self, project, tmp_path):
        render_with_verification(project, tmp_path, FakeMedia())
        subtitles = render_module.build_subtitles(project, timeline())
        path = project.root / render_module.SUBTITLES_NAME
        events = pysubs2.SSAFile.from_string(
            path.read_text(encoding="utf-8"), format_="srt"
        )
        events[1].text = ""
        path.write_text(events.to_string("srt"), encoding="utf-8")

        missing = verify.missing_subtitle_entries(path, subtitles.entries)

        assert missing == [subtitles.entries[1].segment_id]

    def test_two_entries_sharing_a_timing_need_two_events(self, project, tmp_path):
        render_with_verification(project, tmp_path, FakeMedia())
        subtitles = render_module.build_subtitles(project, timeline())
        path = project.root / render_module.SUBTITLES_NAME
        doubled = [*subtitles.entries, subtitles.entries[0]]

        missing = verify.missing_subtitle_entries(path, doubled)

        assert missing == [subtitles.entries[0].segment_id]

    def test_a_render_that_loses_an_entry_is_refused(
        self, project, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(verify, "missing_subtitle_entries", lambda *_: ["s0002"])

        with pytest.raises(InvariantViolationError, match=r"not in out/subtitles\.srt"):
            render_with_verification(project, tmp_path, FakeMedia())

    def test_a_file_that_is_not_there_is_a_usage_error(self, project):
        subtitles = render_module.build_subtitles(project, timeline())

        with pytest.raises(UsageError, match="run `vidprep render` first"):
            verify.missing_subtitle_entries(
                project.root / "nowhere.srt", subtitles.entries
            )


# --------------------------------------------------------------------------- #
#  The command line
# --------------------------------------------------------------------------- #


class TestCli:
    def _damaged(self) -> str:
        return "".join(
            text.replace("作業状況", "") if identifier == "s0002" else text
            for identifier, _, _, text in SEGMENTS
        )

    def test_the_json_output_carries_the_comparison(self, project, tmp_path, run_cli):
        with FakeMedia().installed(tmp_path):
            result = run_cli(
                "render", "--verify-asr", "--json", "--project", str(project.root)
            )

        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)["verify_asr"]
        assert payload["mode"] == verify.GATE
        assert payload["near_boundary_flags"] == 0
        assert payload["boundaries"] == 2 * len(APPROVED)

    def test_a_flag_fails_the_run_under_the_default_mode(self, tmp_path, run_cli):
        """The gate of verification-plan.md §8.1, promoted in #32."""
        cuts = (*CUTS, MIDWORD_CUT)
        flagged = build_project(tmp_path / "flagged", cuts)

        with FakeMedia(reasr=self._damaged()).installed(tmp_path):
            result = run_cli(
                "render", "--verify-asr", "--json", "--project", str(flagged.root)
            )

        assert result.exit_code == EXIT_VALIDATION
        payload = json.loads(result.stdout)["verify_asr"]
        assert payload["mode"] == verify.GATE
        assert payload["flags"][0]["cut_id"] == "c0003"

    def test_advisory_reports_the_same_flag_without_failing(self, tmp_path, run_cli):
        cuts = (*CUTS, MIDWORD_CUT)
        flagged = build_project(tmp_path / "flagged", cuts)
        profile = flagged.profile.model_copy(deep=True)
        profile.render.verify_asr_mode = verify.ADVISORY
        project_module.write_json(flagged.root / project_module.PROFILE_NAME, profile)

        with FakeMedia(reasr=self._damaged()).installed(tmp_path):
            result = run_cli(
                "render", "--verify-asr", "--json", "--project", str(flagged.root)
            )

        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)["verify_asr"]
        assert payload["mode"] == verify.ADVISORY
        assert payload["near_boundary_flags"] == 1
        assert payload["flags"][0]["cut_id"] == "c0003"

    def test_a_dry_run_shows_the_second_pass_it_would_make(
        self, project, tmp_path, run_cli
    ):
        with FakeMedia().installed(tmp_path):
            result = run_cli(
                "render",
                "--verify-asr",
                "--dry-run",
                "--json",
                "--project",
                str(project.root),
            )

        commands = json.loads(result.stdout)["commands"]
        assert any("--vad" in command for command in commands)
        assert result.exit_code == EXIT_OK

    def test_without_the_flag_nothing_is_transcribed_again(
        self, project, tmp_path, run_cli
    ):
        media = FakeMedia()
        with media.installed(tmp_path):
            result = run_cli("render", "--json", "--project", str(project.root))

        assert "verify_asr" not in json.loads(result.stdout)
        assert not any("--vad" in command for command in media.commands)
