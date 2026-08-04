"""Tests for the detect stage: silence conversion, fillers and the merge.

auto-editor is faked at the process boundary — the fake prints the ``--export
v3`` document auto-editor prints — so the whole stage runs on CI, where neither
auto-editor nor any media tool is installed. The timelines are written with a
1000/1 timebase so a test can state a boundary in milliseconds and mean it; one
test uses the 30/1 timebase auto-editor really exports for a WAV.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from vidprep import _autoeditor, _ffmpeg, _fillers, _intervals, cli
from vidprep import detect as detect_module
from vidprep import doctor as doctor_module
from vidprep import project as project_module
from vidprep._intervals import Candidate, Span
from vidprep.errors import (
    EXIT_EXECUTION,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    InvariantViolationError,
    SchemaInvalidError,
    TimelineSchemaError,
    UsageError,
)
from vidprep.models import Cut, Cuts, Profile, Segment

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

AUTO_EDITOR_PATH = "/usr/local/bin/auto-editor"
AUTO_EDITOR_VERSION = "29.3.1"
DURATION = 298.92
TIMEBASE = 1000

#: Speech either side of four gaps: 0.5s (under ``min_duration``), 0.9s (gone
#: once padded, Example 2 of the issue), 1.1s and 10s (both cut).
KEPT = ((0.0, 10.0), (10.5, 12.0), (12.9, 30.0), (31.1, 50.0), (60.0, DURATION))

#: What auto-editor 29.3.1 puts on stdout ahead of the document even with the
#: progress display off: it blanks the line the bar would have occupied. Copied
#: byte for byte from a real ``--export v3`` run (issue #30).
CLEARED_LINE = " " * 78 + "\r"


def timeline_json(
    kept: Sequence[tuple[float, float]] = KEPT,
    timebase: int = TIMEBASE,
    **overrides: Any,
) -> str:
    """Render the ``--export v3`` document auto-editor prints for *kept*."""
    clips = []
    position = 0
    for start, end in kept:
        offset = round(start * timebase)
        duration = round(end * timebase) - offset
        clips.append(
            {
                "name": "audio",
                "src": "/project/audio/processed.wav",
                "start": position,
                "dur": duration,
                "offset": offset,
                "stream": 0,
            }
        )
        position += duration
    document: dict[str, Any] = {
        "version": "3",
        "timebase": f"{timebase}/1",
        "background": "#000000",
        "resolution": [1920, 1080],
        "samplerate": 16000,
        "layout": "1 channels",
        "v": [],
        "a": [clips],
    }
    document.update(overrides)
    return json.dumps(document)


@dataclass
class FakeAutoEditor:
    """auto-editor as a recording of what it would have printed."""

    output: str = field(default_factory=timeline_json)
    commands: list[list[str]] = field(default_factory=list)

    def run(self, args: Sequence[str], timeout: float = 0.0) -> str:
        """Stand in for the export, recording the command it was asked to run."""
        self.commands.append(list(args))
        return self.output


@pytest.fixture
def auto_editor(monkeypatch: pytest.MonkeyPatch) -> FakeAutoEditor:
    """Replace auto-editor with a fake that prints a fixed v3 timeline."""
    fake = FakeAutoEditor()
    monkeypatch.setattr(
        doctor_module,
        "check_auto_editor",
        lambda: {
            "ok": True,
            "path": AUTO_EDITOR_PATH,
            "version": AUTO_EDITOR_VERSION,
            "export_v3": True,
        },
    )
    monkeypatch.setattr(_ffmpeg, "run", fake.run)
    return fake


@pytest.fixture
def detectable(project_dir: Path) -> Path:
    """A project with the processed audio detection needs."""
    audio = project_dir / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "processed.wav").write_bytes(b"pretend this is PCM")
    return project_dir


def write_transcript(root: Path, segments: Sequence[tuple[float, float, str]]) -> None:
    """Write a transcript with the given segments, numbered in order."""
    payload = {
        "version": "1",
        "audio_source": "audio/processed.wav",
        "asr": {
            "backend": "whisper.cpp",
            "model": "large-v3-turbo",
            "vad": "silero-v5",
        },
        "segments": [
            {
                "id": f"s{index:04d}",
                "start": start,
                "end": end,
                "text": text,
                "source": "asr",
                "edits": [],
            }
            for index, (start, end, text) in enumerate(segments, start=1)
        ],
    }
    (root / "transcript.json").write_text(json.dumps(payload), encoding="utf-8")


def write_vad(root: Path, regions: Sequence[tuple[float, float]]) -> None:
    """Write the speech regions ``transcribe`` would have reported."""
    payload = {
        "version": "1",
        "backend": "silero-v5",
        "segments": [{"start": start, "end": end} for start, end in regions],
    }
    (root / "report").mkdir(parents=True, exist_ok=True)
    (root / "report" / "vad.json").write_text(json.dumps(payload), encoding="utf-8")


def write_cuts(root: Path, cuts: Sequence[dict[str, Any]]) -> None:
    """Write a ``cuts.json`` as a previous run and a review would have left it."""
    payload = {"version": "1", "cuts": cuts}
    (root / "cuts.json").write_text(json.dumps(payload), encoding="utf-8")


def read_cuts(root: Path) -> Cuts:
    """Read back the cuts a run wrote."""
    return Cuts.model_validate_json((root / "cuts.json").read_bytes())


def run(root: Path) -> detect_module.Result:
    """Run detection on the project at *root*."""
    return detect_module.run_detect(project_module.load_project(root))


def cut(identifier: str, start: float, end: float, **fields: Any) -> Cut:
    """Build one existing cut for the merge tests."""
    return Cut(
        id=identifier,
        start=start,
        end=end,
        **{"reason": detect_module.SILENCE, "confidence": 0.5, **fields},
    )


def candidate(start: float, end: float, **fields: Any) -> Candidate:
    """Build one freshly detected candidate for the merge tests."""
    return Candidate(
        start=start,
        end=end,
        **{
            "reason": detect_module.SILENCE,
            "confidence": 0.95,
            "status": "approved",
            **fields,
        },
    )


class TestSilenceConversion:
    """The auto-editor conversion layer (REQ-001, REQ-004, REQ-005)."""

    def test_the_timeline_is_asked_for_as_v3_with_the_profile_threshold(
        self, auto_editor, detectable
    ):
        run(detectable)
        command = auto_editor.commands[0]
        assert command[0] == AUTO_EDITOR_PATH
        assert command[command.index("--export") + 1] == "v3"
        assert command[command.index("--edit") + 1] == "audio:threshold=4%"

    def test_padding_is_left_to_vidprep_not_to_auto_editor(
        self, auto_editor, detectable
    ):
        run(detectable)
        command = auto_editor.commands[0]
        assert command[command.index("--margin") + 1] == "0s"

    def test_the_progress_bar_is_kept_off_the_stream_carrying_the_document(
        self, auto_editor, detectable
    ):
        # The bar is drawn on stdout, where `-o -` also puts the timeline, and
        # `--quiet` does not switch it off (issue #30).
        run(detectable)
        command = auto_editor.commands[0]
        assert command[command.index("--progress") + 1] == "none"

    def test_the_line_auto_editor_blanks_before_the_document_is_tolerated(
        self, auto_editor, detectable
    ):
        auto_editor.output = CLEARED_LINE + timeline_json()
        run(detectable)
        intervals = [(item.start, item.end) for item in read_cuts(detectable).cuts]
        assert intervals == [(30.3, 30.8), (50.3, 59.7)]

    def test_silence_is_what_the_kept_clips_leave_between_them(
        self, auto_editor, detectable
    ):
        result = run(detectable)
        # 0.5s falls under min_duration; the other three are detected.
        assert result.silence_detected == 3

    def test_each_cut_is_shrunk_by_the_padding_on_both_sides(
        self, auto_editor, detectable
    ):
        run(detectable)
        intervals = [(item.start, item.end) for item in read_cuts(detectable).cuts]
        assert intervals == [(30.3, 30.8), (50.3, 59.7)]

    def test_a_silence_too_short_once_padded_is_not_offered(
        self, auto_editor, detectable
    ):
        result = run(detectable)
        assert result.silence_dropped == 1
        assert all(item.start != 12.3 for item in read_cuts(detectable).cuts)

    def test_exactly_min_cut_duration_survives_and_a_millisecond_less_does_not(self):
        silence = Profile().silence
        gaps = [Span(10.0, 11.0), Span(20.0, 20.999)]
        kept, dropped = _autoeditor.pad_spans(gaps, silence)
        assert [(span.start, span.end) for span in kept] == [(10.3, 10.7)]
        assert dropped == 1

    def test_a_silence_shorter_than_min_duration_is_never_detected(self):
        kept = [Span(0.0, 10.0), Span(10.5, 20.0)]
        assert _autoeditor.silence_spans(kept, 20.0, 0.6) == []
        assert len(_autoeditor.silence_spans(kept, 20.0, 0.5)) == 1

    def test_the_timebase_auto_editor_really_uses_is_read_as_a_fraction(
        self, auto_editor, detectable
    ):
        auto_editor.output = timeline_json(((0.0, 9.0), (30.0, DURATION)), timebase=30)
        run(detectable)
        intervals = [(item.start, item.end) for item in read_cuts(detectable).cuts]
        assert intervals == [(9.3, 29.7)]

    def test_trailing_silence_runs_to_the_end_of_the_material(
        self, auto_editor, detectable
    ):
        auto_editor.output = timeline_json(((0.0, 100.0),))
        run(detectable)
        assert read_cuts(detectable).cuts[-1].end == pytest.approx(DURATION - 0.3)

    def test_silence_candidates_are_approved_from_the_start(
        self, auto_editor, detectable
    ):
        run(detectable)
        cuts = read_cuts(detectable).cuts
        assert {item.status for item in cuts} == {"approved"}
        assert {item.reason for item in cuts} == {"silence"}


class TestTimelineSchema:
    """A v3 document vidprep does not recognise stops the stage (REQ-003)."""

    def test_an_unknown_key_is_refused_rather_than_ignored(self):
        raw = timeline_json(chapters=[])
        with pytest.raises(TimelineSchemaError, match="chapters"):
            _autoeditor.parse_timeline(raw, AUTO_EDITOR_VERSION)

    def test_an_unknown_key_inside_a_clip_is_refused(self):
        document = json.loads(timeline_json())
        document["a"][0][0]["kind"] = "audio"
        with pytest.raises(TimelineSchemaError, match="kind"):
            _autoeditor.parse_timeline(json.dumps(document), AUTO_EDITOR_VERSION)

    def test_a_future_export_version_is_refused(self):
        raw = timeline_json()
        with pytest.raises(TimelineSchemaError):
            _autoeditor.parse_timeline(
                raw.replace('"version": "3"', '"version": "4"'), AUTO_EDITOR_VERSION
            )

    def test_output_that_is_not_json_at_all_is_refused(self):
        with pytest.raises(TimelineSchemaError):
            _autoeditor.parse_timeline("no timeline here", AUTO_EDITOR_VERSION)

    def test_the_message_names_the_version_that_produced_it(self):
        with pytest.raises(TimelineSchemaError, match=AUTO_EDITOR_VERSION):
            _autoeditor.parse_timeline("{}", AUTO_EDITOR_VERSION)

    def test_a_timebase_that_cannot_be_read_is_refused(self):
        raw = timeline_json().replace('"1000/1"', '"thirty"')
        timeline = _autoeditor.parse_timeline(raw, AUTO_EDITOR_VERSION)
        with pytest.raises(TimelineSchemaError, match="timebase"):
            _autoeditor.kept_spans(timeline)

    def test_a_broken_export_stops_the_run_with_exit_two(
        self, auto_editor, detectable, run_cli
    ):
        auto_editor.output = timeline_json(chapters=[])
        result = run_cli("detect", "-p", str(detectable), "--json")
        assert result.exit_code == EXIT_EXECUTION
        assert json.loads(result.stdout)["error"] == "timeline_schema"
        assert not (detectable / "cuts.json").exists()


class TestFillerDictionary:
    """The two tiers and where they come from (REQ-010, REQ-011)."""

    def test_the_strong_tier_is_the_six_words_the_design_names(self):
        packaged = _fillers.packaged_dictionary()
        assert packaged.strong == [
            "えー",
            "えーと",
            "えっと",
            "あのー",
            "そのー",
            "うーん",
        ]

    def test_the_weak_tier_is_the_three_words_the_design_names(self):
        assert _fillers.packaged_dictionary().weak == ["まあ", "なんか", "こう"]

    def test_the_weak_tier_is_not_matched_unless_it_is_asked_for(self):
        words = _fillers.packaged_dictionary().words(enable_weak=False)
        assert "なんか" not in dict(words)
        assert "なんか" in dict(_fillers.packaged_dictionary().words(enable_weak=True))

    def test_the_longest_word_is_offered_first_so_it_wins(self):
        words = _fillers.packaged_dictionary().words(enable_weak=True)
        lengths = [len(word) for word, _ in words]
        assert lengths == sorted(lengths, reverse=True)

    def test_a_project_adds_entries_instead_of_replacing_them(self, tmp_path):
        (tmp_path / "dictionaries").mkdir()
        (tmp_path / "dictionaries" / "fillers.json").write_text(
            json.dumps({"version": "1", "strong": ["ええと"], "weak": []}),
            encoding="utf-8",
        )
        loaded = _fillers.load_dictionary(tmp_path)
        assert "ええと" in loaded.strong
        assert "えーと" in loaded.strong

    def test_a_project_dictionary_that_lost_its_shape_is_reported(self, tmp_path):
        (tmp_path / "dictionaries").mkdir()
        (tmp_path / "dictionaries" / "fillers.json").write_text("{}", encoding="utf-8")
        with pytest.raises(SchemaInvalidError, match=r"fillers\.json"):
            _fillers.load_dictionary(tmp_path)

    def test_punctuation_and_held_vowels_do_not_hide_a_filler(self):
        words = _fillers.packaged_dictionary().words(enable_weak=False)
        assert _fillers.scan("えーーーと、", words).is_whole
        assert _fillers.scan("えー、えーと。", words).is_whole

    def test_the_longest_match_wins_at_each_position(self):
        words = _fillers.packaged_dictionary().words(enable_weak=False)
        reading = _fillers.scan("えーと", words)
        assert [hit.word for hit in reading.hits] == ["えーと"]


class TestFillerDetection:
    """Which fillers become candidates (REQ-012, REQ-013, REQ-014, REQ-015)."""

    @pytest.fixture
    def quiet(self, auto_editor: FakeAutoEditor) -> FakeAutoEditor:
        """A timeline with no silence at all, so only fillers make cuts."""
        auto_editor.output = timeline_json(((0.0, DURATION),))
        return auto_editor

    def test_a_segment_that_is_only_filler_takes_the_silence_with_it(
        self, quiet, detectable
    ):
        write_transcript(
            detectable,
            [
                (90.0, 95.0, "本題に入ります"),
                (100.0, 100.6, "えーと"),
                (105.0, 110.0, "続きです"),
            ],
        )
        write_vad(detectable, [(90.0, 95.0), (100.0, 100.6), (105.0, 110.0)])
        run(detectable)
        cuts = read_cuts(detectable).cuts
        assert [(item.start, item.end) for item in cuts] == [(99.7, 100.9)]
        assert cuts[0].reason == "filler"
        assert cuts[0].status == "proposed"

    def test_a_filler_at_the_start_is_cut_to_the_speech_region_boundary(
        self, quiet, detectable
    ):
        write_transcript(
            detectable,
            [(190.0, 195.0, "前の話"), (200.0, 210.0, "えーと、今日の本題です")],
        )
        write_vad(detectable, [(190.0, 195.0), (200.0, 200.5), (201.0, 210.0)])
        run(detectable)
        cuts = read_cuts(detectable).cuts
        assert [(item.start, item.end) for item in cuts] == [(199.7, 200.8)]

    def test_a_filler_at_the_end_is_cut_to_the_speech_region_boundary(
        self, quiet, detectable
    ):
        write_transcript(
            detectable,
            [(220.0, 230.0, "今日はここまでです、えーと"), (240.0, 245.0, "次の話")],
        )
        write_vad(detectable, [(220.0, 228.5), (229.0, 230.0), (240.0, 245.0)])
        run(detectable)
        cuts = read_cuts(detectable).cuts
        assert [(item.start, item.end) for item in cuts] == [(228.7, 230.3)]

    def test_a_filler_in_the_middle_is_reported_and_left_alone(self, quiet, detectable):
        write_transcript(
            detectable, [(100.0, 110.0, "今日はえーとクロードコードの話です")]
        )
        write_vad(detectable, [(100.0, 110.0)])
        result = run(detectable)
        assert result.in_sentence == 1
        assert read_cuts(detectable).cuts == []

    def test_an_edge_filler_without_enough_silence_beside_it_is_not_cut(
        self, quiet, detectable
    ):
        write_transcript(
            detectable,
            [(199.9, 199.95, "はい"), (200.0, 210.0, "えーと、今日の本題です")],
        )
        write_vad(detectable, [(199.9, 199.95), (200.0, 200.5), (201.0, 210.0)])
        assert read_cuts_after(detectable) == []

    def test_exactly_the_required_silence_is_enough(self, quiet, detectable):
        write_transcript(
            detectable,
            [(199.6, 199.8, "はい"), (200.0, 210.0, "えーと、今日の本題です")],
        )
        write_vad(detectable, [(199.6, 199.8), (200.0, 200.5), (201.0, 210.0)])
        assert len(read_cuts_after(detectable)) == 1

    def test_an_edge_filler_the_vad_never_split_is_not_guessed_at(
        self, quiet, detectable
    ):
        write_transcript(detectable, [(200.0, 210.0, "えーと、今日の本題です")])
        write_vad(detectable, [(200.0, 210.0)])
        assert read_cuts_after(detectable) == []

    def test_the_weak_tier_only_proposes_cuts_when_it_is_enabled(
        self, quiet, detectable
    ):
        write_transcript(
            detectable,
            [(90.0, 95.0, "本題"), (100.0, 100.6, "なんか"), (105.0, 110.0, "続き")],
        )
        write_vad(detectable, [(90.0, 95.0), (100.0, 100.6), (105.0, 110.0)])
        assert read_cuts_after(detectable) == []
        enable_weak(detectable)
        assert len(read_cuts_after(detectable)) == 1

    def test_a_filler_note_says_which_word_and_where(self, quiet, detectable):
        write_transcript(
            detectable,
            [(90.0, 95.0, "本題"), (100.0, 100.6, "えーと"), (105.0, 110.0, "続き")],
        )
        write_vad(detectable, [(90.0, 95.0), (100.0, 100.6), (105.0, 110.0)])
        run(detectable)
        assert "えーと" in (read_cuts(detectable).cuts[0].note or "")

    def test_the_report_counts_the_fillers_this_run_detected(self, quiet, detectable):
        write_transcript(
            detectable,
            [(90.0, 95.0, "本題"), (100.0, 100.6, "えーと"), (105.0, 110.0, "続き")],
        )
        write_vad(detectable, [(90.0, 95.0), (100.0, 100.6), (105.0, 110.0)])
        payload = run(detectable).to_dict()["filler"]
        assert payload["candidates"] == 1
        assert payload["total_sec"] == pytest.approx(1.2)

    def test_a_filler_cut_never_overlaps_the_silence_cut_beside_it(
        self, auto_editor, detectable
    ):
        # auto-editor heard the quiet up to 100.2, past where the VAD placed
        # the start of the filler, so the two candidates would overlap.
        auto_editor.output = timeline_json(((0.0, 95.0), (100.2, DURATION)))
        write_transcript(
            detectable,
            [(90.0, 95.0, "本題"), (100.0, 100.6, "えーと"), (105.0, 110.0, "続き")],
        )
        write_vad(detectable, [(90.0, 95.0), (100.0, 100.6), (105.0, 110.0)])
        run(detectable)
        intervals = [(item.start, item.end) for item in read_cuts(detectable).cuts]
        assert intervals == [(95.3, 99.9), (99.9, 100.9)]

    def test_a_filler_the_silence_cuts_already_remove_is_not_offered_twice(self):
        speech = _fillers.Speech(
            segments=(
                Segment(id="s0001", start=90.0, end=95.0, text="本題"),
                Segment(id="s0002", start=100.0, end=100.6, text="えーと"),
            ),
            regions=(Span(90.0, 95.0), Span(100.0, 100.6)),
            duration=DURATION,
        )
        candidates, _ = _fillers.candidates(
            speech,
            Profile(),
            _fillers.packaged_dictionary(),
            [Span(99.0, 101.0)],
        )
        assert candidates == []

    def test_without_a_transcript_only_silence_is_detected(
        self, auto_editor, detectable
    ):
        result = run(detectable)
        assert len(result.warnings) == 1
        assert "transcript.json" in result.warnings[0]
        assert {item.reason for item in read_cuts(detectable).cuts} == {"silence"}

    def test_a_transcript_without_its_speech_regions_is_not_used(
        self, auto_editor, detectable
    ):
        write_transcript(detectable, [(100.0, 110.0, "えーと")])
        result = run(detectable)
        assert len(result.warnings) == 1
        assert "vad.json" in result.warnings[0]


def read_cuts_after(root: Path) -> list[Cut]:
    """Run detection and return the cuts it wrote."""
    run(root)
    return list(read_cuts(root).cuts)


def enable_weak(root: Path) -> None:
    """Turn on the weak filler tier in the project's profile."""
    path = root / "profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["filler"]["enable_weak"] = True
    path.write_text(json.dumps(profile), encoding="utf-8")


