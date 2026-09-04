"""Tests for the ``vidprep prep`` orchestrator.

The pipeline itself is not run here — that needs the material and four external
tools. What is checked is the orchestration: which stages it decides to run,
where it stops for the transcript to be proofread, which cut candidates it
approves on a reviewer's behalf, and that what it delivers lands beside the
source material. Every invocation goes through the CLI (``run_cli``), so the
exit codes and the streamed output are what the installed command actually
produces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from vidprep import audio, correct, detect, prep, render, report, transcribe
from vidprep import project as project_module
from vidprep.errors import EXIT_EXECUTION, EXIT_OK, EXIT_USAGE, EXIT_VALIDATION
from vidprep.models import Cut, Cuts

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vidprep.project import Project

    from .conftest import CliResult

STAGE_MODULES = (audio, transcribe, correct, detect, render, report)


class FakeStage:
    """A stage result the orchestrator can report."""

    def __init__(self, name: str) -> None:
        self.name = name

    def lines(self) -> list[str]:
        return [f"✔ {self.name} finished"]


class FakeVerified:
    """A ``--verify-asr`` comparison that did or did not flag a boundary."""

    def __init__(self, *, flagged: bool, mode: str = "gate") -> None:
        self.mode = mode
        self.flags = ("boundary",) if flagged else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "near_boundary_flags": len(self.flags),
            "gating_flags": len(self.flags),
        }


class FakeRender(FakeStage):
    """A render result, whose ``verified`` decides the CLI's exit code."""

    def __init__(self, verified: Any = None) -> None:
        super().__init__(render.STAGE)
        self.verified = verified

    def to_dict(self) -> dict[str, Any]:
        verified = (
            {} if self.verified is None else {"verify_asr": self.verified.to_dict()}
        )
        return {"stage": self.name, **verified}


def make_cuts(*entries: tuple[str, float, float, str, str]) -> Cuts:
    """Build a ``cuts.json`` document from ``(id, start, end, reason, status)``."""
    return Cuts(
        cuts=[
            Cut(id=cut_id, start=start, end=end, reason=reason, status=status)  # type: ignore[arg-type]
            for cut_id, start, end, reason, status in entries
        ]
    )


@pytest.fixture
def video(tmp_path: Path, source_video: Path, fake_probe: None) -> Path:
    """The source material, with no project beside it yet."""
    return source_video


