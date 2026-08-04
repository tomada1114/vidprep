"""Tests for the transcribe stage: mandatory VAD, timing and verification.

The recognisers are faked at the process boundary — the fake writes the JSON
whisper.cpp or mlx-whisper would write and prints the log whisper.cpp prints —
so the whole stage is exercised without either backend installed, which is the
only way CI (Linux, no whisper.cpp) can run these at all.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vidprep import _asr, _ffmpeg
from vidprep import project as project_module
from vidprep import transcribe as transcribe_module
from vidprep.errors import (
    EXIT_EXECUTION,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    AsrFailedError,
    FfmpegError,
    InvariantViolationError,
    SchemaInvalidError,
    UsageError,
)
from vidprep.models import Profile, Transcript, VadReport

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

WHISPER_BINARY = "/usr/local/bin/whisper-cli"
MLX_BINARY = "/usr/local/bin/mlx_whisper"
SPEECH = ((2.34, 10.01), (12.99, 20.0))
SEGMENTS = (
    (2.34, 5.82, "前回の動画ではクロードコードで"),
    (5.82, 9.92, "作業状況を再開する方法について学びました"),
    (13.02, 19.8, "そしてresumeというコマンドを実行すると"),
)

#: What a backend without a detection front-end returns for one extracted
#: region: seconds counted from the start of that region, not of the recording.
REGION_SEGMENTS = ((0.0, 3.48, "この区間の発話"), (3.48, 7.6, "その続き"))

#: The regions and segments measured on the golden sample for issue #24, where
#: whisper.cpp put a segment boundary 0.119s into the 0.200s of silence it
#: inserts between two concatenated regions and mapped it back 1.300s past the
#: end of the first one — into the middle of a 2.180s pause it never heard.
STRANDED_SPEECH = ((216.46, 221.09), (223.27, 226.69), (228.58, 230.14))
STRANDED_SEGMENTS = (
    (216.46, 222.39, "ではですね 一旦動作を確認するために"),
    (222.39, 230.0, "ではそうですね とりあえずこんにちはとでも打ってみます"),
)


def _log(speech: Sequence[tuple[float, float]]) -> str:
    """Render the log whisper.cpp prints while its VAD front-end runs."""
    lines = [
        "whisper_vad: VAD is enabled, processing speech segments only",
        f"whisper_vad: detected {len(speech)} speech segments",
    ]
    lines += [
        f"whisper_vad: vad_segment_info: orig_start: {start:.2f}, "
        f"orig_end: {end:.2f}, vad_start: 0.00, vad_end: 1.00"
        for start, end in speech
    ]
    return "\n".join(lines) + "\n"


def _whisper_json(segments: Sequence[tuple[float, float, str]]) -> str:
    """Render the ``-oj`` document whisper.cpp writes."""
    return json.dumps(
        {
            "transcription": [
                {
                    "offsets": {"from": round(start * 1000), "to": round(end * 1000)},
                    "text": text,
                }
                for start, end, text in segments
            ]
        }
    )


def _mlx_json(segments: Sequence[tuple[float, float, str]]) -> str:
    """Render the document mlx-whisper writes with ``--output-format json``."""
    return json.dumps(
        {
            "text": "".join(text for _, _, text in segments),
            "segments": [
                {"id": index, "start": start, "end": end, "text": text}
                for index, (start, end, text) in enumerate(segments)
            ],
        }
    )


@dataclass
class FakeAsr:
    """A recogniser that records its commands instead of decoding audio."""

    speech: tuple[tuple[float, float], ...] = SPEECH
    segments: tuple[tuple[float, float, str], ...] = SEGMENTS
    region_segments: tuple[tuple[float, float, str], ...] = REGION_SEGMENTS
    exit_code: int = 0
    log: str | None = None
    written: str | None = None

    def __post_init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, args: Sequence[str], timeout: float = 0.0) -> str:
        """Stand in for one external command, writing what it would write."""
        command = list(args)
        self.commands.append(command)
        if command[1:] == ["--version"]:
            return "whisper.cpp version: 1.9.1\n"
        if self.exit_code:
            msg = f"{command[0]} exited with {self.exit_code}: model load failed"
            raise FfmpegError(msg)
        self._write(command)
        return self.log if self.log is not None else _log(self.speech)

    def _write(self, command: list[str]) -> None:
        """Write the transcript the real backend would leave behind."""
        if "--output-dir" in command:
            stem = command[command.index("--output-name") + 1]
            target = Path(command[command.index("--output-dir") + 1]) / f"{stem}.json"
            body = self.written or _mlx_json(self.region_segments)
        elif "-of" in command:
            target = Path(command[command.index("-of") + 1]).with_suffix(".json")
            body = self.written or _whisper_json(self.segments)
        else:  # the detection-only run transcribes nothing
            return
        target.write_text(body, encoding="utf-8")


@pytest.fixture
def asr_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pretend whisper.cpp, mlx-whisper and their weights are installed."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "ggml-large-v3-turbo.bin").write_bytes(b"weights")
    (models / "ggml-silero-v5.1.2.bin").write_bytes(b"vad")
    monkeypatch.setenv("VIDPREP_WHISPER_MODEL_DIR", str(models))
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: MLX_BINARY if name == _asr.MLX_BINARY else WHISPER_BINARY,
    )


@pytest.fixture
def transcribable(project_dir: Path) -> Path:
    """A project whose audio-fix output is in place."""
    processed = project_dir / "audio" / "processed.wav"
    processed.parent.mkdir(parents=True)
    processed.write_bytes(b"pretend this is a wav")
    return project_dir


@pytest.fixture
def fake_asr(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeAsr]:
    """Install a fake recogniser; return the factory that configures it."""

    def _install(**overrides: object) -> FakeAsr:
        fake = FakeAsr(**overrides)  # type: ignore[arg-type]
        monkeypatch.setattr(_ffmpeg, "run_analysis", fake.run)
        monkeypatch.setattr(_ffmpeg, "run", fake.run)
        return fake

    return _install


def _profile(root: Path, **asr: str) -> None:
    """Rewrite profile.json with the given ASR settings."""
    profile = Profile()
    profile.asr = profile.asr.model_copy(update=asr)
    project_module.write_json(root / project_module.PROFILE_NAME, profile)


def _transcript(root: Path) -> Transcript:
    return Transcript.model_validate_json((root / "transcript.json").read_bytes())


class TestHappyPath:
    """REQ-001 / REQ-003 / REQ-007 / REQ-008: what one clean run produces."""

    def test_writes_numbered_segments_on_the_original_timeline(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr()

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert [segment.id for segment in result.segments] == [
            "s0001",
            "s0002",
            "s0003",
        ]
        assert [segment.start for segment in result.segments] == [2.34, 5.82, 13.02]

    def test_records_the_backend_and_the_audio_it_ran_on(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr()

        transcribe_module.run_transcribe(project_module.load_project(transcribable))

        transcript = _transcript(transcribable)
        assert transcript.audio_source == "audio/processed.wav"
        assert transcript.asr.model_dump() == {
            "backend": "whisper.cpp",
            "model": "large-v3-turbo",
            "vad": "silero-v5",
        }

    def test_every_segment_starts_as_untouched_asr_output(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr()

        transcribe_module.run_transcribe(project_module.load_project(transcribable))

        segments = _transcript(transcribable).segments
        assert all(
            segment.source == "asr" and segment.edits == [] for segment in segments
        )

    def test_saves_the_speech_regions_for_the_stages_that_join_on_them(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr()

        transcribe_module.run_transcribe(project_module.load_project(transcribable))

        report = VadReport.model_validate_json(
            (transcribable / "report" / "vad.json").read_bytes()
        )
        assert report.backend == "silero-v5"
        assert [(region.start, region.end) for region in report.segments] == list(
            SPEECH
        )

    def test_records_the_stage_with_the_tool_that_produced_it(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr()

        transcribe_module.run_transcribe(project_module.load_project(transcribable))

        record = project_module.load_project(transcribable).manifest.stages[
            "transcribe"
        ]
        assert record.tool_versions == {"whisper.cpp": "1.9.1"}

    def test_detection_runs_before_recognition_on_the_processed_audio(
        self, asr_env, fake_asr, transcribable
    ):
        fake = fake_asr()

        transcribe_module.run_transcribe(project_module.load_project(transcribable))

        command = fake.commands[0]
        assert "--vad" in command
        assert command[command.index("-f") + 1].endswith("audio/processed.wav")

    def test_speech_duration_is_reported_for_the_effect_measurement(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr()

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert result.to_dict()["vad_segments"] == 2
        assert result.to_dict()["speech_duration"] == pytest.approx(14.68)


class TestSegmentBuilding:
    """The boundary conditions design.md §5.2 spells out for the segment list."""

    def test_a_region_with_nothing_said_in_it_produces_no_segment(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(segments=((2.34, 5.82, "  "), (13.02, 19.8, "本題です")))

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert [(segment.id, segment.text) for segment in result.segments] == [
            ("s0001", "本題です")
        ]

    def test_a_single_region_needs_no_offset_correction(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(speech=((2.34, 10.01),), segments=((2.34, 5.82, "はじめまして"),))

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert [(segment.start, segment.end) for segment in result.segments] == [
            (2.34, 5.82)
        ]

    def test_an_end_on_the_source_duration_is_accepted(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(
            speech=((290.0, 298.92),), segments=((290.5, 298.92, "最後の一言でした"),)
        )

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert result.segments[-1].end == 298.92

    def test_a_region_running_past_the_material_is_trimmed_to_it(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(
            speech=((290.0, 299.4),), segments=((290.5, 298.0, "最後の一言でした"),)
        )

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert result.speech[-1].end == 298.92

    def test_timestamps_running_backwards_stop_the_stage(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(segments=((13.02, 19.8, "あとの発話"), (2.34, 5.82, "まえの発話")))

        with pytest.raises(AsrFailedError, match=r"out of order"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

        assert not (transcribable / "transcript.json").exists()


class TestVerification:
    """REQ-040 / REQ-042: a transcript that claims unspoken words is discarded."""

    def test_a_segment_starting_outside_every_region_is_refused(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(segments=((2.34, 5.82, "実際の発話"), (11.0, 12.5, "無音の中の声")))

        with pytest.raises(InvariantViolationError, match=r"outside every detected"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

        assert not (transcribable / "transcript.json").exists()

    def test_the_boundary_overlap_whisper_carries_over_is_accepted(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(segments=((2.34, 5.82, "実際の発話"), (10.08, 12.5, "境界の発話")))

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert len(result.segments) == 2

    def test_a_start_stranded_between_two_regions_is_moved_onto_the_speech(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(speech=STRANDED_SPEECH, segments=STRANDED_SEGMENTS)

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert [segment.start for segment in result.segments] == [216.46, 223.27]
        assert result.segments[1].end == 230.0
        assert result.segments[1].text == STRANDED_SEGMENTS[1][2]

    def test_moving_a_stranded_start_is_reported_rather_than_done_quietly(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(speech=STRANDED_SPEECH, segments=STRANDED_SEGMENTS)

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert len(result.warnings) == 1
        assert "s0002" in result.warnings[0]
        assert "222.390 → 223.270" in result.warnings[0]
        assert any(line.startswith("⚠") for line in result.lines())

    def test_a_start_in_the_silence_barely_reaching_speech_is_still_refused(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(
            segments=((2.34, 5.82, "実際の発話"), (11.0, 14.0, "無音の中の作り話"))
        )

        with pytest.raises(InvariantViolationError, match=r"outside every detected"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

        assert not (transcribable / "transcript.json").exists()

    def test_a_known_hallucination_over_silence_is_refused(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(
            speech=((2.34, 10.01), (12.99, 20.0), (25.0, 26.0)),
            segments=(
                (2.34, 5.82, "実際の発話"),
                (25.0, 40.0, "ご視聴ありがとうございました"),
            ),
        )

        with pytest.raises(InvariantViolationError, match=r"known hallucination"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    def test_the_same_words_really_spoken_are_kept(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(
            speech=((2.34, 10.01), (12.99, 20.0)),
            segments=((13.0, 19.0, "ご視聴ありがとうございました"),),
        )

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert result.segments[0].text == "ご視聴ありがとうございました"

    def test_the_phrase_list_ships_with_the_package(self):
        assert (
            "ご視聴ありがとうございました" in transcribe_module.hallucination_phrases()
        )

    def test_a_phrase_list_that_lost_its_shape_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            transcribe_module, "HALLUCINATION_RESOURCE", "dictionaries/asr-dict.json"
        )
        transcribe_module.hallucination_phrases.cache_clear()
        try:
            with pytest.raises(SchemaInvalidError, match=r"asr-dict.json"):
                transcribe_module.hallucination_phrases()
        finally:
            transcribe_module.hallucination_phrases.cache_clear()


class TestFailures:
    """REQ-020 / REQ-021 / REQ-022: what stops the stage, and what survives it."""

    def test_missing_processed_audio_asks_for_audio_fix(self, asr_env, project_dir):
        with pytest.raises(UsageError, match=r"vidprep audio-fix"):
            transcribe_module.run_transcribe(project_module.load_project(project_dir))

    def test_a_backend_that_fails_leaves_the_previous_transcript(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr()
        transcribe_module.run_transcribe(project_module.load_project(transcribable))
        before = (transcribable / "transcript.json").read_bytes()
        fake_asr(exit_code=1)

        with pytest.raises(AsrFailedError, match=r"exited with 1"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

        assert (transcribable / "transcript.json").read_bytes() == before

    def test_silence_only_material_is_a_failure_not_an_empty_transcript(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(speech=(), segments=())

        with pytest.raises(AsrFailedError, match=r"no speech \(0 regions\)"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

        assert not (transcribable / "transcript.json").exists()

    def test_a_build_without_vad_support_is_refused(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(log="whisper_print_timings: total time = 100.00 ms\n")

        with pytest.raises(AsrFailedError, match=r"must support --vad"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(('{"nothing": true}', r"cannot read"), id="no-segment-list"),
            pytest.param(("not json at all", r"no readable transcript"), id="not-json"),
            pytest.param(
                ('["a segment"]', r"is not a JSON object"), id="not-an-object"
            ),
            pytest.param(
                ('{"transcription": ["oops"]}', r"cannot read: expected"),
                id="bad-entry",
            ),
        ],
    )
    def test_an_unreadable_transcript_is_refused(
        self, asr_env, fake_asr, transcribable, case
    ):
        written, expected = case
        fake_asr(written=written)

        with pytest.raises(AsrFailedError, match=expected):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    def test_missing_vad_weights_stop_the_stage(
        self, asr_env, fake_asr, transcribable, tmp_path
    ):
        (tmp_path / "models" / "ggml-silero-v5.1.2.bin").unlink()

        with pytest.raises(UsageError, match=r"detection is mandatory"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    def test_a_model_the_profile_names_but_nobody_installed_is_reported(
        self, asr_env, fake_asr, transcribable
    ):
        _profile(transcribable, model="large-v9")

        with pytest.raises(UsageError, match=r"whisper.cpp model 'large-v9'"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    def test_whisper_cpp_is_required_even_for_the_other_backend(
        self, asr_env, transcribable, monkeypatch
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        monkeypatch.setattr(shutil, "which", lambda _: None)

        with pytest.raises(UsageError, match=r"mandatory Silero VAD front-end"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    def test_a_missing_mlx_whisper_is_reported(
        self, asr_env, transcribable, monkeypatch
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: None if name == _asr.MLX_BINARY else WHISPER_BINARY,
        )

        with pytest.raises(UsageError, match=r"mlx_whisper not found"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    def test_detection_needs_a_model_to_host_it(self, asr_env, transcribable, tmp_path):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        (tmp_path / "models" / "ggml-large-v3-turbo.bin").unlink()

        with pytest.raises(UsageError, match=r"hosts the Silero front-end"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    def test_a_segment_that_cannot_exist_stops_the_stage(
        self, asr_env, fake_asr, transcribable
    ):
        fake_asr(speech=((0.0, 10.0),), segments=((-1.0, 5.0, "ありえない時刻"),))

        with pytest.raises(AsrFailedError, match=r"cannot place"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

    def test_a_backend_that_cannot_be_asked_for_its_version_is_still_recorded(
        self, asr_env, fake_asr, transcribable, monkeypatch
    ):
        fake_asr()

        def _gone(args, timeout=0.0):
            msg = f"{args[0]} was not found on PATH"
            raise UsageError(msg)

        monkeypatch.setattr(_ffmpeg, "run", _gone)

        transcribe_module.run_transcribe(project_module.load_project(transcribable))

        stages = project_module.load_project(transcribable).manifest.stages
        assert stages["transcribe"].tool_versions == {"whisper.cpp": "unknown"}

    def test_a_backend_that_will_not_say_its_version_is_still_recorded(
        self, asr_env, fake_asr, transcribable, monkeypatch
    ):
        fake_asr()
        monkeypatch.setattr(_asr, "_VERSION", re.compile(r"never matches (\S+)"))

        transcribe_module.run_transcribe(project_module.load_project(transcribable))

        stages = project_module.load_project(transcribable).manifest.stages
        assert stages["transcribe"].tool_versions == {"whisper.cpp": "unknown"}


class TestMlxBackend:
    """REQ-006: the second backend transcribes the same regions."""

    def test_every_detected_region_is_extracted_and_transcribed_on_its_own(
        self, asr_env, fake_asr, transcribable
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        fake = fake_asr()

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        detect, extract, recognise = fake.commands[:3]
        assert "--vad" in detect
        assert extract[extract.index("-ss") + 1 : extract.index("-ss") + 4] == [
            "2.340",
            "-t",
            "7.670",
        ]
        assert recognise[0] == MLX_BINARY
        assert result.backend == "mlx-whisper"

    def test_region_relative_timestamps_are_corrected_to_the_original_timeline(
        self, asr_env, fake_asr, transcribable
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        fake_asr()

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        # 0.0 / 3.48 in each of the regions starting at 2.34 and at 12.99.
        assert [segment.start for segment in result.segments] == [
            2.34,
            5.82,
            12.99,
            16.47,
        ]

    def test_what_whisper_reads_out_of_the_padding_is_dropped(
        self, asr_env, fake_asr, transcribable
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        fake_asr(
            speech=((2.34, 10.01),),
            region_segments=((0.0, 3.48, "実際の発話"), (9.0, 12.0, "無音の中の声")),
        )

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert [(segment.start, segment.text) for segment in result.segments] == [
            (2.34, "実際の発話")
        ]

    def test_a_segment_running_past_its_region_is_held_inside_it(
        self, asr_env, fake_asr, transcribable
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        fake_asr(
            speech=((2.34, 10.01),), region_segments=((0.0, 30.0, "長すぎる終端"),)
        )

        result = transcribe_module.run_transcribe(
            project_module.load_project(transcribable)
        )

        assert (result.segments[0].start, result.segments[0].end) == (2.34, 10.01)

    def test_the_transcript_names_the_backend_that_produced_it(
        self, asr_env, fake_asr, transcribable
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        fake_asr()

        transcribe_module.run_transcribe(project_module.load_project(transcribable))

        assert _transcript(transcribable).asr.backend == "mlx-whisper"

    def test_silence_only_material_never_reaches_the_recogniser(
        self, asr_env, fake_asr, transcribable
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")
        fake = fake_asr(speech=(), segments=())

        with pytest.raises(AsrFailedError, match=r"no speech"):
            transcribe_module.run_transcribe(project_module.load_project(transcribable))

        assert len(fake.commands) == 1


class TestCommandLine:
    """REQ-002 / REQ-005: the interface, and the plan --dry-run prints."""

    def test_help_offers_no_way_to_skip_detection(self, run_cli):
        result = run_cli("transcribe", "--help")

        assert "--vad" not in result.stdout
        assert "Silero VAD" in result.stdout

    def test_dry_run_shows_the_backend_command_and_writes_nothing(
        self, asr_env, run_cli, transcribable
    ):
        result = run_cli("transcribe", "-p", str(transcribable), "--dry-run", "--json")

        plan = json.loads(result.stdout)
        assert plan["backend"] == "whisper.cpp"
        assert any("--vad" in command for command in plan["commands"])
        assert not (transcribable / "transcript.json").exists()

    def test_dry_run_leaves_the_region_it_would_cut_open(
        self, asr_env, run_cli, transcribable
    ):
        _profile(transcribable, backend="mlx-whisper", model="mlx-community/turbo")

        result = run_cli("transcribe", "-p", str(transcribable), "--dry-run", "--json")

        commands = json.loads(result.stdout)["commands"]
        assert len(commands) == 3
        assert _asr.PLACEHOLDER_START in commands[1]

    def test_json_reports_the_completion_conditions(
        self, asr_env, fake_asr, run_cli, transcribable
    ):
        fake_asr()

        result = run_cli("transcribe", "-p", str(transcribable), "--json")

        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)
        assert payload["vad_outside_starts"] == 0
        assert payload["hallucination_hits"] == []
        assert payload["anchored_starts"] == 0
        assert payload["output"] == "transcript.json"

    def test_json_names_a_start_that_was_moved_onto_its_region(
        self, asr_env, fake_asr, run_cli, transcribable
    ):
        fake_asr(speech=STRANDED_SPEECH, segments=STRANDED_SEGMENTS)

        result = run_cli("transcribe", "-p", str(transcribable), "--json")

        assert result.exit_code == EXIT_OK
        payload = json.loads(result.stdout)
        assert payload["anchored_starts"] == 1
        assert "s0002" in payload["warnings"][0]

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(({}, EXIT_USAGE), id="audio-fix-not-run"),
            pytest.param(({"exit_code": 1}, EXIT_EXECUTION), id="backend-failed"),
            pytest.param(({"speech": ()}, EXIT_EXECUTION), id="no-speech"),
            pytest.param(
                ({"segments": ((11.0, 12.5, "無音の中の声"),)}, EXIT_VALIDATION),
                id="outside-the-speech",
            ),
        ],
    )
    def test_each_failure_exits_with_its_own_code(
        self, asr_env, fake_asr, run_cli, project_dir, case
    ):
        failure, expected = case
        if failure:
            processed = project_dir / "audio" / "processed.wav"
            processed.parent.mkdir(parents=True)
            processed.write_bytes(b"pretend this is a wav")
        fake_asr(**failure)

        result = run_cli("transcribe", "-p", str(project_dir))

        assert result.exit_code == expected

    def test_a_stale_upstream_stage_warns_without_blocking(
        self, asr_env, fake_asr, run_cli, transcribable
    ):
        loaded = project_module.load_project(transcribable)
        project_module.record_stage(loaded, "audio_fix")
        changed = loaded.profile
        changed.audio.highpass_hz = 120
        project_module.write_json(transcribable / "profile.json", changed)
        fake_asr()

        result = run_cli("transcribe", "-p", str(transcribable))

        assert result.exit_code == EXIT_OK
        assert "may be stale" in result.stdout
