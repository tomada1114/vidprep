"""Schema and invariant tests for the intermediate JSON models (design.md §3)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from vidprep.models import (
    Cut,
    Cuts,
    Manifest,
    Profile,
    Styles,
    Telops,
    Transcript,
    VadReport,
    describe_validation_error,
    to_ms,
)

DURATION = 298.92
CONTEXT: dict[str, Any] = {"duration": DURATION}

MANIFEST_SAMPLE: dict[str, Any] = {
    "version": "1",
    "created_at": "2026-08-03T16:00:00+09:00",
    "source": {
        "path": "/Users/masuyama/Movies/VID_20260507_144024.mp4",
        "sha256": "76d8ddd3" + "0" * 56,
        "duration": 298.92,
        "video": {"codec": "h264", "width": 1920, "height": 1080, "fps": "25/1"},
        "audio": {"codec": "aac", "sample_rate": 44100, "channels": 2},
    },
    "stages": {
        "audio_fix": {
            "done_at": "2026-08-03T16:05:00+09:00",
            "params_sha256": "a" * 64,
            "tool_versions": {"ffmpeg": "7.x"},
        }
    },
}

TRANSCRIPT_SAMPLE: dict[str, Any] = {
    "version": "1",
    "audio_source": "audio/processed.wav",
    "asr": {"backend": "whisper.cpp", "model": "large-v3-turbo", "vad": "silero-v5"},
    "segments": [
        {
            "id": "s0001",
            "start": 1.234,
            "end": 4.567,
            "text": "こんにちは、とまだです。",
            "source": "asr",
            "edits": [],
        }
    ],
}

CUTS_SAMPLE: dict[str, Any] = {
    "version": "1",
    "cuts": [
        {
            "id": "c0001",
            "start": 10.500,
            "end": 13.240,
            "reason": "silence",
            "confidence": 0.95,
            "status": "approved",
            "note": None,
        },
        {
            "id": "c0002",
            "start": 45.100,
            "end": 45.900,
            "reason": "filler",
            "confidence": 0.7,
            "status": "proposed",
            "note": "「えーと」+前後無音",
        },
    ],
}

TELOPS_SAMPLE: dict[str, Any] = {
    "version": "1",
    "telops": [
        {
            "segment_id": "s0012",
            "text": "ここが重要",
            "style_preset": "emphasis",
            "start": None,
            "duration": None,
        }
    ],
}

STYLES_SAMPLE: dict[str, Any] = {
    "version": "1",
    "presets": {
        "emphasis": {
            "fontname": "Hiragino Sans W6",
            "fontsize": 64,
            "alignment": 8,
            "primary_colour": "&H00FFFFFF",
        }
    },
}

VAD_SAMPLE: dict[str, Any] = {
    "version": "1",
    "backend": "silero-v5",
    "segments": [{"start": 2.34, "end": 10.01}, {"start": 12.99, "end": 20.0}],
}

PROFILE_SAMPLE: dict[str, Any] = {
    "version": "1",
    "audio": {
        "denoise": "deepfilternet",
        "highpass_hz": 80,
        "loudnorm": {"i": -14.0, "tp": -1.0, "lra": 11.0},
    },
    "asr": {
        "backend": "whisper.cpp",
        "model": "large-v3-turbo",
        "language": "ja",
        "vad": "silero-v5",
    },
    "silence": {
        "threshold": "4%",
        "min_duration": 0.6,
        "pad_pre": 0.3,
        "pad_post": 0.3,
        "min_cut_duration": 0.4,
    },
    "filler": {"enable_weak": False, "require_adjacent_silence": 0.2},
    "render": {
        "crf": 18,
        "preset": "slow",
        "boundary_fade": 0.010,
        "verify_asr_mode": "gate",
    },
    "subtitle": {
        "max_chars_per_line": 20,
        "max_lines": 2,
        "min_display": 0.8,
        "max_cps": 8.0,
    },
}


def make_cuts(*specs: tuple[str, float, float, str]) -> dict[str, Any]:
    """Build a cuts payload from ``(id, start, end, status)`` tuples."""
    return {
        "version": "1",
        "cuts": [
            {
                "id": cut_id,
                "start": start,
                "end": end,
                "reason": "silence",
                "confidence": 0.9,
                "status": status,
            }
            for cut_id, start, end, status in specs
        ],
    }


class TestDesignSamples:
    """REQ-001: every model accepts the sample JSON from design.md §3."""

    @pytest.mark.parametrize(
        ("model", "payload"),
        [
            pytest.param(Manifest, MANIFEST_SAMPLE, id="manifest"),
            pytest.param(Transcript, TRANSCRIPT_SAMPLE, id="transcript"),
            pytest.param(Cuts, CUTS_SAMPLE, id="cuts"),
            pytest.param(Telops, TELOPS_SAMPLE, id="telops"),
            pytest.param(Styles, STYLES_SAMPLE, id="styles"),
            pytest.param(Profile, PROFILE_SAMPLE, id="profile"),
            pytest.param(VadReport, VAD_SAMPLE, id="vad"),
        ],
    )
    def test_sample_payload_validates(self, model, payload):
        assert model.model_validate(payload, context=CONTEXT)

    def test_profile_defaults_match_the_design_table(self):
        assert Profile().model_dump(mode="json") == PROFILE_SAMPLE

    def test_style_presets_accept_the_float_valued_ass_fields(self):
        payload = {
            "version": "1",
            "presets": {
                "emphasis": {
                    "fontname": "Hiragino Sans W6",
                    "fontsize": 64,
                    "outline": 2.5,
                    "shadow": 0.0,
                }
            },
        }

        assert Styles.model_validate(payload).presets["emphasis"].outline == 2.5

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Cuts.model_validate({"version": "1", "cuts": [], "oops": 1})


class TestIdentifiers:
    """REQ-002 / REQ-003: identifiers are ``s0001`` / ``c0001`` shaped."""

    @pytest.mark.parametrize("segment_id", ["seg1", "s001", "s00001", "S0001", ""])
    def test_invalid_segment_id_is_rejected(self, segment_id):
        payload = {**TRANSCRIPT_SAMPLE}
        payload["segments"] = [{**TRANSCRIPT_SAMPLE["segments"][0], "id": segment_id}]
        with pytest.raises(ValidationError, match="String should match pattern"):
            Transcript.model_validate(payload)

    @pytest.mark.parametrize("cut_id", ["cut-1", "c001", "0001", "C0001"])
    def test_invalid_cut_id_is_rejected(self, cut_id):
        with pytest.raises(ValidationError, match="String should match pattern"):
            Cuts.model_validate(make_cuts((cut_id, 1.0, 2.0, "proposed")))

    def test_duplicate_cut_ids_are_rejected_naming_the_id(self):
        payload = make_cuts(
            ("c0001", 1.0, 2.0, "proposed"), ("c0001", 3.0, 4.0, "proposed")
        )
        with pytest.raises(ValidationError, match="duplicate cut ids: c0001"):
            Cuts.model_validate(payload)


class TestIntervals:
    """REQ-004: ``0 <= start < end <= duration`` on every interval."""

    def test_empty_interval_is_rejected(self):
        with pytest.raises(ValidationError, match=r"c0001: start .* strictly before"):
            Cuts.model_validate(make_cuts(("c0001", 5.0, 5.0, "proposed")))

    def test_inverted_interval_is_rejected(self):
        with pytest.raises(ValidationError, match="c0001: start"):
            Cuts.model_validate(make_cuts(("c0001", 9.0, 5.0, "proposed")))

    def test_negative_start_is_rejected(self):
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            Cuts.model_validate(make_cuts(("c0001", -1.0, 5.0, "proposed")))

    def test_end_past_the_source_duration_is_rejected(self):
        payload = make_cuts(("c0001", 10.0, DURATION + 0.001, "proposed"))
        with pytest.raises(ValidationError, match="past the source duration"):
            Cuts.model_validate(payload, context=CONTEXT)

    def test_end_exactly_at_the_source_duration_is_accepted(self):
        payload = make_cuts(("c0001", 10.0, DURATION, "proposed"))
        assert Cuts.model_validate(payload, context=CONTEXT).cuts[0].end == DURATION

    def test_duration_is_unchecked_without_a_context(self):
        payload = make_cuts(("c0001", 10.0, DURATION + 100, "proposed"))
        assert Cuts.model_validate(payload).cuts[0].end == DURATION + 100

    def test_segment_interval_is_checked_too(self):
        payload = {**TRANSCRIPT_SAMPLE}
        payload["segments"] = [
            {**TRANSCRIPT_SAMPLE["segments"][0], "start": 4.0, "end": 4.0}
        ]
        with pytest.raises(ValidationError, match="s0001: start"):
            Transcript.model_validate(payload)


class TestApprovedOverlap:
    """REQ-005: approved cuts may not overlap; proposed ones may."""

    def test_overlapping_approved_cuts_are_rejected(self):
        payload = make_cuts(
            ("c0001", 10.0, 13.0, "approved"), ("c0002", 12.0, 15.0, "approved")
        )
        with pytest.raises(ValidationError, match="approved cuts overlap"):
            Cuts.model_validate(payload)

    def test_overlap_message_names_both_cuts_and_their_ranges(self):
        payload = make_cuts(
            ("c0001", 10.5, 13.24, "approved"), ("c0002", 12.0, 15.0, "approved")
        )
        with pytest.raises(ValidationError) as caught:
            Cuts.model_validate(payload)
        assert describe_validation_error(caught.value) == (
            "approved cuts overlap: c0001(10.500-13.240) x c0002(12.000-15.000)"
        )

    def test_overlap_with_a_proposed_cut_is_accepted(self):
        payload = make_cuts(
            ("c0001", 10.0, 13.0, "approved"), ("c0002", 12.0, 15.0, "proposed")
        )
        assert len(Cuts.model_validate(payload).cuts) == 2

    def test_approved_cuts_touching_at_a_boundary_are_accepted(self):
        payload = make_cuts(
            ("c0001", 10.0, 13.0, "approved"), ("c0002", 13.0, 15.0, "approved")
        )
        assert len(Cuts.model_validate(payload).cuts) == 2

    def test_overlap_is_detected_regardless_of_declaration_order(self):
        payload = make_cuts(
            ("c0002", 12.0, 15.0, "approved"), ("c0001", 10.0, 13.0, "approved")
        )
        with pytest.raises(ValidationError, match=r"c0001.* x c0002"):
            Cuts.model_validate(payload)

    def test_no_cuts_is_accepted(self):
        assert Cuts().cuts == []


class TestSerialisation:
    """REQ-006: seconds are serialised rounded to milliseconds."""

    def test_seconds_are_rounded_to_three_decimals(self):
        cut = Cut(id="c0001", start=1.23456, end=2.98765, reason="silence")
        dumped = cut.model_dump(mode="json")
        assert (dumped["start"], dumped["end"]) == (1.235, 2.988)

    def test_round_trip_keeps_the_rounded_value(self):
        cut = Cut(id="c0001", start=1.23456, end=2.0, reason="silence")
        assert Cut.model_validate_json(cut.model_dump_json()).start == 1.235

    @pytest.mark.parametrize(
        ("seconds", "expected"), [(1.2344, 1234), (1.2345, 1234), (0.0, 0)]
    )
    def test_to_ms_rounds_to_whole_milliseconds(self, seconds, expected):
        assert to_ms(seconds) == expected


class TestForwardCompatibility:
    """REQ-007 and design.md §8: unknown-but-planned values stay valid."""

    def test_unknown_cut_reason_is_accepted(self):
        payload = make_cuts(("c0001", 1.0, 2.0, "proposed"))
        payload["cuts"][0]["reason"] = "restart"
        assert Cuts.model_validate(payload).cuts[0].reason == "restart"

    def test_empty_cut_reason_is_rejected(self):
        payload = make_cuts(("c0001", 1.0, 2.0, "proposed"))
        payload["cuts"][0]["reason"] = ""
        with pytest.raises(ValidationError, match="at least 1 character"):
            Cuts.model_validate(payload)

    def test_unknown_cut_status_is_rejected(self):
        payload = make_cuts(("c0001", 1.0, 2.0, "maybe"))
        with pytest.raises(ValidationError, match="Input should be"):
            Cuts.model_validate(payload)

    def test_word_timestamps_are_accepted_when_a_backend_adds_them(self):
        payload = {**TRANSCRIPT_SAMPLE}
        payload["segments"] = [
            {
                **TRANSCRIPT_SAMPLE["segments"][0],
                "words": [
                    {"start": 1.234, "end": 1.9, "text": "こんにちは", "prob": 0.9}
                ],
            }
        ]
        transcript = Transcript.model_validate(payload)
        assert transcript.segments[0].words is not None


class TestSpeechRegions:
    """The speech regions transcribe records for the stages that join on them."""

    def test_regions_must_be_ordered_and_disjoint(self):
        payload = {
            "version": "1",
            "backend": "silero-v5",
            "segments": [{"start": 2.0, "end": 10.0}, {"start": 9.0, "end": 12.0}],
        }
        with pytest.raises(ValidationError, match=r"speech\[1\]: starts"):
            VadReport.model_validate(payload)

    def test_regions_touching_at_a_boundary_are_accepted(self):
        payload = {
            "version": "1",
            "backend": "silero-v5",
            "segments": [{"start": 2.0, "end": 10.0}, {"start": 10.0, "end": 12.0}],
        }
        assert len(VadReport.model_validate(payload).segments) == 2

    def test_a_region_past_the_source_duration_is_rejected(self):
        payload = {
            "version": "1",
            "backend": "silero-v5",
            "segments": [{"start": 2.0, "end": DURATION + 0.1}],
        }
        with pytest.raises(ValidationError, match="past the source duration"):
            VadReport.model_validate(payload, context=CONTEXT)


class TestAsrSettings:
    """REQ-002: profile.json cannot ask for a run without detection."""

    def test_detection_cannot_be_switched_off(self):
        payload = {**PROFILE_SAMPLE, "asr": {**PROFILE_SAMPLE["asr"], "vad": "none"}}
        with pytest.raises(ValidationError, match="Input should be 'silero-v5'"):
            Profile.model_validate(payload)

    def test_an_unknown_backend_is_rejected(self):
        payload = {**PROFILE_SAMPLE, "asr": {**PROFILE_SAMPLE["asr"], "backend": "x"}}
        with pytest.raises(ValidationError, match="Input should be"):
            Profile.model_validate(payload)


class TestTelopTiming:
    def test_segment_reference_is_enough(self):
        assert Telops.model_validate(TELOPS_SAMPLE).telops[0].segment_id == "s0012"

    def test_explicit_start_and_duration_are_enough(self):
        payload = {
            "version": "1",
            "telops": [{"text": "直接指定", "start": 12.0, "duration": 2.0}],
        }
        assert Telops.model_validate(payload).telops[0].start == 12.0

    def test_untimed_telop_is_rejected(self):
        payload = {"version": "1", "telops": [{"text": "いつ出すの"}]}
        with pytest.raises(ValidationError, match="needs segment_id"):
            Telops.model_validate(payload)


class TestErrorRendering:
    def test_field_errors_name_their_location(self):
        with pytest.raises(ValidationError) as caught:
            Cuts.model_validate(make_cuts(("nope", 1.0, 2.0, "proposed")))
        assert describe_validation_error(caught.value).startswith("cuts.0.id: ")
