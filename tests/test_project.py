"""Tests for project creation, verification and stage bookkeeping."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vidprep import project
from vidprep.errors import (
    HashMismatchError,
    NotAProjectError,
    SchemaInvalidError,
    UsageError,
)
from vidprep.models import Profile

from .conftest import SAMPLE_DURATION

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "vidprep"


@pytest.mark.usefixtures("fake_probe")
class TestInit:
    """REQ-010 / REQ-011: init records the source specs without touching it."""

    def test_writes_manifest_and_profile(self, tmp_path, source_video):
        created = project.init_project(tmp_path / "work", source_video)

        assert (created.root / "vidprep.json").is_file()
        assert (created.root / "profile.json").is_file()
        assert created.manifest.source.duration == SAMPLE_DURATION
        assert created.manifest.source.video.fps == "25/1"
        assert created.manifest.source.sha256 == project.sha256_file(source_video)

    def test_written_profile_holds_the_packaged_defaults(self, tmp_path, source_video):
        created = project.init_project(tmp_path / "work", source_video)
        written = json.loads((created.root / "profile.json").read_text())

        assert written == Profile().model_dump(mode="json")

    def test_source_is_referenced_by_absolute_path_and_left_alone(
        self, tmp_path, source_video
    ):
        before = source_video.read_bytes()
        created = project.init_project(tmp_path / "work", source_video)

        assert created.manifest.source.path == str(source_video)
        assert created.source_path == source_video
        assert source_video.read_bytes() == before

    def test_copy_source_imports_the_material(self, tmp_path, source_video):
        created = project.init_project(
            tmp_path / "work", source_video, copy_source=True
        )
        imported = created.root / "source" / source_video.name

        assert imported.is_file()
        assert created.manifest.source.path == f"source/{source_video.name}"
        assert created.source_path == imported

    def test_missing_source_is_a_usage_error(self, tmp_path):
        with pytest.raises(UsageError, match="source material not found"):
            project.init_project(tmp_path / "work", tmp_path / "absent.mp4")

    def test_non_empty_target_directory_is_a_usage_error(self, tmp_path, source_video):
        target = tmp_path / "work"
        target.mkdir()
        (target / "keepme.txt").write_text("existing work")

        with pytest.raises(UsageError, match="not an empty directory"):
            project.init_project(target, source_video)

    def test_plan_lists_the_command_and_the_writes(self, tmp_path, source_video):
        plan = project.init_plan(tmp_path / "work", source_video, copy_source=True)

        assert plan["commands"][0][0] == "ffprobe"
        assert str(tmp_path / "work" / "vidprep.json") in plan["writes"]
        assert str(tmp_path / "work" / "source" / source_video.name) in plan["writes"]


class TestLoad:
    def test_missing_manifest_is_not_a_project(self, tmp_path):
        with pytest.raises(NotAProjectError, match="is not a vidprep project"):
            project.load_project(tmp_path)

    def test_missing_profile_is_a_usage_error(self, project_dir):
        (project_dir / "profile.json").unlink()

        with pytest.raises(UsageError, match=r"missing profile\.json"):
            project.load_project(project_dir)

    def test_corrupt_manifest_is_a_schema_error(self, project_dir):
        (project_dir / "vidprep.json").write_text("{not json")

        with pytest.raises(SchemaInvalidError, match=r"vidprep\.json"):
            project.load_project(project_dir)

    def test_defaults_to_the_current_directory(self, project_dir, monkeypatch):
        monkeypatch.chdir(project_dir)

        assert project.load_project().root == project_dir


class TestVerifySource:
    """REQ-012: the material is re-hashed before every stage."""

    def test_untouched_source_passes(self, project_dir):
        project.verify_source(project.load_project(project_dir))

    def test_replaced_source_is_detected(self, project_dir):
        loaded = project.load_project(project_dir)
        loaded.source_path.write_bytes(b"a completely different recording")

        with pytest.raises(HashMismatchError, match="sha256 mismatch"):
            project.verify_source(loaded)

    def test_removed_source_is_a_usage_error(self, project_dir):
        loaded = project.load_project(project_dir)
        loaded.source_path.unlink()

        with pytest.raises(UsageError, match="source material not found"):
            project.verify_source(loaded)


class TestValidateArtifacts:
    def test_absent_artifacts_are_skipped(self, project_dir):
        assert project.validate_artifacts(project.load_project(project_dir)) == []

    def test_valid_artifacts_are_reported(self, project_dir):
        (project_dir / "cuts.json").write_text(
            json.dumps(
                {
                    "version": "1",
                    "cuts": [
                        {
                            "id": "c0001",
                            "start": 1.0,
                            "end": 2.0,
                            "reason": "silence",
                            "status": "approved",
                        }
                    ],
                }
            )
        )

        assert project.validate_artifacts(project.load_project(project_dir)) == [
            "cuts.json"
        ]

    def test_interval_past_the_manifest_duration_is_rejected(self, project_dir):
        (project_dir / "cuts.json").write_text(
            json.dumps(
                {
                    "version": "1",
                    "cuts": [
                        {
                            "id": "c0001",
                            "start": 1.0,
                            "end": SAMPLE_DURATION + 5,
                            "reason": "silence",
                        }
                    ],
                }
            )
        )

        with pytest.raises(SchemaInvalidError, match="past the source duration"):
            project.validate_artifacts(project.load_project(project_dir))


class TestAtomicWrite:
    """REQ-025: a failed write never damages the previous version."""

    def test_existing_file_survives_a_failed_replace(self, tmp_path, monkeypatch):
        target = tmp_path / "cuts.json"
        target.write_text("previous good content")

        def explode(*args: object, **kwargs: object) -> None:
            msg = "disk gave up"
            raise OSError(msg)

        monkeypatch.setattr(os, "replace", explode)

        with pytest.raises(OSError, match="disk gave up"):
            project.atomic_write_text(target, "half written")

        assert target.read_text() == "previous good content"
        assert list(tmp_path.iterdir()) == [target]

    def test_successful_write_leaves_no_temporary_file(self, tmp_path):
        target = tmp_path / "cuts.json"
        project.atomic_write_text(target, "content")

        assert target.read_text() == "content"
        assert list(tmp_path.iterdir()) == [target]


class TestStageRecords:
    """REQ-013 / REQ-014: provenance is recorded and staleness is a warning."""

    def test_record_stage_persists_the_record(self, project_dir):
        loaded = project.load_project(project_dir)

        updated = project.record_stage(loaded, "audio_fix", {"ffmpeg": "7.1"})

        assert updated.manifest.stages["audio_fix"].tool_versions == {"ffmpeg": "7.1"}
        reloaded = project.load_project(project_dir)
        assert reloaded.manifest.stages["audio_fix"].params_sha256 == (
            project.stage_params_sha256(reloaded.profile, "audio_fix")
        )

    def test_recording_a_stage_keeps_earlier_records(self, project_dir):
        loaded = project.record_stage(project.load_project(project_dir), "audio_fix")

        updated = project.record_stage(loaded, "transcribe")

        assert set(updated.manifest.stages) == {"audio_fix", "transcribe"}

    def test_params_hash_tracks_only_the_relevant_profile_sections(self):
        profile = Profile()
        other = profile.model_copy(
            update={"subtitle": profile.subtitle.model_copy(update={"max_lines": 3})}
        )

        assert project.stage_params_sha256(profile, "audio_fix") == (
            project.stage_params_sha256(other, "audio_fix")
        )
        assert project.stage_params_sha256(profile, "render") != (
            project.stage_params_sha256(other, "render")
        )

    def test_no_warning_while_the_profile_is_unchanged(self, project_dir):
        project.record_stage(project.load_project(project_dir), "audio_fix")

        reloaded = project.load_project(project_dir)
        assert project.stale_upstream_warnings(reloaded, "transcribe") == []

    def test_changed_profile_warns_the_downstream_stage(self, project_dir):
        project.record_stage(project.load_project(project_dir), "audio_fix")
        changed = Profile()
        changed.audio.highpass_hz = 120
        project.write_json(project_dir / "profile.json", changed)

        warnings = project.stale_upstream_warnings(
            project.load_project(project_dir), "transcribe"
        )

        assert len(warnings) == 1
        assert "audio_fix" in warnings[0]

    def test_stages_that_never_ran_are_not_reported(self, project_dir):
        loaded = project.load_project(project_dir)

        assert project.stale_upstream_warnings(loaded, "render") == []


class TestSubprocessIsolation:
    """REQ-030: pipeline stages spawn processes only through the wrapper."""

    #: ``doctor`` probes external tools that are not ffmpeg — and must record a
    #: failure rather than raise on one — so it spawns its own processes.
    SPAWNERS = ("_ffmpeg.py", "doctor.py")

    def test_no_module_but_the_wrapper_imports_subprocess(self):
        offenders = [
            path.name
            for path in sorted(SRC_ROOT.rglob("*.py"))
            if path.name not in self.SPAWNERS and "subprocess" in path.read_text()
        ]

        assert offenders == []
