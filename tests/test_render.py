"""Tests for the render stage: the cut video, and the subtitles timed with it.

ffmpeg and ffprobe are replaced by :class:`FakeFfmpeg`, which reads the filter
graph vidprep hands it and answers as a real encoder would have — the lengths
it reports are derived from the ``trim`` intervals in that graph, so the
completion conditions of verification-plan.md §8 can be probed on either side
of their boundary without encoding anything. The suite therefore runs on CI,
where no media tool is installed.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pysubs2
import pytest

from vidprep import _ffmpeg, _reencode, _subtitles, audio, cli
from vidprep import project as project_module
from vidprep import render as render_module
from vidprep.errors import (
    EXIT_EXECUTION,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    FfmpegError,
    InvariantViolationError,
    UsageError,
)
from vidprep.models import Profile, SubtitleProfile
from vidprep.timeline import Timeline

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vidprep.project import Project

DURATION = 298.92
FPS = "25/1"
FRAME = 0.04

#: ``(id, start, end, status)``. The two approved cuts remove 42 seconds; the
#: proposed and the rejected one must survive into the output (REQ-002).
CUTS = (
    ("c0001", 10.0, 12.0, "approved"),
    ("c0002", 20.0, 20.5, "proposed"),
    ("c0003", 30.0, 31.0, "rejected"),
    ("c0004", 100.0, 140.0, "approved"),
)
REMOVED = 42.0

LONG_TEXT = "今日はクロードコードを使って動画編集を自動化する話をします"

#: ``(id, start, end, text)``, one per mapping rule of design.md §4.
SEGMENTS = (
    ("s0001", 1.0, 3.0, "こんにちは"),
    ("s0002", 10.2, 11.5, "この一言は消える"),
    ("s0003", 13.0, 13.5, "短い"),
    ("s0004", 50.0, 53.0, LONG_TEXT),
    ("s0005", 99.0, 141.0, "カットをまたぐ一文です"),
)

#: What loudnorm prints for an output that kept the -14 LUFS target.
LOUDNORM_REPORT = {
    "input_i": "-14.08",
    "input_tp": "-1.29",
    "input_lra": "7.4",
    "input_thresh": "-24.30",
    "target_offset": "0.10",
}

VIDEO_TRIM = re.compile(r"\[0:v\]trim=start=([\d.]+):end=([\d.]+)")
AUDIO_TRIM = re.compile(r"\[1:a\]atrim=start=([\d.]+):end=([\d.]+)")


class FakeFfmpeg:
    """Stand-in for ffmpeg and ffprobe, answering from the graph it was given.

    The encode command's ``trim`` intervals say how long the result should be,
    so by default the file it pretends to write is exactly right; the deltas
    move each reported length away from that by a chosen amount.
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.rendered = 0.0
        self.duration_delta = 0.0
        self.video_delta = 0.0
        self.audio_delta = 0.0
        self.report = dict(LOUDNORM_REPORT)
        self.error: Exception | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the real subprocess wrappers with this fake."""
        monkeypatch.setattr(_ffmpeg, "run", self.run)
        monkeypatch.setattr(_ffmpeg, "run_analysis", self.run_analysis)

    @property
    def encode(self) -> list[str]:
        """The single ffmpeg invocation that produced the output."""
        return next(args for args in self.commands if "-filter_complex" in args)

    @property
    def graph(self) -> str:
        """The filter graph of that invocation."""
        return self.encode[self.encode.index("-filter_complex") + 1]

    def keeps(self) -> list[tuple[float, float]]:
        """The intervals the graph keeps, read back from its video trims."""
        return [(float(a), float(b)) for a, b in VIDEO_TRIM.findall(self.graph)]

    def run(self, args: list[str], timeout: float = 0.0) -> str:
        """Write what ffmpeg would write, or answer what ffprobe would answer."""
        self.commands.append(list(args))
        if args[0] == _ffmpeg.FFMPEG:
            graph = args[args.index("-filter_complex") + 1]
            self.rendered = sum(
                float(end) - float(start) for start, end in VIDEO_TRIM.findall(graph)
            )
            if self.error is not None:
                raise self.error
            Path(args[-1]).write_bytes(b"rendered mp4")
            return ""
        if "format=duration" in args:
            return f"{self.rendered + self.duration_delta:.6f}\n"
        kind = args[args.index("-select_streams") + 1]
        delta = self.video_delta if kind == "v" else self.audio_delta
        return f"{self.rendered + delta:.6f}\n"

    def run_analysis(self, args: list[str], timeout: float = 0.0) -> str:
        """Answer the loudness measurement pass."""
        self.commands.append(list(args))
        return f"[Parsed_loudnorm_1 @ 0x1] \n{json.dumps(self.report, indent=1)}\n"


def write_cuts(
    root: Path, cuts: Sequence[tuple[str, float, float, str]] = CUTS
) -> None:
    """Write a ``cuts.json`` holding *cuts*, bypassing the schema on purpose."""
    payload = {
        "version": "1",
        "cuts": [
            {
                "id": identifier,
                "start": start,
                "end": end,
                "reason": "silence",
                "status": status,
            }
            for identifier, start, end, status in cuts
        ],
    }
    (root / "cuts.json").write_text(json.dumps(payload), encoding="utf-8")


def write_transcript(
    root: Path, segments: Sequence[tuple[str, float, float, str]] = SEGMENTS
) -> None:
    """Write a ``transcript.json`` holding *segments*."""
    payload = {
        "version": "1",
        "audio_source": "audio/processed.wav",
        "asr": {
            "backend": "whisper.cpp",
            "model": "large-v3-turbo",
            "vad": "silero-v5",
        },
        "segments": [
            {"id": identifier, "start": start, "end": end, "text": text}
            for identifier, start, end, text in segments
        ],
    }
    (root / "transcript.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    """The fake media tools, installed for the duration of one test."""
    fake = FakeFfmpeg()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def prepared(project_dir: Path) -> Path:
    """A project whose upstream stages have all run."""
    processed = project_dir / audio.OUTPUT_NAME
    processed.parent.mkdir(parents=True, exist_ok=True)
    processed.write_bytes(b"pretend this is a wav")
    write_cuts(project_dir)
    write_transcript(project_dir)
    return project_dir


@pytest.fixture
def loaded(prepared: Path) -> Project:
    """The prepared project, loaded."""
    return project_module.load_project(prepared)


def subtitles_of(loaded: Project) -> pysubs2.SSAFile:
    """Parse the SRT a render wrote back with pysubs2 (REQ-010)."""
    path = loaded.root / render_module.SUBTITLES_NAME
    return pysubs2.SSAFile.from_string(path.read_text(encoding="utf-8"), format_="srt")


# --------------------------------------------------------------------------- #
#  The renderer (REQ-001 .. REQ-004, REQ-040)
# --------------------------------------------------------------------------- #


class TestRenderer:
    def test_the_reencode_renderer_implements_the_protocol(self):
        renderer: _reencode.Renderer = _reencode.ReencodeRenderer(fps=FPS)

        assert callable(renderer.render)

    def test_only_approved_cuts_are_removed(self, tools, loaded):
        render_module.run_render(loaded)

        kept = tools.keeps()
        assert kept == [(0.0, 10.0), (12.0, 100.0), (140.0, DURATION)]

    def test_a_proposed_and_a_rejected_cut_stay_in_the_output(self, tools, loaded):
        result = render_module.run_render(loaded)

        applied = result.to_dict()["cuts_applied"]
        assert applied == {"approved": 2, "skipped_proposed": 1, "skipped_rejected": 1}
        # 20.0-20.5 (proposed) and 30.0-31.0 (rejected) fall inside a kept span.
        assert any(start <= 20.0 and end >= 31.0 for start, end in tools.keeps())

    def test_the_video_keeps_its_resolution_codec_and_frame_rate(self, tools, loaded):
        render_module.run_render(loaded)

        command = tools.encode
        assert command[command.index("-c:v") + 1] == "libx264"
        assert command[command.index("-crf") + 1] == "18"
        assert command[command.index("-preset") + 1] == "slow"
        assert command[command.index("-r") + 1] == FPS
        # No scaling filter: the source resolution is whatever comes out.
        assert "scale" not in tools.graph

    def test_the_audio_comes_from_the_processed_wav_as_aac_320k(self, tools, loaded):
        render_module.run_render(loaded)

        command = tools.encode
        inputs = [
            command[index + 1] for index, arg in enumerate(command) if arg == "-i"
        ]
        assert inputs[1].endswith(str(audio.OUTPUT_NAME))
        assert command[command.index("-c:a") + 1] == "aac"
        assert command[command.index("-b:a") + 1] == "320k"
        assert [(float(a), float(b)) for a, b in AUDIO_TRIM.findall(tools.graph)] == (
            tools.keeps()
        )

    def test_every_boundary_fades_without_changing_a_length(self, tools, loaded):
        render_module.run_render(loaded)

        graph = tools.graph
        for start, end in tools.keeps():
            length = end - start
            assert "afade=t=in:st=0:d=0.010000" in graph
            assert f"afade=t=out:st={length - 0.01:.6f}:d=0.010000" in graph
        # A crossfade would join the intervals; concat does not overlap them.
        assert "acrossfade" not in graph
        assert f"concat=n={len(tools.keeps())}:v=1:a=1" in graph

    def test_a_kept_interval_too_short_for_two_fades_gets_shorter_ones(self):
        renderer = _reencode.ReencodeRenderer(fps=FPS)

        graph = renderer.filtergraph([(0.0, 0.012)], 0.010)

        assert "afade=t=in:st=0:d=0.006000" in graph
        assert "afade=t=out:st=0.006000:d=0.006000" in graph

    def test_cuts_are_pulled_in_to_the_frame_grid(self):
        aligned = _reencode.align_to_frames([(1.011, 2.999)], FPS)

        # 1.011 is inside frame 25 (1.00-1.04) and 2.999 inside frame 74.
        assert aligned == [(1.04, 2.96)]

    def test_a_cut_shorter_than_a_frame_disappears(self):
        assert _reencode.align_to_frames([(1.011, 1.019)], FPS) == []

    def test_a_container_without_a_frame_rate_leaves_the_cuts_alone(self):
        cuts = [(1.011, 2.999)]

        assert _reencode.align_to_frames(cuts, "0/1") == cuts
        assert _reencode.ReencodeRenderer(fps="0/1").frame_ms == 40.0

    def test_alignment_never_lengthens_a_cut(self):
        cuts = [(10.017, 12.033), (100.0, 140.0)]

        for (start, end), (was_start, was_end) in zip(
            _reencode.align_to_frames(cuts, FPS), cuts, strict=True
        ):
            assert start >= was_start
            assert end <= was_end


# --------------------------------------------------------------------------- #
#  Verifying the output (REQ-006 .. REQ-008)
# --------------------------------------------------------------------------- #


class TestOutputVerification:
    def test_a_length_off_by_exactly_one_frame_passes(self, tools, loaded):
        tools.duration_delta = FRAME

        result = render_module.run_render(loaded)

        assert result.to_dict()["duration"]["delta_ms"] == pytest.approx(40.0)

    def test_a_length_off_by_more_than_one_frame_fails(self, tools, loaded):
        tools.duration_delta = FRAME + 0.001

        with pytest.raises(InvariantViolationError, match=r"41\.0ms > 40\.0ms"):
            render_module.run_render(loaded)

    def test_streams_exactly_50ms_apart_pass(self, tools, loaded):
        tools.video_delta = 0.050

        result = render_module.run_render(loaded)

        assert result.to_dict()["streams"]["av_delta_ms"] == pytest.approx(50.0)

    def test_streams_more_than_50ms_apart_fail(self, tools, loaded):
        tools.audio_delta = -0.051

        with pytest.raises(InvariantViolationError, match=r"51\.0ms > 50ms"):
            render_module.run_render(loaded)

    def test_loudness_at_the_edge_of_the_tolerance_passes(self, tools, loaded):
        tools.report["input_i"] = "-14.50"

        result = render_module.run_render(loaded)

        assert result.to_dict()["loudness"]["integrated_lufs"] == -14.5

    def test_loudness_outside_the_tolerance_fails(self, tools, loaded):
        tools.report["input_i"] = "-14.51"

        with pytest.raises(InvariantViolationError, match=r"-14\.51 LUFS"):
            render_module.run_render(loaded)

    def test_a_rejected_output_never_replaces_the_previous_one(self, tools, loaded):
        target = loaded.root / render_module.VIDEO_NAME
        target.parent.mkdir(parents=True)
        target.write_bytes(b"the render from yesterday")
        tools.duration_delta = 1.0

        with pytest.raises(InvariantViolationError):
            render_module.run_render(loaded)

        assert target.read_bytes() == b"the render from yesterday"


# --------------------------------------------------------------------------- #
#  What has to be true before anything is encoded (REQ-005, REQ-020 .. REQ-023)
# --------------------------------------------------------------------------- #


class TestPreconditions:
    def test_a_missing_processed_wav_asks_for_audio_fix(self, tools, loaded):
        (loaded.root / audio.OUTPUT_NAME).unlink()

        with pytest.raises(UsageError, match="run `vidprep audio-fix` first"):
            render_module.run_render(loaded)

    def test_a_missing_cuts_file_asks_for_detect(self, tools, loaded):
        (loaded.root / "cuts.json").unlink()

        with pytest.raises(UsageError, match="run `vidprep detect` first"):
            render_module.run_render(loaded)

    def test_a_missing_transcript_asks_for_transcribe(self, tools, loaded):
        (loaded.root / "transcript.json").unlink()

        with pytest.raises(UsageError, match="run `vidprep transcribe` first"):
            render_module.run_render(loaded)

    def test_a_dry_run_refuses_for_the_same_reasons(self, tools, loaded):
        (loaded.root / "transcript.json").unlink()

        with pytest.raises(UsageError, match="run `vidprep transcribe` first"):
            render_module.plan(loaded)

    def test_overlapping_approved_cuts_stop_the_render(self, run_cli, prepared, tools):
        write_cuts(
            prepared,
            (
                ("c0007", 45.1, 45.9, "approved"),
                ("c0008", 45.5, 46.2, "approved"),
            ),
        )

        result = run_cli("render", "-p", str(prepared), "--json")

        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout)["error"] == "schema_invalid"
        assert not (prepared / render_module.VIDEO_NAME).exists()

    def test_replaced_source_material_stops_the_render(self, run_cli, prepared, tools):
        loaded = project_module.load_project(prepared)
        loaded.source_path.write_bytes(b"a different recording entirely")

        result = run_cli("render", "-p", str(prepared), "--json")

        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout)["error"] == "hash_mismatch"
        assert not (prepared / render_module.VIDEO_NAME).exists()

    def test_a_failing_ffmpeg_leaves_the_previous_output_alone(
        self, run_cli, prepared, tools
    ):
        target = prepared / render_module.VIDEO_NAME
        target.parent.mkdir(parents=True)
        target.write_bytes(b"the render from yesterday")
        tools.error = FfmpegError("ffmpeg exited with 1: Invalid argument")

        result = run_cli("render", "-p", str(prepared), "--json")

        assert result.exit_code == EXIT_EXECUTION
        assert json.loads(result.stdout)["error"] == "ffmpeg_failed"
        assert target.read_bytes() == b"the render from yesterday"

    def test_cuts_that_remove_everything_are_refused(self, tools, loaded):
        write_cuts(loaded.root, (("c0001", 0.0, DURATION, "approved"),))
        reloaded = project_module.load_project(loaded.root)

        with pytest.raises(InvariantViolationError, match="the whole recording"):
            render_module.run_render(reloaded)

    def test_the_source_material_is_never_written_to(self, tools, loaded):
        before = project_module.sha256_file(loaded.source_path)

        render_module.run_render(loaded)

        assert project_module.sha256_file(loaded.source_path) == before


# --------------------------------------------------------------------------- #
#  Subtitles (REQ-010 .. REQ-016, REQ-041)
# --------------------------------------------------------------------------- #


class TestSubtitles:
    def test_the_srt_is_timed_by_the_same_timeline_as_the_video(self, tools, loaded):
        render_module.run_render(loaded)

        timeline = Timeline([(10.0, 12.0), (100.0, 140.0)], DURATION)
        assert tools.keeps() == list(timeline.keeps)
        expected, _ = timeline.map_segments(
            [(name, start, end) for name, start, end, _ in SEGMENTS]
        )
        subs = subtitles_of(loaded)
        assert [(event.start, event.end) for event in subs] == [
            (round(segment.start * 1000), round(segment.end * 1000))
            for segment in expected
        ]

    def test_every_kept_segment_is_in_the_srt_and_every_absence_is_explained(
        self, tools, loaded
    ):
        result = render_module.run_render(loaded)

        dropped = [
            warning.segment_id
            for warning in result.subtitles.warnings
            if warning.kind == "dropped_by_cut"
        ]
        present = {entry.segment_id for entry in result.subtitles.entries}
        assert dropped == ["s0002"]
        assert present == {name for name, *_ in SEGMENTS} - set(dropped)
        assert len(subtitles_of(loaded)) == len(present)

    def test_timestamps_are_strictly_increasing_and_never_overlap(self, tools, loaded):
        render_module.run_render(loaded)

        events = list(subtitles_of(loaded))
        for earlier, later in itertools.pairwise(events):
            assert earlier.start <= earlier.end < later.start

    def test_a_segment_left_on_screen_too_briefly_is_reported_but_kept(
        self, tools, loaded
    ):
        result = render_module.run_render(loaded)

        brief = [
            warning
            for warning in result.subtitles.warnings
            if warning.kind == "min_display"
        ]
        assert [warning.segment_id for warning in brief] == ["s0003"]
        assert brief[0].value == pytest.approx(0.5)
        assert "s0003" in {entry.segment_id for entry in result.subtitles.entries}

    def test_a_segment_read_too_fast_is_listed(self, tools, loaded):
        result = render_module.run_render(loaded)

        fast = [
            warning
            for warning in result.subtitles.warnings
            if warning.kind == "max_cps"
        ]
        assert [warning.segment_id for warning in fast] == ["s0004"]
        assert fast[0].value == round(_subtitles.text_width(LONG_TEXT) / 3.0, 2)

    def test_the_counts_reported_match_the_warnings_raised(self, tools, loaded):
        result = render_module.run_render(loaded)

        assert result.to_dict()["subtitles"] == {
            "entries": 4,
            "dropped_by_cut": 1,
            "warn_min_display": 1,
            "warn_max_cps": 1,
            "warn_line_overflow": 0,
        }

    def test_no_wrap_writes_a_second_unbroken_file(self, tools, loaded):
        result = render_module.run_render(loaded, no_wrap=True)

        wrapped = (loaded.root / render_module.SUBTITLES_NAME).read_text("utf-8")
        unwrapped = (loaded.root / render_module.NOWRAP_NAME).read_text("utf-8")
        assert str(render_module.NOWRAP_NAME) in result.outputs
        assert LONG_TEXT in unwrapped
        assert LONG_TEXT not in wrapped  # it was broken across two lines
        assert len(pysubs2.SSAFile.from_string(unwrapped, format_="srt")) == 4

    def test_the_srt_survives_a_pysubs2_round_trip(self, tools, loaded):
        render_module.run_render(loaded)

        original = (loaded.root / render_module.SUBTITLES_NAME).read_text("utf-8")
        parsed = pysubs2.SSAFile.from_string(original, format_="srt")
        assert parsed.to_string("srt") == original


class TestLineBreaking:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("あいう", 3.0, id="full-width"),
            pytest.param("abc", 1.5, id="half-width"),
            pytest.param("\uff41\uff42\uff43", 3.0, id="full-width-latin"),
            pytest.param("", 0.0, id="empty"),
        ],
    )
    def test_width_is_counted_in_full_width_characters(self, text, expected):
        assert _subtitles.text_width(text) == expected

    def test_a_line_of_exactly_the_limit_is_not_broken(self):
        text = "あ" * 20

        assert _subtitles.wrap(text, 20, 2) == (text,)

    def test_one_character_more_than_the_limit_is_broken(self):
        lines = _subtitles.wrap(
            "今日はクロードコードを使って動画編集を自動化します", 20, 2
        )

        assert len(lines) == 2
        assert all(_subtitles.text_width(line) <= 20 for line in lines)

    def test_text_that_does_not_fit_is_packed_rather_than_truncated(self):
        text = LONG_TEXT * 3

        lines = _subtitles.wrap(text, 20, 2)

        assert len(lines) == 2
        assert "".join(lines) == text

    def test_an_overfull_entry_is_reported(self, tools, loaded):
        write_transcript(loaded.root, (("s0001", 1.0, 60.0, LONG_TEXT * 3),))
        reloaded = project_module.load_project(loaded.root)

        result = render_module.run_render(reloaded)

        assert result.to_dict()["subtitles"]["warn_line_overflow"] == 1

    def test_a_break_never_falls_inside_a_budoux_phrase(self):
        lines = _subtitles.wrap(LONG_TEXT, 20, 2)

        assert "".join(lines) == LONG_TEXT
        for line in lines:
            assert line in "".join(_subtitles.phrases(LONG_TEXT))

    @pytest.mark.parametrize(
        ("display", "warned"),
        [pytest.param(1.0, False, id="exactly-max-cps"), (0.5, True)],
    )
    def test_the_reading_speed_limit_is_inclusive(self, display, warned):
        profile = SubtitleProfile()
        entry = _subtitles.Entry("s0001", 0.0, display, ("あいうえおかきく",))

        assert (entry.cps > profile.max_cps) is warned


# --------------------------------------------------------------------------- #
#  The stage and its command line
# --------------------------------------------------------------------------- #


class TestStage:
    def test_the_result_reports_what_was_cut_and_measured(self, tools, loaded):
        result = render_module.run_render(loaded).to_dict()

        assert result["renderer"] == "ReencodeRenderer"
        assert result["duration"] == {
            "source": DURATION,
            "removed": REMOVED,
            "expected": round(DURATION - REMOVED, 3),
            "actual": round(DURATION - REMOVED, 3),
            "delta_ms": 0.0,
        }
        assert result["outputs"] == ["out/output.mp4", "out/subtitles.srt"]

    def test_a_project_with_no_approved_cuts_is_re_encoded_whole(self, tools, loaded):
        write_cuts(loaded.root, (("c0002", 20.0, 20.5, "proposed"),))
        reloaded = project_module.load_project(loaded.root)

        result = render_module.run_render(reloaded)

        assert tools.keeps() == [(0.0, DURATION)]
        assert len(result.subtitles.entries) == len(SEGMENTS)

    def test_the_stage_is_recorded_in_the_manifest(self, tools, loaded):
        render_module.run_render(loaded)

        manifest = project_module.load_project(loaded.root).manifest
        assert render_module.STAGE in manifest.stages

    def test_a_dry_run_writes_nothing(self, run_cli, prepared, tools):
        result = run_cli("render", "-p", str(prepared), "--dry-run", "--json")

        assert result.exit_code == EXIT_OK
        assert tools.commands == []
        assert not (prepared / "out").exists()

    def test_the_dry_run_shows_the_encoder_settings_and_the_fades(
        self, run_cli, prepared, tools
    ):
        result = run_cli("render", "-p", str(prepared), "--dry-run", "--json")

        plan = json.loads(result.stdout)
        encode = " ".join(plan["commands"][0])
        assert plan["renderer"] == "ReencodeRenderer"
        assert "-crf 18" in encode
        assert "-preset slow" in encode
        assert "afade=t=in:st=0:d=0.010000" in encode

    def test_the_dry_run_lists_the_second_subtitle_file(self, run_cli, prepared, tools):
        result = run_cli(
            "render", "-p", str(prepared), "--dry-run", "--no-wrap", "--json"
        )

        writes = json.loads(result.stdout)["writes"]
        assert str(prepared / render_module.NOWRAP_NAME) in writes

    def test_the_command_succeeds_and_names_what_it_wrote(
        self, run_cli, prepared, tools
    ):
        result = run_cli("render", "-p", str(prepared), "--no-wrap")

        assert result.exit_code == EXIT_OK
        assert "out/output.mp4" in result.stdout
        assert "out/subtitles.srt" in result.stdout
        assert (prepared / render_module.NOWRAP_NAME).is_file()

    def test_render_is_registered_as_a_command(self):
        registered = {
            command.callback.__name__
            for command in cli.app.registered_commands
            if command.callback is not None
        }

        assert "render" in registered

    def test_a_stale_upstream_stage_warns_without_blocking(
        self, run_cli, prepared, tools
    ):
        loaded = project_module.load_project(prepared)
        project_module.record_stage(loaded, "audio_fix")
        changed = loaded.profile
        changed.audio.highpass_hz = 120
        project_module.write_json(prepared / "profile.json", changed)

        result = run_cli("render", "-p", str(prepared))

        assert result.exit_code == EXIT_OK
        assert "may be stale" in result.stdout

    def test_a_project_that_is_not_one_fails_with_a_usage_error(
        self, run_cli, tmp_path
    ):
        result = run_cli("render", "-p", str(tmp_path), "--json")

        assert result.exit_code == EXIT_USAGE
        assert json.loads(result.stdout)["error"] == "not_a_project"


def test_the_default_profile_drives_the_render(tools, loaded):
    """The values the issue pins live in ``profile.json``, not in the code."""
    profile = Profile()

    assert (profile.render.crf, profile.render.preset) == (18, "slow")
    assert profile.render.boundary_fade == 0.010
    assert profile.subtitle.max_chars_per_line == 20
    assert profile.subtitle.max_lines == 2
    assert profile.subtitle.min_display == 0.8
    assert profile.subtitle.max_cps == 8.0
