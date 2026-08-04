"""Tests for the report stage.

ffmpeg and ffprobe are replaced by :class:`FakeTools`, which records every
command line it was given, answers analysis passes with the logs the real
filters print, and creates the file a writing command would have written. The
suite therefore asserts on the exact windows, filters and concat plan vidprep
asks for while staying runnable on CI, where neither tool is installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from vidprep import _boundaries, _ffmpeg, _review, audio, report
from vidprep import project as project_module
from vidprep.errors import EXIT_OK, FfmpegError
from vidprep.models import Cut

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vidprep.project import Project

SOURCE_SECONDS = 298.92
RENDERED_SECONDS = 197.508

SOURCE_NAME = "clip.mp4"
PROCESSED_NAME = "processed.wav"
OUTPUT_NAME = "output.mp4"
DIGEST_NAME = _boundaries.DIGEST_NAME.name

#: What ffprobe reports for the digest the fake stitched together.
DIGEST_SECONDS = 21.94

#: Integrated loudness the fake reports for each file it is asked about.
LOUDNESS = {SOURCE_NAME: "-22.24", PROCESSED_NAME: "-14.06", OUTPUT_NAME: "-14.08"}

#: Noise-floor RMS the fake reports over the silence of each file.
NOISE_FLOOR = {SOURCE_NAME: "-45.87", PROCESSED_NAME: "-37.05"}

SILENCE_LOG = """\
[silencedetect @ 0x1] silence_start: 10.0
[silencedetect @ 0x1] silence_end: 12.0 | silence_duration: 2.0
"""

CUTS = [
    {
        "id": "c0001",
        "start": 0.5,
        "end": 1.2,
        "reason": "silence",
        "confidence": 0.95,
        "status": "approved",
    },
    {
        "id": "c0002",
        "start": 10.0,
        "end": 12.0,
        "reason": "silence",
        "confidence": 0.95,
        "status": "approved",
    },
    {
        "id": "c0003",
        "start": 45.1,
        "end": 46.0,
        "reason": "filler",
        "confidence": 0.7,
        "status": "proposed",
        "note": "filler plus the silence around it",
    },
    {
        "id": "c0004",
        "start": 296.0,
        "end": 298.0,
        "reason": "silence",
        "confidence": 0.95,
        "status": "rejected",
    },
]

SEGMENTS = [
    {"id": "s0002", "start": 1.2, "end": 3.0, "text": "本日はよろしくお願いします"},
    {"id": "s0003", "start": 10.5, "end": 11.5, "text": "この部分は消えます"},
    {"id": "s0004", "start": 43.2, "end": 45.1, "text": "じゃあ動かしてみましょう"},
    {"id": "s0005", "start": 45.1, "end": 46.0, "text": "えーと"},
    {"id": "s0006", "start": 46.0, "end": 48.4, "text": "まずインストールからです"},
    {"id": "s0007", "start": 100.0, "end": 100.5, "text": "とても" * 6},
]

TRANSCRIPT = {
    "version": "1",
    "audio_source": "audio/processed.wav",
    "asr": {"backend": "whisper.cpp", "model": "large-v3-turbo", "vad": "silero-v5"},
    "segments": SEGMENTS,
}


def loudnorm_log(integrated: str) -> str:
    """Render the JSON block the loudnorm filter prints to stderr."""
    report_json = {
        "input_i": integrated,
        "input_tp": "-1.31",
        "input_lra": "8.9",
        "input_thresh": "-24.0",
        "target_offset": "0.1",
    }
    return f"[Parsed_loudnorm_1 @ 0x1] \n{json.dumps(report_json, indent=1)}\n"


def astats_log(level: str) -> str:
    """Render the astats overall section the way ffmpeg prints it."""
    return f"[Parsed_astats_2 @ 0x1] Overall\n[Parsed_astats_2 @ 0x1] RMS level dB: {level}\n"


class FakeTools:
    """Stand-in for ffmpeg and ffprobe, keyed by the file each command reads."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.silence = SILENCE_LOG
        self.rendered_seconds = RENDERED_SECONDS
        self.digest_seconds = DIGEST_SECONDS
        #: Output paths whose command should fail, by file name.
        self.failing: set[str] = set()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the real subprocess wrappers with this fake."""
        monkeypatch.setattr(_ffmpeg, "run", self.run)
        monkeypatch.setattr(_ffmpeg, "run_analysis", self.run_analysis)

    def _inputs(self, args: list[str]) -> list[str]:
        return [
            Path(args[index + 1]).name
            for index, value in enumerate(args)
            if value == "-i"
        ]

    def run(self, args: list[str], timeout: float = 0.0) -> str:
        """Answer ffprobe, or create the file the writing command names."""
        self.commands.append(list(args))
        if args[0] == _ffmpeg.FFPROBE:
            probed = {
                OUTPUT_NAME: self.rendered_seconds,
                DIGEST_NAME: self.digest_seconds,
            }
            seconds = probed.get(Path(args[-1]).name, SOURCE_SECONDS)
            return f"{seconds:.6f}\n"
        target = Path(args[-1])
        if target.name in self.failing:
            msg = f"ffmpeg exited with 1: could not write {target.name}"
            raise FfmpegError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake media")
        return ""

    def run_analysis(self, args: list[str], timeout: float = 0.0) -> str:
        """Answer a measurement pass with the log the real filter would print."""
        self.commands.append(list(args))
        filters = args[args.index("-af") + 1]
        name = self._inputs(args)[0]
        if "silencedetect" in filters:
            return self.silence
        if "astats" in filters:
            return astats_log(NOISE_FLOOR[name])
        return loudnorm_log(LOUDNESS[name])


@pytest.fixture
def fake_tools(monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    """Install the fake media tools for the whole test."""
    tools = FakeTools()
    tools.install(monkeypatch)
    return tools


def write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Write *payload* as the artifact at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def prepared(project_dir: Path) -> Path:
    """A project with cuts, a transcript, processed audio and a rendered output."""
    write_json(project_dir / "cuts.json", {"version": "1", "cuts": CUTS})
    write_json(project_dir / "transcript.json", TRANSCRIPT)
    for name in ("audio/processed.wav", "out/output.mp4"):
        path = project_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake media")
    return project_dir


@pytest.fixture
def loaded(prepared: Path) -> Project:
    """The prepared project, loaded."""
    return project_module.load_project(prepared)


def stats_of(root: Path) -> dict[str, Any]:
    """Return the statistics document the last run wrote."""
    loaded: dict[str, Any] = json.loads(
        (root / report.STATS_NAME).read_text(encoding="utf-8")
    )
    return loaded


# --------------------------------------------------------------------------- #
#  Windows (design.md §5.6: ±2 seconds around every cut)
# --------------------------------------------------------------------------- #


class TestWindows:
    def test_a_window_spans_two_seconds_on_each_side_of_the_cut(self):
        cut = Cut(id="c0002", start=10.0, end=12.0, reason="silence")

        window = _boundaries.windows([cut], SOURCE_SECONDS)[0]

        assert (window.start, window.end) == (8.0, 14.0)

    def test_a_window_at_the_start_of_the_material_is_clamped_to_zero(self):
        cut = Cut(id="c0001", start=0.5, end=1.2, reason="silence")

        window = _boundaries.windows([cut], SOURCE_SECONDS)[0]

        assert (window.start, window.end) == (0.0, 3.2)

    def test_a_window_at_the_end_of_the_material_is_clamped_to_the_duration(self):
        cut = Cut(id="c0009", start=296.0, end=298.0, reason="silence")

        window = _boundaries.windows([cut], SOURCE_SECONDS)[0]

        assert (window.start, window.end) == (294.0, SOURCE_SECONDS)

    def test_cuts_closer_than_the_margin_keep_one_window_each(self):
        cuts = [
            Cut(id="c0001", start=10.0, end=10.5, reason="silence"),
            Cut(id="c0002", start=11.0, end=11.5, reason="silence"),
        ]

        produced = _boundaries.windows(cuts, SOURCE_SECONDS)

        assert [(window.start, window.end) for window in produced] == [
            (8.0, 12.5),
            (9.0, 13.5),
        ]

    def test_windows_are_ordered_by_start_whatever_order_the_cuts_arrive_in(self):
        cuts = [
            Cut(id="c0002", start=20.0, end=21.0, reason="silence"),
            Cut(id="c0001", start=10.0, end=11.0, reason="silence"),
        ]

        produced = _boundaries.windows(cuts, SOURCE_SECONDS)

        assert [window.cut_id for window in produced] == ["c0001", "c0002"]

    def test_the_image_name_carries_the_cut_id(self):
        cut = Cut(id="c0007", start=10.0, end=11.0, reason="silence")

        assert _boundaries.windows([cut], SOURCE_SECONDS)[0].image_name == "c0007.png"

    def test_a_quote_in_the_path_is_escaped_for_the_concat_demuxer(self):
        listing = _boundaries.concat_listing([Path("/work/it's/clip.mp4")])

        assert listing == "file '/work/it'\\''s/clip.mp4'\n"


# --------------------------------------------------------------------------- #
#  stats.json
# --------------------------------------------------------------------------- #


class TestStats:
    def test_the_durations_and_the_reduction_ratio_are_reported(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert result.stats["duration"] == {
            "source": SOURCE_SECONDS,
            "rendered": RENDERED_SECONDS,
            "reduction_ratio": round(1 - RENDERED_SECONDS / SOURCE_SECONDS, 3),
        }

    def test_cuts_are_broken_down_by_reason_and_by_status(self, fake_tools, loaded):
        result = report.run_report(loaded)

        assert result.stats["cuts"]["by_reason"]["silence"] == {
            "count": 3,
            "sec": 4.7,
            "approved": 2,
            "proposed": 0,
            "rejected": 1,
        }
        assert result.stats["cuts"]["by_reason"]["filler"]["count"] == 1

    def test_every_documented_reason_is_present_even_at_zero(self, fake_tools, loaded):
        result = report.run_report(loaded)

        assert result.stats["cuts"]["by_reason"]["manual"] == {
            "count": 0,
            "sec": 0.0,
            "approved": 0,
            "proposed": 0,
            "rejected": 0,
        }

    def test_a_reason_only_a_human_wrote_gets_its_own_entry(self, fake_tools, loaded):
        write_json(
            loaded.root / "cuts.json",
            {
                "version": "1",
                "cuts": [
                    {
                        "id": "c0010",
                        "start": 5.0,
                        "end": 6.0,
                        "reason": "restart",
                        "status": "proposed",
                    }
                ],
            },
        )

        result = report.run_report(loaded)

        assert result.stats["cuts"]["by_reason"]["restart"]["count"] == 1

    def test_only_approved_cuts_count_towards_the_length_that_would_be_removed(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert result.stats["cuts"]["approved_total_sec"] == 2.7

    def test_loudness_is_reported_before_and_after_with_the_target(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert result.stats["loudness"] == {
            "source": -22.24,
            "processed": -14.06,
            "rendered": -14.08,
            "target": -14.0,
            "tolerance": 0.5,
        }

    def test_the_noise_floor_is_reported_level_matched_as_well_as_absolute(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert result.stats["noise_floor"] == {
            "source": {"rms_db": -45.87, "below_programme_db": 23.63},
            "processed": {"rms_db": -37.05, "below_programme_db": 22.99},
        }

    def test_the_noise_floor_is_read_over_the_same_silence_on_both_sides(
        self, fake_tools, loaded
    ):
        report.run_report(loaded)

        expressions = [
            command[command.index("-af") + 1]
            for command in fake_tools.commands
            if "-af" in command and "astats" in command[command.index("-af") + 1]
        ]
        assert len(expressions) == 2
        assert expressions[0] == expressions[1]

    def test_without_silence_the_noise_floor_is_unknown_and_the_run_says_so(
        self, fake_tools, loaded
    ):
        fake_tools.silence = "[silencedetect @ 0x1] nothing to report\n"

        result = report.run_report(loaded)

        assert result.stats["noise_floor"] == {"source": None, "processed": None}
        assert any("noise floor" in warning for warning in result.warnings)

    def test_a_failed_measurement_is_a_warning_rather_than_a_failure(
        self, fake_tools, loaded, monkeypatch
    ):
        def explode(path, targets, intervals=()):
            msg = "ffmpeg exited with 1: no such filter"
            raise FfmpegError(msg)

        monkeypatch.setattr(audio, "measure", explode)

        result = report.run_report(loaded)

        assert result.stats["loudness"]["source"] is None
        assert any("could not be measured" in warning for warning in result.warnings)

    def test_when_the_silence_cannot_be_detected_the_floor_stays_unknown(
        self, fake_tools, loaded, monkeypatch
    ):
        def explode(path, seconds):
            msg = "ffmpeg exited with 1: no such filter"
            raise FfmpegError(msg)

        monkeypatch.setattr(audio, "detect_silence", explode)

        result = report.run_report(loaded)

        assert result.stats["noise_floor"]["source"] is None
        assert any("silence of the source" in warning for warning in result.warnings)

    def test_a_rendered_output_whose_length_cannot_be_read_stays_null(
        self, fake_tools, loaded, monkeypatch
    ):
        def explode(path):
            msg = "ffprobe exited with 1: invalid data"
            raise FfmpegError(msg)

        monkeypatch.setattr(_ffmpeg, "duration", explode)

        result = report.run_report(loaded)

        assert result.stats["duration"]["rendered"] is None
        assert any("out/output.mp4" in warning for warning in result.warnings)

    def test_the_stats_document_is_written_where_compare_stats_looks_for_it(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert stats_of(loaded.root) == result.stats
        assert stats_of(loaded.root)["version"] == "1"


# --------------------------------------------------------------------------- #
#  Subtitle warnings
# --------------------------------------------------------------------------- #


class TestSubtitleWarnings:
    def test_a_segment_an_approved_cut_swallows_names_the_cut_that_removed_it(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert result.stats["subtitles"]["warnings"]["dropped_by_cut"] == [
            {"segment_id": "s0003", "cut_id": "c0002"}
        ]

    def test_an_entry_below_min_display_is_listed_with_its_threshold(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert result.stats["subtitles"]["warnings"]["min_display"] == [
            {"segment_id": "s0007", "display_sec": 0.5, "threshold": 0.8}
        ]

    def test_an_entry_above_max_cps_is_listed_with_its_reading_speed(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert result.stats["subtitles"]["warnings"]["max_cps"] == [
            {"segment_id": "s0007", "cps": 36.0, "threshold": 8.0}
        ]

    def test_the_entries_that_survive_the_cuts_are_counted(self, fake_tools, loaded):
        result = report.run_report(loaded)

        assert result.stats["subtitles"]["entries"] == len(SEGMENTS) - 1

    def test_a_transcript_without_a_single_warning_still_carries_every_key(
        self, fake_tools, loaded
    ):
        write_json(
            loaded.root / "transcript.json",
            {**TRANSCRIPT, "segments": SEGMENTS[:1]},
        )

        result = report.run_report(loaded)

        assert result.stats["subtitles"]["warnings"] == {
            "dropped_by_cut": [],
            "min_display": [],
            "max_cps": [],
        }

    def test_a_segment_two_touching_cuts_removed_between_them_names_no_single_cut(
        self, fake_tools, loaded
    ):
        write_json(
            loaded.root / "cuts.json",
            {
                "version": "1",
                "cuts": [
                    {
                        "id": "c0001",
                        "start": 10.0,
                        "end": 12.0,
                        "reason": "silence",
                        "status": "approved",
                    },
                    {
                        "id": "c0002",
                        "start": 12.0,
                        "end": 14.0,
                        "reason": "silence",
                        "status": "approved",
                    },
                ],
            },
        )
        write_json(
            loaded.root / "transcript.json",
            {
                **TRANSCRIPT,
                "segments": [
                    {"id": "s0020", "start": 11.0, "end": 12.5, "text": "またぎます"}
                ],
            },
        )

        result = report.run_report(loaded)

        assert result.stats["subtitles"]["warnings"]["dropped_by_cut"] == [
            {"segment_id": "s0020", "cut_id": None}
        ]

    def test_an_entry_squeezed_to_nothing_is_reported_as_short_not_as_fast(
        self, fake_tools, loaded
    ):
        write_json(loaded.root / "cuts.json", {"version": "1", "cuts": []})
        write_json(
            loaded.root / "transcript.json",
            {
                **TRANSCRIPT,
                "segments": [
                    {"id": "s0010", "start": 20.0, "end": 20.001, "text": "あ"},
                    {"id": "s0011", "start": 20.001, "end": 21.0, "text": "い"},
                ],
            },
        )

        result = report.run_report(loaded)

        warnings = result.stats["subtitles"]["warnings"]
        assert warnings["min_display"] == [
            {"segment_id": "s0010", "display_sec": 0.0, "threshold": 0.8}
        ]
        assert warnings["max_cps"] == []

    def test_a_manifest_claiming_a_zero_length_source_maps_nothing(
        self, fake_tools, project_dir
    ):
        manifest = json.loads(
            (project_dir / project_module.MANIFEST_NAME).read_text(encoding="utf-8")
        )
        manifest["source"]["duration"] = 0.0
        write_json(project_dir / project_module.MANIFEST_NAME, manifest)
        write_json(project_dir / "transcript.json", {**TRANSCRIPT, "segments": []})

        result = report.run_report(project_module.load_project(project_dir))

        assert result.stats["subtitles"]["entries"] is None
        assert any("could not be mapped" in warning for warning in result.warnings)

    def test_without_a_transcript_the_keys_are_there_and_the_counts_are_null(
        self, fake_tools, loaded
    ):
        (loaded.root / "transcript.json").unlink()

        result = report.run_report(loaded)

        assert result.stats["subtitles"] == {
            "entries": None,
            "warnings": {"dropped_by_cut": [], "min_display": [], "max_cps": []},
        }


# --------------------------------------------------------------------------- #
#  Waveforms and the digest
# --------------------------------------------------------------------------- #


class TestBoundaryArtifacts:
    def test_one_waveform_is_drawn_per_cut_and_named_after_it(self, fake_tools, loaded):
        report.run_report(loaded)

        produced = sorted(
            path.name for path in (loaded.root / _boundaries.BOUNDARIES_DIR).iterdir()
        )
        assert produced == ["c0001.png", "c0002.png", "c0003.png", "c0004.png"]

    def test_a_waveform_covers_the_cut_plus_two_seconds_on_each_side(
        self, fake_tools, loaded
    ):
        report.run_report(loaded)

        drawn = [
            command
            for command in fake_tools.commands
            if any("showwavespic" in argument for argument in command)
        ]
        first = drawn[0]
        assert first[first.index("-ss") + 1] == "0.000"
        assert first[first.index("-t") + 1] == "3.200"

    def test_the_digest_alternates_a_clip_and_a_black_separator(
        self, fake_tools, loaded
    ):
        report.run_report(loaded)

        listing = next(
            command
            for command in fake_tools.commands
            if "concat" in command and command[0] == _ffmpeg.FFMPEG
        )
        assert listing[listing.index("-i") + 1].endswith(_boundaries.CONCAT_FILE)
        assert (loaded.root / _boundaries.DIGEST_NAME).is_file()

    def test_the_separator_is_half_a_second_of_silent_black(self, fake_tools, loaded):
        report.run_report(loaded)

        separator = next(
            command
            for command in fake_tools.commands
            if any(argument.startswith("color=c=black") for argument in command)
        )
        assert separator[separator.index("-t") + 1] == "0.500"
        assert any("anullsrc" in argument for argument in separator)
        assert "color=c=black:s=1920x1080:r=25/1" in separator

    def test_the_digest_is_expected_to_be_every_window_plus_one_separator_each(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        windows = _boundaries.windows(
            [Cut.model_validate(cut) for cut in CUTS], SOURCE_SECONDS
        )
        expected = sum(window.seconds for window in windows) + len(windows) * 0.5
        assert result.stats["artifacts"]["boundary_digest_expected_sec"] == round(
            expected, 3
        )

    def test_the_digest_that_was_built_is_measured_rather_than_assumed(
        self, fake_tools, loaded
    ):
        result = report.run_report(loaded)

        assert result.stats["artifacts"]["boundary_digest_sec"] == DIGEST_SECONDS

    def test_a_waveform_that_ffmpeg_refuses_costs_that_window_only(
        self, fake_tools, loaded
    ):
        fake_tools.failing = {"c0002.png"}

        result = report.run_report(loaded)

        assert result.stats["artifacts"]["boundaries_png"] == len(CUTS) - 1
        assert result.stats["artifacts"]["boundary_digest"] is not None
        assert any("c0002" in warning for warning in result.warnings)

    def test_waveforms_from_a_previous_cut_set_do_not_survive_a_rerun(
        self, fake_tools, loaded
    ):
        stale = loaded.root / _boundaries.BOUNDARIES_DIR / "c9999.png"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"from an older detect run")

        report.run_report(loaded)

        assert not stale.exists()

    def test_without_processed_audio_the_digest_is_still_built(
        self, fake_tools, loaded
    ):
        (loaded.root / audio.OUTPUT_NAME).unlink()

        result = report.run_report(loaded)

        assert result.stats["artifacts"]["boundaries_png"] == 0
        assert result.stats["artifacts"]["boundary_digest"] is not None

    def test_a_clip_ffmpeg_refuses_costs_that_window_of_the_digest_only(
        self, fake_tools, loaded
    ):
        fake_tools.failing = {"clip-0001.mp4"}

        result = report.run_report(loaded)

        assert result.stats["artifacts"]["boundary_digest"] is not None
        assert any("c0002: digest clip" in warning for warning in result.warnings)

    def test_when_no_clip_can_be_cut_the_digest_is_left_unbuilt(
        self, fake_tools, loaded
    ):
        fake_tools.failing = {f"clip-000{index}.mp4" for index in range(len(CUTS))}

        result = report.run_report(loaded)

        assert result.stats["artifacts"]["boundary_digest"] is None
        assert any("no boundary clip" in warning for warning in result.warnings)

    def test_a_failure_while_stitching_leaves_the_rest_of_the_report_standing(
        self, fake_tools, loaded
    ):
        fake_tools.failing = {_boundaries.SEPARATOR_FILE}

        result = report.run_report(loaded)

        assert result.stats["artifacts"]["boundary_digest"] is None
        assert result.stats["artifacts"]["boundaries_png"] == len(CUTS)
        assert any(
            "the boundary digest was not built" in warning
            for warning in result.warnings
        )


# --------------------------------------------------------------------------- #
#  Boundary conditions
# --------------------------------------------------------------------------- #


class TestBoundaries:
    def test_without_cuts_json_the_cut_sections_are_empty_and_the_run_succeeds(
        self, fake_tools, loaded
    ):
        (loaded.root / "cuts.json").unlink()

        result = report.run_report(loaded)

        assert result.stats["cuts"]["approved_total_sec"] == 0.0
        assert result.stats["artifacts"]["boundary_digest"] is None
        assert any("cuts.json" in warning for warning in result.warnings)

    def test_with_no_cuts_at_all_no_digest_is_generated(self, fake_tools, loaded):
        write_json(loaded.root / "cuts.json", {"version": "1", "cuts": []})

        result = report.run_report(loaded)

        assert result.stats["artifacts"] == {
            "boundaries_png": 0,
            "boundary_digest": None,
            "boundary_digest_sec": None,
            "boundary_digest_expected_sec": None,
        }
        assert any("no cuts to review" in warning for warning in result.warnings)

    def test_a_digest_from_an_earlier_run_is_removed_when_the_cuts_are_gone(
        self, fake_tools, loaded
    ):
        report.run_report(loaded)
        write_json(loaded.root / "cuts.json", {"version": "1", "cuts": []})

        report.run_report(loaded)

        assert not (loaded.root / _boundaries.DIGEST_NAME).exists()

    def test_without_a_rendered_output_the_output_statistics_are_null(
        self, fake_tools, loaded
    ):
        (loaded.root / report.RENDERED_NAME).unlink()

        result = report.run_report(loaded)

        assert result.stats["duration"]["rendered"] is None
        assert result.stats["duration"]["reduction_ratio"] is None
        assert result.stats["loudness"]["rendered"] is None
        assert any("out/output.mp4" in warning for warning in result.warnings)


# --------------------------------------------------------------------------- #
#  Invariants
# --------------------------------------------------------------------------- #


class TestInvariants:
    def test_the_inputs_are_not_touched(self, fake_tools, loaded):
        names = ("cuts.json", "transcript.json", project_module.MANIFEST_NAME)
        before = {
            name: project_module.sha256_file(loaded.root / name) for name in names
        }

        report.run_report(loaded)

        assert {
            name: project_module.sha256_file(loaded.root / name) for name in names
        } == before

    def test_two_runs_of_the_same_project_differ_only_in_when_they_ran(
        self, fake_tools, loaded
    ):
        first = report.run_report(loaded).stats
        second = report.run_report(loaded).stats

        assert first.pop("generated_at") != ""
        assert second.pop("generated_at") != ""
        assert first == second

    def test_the_run_is_not_recorded_as_a_stage(self, fake_tools, loaded):
        report.run_report(loaded)

        reloaded = project_module.load_project(loaded.root)
        assert report.STAGE not in reloaded.manifest.stages


# --------------------------------------------------------------------------- #
#  --cuts
# --------------------------------------------------------------------------- #


class TestReview:
    def test_a_candidate_is_shown_with_what_it_deletes_and_what_surrounds_it(
        self, fake_tools, loaded
    ):
        listing = report.run_review(loaded).to_dict()

        filler = next(entry for entry in listing["cuts"] if entry["id"] == "c0003")
        assert [item["segment_id"] for item in filler["removed"]] == ["s0005"]
        assert filler["before"]["segment_id"] == "s0004"
        assert filler["after"]["segment_id"] == "s0006"

    def test_the_listing_carries_every_field_a_status_decision_needs(
        self, fake_tools, loaded
    ):
        listing = report.run_review(loaded).to_dict()

        filler = next(entry for entry in listing["cuts"] if entry["id"] == "c0003")
        assert filler["reason"] == "filler"
        assert filler["status"] == "proposed"
        assert filler["confidence"] == 0.7
        assert filler["note"] == "filler plus the silence around it"
        assert filler["duration"] == 0.9

    def test_a_silence_candidate_removes_no_speech_and_says_so(
        self, fake_tools, loaded
    ):
        listing = report.run_review(loaded).to_dict()

        silence = next(entry for entry in listing["cuts"] if entry["id"] == "c0001")
        assert silence["removed"] == []
        assert silence["before"] is None
        assert silence["after"]["segment_id"] == "s0002"

    def test_the_text_listing_marks_the_segment_that_disappears(
        self, fake_tools, loaded
    ):
        lines = report.run_review(loaded).lines()

        assert any("✖ removed s0005" in line for line in lines)
        assert any("えーと" in line for line in lines)

    def test_without_a_transcript_the_intervals_are_shown_and_the_text_is_null(
        self, fake_tools, loaded
    ):
        (loaded.root / "transcript.json").unlink()

        result = report.run_review(loaded)

        assert [entry["id"] for entry in result.to_dict()["cuts"]] == [
            "c0001",
            "c0002",
            "c0003",
            "c0004",
        ]
        assert all(entry["removed"] is None for entry in result.to_dict()["cuts"])
        assert any("transcript.json" in warning for warning in result.warnings)

    def test_without_cuts_json_the_listing_points_at_detect(self, fake_tools, loaded):
        (loaded.root / "cuts.json").unlink()

        result = report.run_review(loaded)

        assert result.to_dict() == {"cuts": []}
        assert any("vidprep detect" in warning for warning in result.warnings)

    def test_the_listing_writes_nothing(self, fake_tools, loaded):
        report.run_review(loaded)

        assert not (loaded.root / report.STATS_NAME).exists()
        assert not (loaded.root / _boundaries.DIGEST_NAME).exists()

    def test_a_project_without_candidates_says_there_is_nothing_to_review(self):
        assert _review.lines([]) == ["no cut candidates to review"]


# --------------------------------------------------------------------------- #
#  The command line
# --------------------------------------------------------------------------- #


class TestCommandLine:
    def test_json_prints_exactly_what_was_written_to_stats_json(
        self, fake_tools, run_cli, prepared
    ):
        result = run_cli("report", "-p", str(prepared), "--json")

        assert result.exit_code == EXIT_OK
        assert json.loads(result.stdout) == stats_of(prepared)

    def test_warnings_stay_out_of_the_json_document(
        self, fake_tools, run_cli, prepared
    ):
        (prepared / report.RENDERED_NAME).unlink()

        result = run_cli("report", "-p", str(prepared), "--json")

        assert json.loads(result.stdout)["duration"]["rendered"] is None
        assert "out/output.mp4" in result.stderr

    def test_running_before_detect_and_render_still_exits_zero(
        self, fake_tools, run_cli, prepared
    ):
        (prepared / "cuts.json").unlink()
        (prepared / report.RENDERED_NAME).unlink()

        result = run_cli("report", "-p", str(prepared))

        assert result.exit_code == EXIT_OK
        assert "cuts.json" in result.stdout

    def test_cuts_and_json_together_produce_the_listing_as_json(
        self, fake_tools, run_cli, prepared
    ):
        result = run_cli("report", "-p", str(prepared), "--cuts", "--json")

        assert result.exit_code == EXIT_OK
        assert [entry["id"] for entry in json.loads(result.stdout)["cuts"]] == [
            "c0001",
            "c0002",
            "c0003",
            "c0004",
        ]

    def test_cuts_prints_the_context_for_a_human(self, fake_tools, run_cli, prepared):
        result = run_cli("report", "-p", str(prepared), "--cuts")

        assert "c0003  45.100-46.000 (0.900s)  reason=filler" in result.stdout
        assert "status=proposed  conf=0.70" in result.stdout

    def test_dry_run_without_cuts_lists_no_boundary_command(
        self, fake_tools, run_cli, prepared
    ):
        (prepared / "cuts.json").unlink()

        result = run_cli("report", "-p", str(prepared), "--dry-run")

        assert result.exit_code == EXIT_OK
        assert "showwavespic" not in result.stdout
        assert "concat" not in result.stdout

    def test_dry_run_without_processed_audio_lists_no_waveform_command(
        self, fake_tools, run_cli, prepared
    ):
        (prepared / audio.OUTPUT_NAME).unlink()

        result = run_cli("report", "-p", str(prepared), "--dry-run")

        assert result.exit_code == EXIT_OK
        assert "showwavespic" not in result.stdout
        assert "scale=1920:1080" in result.stdout

    def test_dry_run_lists_the_commands_and_writes_nothing(
        self, fake_tools, run_cli, prepared
    ):
        result = run_cli("report", "-p", str(prepared), "--dry-run")

        assert result.exit_code == EXIT_OK
        assert "showwavespic" in result.stdout
        assert not (prepared / report.STATS_NAME).exists()
        assert fake_tools.commands == []