class TestMergeRules:
    """The rules that keep a review alive across runs (REQ-020 to REQ-023)."""

    def test_an_iou_of_exactly_a_half_is_the_same_cut(self):
        merged = detect_module.merge([cut("c0001", 0.0, 10.0)], [candidate(0.0, 20.0)])
        assert merged.matched == 1
        assert merged.cuts[0].id == "c0001"

    def test_an_iou_just_under_a_half_is_a_different_cut(self):
        merged = detect_module.merge([cut("c0001", 0.0, 10.0)], [candidate(0.0, 20.04)])
        assert merged.matched == 0
        assert merged.cuts[0].id == "c0002"

    def test_an_iou_just_over_a_half_is_the_same_cut(self):
        merged = detect_module.merge([cut("c0001", 0.0, 10.0)], [candidate(0.0, 19.96)])
        assert merged.matched == 1

    def test_a_match_keeps_the_verdict_and_takes_the_new_interval(self):
        existing = cut("c0007", 45.1, 45.9, status="rejected", note="keep the pause")
        merged = detect_module.merge([existing], [candidate(45.05, 45.95)])
        (updated,) = merged.cuts
        assert (updated.id, updated.status, updated.note) == (
            "c0007",
            "rejected",
            "keep the pause",
        )
        assert (updated.start, updated.end) == (45.05, 45.95)
        assert updated.confidence == 0.95

    def test_an_unmatched_verdict_is_kept_whatever_it_says(self):
        existing = [
            cut("c0001", 10.0, 11.0, status="approved"),
            cut("c0002", 20.0, 21.0, status="rejected"),
            cut("c0003", 30.0, 31.0, reason=detect_module.MANUAL),
            cut("c0004", 40.0, 41.0, status="proposed"),
        ]
        merged = detect_module.merge(existing, [])
        assert [item.id for item in merged.cuts] == ["c0001", "c0002", "c0003"]
        assert merged.dropped_proposed == 1
        assert merged.kept_unmatched == 3

    def test_a_withdrawn_proposal_does_not_lend_its_number_to_anything(self):
        existing = [cut("c0001", 10.0, 11.0), cut("c0002", 20.0, 21.0)]
        merged = detect_module.merge(existing, [candidate(100.0, 101.0)])
        assert [item.id for item in merged.cuts] == ["c0003"]
        assert merged.next_id == "c0004"

    def test_numbering_continues_above_a_hand_written_cut(self):
        existing = [cut("c0100", 5.0, 6.0, reason=detect_module.MANUAL)]
        merged = detect_module.merge(existing, [candidate(50.0, 51.0)])
        assert sorted(item.id for item in merged.cuts) == ["c0100", "c0101"]

    def test_only_cuts_of_the_same_reason_are_matched(self):
        existing = [
            cut("c0001", 10.0, 11.0, reason=detect_module.FILLER, status="rejected")
        ]
        merged = detect_module.merge(existing, [candidate(10.0, 11.0)])
        assert merged.matched == 0
        assert [(item.id, item.reason) for item in merged.cuts] == [
            ("c0001", "filler"),
            ("c0002", "silence"),
        ]

    def test_the_first_run_numbers_from_one(self):
        merged = detect_module.merge([], [candidate(10.0, 11.0), candidate(20.0, 21.0)])
        assert [item.id for item in merged.cuts] == ["c0001", "c0002"]

    def test_each_existing_cut_is_matched_at_most_once(self):
        existing = [cut("c0001", 10.0, 20.0)]
        merged = detect_module.merge(
            existing, [candidate(10.0, 20.0), candidate(10.1, 20.1)]
        )
        assert merged.matched == 1
        assert sorted(item.id for item in merged.cuts) == ["c0001", "c0002"]

    def test_cuts_come_out_in_timeline_order(self):
        merged = detect_module.merge(
            [cut("c0001", 100.0, 101.0, status="approved")],
            [candidate(10.0, 11.0), candidate(50.0, 51.0)],
        )
        starts = [item.start for item in merged.cuts]
        assert starts == sorted(starts)

    def test_a_fresh_cut_gives_way_to_an_approved_one_it_overlaps(self):
        existing = [cut("c0001", 10.0, 20.0, status="approved")]
        merged = detect_module.merge(existing, [candidate(15.0, 25.0)])
        by_id = {item.id: item for item in merged.cuts}
        assert by_id["c0001"].status == "approved"
        assert by_id["c0002"].status == "proposed"
        assert merged.demoted == ("c0002",)


