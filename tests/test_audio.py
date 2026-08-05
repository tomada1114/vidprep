"""Tests for the audio-fix stage.

No real ffmpeg, ffprobe or DeepFilterNet runs here: the three of them are
replaced by :class:`FakeTools`, which records the command lines it was given
and answers with the logs the real tools would have printed. That keeps the
suite runnable on a machine with none of them installed while still asserting
on the thing that matters — the exact chain vidprep asks ffmpeg to apply.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vidprep import _ffmpeg, audio, doctor
from vidprep import project as project_module
from vidprep.errors import (
    EXIT_EXECUTION,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    ExecutionFailedError,
    InvariantViolationError,
    UsageError,
)

if TYPE_CHECKING:
    from vidprep.project import Project

SOURCE_SECONDS = 298.92
DEEPFILTERNET_PATH = "/opt/bin/deep-filter"

#: What loudnorm reports for the golden sample before anything touched it.
BEFORE_REPORT = {
    "input_i": "-22.24",
    "input_tp": "-6.22",
    "input_lra": "7.4",
    "input_thresh": "-32.50",
    "output_i": "-14.00",
    "output_tp": "-1.00",
    "output_lra": "7.4",
    "output_thresh": "-24.00",
    "normalization_type": "linear",
    "target_offset": "0.12",
}
#: What the pass-1 analysis of the whole chain reports.
CHAIN_REPORT = {**BEFORE_REPORT, "input_i": "-22.10", "target_offset": "0.30"}
#: What the finished file measures.
AFTER_REPORT = {
    **BEFORE_REPORT,
    "input_i": "-14.06",
    "input_tp": "-1.31",
    "input_lra": "8.9",
}

SILENCE_LOG = """\
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 2.020136 | silence_duration: 2.020136
[silencedetect @ 0x1] silence_start: 4.017052
[silencedetect @ 0x1] silence_end: 6.5 | silence_duration: 2.482948
"""


def loudnorm_log(report: dict[str, str]) -> str:
    """Render *report* the way the loudnorm filter prints it to stderr."""
    return f"[Parsed_loudnorm_2 @ 0x1] \n{json.dumps(report, indent=1)}\n"


def astats_log(level: str) -> str:
    """Render the astats overall section the way ffmpeg prints it."""
    return f"[Parsed_astats_3 @ 0x1] Overall\n[Parsed_astats_3 @ 0x1] RMS level dB: {level}\n"


class FakeTools:
    """Stand-in for ffmpeg, ffprobe and DeepFilterNet.

    Answers are keyed by the *name of the input file* each command reads, which
    is what distinguishes "measure the source" from "measure the result".
    """

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.source_seconds = SOURCE_SECONDS
        self.output_seconds = SOURCE_SECONDS
        self.reports = {
            audio.EXTRACTED_NAME: BEFORE_REPORT,
            audio.DENOISED_DIR: CHAIN_REPORT,
            audio.RENDERED_NAME: AFTER_REPORT,
        }
        self.rms = {audio.EXTRACTED_NAME: "-58.3", audio.RENDERED_NAME: "-71.4"}
        #: The pre-loudnorm floor, told apart by the high-pass its command
        #: carries: it is the only measurement that filters before selecting.
        self.denoised_rms = "-63.9"
        self.silence = SILENCE_LOG
        self.denoiser_writes = True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace the real subprocess wrappers with this fake."""
        monkeypatch.setattr(_ffmpeg, "run", self.run)
        monkeypatch.setattr(_ffmpeg, "run_analysis", self.run_analysis)

    def _key(self, path: str) -> str:
        """Return the answer key for the file *path* names."""
        parent = Path(path).parent.name
        return audio.DENOISED_DIR if parent == audio.DENOISED_DIR else Path(path).name

    def _input(self, args: list[str]) -> str:
        return args[args.index("-i") + 1]

    def run(self, args: list[str], timeout: float = 0.0) -> str:
        """Answer ffprobe, write the file ffmpeg or DeepFilterNet would write."""
        self.commands.append(list(args))
        if args[0] == _ffmpeg.FFPROBE:
            name = self._key(args[-1])
            seconds = (
                self.output_seconds
                if name == audio.RENDERED_NAME
                else self.source_seconds
            )
            return f"{seconds:.6f}\n"
        if args[0] == _ffmpeg.FFMPEG:
            self._write(Path(args[-1]))
            return ""
        if self.denoiser_writes:
            out_dir = Path(args[args.index("--output-dir") + 1])
            self._write(out_dir / Path(args[-1]).name)
        return ""

    def run_analysis(self, args: list[str], timeout: float = 0.0) -> str:
        """Answer a measurement pass with the log its filters would print."""
        self.commands.append(list(args))
        filters = args[args.index("-af") + 1]
        key = self._key(self._input(args))
        if "silencedetect" in filters:
            return self.silence
        if "astats" in filters:
            level = self.denoised_rms if "highpass" in filters else self.rms[key]
            return astats_log(level)
        return loudnorm_log(self.reports[key])

    @staticmethod
    def _write(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF....WAVEfmt ")

    def filters_of(self, index: int) -> str:
        """Return the ``-af`` value of the *index*-th recorded command."""
        args = self.commands[index]
        return args[args.index("-af") + 1]

    @property
    def ffmpeg_filters(self) -> list[str]:
        """Every ``-af`` value ffmpeg was given, in the order it was given."""
        return [args[args.index("-af") + 1] for args in self.commands if "-af" in args]


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakeTools:
    """Replace the external media tools and report a full environment."""
    fake = FakeTools()
    fake.install(monkeypatch)
    monkeypatch.setattr(
        doctor, "check_ffmpeg", lambda: {"ok": True, "version": "7.1.1"}
    )
    monkeypatch.setattr(
        doctor,
        "check_deepfilternet",
        lambda: {
            "ok": True,
            "optional": True,
            "path": DEEPFILTERNET_PATH,
            "version": "0.5.6",
        },
    )
    return fake


@pytest.fixture
def without_deepfilternet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report DeepFilterNet as absent, as an environment without it would."""
    monkeypatch.setattr(
        doctor,
        "check_deepfilternet",
        lambda: {
            "ok": False,
            "optional": True,
            "fallback": audio.AFFTDN,
            "error": "none of deep-filter, deepFilter found in PATH",
        },
    )


@pytest.fixture
def loaded(project_dir: Path) -> Project:
    """The initialised project, loaded the way a stage receives it."""
    return project_module.load_project(project_dir)


class TestChainOrder:
    """REQ-001 / REQ-002: the chain, and the two loudnorm passes."""

    def test_deepfilternet_runs_before_the_ffmpeg_chain(self, tools, loaded):
        plan = audio.plan(loaded)

        denoise, analysis = plan["commands"][1], plan["commands"][2]
        assert denoise[0] == DEEPFILTERNET_PATH
        assert "--compensate-delay" in denoise
        assert analysis[analysis.index("-i") + 1].endswith(
            f"{audio.DENOISED_DIR}/{audio.EXTRACTED_NAME}"
        )

    def test_highpass_precedes_loudnorm_in_every_pass(self, tools, loaded):
        plan = audio.plan(loaded)

        for filters in fake_filters(plan):
            assert filters.index("highpass=f=80") < filters.index("loudnorm=")

    def test_fallback_inserts_afftdn_at_the_head_of_the_chain(
        self, tools, without_deepfilternet, loaded
    ):
        plan = audio.plan(loaded)

        filters = fake_filters(plan)[0]
        assert filters.index("afftdn") < filters.index("highpass=f=80")
        assert filters.index("highpass=f=80") < filters.index("loudnorm=")

    def test_first_pass_only_measures(self, tools, loaded):
        analysis = audio.plan(loaded)["commands"][2]

        filters = analysis[analysis.index("-af") + 1]
        assert "print_format=json" in filters
        assert "measured_I" not in filters
        assert analysis[-3:] == ["-f", "null", "-"]

    def test_second_pass_carries_the_measurements_and_linear_mode(self, tools, loaded):
        render = audio.plan(loaded)["commands"][3]

        filters = render[render.index("-af") + 1]
        for option in audio.MEASURED_KEYS:
            assert f"{option}=<{option}>" in filters
        assert "linear=true" in filters

    def test_second_pass_pins_the_output_format(self, tools, loaded):
        render = audio.plan(loaded)["commands"][3]

        assert render[render.index("-c:a") + 1] == "pcm_s16le"
        assert render[render.index("-ar") + 1] == "44100"
        assert render[render.index("-ac") + 1] == "2"

    def test_targets_come_from_the_profile(self, tools, loaded):
        loaded.profile.audio.highpass_hz = 120
        loaded.profile.audio.loudnorm.i = -16.0

        filters = fake_filters(audio.plan(loaded))[0]

        assert "highpass=f=120" in filters
        assert "loudnorm=I=-16:TP=-1:LRA=11" in filters

    def test_afftdn_in_the_profile_never_looks_for_deepfilternet(self, tools, loaded):
        loaded.profile.audio.denoise = audio.AFFTDN

        plan = audio.plan(loaded)

        assert plan["denoise_used"] == audio.AFFTDN
        assert plan["warnings"] == []
        assert all(command[0].startswith("ff") for command in plan["commands"])

    def test_unknown_denoiser_is_a_usage_error(self, tools, loaded):
        loaded.profile.audio.denoise = "rnnoise"

        with pytest.raises(UsageError, match=r"audio\.denoise must be one of"):
            audio.plan(loaded)


def fake_filters(plan: dict[str, object]) -> list[str]:
    """Return every ``-af`` value in a plan, in order."""
    commands: list[list[str]] = plan["commands"]  # type: ignore[assignment]
    return [args[args.index("-af") + 1] for args in commands if "-af" in args]


class TestPlan:
    def test_dry_run_writes_nothing(self, tools, loaded, project_dir):
        audio.plan(loaded, with_stats=True)

        assert not (project_dir / "audio").exists()
        assert tools.commands == []

    def test_plan_names_what_it_would_write(self, tools, loaded, project_dir):
        plan = audio.plan(loaded)

        assert plan["writes"] == [
            str(project_dir / "audio" / "processed.wav"),
            str(project_dir / "vidprep.json"),
        ]

    def test_stats_adds_the_measurement_passes(self, tools, loaded):
        plain = audio.plan(loaded)
        with_stats = audio.plan(loaded, with_stats=True)

        added = with_stats["commands"][len(plain["commands"]) :]
        assert len(added) == 6
        assert any("silencedetect" in " ".join(command) for command in added)

    def test_stats_names_the_noise_floor_it_would_write(
        self, tools, loaded, project_dir
    ):
        plan = audio.plan(loaded, with_stats=True)

        assert str(project_dir / "report" / "noise_floor.json") in plan["writes"]

    def test_fallback_is_announced_in_the_plan(
        self, tools, without_deepfilternet, loaded
    ):
        plan = audio.plan(loaded)

        assert plan["denoise_used"] == audio.AFFTDN
        assert "falling back to afftdn" in plan["warnings"][0]


class TestRun:
    def test_writes_the_processed_audio(self, tools, loaded, project_dir):
        result = audio.run_audio_fix(loaded)

        assert (project_dir / "audio" / "processed.wav").is_file()
        assert result.denoise_used == audio.DEEPFILTERNET
        assert result.delta_ms == pytest.approx(0.0)

    def test_measurements_feed_the_second_pass(self, tools, loaded):
        audio.run_audio_fix(loaded)

        render = next(
            args
            for args in tools.commands
            if "-af" in args and "measured_I" in args[args.index("-af") + 1]
        )
        filters = render[render.index("-af") + 1]
        assert "measured_I=-22.10" in filters
        assert "offset=0.30" in filters
        assert "linear=true" in filters

    def test_the_source_material_is_never_written(self, tools, loaded, source_video):
        before = source_video.read_bytes()

        audio.run_audio_fix(loaded)

        assert source_video.read_bytes() == before

    def test_the_working_directory_is_cleaned_up(self, tools, loaded, project_dir):
        audio.run_audio_fix(loaded)

        assert list(project_dir.glob(f"{audio.WORKSPACE_PREFIX}*")) == []

    def test_the_stage_is_recorded(self, tools, loaded, project_dir):
        audio.run_audio_fix(loaded)

        manifest = json.loads((project_dir / "vidprep.json").read_text())
        record = manifest["stages"]["audio_fix"]
        assert record["done_at"]
        assert len(record["params_sha256"]) == 64
        assert record["tool_versions"] == {"ffmpeg": "7.1.1", "deepfilternet": "0.5.6"}

    def test_the_fallback_records_no_denoiser_version(
        self, tools, without_deepfilternet, loaded, project_dir
    ):
        result = audio.run_audio_fix(loaded)

        manifest = json.loads((project_dir / "vidprep.json").read_text())
        assert result.denoise_used == audio.AFFTDN
        assert manifest["stages"]["audio_fix"]["tool_versions"] == {"ffmpeg": "7.1.1"}

    def test_the_fallback_warns_and_still_succeeds(
        self, tools, without_deepfilternet, loaded
    ):
        result = audio.run_audio_fix(loaded)

        assert result.denoise_used == audio.AFFTDN
        assert "falling back to afftdn" in result.warnings[0]
        assert result.to_dict()["denoise_used"] == audio.AFFTDN

    def test_a_denoiser_that_writes_nothing_is_reported(self, tools, loaded):
        tools.denoiser_writes = False

        with pytest.raises(ExecutionFailedError, match="wrote no output"):
            audio.run_audio_fix(loaded)

    def test_rerunning_replaces_the_previous_output(self, tools, loaded, project_dir):
        output = project_dir / "audio" / "processed.wav"
        output.parent.mkdir()
        output.write_bytes(b"the previous take")

        audio.run_audio_fix(loaded)

        assert output.read_bytes() != b"the previous take"


class TestLoudnormFailures:
    """REQ-021: output is never written from measurements vidprep cannot read."""

    def test_a_missing_report_stops_the_run(self, tools, loaded, project_dir):
        tools.reports[audio.DENOISED_DIR] = {}

        with pytest.raises(ExecutionFailedError, match="printed no JSON report"):
            audio.run_audio_fix(loaded)

        assert not (project_dir / "audio" / "processed.wav").exists()

    def test_an_incomplete_report_stops_the_run(self, tools, loaded):
        tools.reports[audio.DENOISED_DIR] = {"input_i": "-22.10"}

        with pytest.raises(ExecutionFailedError, match="missing input_tp"):
            audio.run_audio_fix(loaded)

    def test_a_truncated_report_stops_the_run(self, tools, loaded, monkeypatch):
        monkeypatch.setattr(
            _ffmpeg, "run_analysis", lambda *_a, **_k: '{ "input_i" : "-22.1"'
        )

        with pytest.raises(ExecutionFailedError, match="printed no JSON report"):
            audio.run_audio_fix(loaded)

    def test_a_malformed_report_stops_the_run(self, tools, loaded, monkeypatch):
        monkeypatch.setattr(
            _ffmpeg, "run_analysis", lambda *_a, **_k: '{ "input_i" : }'
        )

        with pytest.raises(ExecutionFailedError, match="could not read the loudnorm"):
            audio.run_audio_fix(loaded)

    def test_a_measurement_that_is_not_a_number_stops_the_run(self, tools, loaded):
        tools.reports[audio.EXTRACTED_NAME] = {**BEFORE_REPORT, "input_i": "n/a"}

        with pytest.raises(ExecutionFailedError, match="could not read the loudness"):
            audio.run_audio_fix(loaded, with_stats=True)


class TestLengthInvariant:
    """REQ-005 / REQ-022: a length that moved means the run is thrown away."""

    @pytest.mark.parametrize(
        ("output_seconds", "expected_ms"),
        [
            pytest.param(SOURCE_SECONDS, 0.0, id="identical"),
            pytest.param(SOURCE_SECONDS - 0.001, 1.0, id="one-millisecond-short"),
            pytest.param(SOURCE_SECONDS + 0.001, 1.0, id="one-millisecond-long"),
        ],
    )
    def test_a_delta_of_at_most_one_millisecond_passes(
        self, tools, loaded, output_seconds, expected_ms
    ):
        tools.output_seconds = output_seconds

        result = audio.run_audio_fix(loaded)

        assert result.delta_ms == pytest.approx(expected_ms, abs=1e-6)

    def test_a_larger_delta_is_a_verification_failure(self, tools, loaded):
        tools.output_seconds = SOURCE_SECONDS - 0.018

        with pytest.raises(InvariantViolationError, match=r"delta 18\.0ms > 1ms"):
            audio.run_audio_fix(loaded)

    def test_the_previous_output_survives_a_verification_failure(
        self, tools, loaded, project_dir
    ):
        output = project_dir / "audio" / "processed.wav"
        output.parent.mkdir()
        output.write_bytes(b"the previous take")
        tools.output_seconds = SOURCE_SECONDS - 0.018

        with pytest.raises(InvariantViolationError):
            audio.run_audio_fix(loaded)

        assert output.read_bytes() == b"the previous take"

    def test_the_stage_is_not_recorded_after_a_failure(
        self, tools, loaded, project_dir
    ):
        tools.output_seconds = SOURCE_SECONDS - 0.018

        with pytest.raises(InvariantViolationError):
            audio.run_audio_fix(loaded)

        manifest = json.loads((project_dir / "vidprep.json").read_text())
        assert manifest["stages"] == {}

    def test_the_render_is_capped_at_the_source_length(self, tools, loaded):
        audio.run_audio_fix(loaded)

        render = next(args for args in tools.commands if "-t" in args)

        assert render[render.index("-af") + 1].endswith(",apad")
        assert render[render.index("-t") + 1] == f"{SOURCE_SECONDS:.6f}"

    def test_an_unreadable_duration_is_reported(self, tools, loaded, monkeypatch):
        monkeypatch.setattr(_ffmpeg, "run", lambda *_a, **_k: "n/a")

        with pytest.raises(ExecutionFailedError, match="could not read the duration"):
            audio.run_audio_fix(loaded)


class TestStats:
    """REQ-006 / REQ-007: the before/after table and the noise floor."""

    def test_reports_four_metrics_on_each_side(self, tools, loaded):
        payload = audio.run_audio_fix(loaded, with_stats=True).to_dict()

        expected = {"integrated_lufs", "true_peak_dbtp", "lra", "noise_floor_rms_db"}
        assert set(payload["before"]) == expected
        assert set(payload["after"]) == expected

    def test_measures_the_source_not_the_denoised_audio(self, tools, loaded):
        payload = audio.run_audio_fix(loaded, with_stats=True).to_dict()

        assert payload["before"]["integrated_lufs"] == -22.24
        assert payload["after"]["integrated_lufs"] == -14.06
        assert payload["after"]["true_peak_dbtp"] == -1.31

    def test_the_noise_floor_drops(self, tools, loaded):
        payload = audio.run_audio_fix(loaded, with_stats=True).to_dict()

        assert payload["before"]["noise_floor_rms_db"] == -58.3
        assert payload["after"]["noise_floor_rms_db"] == -71.4

    def test_the_floor_of_req_007_is_compared_before_loudnorm(self, tools, loaded):
        payload = audio.run_audio_fix(loaded, with_stats=True).to_dict()

        assert payload["noise_floor"] == {
            "before_rms_db": -58.3,
            "after_rms_db": -63.9,
            "delta_db": -5.6,
            "improved": True,
            "silence_sec": 4.103,
        }

    def test_the_floor_after_denoising_is_read_off_the_denoised_file(
        self, tools, loaded
    ):
        audio.run_audio_fix(loaded, with_stats=True)

        command = next(
            args
            for args in tools.commands
            if "-af" in args
            and "astats" in args[args.index("-af") + 1]
            and "highpass" in args[args.index("-af") + 1]
        )
        filters = command[command.index("-af") + 1]
        assert Path(command[command.index("-i") + 1]).parent.name == audio.DENOISED_DIR
        assert filters.startswith(
            f"highpass=f=80,{audio.FLOOR_FRAME},aselect="
        )  # no denoiser: DFN ran
        assert "loudnorm" not in filters

    def test_the_in_band_denoiser_is_applied_before_the_floor_is_read(
        self, tools, without_deepfilternet, loaded
    ):
        audio.run_audio_fix(loaded, with_stats=True)

        filters = next(
            f
            for f in tools.ffmpeg_filters
            if "astats" in f and "aselect" in f and f.startswith(audio.AFFTDN)
        )
        assert filters.startswith(
            f"{audio.AFFTDN},highpass=f=80,{audio.FLOOR_FRAME},aselect="
        )
        assert "loudnorm" not in filters

    def test_a_floor_that_did_not_drop_is_not_an_improvement(self, tools, loaded):
        tools.denoised_rms = "-57.9"

        payload = audio.run_audio_fix(loaded, with_stats=True).to_dict()

        assert payload["noise_floor"]["delta_db"] == 0.4
        assert payload["noise_floor"]["improved"] is False

    def test_the_comparison_is_recorded_for_report_to_quote(
        self, tools, loaded, project_dir
    ):
        audio.run_audio_fix(loaded, with_stats=True)

        written = json.loads(
            (project_dir / "report" / "noise_floor.json").read_text(encoding="utf-8")
        )
        assert written == {
            "version": "1",
            "silence_sec": 4.103,
            "before_rms_db": -58.3,
            "after_rms_db": -63.9,
        }

    def test_a_run_without_stats_drops_a_stale_measurement(
        self, tools, loaded, project_dir
    ):
        audio.run_audio_fix(loaded, with_stats=True)

        audio.run_audio_fix(loaded)

        assert not (project_dir / "report" / "noise_floor.json").exists()

    def test_every_side_is_measured_over_the_same_silence(self, tools, loaded):
        audio.run_audio_fix(loaded, with_stats=True)

        selections = [
            filters[filters.index("aselect") :]
            for filters in tools.ffmpeg_filters
            if "aselect" in filters
        ]
        assert len(selections) == 3
        assert len(set(selections)) == 1
        assert "between(t,0.100,1.920)" in selections[0]
        assert "between(t,4.117,6.400)" in selections[0]

    def test_the_ends_of_each_silence_are_left_out_of_the_measurement(
        self, tools, loaded
    ):
        audio.run_audio_fix(loaded, with_stats=True)

        selection = next(f for f in tools.ffmpeg_filters if "aselect" in f)
        assert f"{audio.FLOOR_FRAME},aselect=" in selection
        # 0.000-2.020 and 4.017-6.500, each pulled in by the guard.
        assert "between(t,0.100,1.920)+between(t,4.117,6.400)" in selection

    def test_a_silence_too_short_for_the_guard_is_left_out(self, tools, loaded):
        brief = audio.SILENCE_GUARD_SECONDS

        assert audio.floor_intervals([(1.0, 1.0 + 2 * brief)]) == []
        assert audio.floor_intervals([(1.0, 2.0)]) == [(1.0 + brief, 2.0 - brief)]

    def test_nothing_left_after_the_guard_is_measured_as_unknown(self, tools, loaded):
        brief = [(1.0, 1.0 + audio.SILENCE_GUARD_SECONDS)]

        assert audio.noise_floor(Path("source.wav"), brief) is None
        assert tools.commands == []

    def test_without_silence_the_noise_floor_is_unknown(self, tools, loaded):
        tools.silence = "[silencedetect @ 0x1] nothing quiet enough\n"

        result = audio.run_audio_fix(loaded, with_stats=True)

        payload = result.to_dict()
        assert payload["before"]["noise_floor_rms_db"] is None
        assert payload["noise_floor"] == {
            "before_rms_db": None,
            "after_rms_db": None,
            "delta_db": None,
            "improved": None,
            "silence_sec": 0.0,
        }
        assert "noise floor could not be measured" in result.warnings[0]

    @pytest.mark.parametrize("level", ["-inf", "unavailable"])
    def test_a_level_that_is_not_a_number_is_reported_as_unknown(
        self, tools, loaded, level
    ):
        tools.rms[audio.RENDERED_NAME] = level

        payload = audio.run_audio_fix(loaded, with_stats=True).to_dict()

        assert payload["after"]["noise_floor_rms_db"] is None

    def test_an_unopened_silence_is_ignored(self, tools, loaded):
        tools.silence = (
            "[silencedetect @ 0x1] silence_end: 1.5 | silence_duration: 1.5\n"
            "[silencedetect @ 0x1] silence_start: 3.0\n"
            "[silencedetect @ 0x1] silence_end: 5.0 | silence_duration: 2.0\n"
        )

        audio.run_audio_fix(loaded, with_stats=True)

        selection = next(f for f in tools.ffmpeg_filters if "aselect" in f)
        assert "between(t,3.100,4.900)" in selection
        assert "1.500" not in selection

    def test_an_unfinished_silence_runs_to_the_end(self, tools, loaded):
        tools.silence = "[silencedetect @ 0x1] silence_start: 290.0\n"

        audio.run_audio_fix(loaded, with_stats=True)

        selection = next(f for f in tools.ffmpeg_filters if "aselect" in f)
        assert f"between(t,290.100,{SOURCE_SECONDS - 0.1:.3f})" in selection

    def test_missing_astats_output_is_reported(self, tools, loaded):
        tools.rms[audio.EXTRACTED_NAME] = ""

        with pytest.raises(ExecutionFailedError, match="no overall RMS level"):
            audio.run_audio_fix(loaded, with_stats=True)

    def test_stats_are_omitted_when_not_asked_for(self, tools, loaded):
        payload = audio.run_audio_fix(loaded).to_dict()

        assert "before" not in payload
        assert "after" not in payload
        assert "noise_floor" not in payload


class TestCli:
    def test_reports_the_result_as_json(self, tools, run_cli, project_dir):
        result = run_cli("audio-fix", "-p", str(project_dir), "--stats", "--json")

        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)
        assert payload["denoise_used"] == audio.DEEPFILTERNET
        assert payload["output"] == "audio/processed.wav"
        assert payload["duration"]["delta_ms"] == 0.0
        assert payload["before"]["integrated_lufs"] == -22.24

    def test_reports_the_result_for_a_human(self, tools, run_cli, project_dir):
        result = run_cli("audio-fix", "-p", str(project_dir), "--stats")

        assert result.exit_code == EXIT_OK
        assert "-22.24 → -14.06 LUFS" in result.stdout
        assert "TP -1.31 dBTP" in result.stdout

    def test_dry_run_shows_the_chain_and_writes_nothing(
        self, tools, run_cli, project_dir
    ):
        result = run_cli("audio-fix", "-p", str(project_dir), "--dry-run")

        assert result.exit_code == EXIT_OK
        assert "highpass=f=80" in result.stdout
        assert "linear=true" in result.stdout
        assert not (project_dir / "audio").exists()

    def test_the_fallback_warns_on_stderr_in_json_mode(
        self, tools, without_deepfilternet, run_cli, project_dir
    ):
        result = run_cli("audio-fix", "-p", str(project_dir), "--json")

        assert result.exit_code == EXIT_OK
        assert "falling back to afftdn" in result.stderr
        assert json.loads(result.stdout)["denoise_used"] == audio.AFFTDN

    def test_an_unreadable_analysis_exits_two(self, tools, run_cli, project_dir):
        tools.reports[audio.DENOISED_DIR] = {}

        result = run_cli("audio-fix", "-p", str(project_dir))

        assert result.exit_code == EXIT_EXECUTION

    def test_a_changed_length_exits_three(self, tools, run_cli, project_dir):
        tools.output_seconds = SOURCE_SECONDS - 0.018

        result = run_cli("audio-fix", "-p", str(project_dir), "--json")

        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout)["error"] == "invariant_violated"

    def test_a_replaced_source_exits_three(
        self, tools, run_cli, project_dir, source_video
    ):
        source_video.write_bytes(b"a different recording entirely")

        result = run_cli("audio-fix", "-p", str(project_dir))

        assert result.exit_code == EXIT_VALIDATION
        assert "sha256 mismatch" in result.stderr

    def test_outside_a_project_it_is_a_usage_error(self, tools, run_cli, tmp_path):
        result = run_cli("audio-fix", "-p", str(tmp_path))

        assert result.exit_code == EXIT_USAGE
        assert "is not a vidprep project" in result.stderr