@pytest.fixture
def run(run_cli: Callable[..., CliResult]) -> Callable[..., CliResult]:
    """Run ``vidprep prep`` over one video the way the installed command would."""

    def _run(video: Path, *args: str) -> CliResult:
        return run_cli("prep", str(video), *args)

    return _run


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every stage with a fake that only records itself in the manifest.

    Returns:
        The names of the stages that ran, in order, appended to as they run.
    """
    ran: list[str] = []

    def _fake(module: Any, attribute: str, stage: str) -> None:
        def _run(loaded: Project, **_: Any) -> FakeStage:
            ran.append(stage)
            project_module.record_stage(loaded, stage)
            return FakeStage(stage)

        monkeypatch.setattr(module, attribute, _run)

    def _run_detect(loaded: Project, **_: Any) -> FakeStage:
        ran.append(detect.STAGE)
        write_cuts(loaded.root, make_cuts(("c0001", 1.0, 3.0, "silence", "approved")))
        project_module.record_stage(loaded, detect.STAGE)
        return FakeStage(detect.STAGE)

    def _run_report(loaded: Project, **_: Any) -> FakeStage:
        # `report` records no stage, exactly as the real one does not: it is
        # recognised as finished by the statistics document it leaves behind.
        ran.append(report.STAGE)
        stats = loaded.root / report.STATS_NAME
        stats.parent.mkdir(parents=True, exist_ok=True)
        stats.write_text('{"version": "2"}\n', encoding="utf-8")
        return FakeStage(report.STAGE)

    _fake(audio, "run_audio_fix", audio.STAGE)
    _fake(transcribe, "run_transcribe", transcribe.STAGE)
    monkeypatch.setattr(detect, "run_detect", _run_detect)
    monkeypatch.setattr(report, "run_report", _run_report)
    monkeypatch.setattr(prep, "_dictionary_pass", _dictionary(ran))
    monkeypatch.setattr(render, "run_render", _render(ran))
    return ran


def _dictionary(ran: list[str]) -> Callable[[Project], FakeStage]:
    """A ``correct`` stage that records itself without reading a transcript."""

    def _run(loaded: Project) -> FakeStage:
        ran.append(correct.STAGE)
        project_module.record_stage(loaded, correct.STAGE)
        return FakeStage(correct.STAGE)

    return _run


def _render(ran: list[str], verified: Any = None) -> Callable[..., FakeRender]:
    """A ``render`` stage that writes the two files the delivery copies."""

    def _run(loaded: Project, **_: Any) -> FakeRender:
        ran.append(render.STAGE)
        for name, text in (
            (render.VIDEO_NAME, "encoded"),
            (render.SUBTITLES_NAME, "1\n00:00:00,000 --> 00:00:01,000\nhi\n"),
            (render.TEXT_NAME, "[00:00] hi\n"),
        ):
            target = loaded.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        project_module.record_stage(loaded, render.STAGE)
        return FakeRender(verified)

    return _run


def cuts_of(project: Path) -> Cuts:
    """Read back the cut document the orchestrator left behind."""
    return Cuts.model_validate_json((project / detect.CUTS_NAME).read_bytes())


def write_cuts(project: Path, document: Cuts) -> None:
    """Put *document* in the project as ``detect`` would have."""
    project_module.write_json(project / detect.CUTS_NAME, document)


class TestProjectLocation:
    def test_the_project_sits_beside_the_source_named_after_it(
        self, tmp_path: Path
    ) -> None:
        assert prep.project_for(tmp_path / "talk01.mp4") == tmp_path / "talk01.vidprep"

    def test_a_source_that_is_not_there_is_a_usage_error(
        self, run: Callable[..., CliResult], tmp_path: Path
    ) -> None:
        assert run(tmp_path / "absent.mp4").exit_code == EXIT_USAGE

    def test_the_first_run_creates_the_project(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")

        assert (prep.project_for(video) / project_module.MANIFEST_NAME).is_file()


class TestLlmPause:
    def test_the_run_that_transcribes_stops_for_proofreading(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        result = run(video)

        assert result.exit_code == EXIT_OK
        assert stages == [audio.STAGE, transcribe.STAGE, correct.STAGE]
        assert "/correct-transcript" in result.stdout

    def test_the_next_run_continues_instead_of_asking_again(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video)
        stages.clear()

        result = run(video)

        assert result.exit_code == EXIT_OK
        assert stages == [detect.STAGE, render.STAGE, report.STAGE]

    def test_yes_runs_the_whole_pipeline_in_one_go(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        result = run(video, "--yes")

        assert result.exit_code == EXIT_OK
        assert stages == [
            audio.STAGE,
            transcribe.STAGE,
            correct.STAGE,
            detect.STAGE,
            render.STAGE,
            report.STAGE,
        ]


class TestResuming:
    def test_a_report_that_left_no_stage_record_is_still_recognised_as_finished(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")
        assert (
            report.STAGE
            not in project_module.load_project(prep.project_for(video)).manifest.stages
        )
        stages.clear()

        run(video, "--yes")

        assert stages == []

    def test_a_report_whose_statistics_are_gone_runs_again(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")
        (prep.project_for(video) / report.STATS_NAME).unlink()
        stages.clear()

        run(video, "--yes")

        assert stages == [report.STAGE]

    def test_a_finished_pipeline_re_runs_nothing(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")
        stages.clear()

        result = run(video, "--yes")

        assert result.exit_code == EXIT_OK
        assert stages == []

    def test_a_changed_parameter_re_runs_that_stage_and_everything_after_it(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")
        project = prep.project_for(video)
        loaded = project_module.load_project(project)
        loaded.profile.silence.min_duration = 1.5
        project_module.write_json(project / project_module.PROFILE_NAME, loaded.profile)
        stages.clear()

        run(video, "--yes")

        assert stages == [detect.STAGE, render.STAGE, report.STAGE]

    def test_a_changed_parameter_does_not_re_run_the_stages_before_it(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")
        project = prep.project_for(video)
        loaded = project_module.load_project(project)
        loaded.profile.render.crf = 20
        project_module.write_json(project / project_module.PROFILE_NAME, loaded.profile)
        stages.clear()

        run(video, "--yes")

        assert stages == [render.STAGE, report.STAGE]


class TestEditsBetweenRunsReachTheOutput:
    """An artifact rewritten between two runs is not left out of the render.

    Exercised here with ``cuts.json``, which is what the faked pipeline
    produces; ``transcript.json`` takes the same path and is the case that
    motivates it. ``correct --apply-patch`` and a hand-edited cut status both
    run outside the pipeline: no stage lands in the invocation's ran-set and no
    profile value moves, so before inputs were hashed the render was skipped as
    up to date and the delivered subtitles kept the old wording — silently,
    with a zero exit code.
    """

    def test_an_artifact_edited_between_runs_re_renders(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")
        write_cuts(
            prep.project_for(video),
            make_cuts(("c0001", 1.0, 3.5, "silence", "approved")),
        )
        stages.clear()

        result = run(video, "--yes")

        assert result.exit_code == EXIT_OK
        assert render.STAGE in stages

    def test_an_untouched_project_re_runs_nothing(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")
        stages.clear()

        run(video, "--yes")

        assert stages == []


class TestFillerApproval:
    @pytest.fixture
    def detected(self, monkeypatch: pytest.MonkeyPatch, stages: list[str]) -> Cuts:
        """A ``detect`` that proposes one silence cut and two filler cuts."""
        document = make_cuts(
            ("c0001", 1.0, 3.0, "silence", "approved"),
            ("c0002", 5.0, 5.4, "filler", "proposed"),
            ("c0003", 9.0, 9.5, "filler", "rejected"),
        )

        def _run(loaded: Project) -> FakeStage:
            stages.append(detect.STAGE)
            write_cuts(loaded.root, document)
            project_module.record_stage(loaded, detect.STAGE)
            return FakeStage(detect.STAGE)

        monkeypatch.setattr(detect, "run_detect", _run)
        return document

    def test_a_proposed_filler_cut_is_approved(
        self, run: Callable[..., CliResult], video: Path, detected: Cuts
    ) -> None:
        run(video, "--yes")

        statuses = {cut.id: cut.status for cut in cuts_of(prep.project_for(video)).cuts}
        assert statuses["c0002"] == "approved"

    def test_a_rejected_filler_cut_keeps_its_verdict(
        self, run: Callable[..., CliResult], video: Path, detected: Cuts
    ) -> None:
        run(video, "--yes")

        statuses = {cut.id: cut.status for cut in cuts_of(prep.project_for(video)).cuts}
        assert statuses["c0003"] == "rejected"

    def test_keep_fillers_leaves_every_proposal_alone(
        self, run: Callable[..., CliResult], video: Path, detected: Cuts
    ) -> None:
        run(video, "--yes", "--keep-fillers")

        statuses = {cut.id: cut.status for cut in cuts_of(prep.project_for(video)).cuts}
        assert statuses["c0002"] == "proposed"

    def test_the_weak_tier_being_enabled_declines_the_approval(
        self, run: Callable[..., CliResult], video: Path, detected: Cuts
    ) -> None:
        run(video, "--yes", "--keep-fillers")
        project = prep.project_for(video)
        loaded = project_module.load_project(project)
        loaded.profile.filler.enable_weak = True
        project_module.write_json(project / project_module.PROFILE_NAME, loaded.profile)

        result = run(video, "--yes")

        statuses = {cut.id: cut.status for cut in cuts_of(project).cuts}
        assert statuses["c0002"] == "proposed"
        assert "filler.enable_weak" in result.stdout

    def test_approving_a_filler_cut_re_renders(
        self,
        run: Callable[..., CliResult],
        video: Path,
        detected: Cuts,
        stages: list[str],
    ) -> None:
        run(video, "--yes")
        stages.clear()
        write_cuts(
            prep.project_for(video),
            make_cuts(("c0004", 20.0, 20.6, "filler", "proposed")),
        )

        run(video, "--yes")

        assert stages == [render.STAGE, report.STAGE]


class TestDelivery:
    def test_the_render_and_its_subtitles_land_beside_the_source(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")

        assert (video.with_name(f"{video.stem}.edited.mp4")).is_file()
        assert (video.with_name(f"{video.stem}.srt")).read_text(encoding="utf-8")
        assert (video.with_name(f"{video.stem}.txt")).read_text(encoding="utf-8")

    def test_a_second_run_replaces_what_the_first_delivered(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        delivered = video.with_name(f"{video.stem}.edited.mp4")
        run(video, "--yes")
        delivered.write_text("stale", encoding="utf-8")
        project = prep.project_for(video)
        (project / render.VIDEO_NAME).write_text("re-encoded", encoding="utf-8")

        run(video, "--yes")

        assert delivered.read_text(encoding="utf-8") == "re-encoded"

    def test_a_render_that_produced_nothing_is_reported(
        self,
        run: Callable[..., CliResult],
        video: Path,
        stages: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            render,
            "run_render",
            lambda loaded, **_: FakeRender(),  # noqa: ARG005
        )

        assert run(video, "--yes").exit_code == EXIT_USAGE

    def test_a_render_missing_only_the_transcript_is_reported(
        self,
        run: Callable[..., CliResult],
        video: Path,
        stages: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _run(loaded: Project, **_: Any) -> FakeRender:
            for name, text in (
                (render.VIDEO_NAME, "encoded"),
                (render.SUBTITLES_NAME, "1\n00:00:00,000 --> 00:00:01,000\nhi\n"),
            ):
                target = loaded.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            project_module.record_stage(loaded, render.STAGE)
            return FakeRender()

        monkeypatch.setattr(render, "run_render", _run)

        result = run(video, "--yes")

        assert result.exit_code == EXIT_USAGE
        assert "vidprep render" in result.stderr


class TestVerifyAsrGate:
    def test_a_flagged_boundary_fails_the_run_but_still_delivers(
        self,
        run: Callable[..., CliResult],
        video: Path,
        stages: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            render, "run_render", _render(stages, FakeVerified(flagged=True))
        )

        result = run(video, "--yes")

        assert result.exit_code == EXIT_VALIDATION
        assert video.with_name(f"{video.stem}.edited.mp4").is_file()

    def test_an_advisory_flag_leaves_the_exit_code_alone(
        self,
        run: Callable[..., CliResult],
        video: Path,
        stages: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            render,
            "run_render",
            _render(stages, FakeVerified(flagged=True, mode="advisory")),
        )

        assert run(video, "--yes").exit_code == EXIT_OK

    def test_no_verify_asr_is_passed_through_to_the_render(
        self,
        run: Callable[..., CliResult],
        video: Path,
        stages: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        asked: list[bool] = []

        def _run(loaded: Project, **kwargs: Any) -> FakeRender:
            asked.append(kwargs["verify_asr"])
            return _render(stages)(loaded)

        monkeypatch.setattr(render, "run_render", _run)
        run(video, "--yes", "--no-verify-asr")

        assert asked == [False]


class TestUnexpectedFailures:
    def test_a_failure_no_stage_models_is_reported_with_its_type(
        self,
        run: Callable[..., CliResult],
        video: Path,
        stages: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _explode(loaded: Project, **_: Any) -> FakeStage:
            msg = "the disk went away"
            raise OSError(msg)

        monkeypatch.setattr(audio, "run_audio_fix", _explode)

        result = run(video, "--yes")

        assert result.exit_code == EXIT_EXECUTION
        assert "OSError" in result.stderr


class TestDryRun:
    def test_a_fresh_video_stops_the_plan_at_the_pause(
        self, run: Callable[..., CliResult], video: Path
    ) -> None:
        result = run(video, "--dry-run")

        assert result.exit_code == EXIT_OK
        assert not prep.project_for(video).exists()
        assert "would run: vidprep audio-fix" in result.stdout
        assert "would run: vidprep correct" in result.stdout
        assert "would run: vidprep render" not in result.stdout
        assert "would stop" in result.stdout

    def test_yes_plans_every_stage(
        self, run: Callable[..., CliResult], video: Path
    ) -> None:
        result = run(video, "--dry-run", "--yes")

        assert result.exit_code == EXIT_OK
        assert "would run: vidprep report" in result.stdout

    def test_a_finished_pipeline_plans_nothing_but_delivery(
        self, run: Callable[..., CliResult], video: Path, stages: list[str]
    ) -> None:
        run(video, "--yes")
        stages.clear()

        result = run(video, "--dry-run", "--yes")

        assert result.exit_code == EXIT_OK
        assert "would run" not in result.stdout
        assert "would write" in result.stdout
        assert f"{video.stem}.txt" in result.stdout