class TestReRun:
    """Detection run twice, the way the parameters are meant to be tuned."""

    def test_a_verdict_survives_a_padding_change(self, auto_editor, detectable):
        run(detectable)
        first = read_cuts(detectable).cuts
        write_cuts(
            detectable,
            [
                {**item.model_dump(mode="json"), "status": "rejected", "note": "no"}
                if item.id == "c0001"
                else item.model_dump(mode="json")
                for item in first
            ],
        )
        widen_padding(detectable)
        run(detectable)
        after = {item.id: item for item in read_cuts(detectable).cuts}
        assert after["c0001"].status == "rejected"
        assert after["c0001"].note == "no"
        assert after["c0001"].start == 30.25

    def test_nothing_is_renumbered_when_nothing_changed(self, auto_editor, detectable):
        run(detectable)
        before = [item.model_dump(mode="json") for item in read_cuts(detectable).cuts]
        result = run(detectable)
        after = [item.model_dump(mode="json") for item in read_cuts(detectable).cuts]
        assert before == after
        assert result.merged.added == 0
        assert result.merged.matched == len(before)


def widen_padding(root: Path) -> None:
    """Loosen the padding the way a review of the boundaries would."""
    path = root / "profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["silence"]["pad_pre"] = 0.25
    profile["silence"]["pad_post"] = 0.25
    path.write_text(json.dumps(profile), encoding="utf-8")


