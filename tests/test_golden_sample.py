"""End-to-end check of ``init`` against the golden sample.

The sample is deliberately not in git (verification-plan.md §2), so these tests
skip wherever it — or ffprobe — is unavailable, including CI.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vidprep import project

GOLDEN = (
    Path(__file__).resolve().parents[1] / "fixtures" / "raw" / "VID_20260507_144024.mp4"
)
GOLDEN_SHA256 = "76d8ddd300d1cf12776a1a717cfecc318b4968ce75103d96e4d0f8c79aae1218"
GOLDEN_DURATION = 298.92

pytestmark = [
    pytest.mark.skipif(not GOLDEN.is_file(), reason="golden sample is not available"),
    pytest.mark.skipif(
        shutil.which("ffprobe") is None, reason="ffprobe is not on PATH"
    ),
]


def test_init_records_the_documented_specs(tmp_path):
    created = project.init_project(tmp_path / "talk01", GOLDEN)
    source = created.manifest.source

    assert source.sha256 == GOLDEN_SHA256
    assert source.duration == GOLDEN_DURATION
    assert (source.video.width, source.video.height) == (1920, 1080)
    assert source.video.fps == "25/1"
    assert (source.audio.codec, source.audio.sample_rate) == ("aac", 44100)


def test_source_verification_passes_on_the_untouched_sample(tmp_path):
    created = project.init_project(tmp_path / "talk01", GOLDEN)

    project.verify_source(project.load_project(created.root))