class TestSpeechSafety:
    """The cross-check that makes unattended detection safe (REQ-041)."""

    def test_a_cut_that_would_remove_speech_stops_the_run(
        self, auto_editor, detectable
    ):
        auto_editor.output = timeline_json(((0.0, 30.0), (60.0, DURATION)))
        write_transcript(detectable, [(29.0, 31.0, "話しています")])
        write_vad(detectable, [(29.0, 31.0)])
        with pytest.raises(InvariantViolationError, match="speech"):
            run(detectable)
        assert not (detectable / "cuts.json").exists()

    def test_the_padding_may_graze_a_word(self, auto_editor, detectable):
        auto_editor.output = timeline_json(((0.0, 30.0), (60.0, DURATION)))
        write_transcript(detectable, [(29.0, 30.5, "話しています")])
        write_vad(detectable, [(29.0, 30.5)])
        result = run(detectable)
        assert result.max_speech_overlap == pytest.approx(0.2)

    def test_a_segment_timed_over_silence_is_reported_not_deleted(
        self, auto_editor, detectable
    ):
        auto_editor.output = timeline_json(((0.0, 30.0), (60.0, DURATION)))
        # The recogniser stopped the segment at the end of a later region.
        write_transcript(detectable, [(20.0, 70.0, "ひとこと")])
        write_vad(detectable, [(20.0, 25.0), (65.0, 70.0)])
        result = run(detectable)
        assert result.flagged_segments == ("s0001",)
        assert len(read_cuts(detectable).cuts) == 1

    def test_a_cut_and_a_word_that_do_not_meet_report_no_overlap(
        self, auto_editor, detectable
    ):
        write_transcript(detectable, [(0.0, 10.0, "話しています")])
        write_vad(detectable, [(0.0, 10.0)])
        result = run(detectable)
        assert result.max_speech_overlap == 0.0
        assert result.flagged_segments == ()

    def test_without_a_transcript_the_cuts_are_reported_as_unchecked(
        self, auto_editor, detectable
    ):
        result = run(detectable)
        assert result.max_speech_overlap is None
        assert result.to_dict()["speech"]["max_overlap_sec"] is None


class TestInvariants:
    """What is written is what the schema allows (REQ-040)."""

    def test_two_approved_cuts_never_overlap_in_the_written_file(
        self, auto_editor, detectable
    ):
        write_cuts(
            detectable,
            [
                {
                    "id": "c0100",
                    "start": 50.0,
                    "end": 55.0,
                    "reason": "manual",
                    "confidence": 1.0,
                    "status": "approved",
                    "note": None,
                }
            ],
        )
        run(detectable)
        Cuts.model_validate(
            json.loads((detectable / "cuts.json").read_text(encoding="utf-8")),
            context={"duration": DURATION},
        )

    def test_a_cut_past_the_end_of_the_material_is_refused(
        self, auto_editor, detectable, monkeypatch
    ):
        monkeypatch.setattr(
            _autoeditor,
            "pad_spans",
            lambda *_: ([Span(DURATION - 1.0, DURATION + 5.0)], 0),
        )
        with pytest.raises(SchemaInvalidError, match=r"cuts\.json"):
            run(detectable)

    def test_the_stage_is_recorded_with_the_tool_that_produced_it(
        self, auto_editor, detectable
    ):
        run(detectable)
        manifest = project_module.load_project(detectable).manifest
        assert manifest.stages["detect"].tool_versions == {
            "auto_editor": AUTO_EDITOR_VERSION
        }

    def test_a_cuts_file_that_lost_its_shape_stops_the_run(
        self, auto_editor, detectable
    ):
        (detectable / "cuts.json").write_text('{"version": "1"', encoding="utf-8")
        with pytest.raises(SchemaInvalidError):
            run(detectable)


class TestFailures:
    """What detection refuses to start on."""

    def test_missing_processed_audio_asks_for_audio_fix(self, auto_editor, project_dir):
        with pytest.raises(UsageError, match="audio-fix"):
            run(project_dir)

    def test_an_unusable_auto_editor_is_reported_before_anything_runs(
        self, detectable, monkeypatch
    ):
        monkeypatch.setattr(
            doctor_module,
            "check_auto_editor",
            lambda: {"ok": False, "error": "auto-editor not found in PATH"},
        )
        with pytest.raises(UsageError, match="auto-editor"):
            run(detectable)

    def test_more_cuts_than_the_identifier_can_express_are_refused(self):
        existing = [cut("c9999", 10.0, 11.0, status="approved")]
        with pytest.raises(InvariantViolationError, match="9999"):
            detect_module.merge(existing, [candidate(100.0, 101.0)])


class TestCommandLine:
    """The subcommand, its dry run and its exit codes."""

    def test_the_dry_run_shows_the_command_and_writes_nothing(
        self, auto_editor, detectable, run_cli
    ):
        result = run_cli("detect", "-p", str(detectable), "--dry-run")
        assert result.exit_code == EXIT_OK
        assert "auto-editor" in result.stdout
        assert "--export v3" in result.stdout.replace("\n", " ")
        assert not (detectable / "cuts.json").exists()

    def test_the_json_report_carries_the_numbers_the_review_needs(
        self, auto_editor, detectable, run_cli
    ):
        write_transcript(detectable, [(0.0, 9.0, "こんにちは")])
        write_vad(detectable, [(0.0, 9.0)])
        result = run_cli("detect", "-p", str(detectable), "--json")
        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)
        assert payload["auto_editor_version"] == AUTO_EDITOR_VERSION
        assert payload["silence"] == {
            "detected": 3,
            "after_padding": 2,
            "dropped_by_min_cut_duration": 1,
            "total_sec": 9.9,
            "status": "approved",
        }
        assert payload["filler"]["weak_enabled"] is False
        assert payload["cuts_total"] == 2
        assert payload["id_range"] == ["c0001", "c0002"]
        assert payload["next_id"] == "c0003"

    def test_a_cut_that_would_remove_speech_exits_three(
        self, auto_editor, detectable, run_cli
    ):
        auto_editor.output = timeline_json(((0.0, 30.0), (60.0, DURATION)))
        write_transcript(detectable, [(29.0, 31.0, "話しています")])
        write_vad(detectable, [(29.0, 31.0)])
        result = run_cli("detect", "-p", str(detectable), "--json")
        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout)["error"] == "invariant_violated"

    def test_a_project_without_processed_audio_exits_one(
        self, auto_editor, project_dir, run_cli
    ):
        result = run_cli("detect", "-p", str(project_dir))
        assert result.exit_code == EXIT_USAGE

    def test_the_stage_is_registered_as_a_command(self):
        registered = {
            command.callback.__name__
            for command in cli.app.registered_commands
            if command.callback is not None
        }

        assert "detect" in registered


class TestIntervalArithmetic:
    """The helpers the rest of the module is built on."""

    def test_touching_intervals_become_one(self):
        merged = _intervals.merge_spans(
            [Span(0.0, 1.0), Span(1.0, 2.0), Span(5.0, 6.0)]
        )
        assert [(item.start, item.end) for item in merged] == [(0.0, 2.0), (5.0, 6.0)]

    def test_a_hole_punched_in_the_middle_leaves_two_pieces(self):
        pieces = _intervals.subtract(Span(0.0, 10.0), [Span(4.0, 6.0)])
        assert [(item.start, item.end) for item in pieces] == [(0.0, 4.0), (6.0, 10.0)]

    def test_intervals_that_do_not_meet_have_no_union_overlap(self):
        assert _intervals.intersection_over_union(Span(0, 1), Span(2, 3)) == 0.0

    def test_identical_intervals_overlap_completely(self):
        assert _intervals.intersection_over_union(Span(0, 1), Span(0, 1)) == 1.0
