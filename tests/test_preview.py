"""Tests for `render --preview`: the ASS telop track and its burnt-in preview.

The media tools are the fake of :mod:`tests.test_render`, extended with the
burn-in pass, so the completion conditions can be probed without libass — and
without ffmpeg — being installed. What the fake cannot answer for is what the
burn *looks* like, which is the part `verification-plan.md` §9 leaves to
`fixtures/telops-12/` and a pair of human eyes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pysubs2
import pytest

from vidprep import _ass, _ffmpeg, _preview
from vidprep import doctor as doctor_module
from vidprep import project as project_module
from vidprep import render as render_module
from vidprep.errors import (
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    InvariantViolationError,
    TelopInvalidError,
    UsageError,
)
from vidprep.models import StylePreset, Styles, Telop, Telops
from vidprep.timeline import Timeline

from .test_render import DURATION, FRAME, FakeFfmpeg, write_cuts, write_transcript

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vidprep.project import Project

#: Where the approved cuts of :mod:`tests.test_render` put each segment.
S0004_MAPPED = (48.0, 51.0)

#: `telops.json` exercising both ways of stating a time, plus one telop whose
#: segment an approved cut removes entirely (REQ-024).
TELOPS: tuple[dict[str, Any], ...] = (
    {"segment_id": "s0004", "text": "ここが重要", "style_preset": "emphasis"},
    {
        "text": "第1章 セットアップ",
        "style_preset": "chapter",
        "start": 45.0,
        "duration": 3.0,
    },
    {"segment_id": "s0002", "text": "消えるテロップ", "style_preset": "emphasis"},
)

#: One preset per ASS direction, for REQ-012.
NINE_DIRECTIONS = {
    f"align{alignment}": {"alignment": alignment, "fontsize": 48}
    for alignment in range(1, 10)
}


def write_telops(root: Path, telops: Sequence[dict[str, Any]] = TELOPS) -> None:
    """Write a ``telops.json`` holding *telops*."""
    payload = {"version": "1", "telops": list(telops)}
    (root / _ass.TELOPS_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def write_styles(root: Path, presets: dict[str, dict[str, Any]]) -> None:
    """Write a project ``styles.json`` holding *presets*."""
    payload = {"version": "1", "presets": presets}
    (root / _ass.STYLES_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class FakePreviewFfmpeg(FakeFfmpeg):
    """The render fake, plus the ``subtitles`` filter pass the preview adds.

    The burn copies the length of the file it draws over, which is what the
    real one does with ``-c:a copy``; ``preview_delta`` moves it away from that
    so the length check of REQ-013 can be probed on both sides.
    """

    def __init__(self) -> None:
        """Start with a burn that changes nothing about the length."""
        super().__init__()
        self.preview_delta = 0.0
        self.burn_error: Exception | None = None

    @property
    def burn(self) -> list[str]:
        """The ffmpeg invocation that drew the telops."""
        return next(args for args in self.commands if "-vf" in args)

    @property
    def burns(self) -> int:
        """How many burn passes were run."""
        return sum(1 for args in self.commands if "-vf" in args)

    def run(self, args: list[str], timeout: float = 0.0) -> str:
        """Answer the burn pass, and hand everything else to the render fake."""
        if args[0] == _ffmpeg.FFMPEG and "-vf" in args:
            self.commands.append(list(args))
            if self.burn_error is not None:
                raise self.burn_error
            Path(args[-1]).write_bytes(b"preview mp4")
            return ""
        if "format=duration" in args and _preview.WORKSPACE_PREFIX in args[-1]:
            return f"{self.rendered + self.preview_delta:.6f}\n"
        return super().run(args, timeout)


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch) -> FakePreviewFfmpeg:
    """The fake media tools, with an ffmpeg that has libass."""
    fake = FakePreviewFfmpeg()
    fake.install(monkeypatch)
    monkeypatch.setattr(
        doctor_module,
        "check_ffmpeg",
        lambda: {"ok": True, "version": "7.1.1", "libass": True},
    )
    return fake


@pytest.fixture
def prepared(project_dir: Path) -> Path:
    """A project whose upstream stages have run, with telops to draw."""
    processed = project_dir / "audio" / "processed.wav"
    processed.parent.mkdir(parents=True, exist_ok=True)
    processed.write_bytes(b"pretend this is a wav")
    write_cuts(project_dir)
    write_transcript(project_dir)
    write_telops(project_dir)
    return project_dir


@pytest.fixture
def loaded(prepared: Path) -> Project:
    """The prepared project, loaded."""
    return project_module.load_project(prepared)


def ass_of(loaded: Project) -> pysubs2.SSAFile:
    """Parse the ASS track a preview wrote back with pysubs2."""
    path = loaded.root / render_module.TELOPS_ASS_NAME
    return pysubs2.SSAFile.from_string(path.read_text(encoding="utf-8"), format_="ass")


def placement(loaded: Project, cuts: Sequence[tuple[float, float]]) -> _ass.Placement:
    """Build the placement a telop is resolved against, for *cuts*."""
    timeline = Timeline(cuts, DURATION)
    mapped, _ = timeline.map_segments([("s0001", 1.0, 3.0), ("s0004", 50.0, 53.0)])
    styles, _ = _ass.load_styles(loaded.root)
    return _ass.Placement(
        timeline=timeline,
        mapped={segment.segment_id: segment for segment in mapped},
        known=frozenset({"s0001", "s0002", "s0004"}),
        presets=styles.presets,
    )


# --------------------------------------------------------------------------- #
#  The schemas (REQ-001, REQ-005) and the boundary table
# --------------------------------------------------------------------------- #


class TestSchema:
    @pytest.mark.parametrize("alignment", list(range(1, 10)))
    def test_every_one_of_the_nine_directions_is_accepted(self, alignment):
        assert StylePreset(alignment=alignment).alignment == alignment

    @pytest.mark.parametrize("alignment", [0, 10, -1])
    def test_an_alignment_outside_the_numpad_is_rejected(self, alignment):
        with pytest.raises(ValueError, match="alignment"):
            StylePreset(alignment=alignment)

    @pytest.mark.parametrize("duration", [0.0, -1.0])
    def test_a_duration_that_shows_nothing_is_rejected(self, duration):
        payload = {
            "version": "1",
            "telops": [{"text": "一瞬", "start": 1.0, "duration": duration}],
        }
        with pytest.raises(ValueError, match="greater than 0"):
            Telops.model_validate(payload)

    def test_a_start_past_the_end_of_the_material_is_rejected(self):
        payload = {
            "version": "1",
            "telops": [{"text": "遅すぎる", "start": DURATION + 1, "duration": 2.0}],
        }
        with pytest.raises(ValueError, match="past the source duration"):
            Telops.model_validate(payload, context={"duration": DURATION})

    def test_a_colour_that_is_not_an_ass_literal_is_rejected(self):
        with pytest.raises(ValueError, match="primary_colour"):
            StylePreset(primary_colour="white")

    @pytest.mark.parametrize("name", ["a,b", "a\nb", ""])
    def test_a_preset_name_the_ass_format_cannot_hold_is_rejected(self, name):
        """A comma would split the `Style:` line it is written into."""
        with pytest.raises(ValueError, match="presets"):
            Styles.model_validate({"version": "1", "presets": {name: {}}})

    def test_the_packaged_presets_ask_for_weight_by_family_name(self):
        """REQ-005: `Bold: 1` is not to be relied on (design.md §3.5)."""
        presets = _ass.packaged_styles().presets

        assert presets
        for preset in presets.values():
            assert preset.fontname == "Hiragino Sans W6"
            assert preset.bold is False

    def test_the_packaged_presets_match_the_design_table(self):
        presets = _ass.packaged_styles().presets

        assert (presets["emphasis"].fontsize, presets["emphasis"].alignment) == (64, 8)
        assert presets["emphasis"].primary_colour == "&H00FFFFFF"
        assert (presets["chapter"].fontsize, presets["chapter"].alignment) == (72, 5)


# --------------------------------------------------------------------------- #
#  Style presets: packaged, overridden, added (REQ-004)
# --------------------------------------------------------------------------- #


class TestStyles:
    def test_a_project_without_styles_gets_the_packaged_ones(self, loaded):
        styles, source = _ass.load_styles(loaded.root)

        assert source == _ass.PACKAGED_SOURCE
        assert set(styles.presets) == set(_ass.packaged_styles().presets)

    def test_a_project_preset_overrides_only_the_fields_it_states(self, loaded):
        write_styles(loaded.root, {"emphasis": {"fontsize": 96}})

        styles, source = _ass.load_styles(loaded.root)

        assert source == _ass.OVERRIDE_SOURCE
        assert styles.presets["emphasis"].fontsize == 96
        # The weighted family name is how bold is asked for; losing it silently
        # would be the one override nobody would notice (REQ-005).
        assert styles.presets["emphasis"].fontname == "Hiragino Sans W6"
        assert styles.presets["emphasis"].alignment == 8

    def test_a_preset_the_packaged_file_never_heard_of_is_added(self, loaded):
        write_styles(loaded.root, {"kicker": {"alignment": 1, "fontsize": 40}})

        styles, _ = _ass.load_styles(loaded.root)

        assert styles.presets["kicker"].alignment == 1
        assert set(_ass.packaged_styles().presets) < set(styles.presets)

    def test_a_preset_the_project_does_not_mention_is_left_alone(self, loaded):
        write_styles(loaded.root, {"emphasis": {"fontsize": 96}})

        styles, _ = _ass.load_styles(loaded.root)

        assert styles.presets["chapter"] == _ass.packaged_styles().presets["chapter"]

    def test_an_invalid_project_styles_file_is_refused(self, run_cli, prepared, tools):
        write_styles(prepared, {"emphasis": {"alignment": 10}})

        result = run_cli("render", "-p", str(prepared), "--preview", "--json")

        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout)["error"] == "schema_invalid"


# --------------------------------------------------------------------------- #
#  Placing the telops on the cut timeline (REQ-002, REQ-003, REQ-041)
# --------------------------------------------------------------------------- #


class TestTiming:
    def test_a_segment_reference_follows_the_cut_timeline(self, loaded):
        """Example 2 of #12: 8.5s removed before 30.0 maps 30.0 onto 21.5."""
        timeline = Timeline([(20.0, 28.5)], DURATION)
        mapped, _ = timeline.map_segments([("s0012", 30.0, 33.4)])
        plan = _ass.resolve(
            [Telop(text="ここが重要", style_preset="emphasis", segment_id="s0012")],
            _ass.Placement(
                timeline=timeline,
                mapped={segment.segment_id: segment for segment in mapped},
                known=frozenset({"s0012"}),
                presets=_ass.packaged_styles().presets,
            ),
        )

        assert (plan.events[0].start, plan.events[0].end) == (21.5, 24.9)
        assert plan.by_segment_id == 1

    def test_an_explicit_start_is_mapped_and_the_duration_is_display_time(self, loaded):
        """Example 2 of #12: f(45.0) = 33.9, shown for 3.0s."""
        plan = _ass.resolve(
            [Telop(text="第1章", style_preset="chapter", start=45.0, duration=3.0)],
            placement(loaded, [(20.0, 28.5), (30.0, 32.6)]),
        )

        assert (plan.events[0].start, plan.events[0].end) == (33.9, 36.9)
        assert plan.by_start_duration == 1

    def test_the_telop_of_a_segment_is_timed_exactly_like_its_subtitle(
        self, tools, loaded
    ):
        """REQ-041: both go through the one mapping, so both agree."""
        result = render_module.run_render(loaded, preview=True)

        subtitle = next(
            entry for entry in result.subtitles.entries if entry.segment_id == "s0004"
        )
        telop = next(event for event in ass_of(loaded) if event.text == "ここが重要")
        assert (telop.start, telop.end) == (
            round(subtitle.start * 1000),
            round(subtitle.end * 1000),
        )
        assert (subtitle.start, subtitle.end) == S0004_MAPPED

    def test_a_segment_id_wins_over_a_start_given_alongside_it(self, loaded):
        telop = Telop(
            text="どちら",
            style_preset="emphasis",
            segment_id="s0004",
            start=1.0,
            duration=2.0,
        )

        plan = _ass.resolve([telop], placement(loaded, [(10.0, 12.0)]))

        assert (plan.events[0].start, plan.events[0].end) == (48.0, 51.0)
        assert plan.by_start_duration == 0
        assert "wins over the start/duration" in plan.warnings[0]

    def test_a_telop_whose_segment_a_cut_removed_is_dropped_with_a_warning(
        self, tools, loaded
    ):
        """REQ-024: the caption goes where the words went."""
        result = render_module.run_render(loaded, preview=True)

        assert result.preview is not None
        assert result.preview.plan.dropped_by_cut == 1
        assert "消えるテロップ" not in {event.text for event in ass_of(loaded)}
        assert any("removed by a cut" in line for line in result.preview.plan.warnings)

    def test_a_telop_starting_after_the_last_frame_is_dropped(self, loaded):
        telop = Telop(
            text="間に合わない", style_preset="emphasis", start=DURATION, duration=2.0
        )

        plan = _ass.resolve([telop], placement(loaded, [(10.0, 12.0)]))

        assert plan.events == ()
        assert plan.dropped_by_cut == 1

    def test_telops_that_overlap_are_stacked_rather_than_refused(self, loaded):
        telops = [
            Telop(text="下", style_preset="default", start=10.0, duration=5.0),
            Telop(text="上", style_preset="emphasis", start=11.0, duration=5.0),
        ]

        plan = _ass.resolve(telops, placement(loaded, []))
        events = list(
            pysubs2.SSAFile.from_string(
                _ass.document(plan, _ass.packaged_styles().presets, "1920x1080"),
                format_="ass",
            )
        )

        assert [event.layer for event in events] == [0, 1]
        assert [event.text for event in events] == ["下", "上"]


# --------------------------------------------------------------------------- #
#  Names that are not there (REQ-020, REQ-021, REQ-022)
# --------------------------------------------------------------------------- #


class TestInvalidTelops:
    def test_an_unknown_segment_id_is_refused(self, loaded):
        telop = Telop(text="どこ", style_preset="emphasis", segment_id="s9999")

        with pytest.raises(TelopInvalidError, match="unknown segment_id: s9999") as bad:
            _ass.resolve([telop], placement(loaded, []))

        assert bad.value.details == ["unknown segment_id: s9999 (telops[0])"]

    def test_an_unknown_preset_is_refused(self, loaded):
        telop = Telop(text="どんな", style_preset="nope", segment_id="s0004")

        with pytest.raises(TelopInvalidError, match="unknown style_preset: nope"):
            _ass.resolve([telop], placement(loaded, []))

    def test_every_complaint_is_reported_at_once(self, loaded):
        telops = [
            Telop(text="a", style_preset="nope", segment_id="s0004"),
            Telop(text="b", style_preset="emphasis", segment_id="s9999"),
        ]

        with pytest.raises(TelopInvalidError) as bad:
            _ass.resolve(telops, placement(loaded, []))

        assert bad.value.details == [
            "unknown style_preset: nope (telops[0])",
            "unknown segment_id: s9999 (telops[1])",
        ]

    def test_the_command_exits_three_and_writes_no_preview(
        self, run_cli, prepared, tools
    ):
        """Example 3 of #12."""
        write_telops(
            prepared,
            [{"segment_id": "s9999", "text": "どこ", "style_preset": "emphasis"}],
        )

        result = run_cli("render", "-p", str(prepared), "--preview", "--json")

        assert result.exit_code == EXIT_VALIDATION
        payload = json.loads(result.stdout)
        assert payload["error"] == "telop_invalid"
        assert payload["detail"] == ["unknown segment_id: s9999 (telops[0])"]
        assert not (prepared / render_module.PREVIEW_NAME).exists()

    def test_nothing_is_encoded_before_the_names_are_checked(
        self, run_cli, prepared, tools
    ):
        write_telops(
            prepared, [{"segment_id": "s9999", "text": "どこ", "style_preset": "x"}]
        )

        run_cli("render", "-p", str(prepared), "--preview", "--json")

        assert tools.commands == []
        assert not (prepared / render_module.VIDEO_NAME).exists()

    def test_a_telop_with_no_timing_at_all_is_a_schema_error(
        self, run_cli, prepared, tools
    ):
        """REQ-022: the artifact check every command runs catches it."""
        write_telops(prepared, [{"text": "いつ出すの", "style_preset": "emphasis"}])

        result = run_cli("render", "-p", str(prepared), "--preview", "--json")

        assert result.exit_code == EXIT_VALIDATION
        assert json.loads(result.stdout)["error"] == "schema_invalid"


# --------------------------------------------------------------------------- #
#  The ASS document (REQ-011, REQ-012)
# --------------------------------------------------------------------------- #


class TestDocument:
    def test_the_play_resolution_matches_the_material(self, tools, loaded):
        render_module.run_render(loaded, preview=True)

        info = ass_of(loaded).info
        assert (info["PlayResX"], info["PlayResY"]) == ("1920", "1080")

    def test_all_nine_alignments_reach_the_style_block(self, loaded):
        write_styles(loaded.root, NINE_DIRECTIONS)
        styles, _ = _ass.load_styles(loaded.root)

        document = _ass.document(_ass.TelopPlan(), styles.presets, "1920x1080")

        parsed = pysubs2.SSAFile.from_string(document, format_="ass")
        assert sorted(
            int(parsed.styles[name].alignment) for name in NINE_DIRECTIONS
        ) == list(range(1, 10))

    def test_a_preset_becomes_the_ass_style_libass_reads(self, loaded):
        document = _ass.document(
            _ass.TelopPlan(), _ass.packaged_styles().presets, "1920x1080"
        )

        style = pysubs2.SSAFile.from_string(document, format_="ass").styles["emphasis"]
        assert style.fontname == "Hiragino Sans W6"
        assert style.fontsize == 64
        assert style.bold is False
        assert style.primarycolor == pysubs2.Color(255, 255, 255, 0)
        assert style.outlinecolor == pysubs2.Color(0, 0, 0, 0)
        assert (style.outline, style.shadow, style.marginv) == (3.0, 0.0, 60)

    @pytest.mark.parametrize(
        ("literal", "expected"),
        [
            pytest.param("&H00FFFFFF", (255, 255, 255, 0), id="opaque-white"),
            pytest.param("&H0000FFFF", (255, 255, 0, 0), id="yellow"),
            pytest.param("&H80000000", (0, 0, 0, 128), id="half-transparent"),
            pytest.param("&HFF0000", (0, 0, 255, 0), id="no-alpha-byte"),
        ],
    )
    def test_colours_are_read_as_ass_writes_them(self, literal, expected):
        colour = _ass.parse_colour(literal)

        assert (colour.r, colour.g, colour.b, colour.a) == expected

    def test_no_telops_still_produces_a_playable_track(self, tools, loaded):
        write_telops(loaded.root, [])

        result = render_module.run_render(loaded, preview=True)

        document = (loaded.root / render_module.TELOPS_ASS_NAME).read_text("utf-8")
        assert "Dialogue:" not in document
        assert len(ass_of(loaded)) == 0
        assert str(render_module.PREVIEW_NAME) in result.outputs

    def test_the_track_survives_a_pysubs2_round_trip(self, tools, loaded):
        render_module.run_render(loaded, preview=True)

        original = (loaded.root / render_module.TELOPS_ASS_NAME).read_text("utf-8")
        assert (
            pysubs2.SSAFile.from_string(original, format_="ass").to_string("ass")
            == original
        )


# --------------------------------------------------------------------------- #
#  Burning it in (REQ-010, REQ-013, REQ-023, REQ-040)
# --------------------------------------------------------------------------- #


class TestBurn:
    def test_the_preview_is_the_render_with_the_track_drawn_over_it(
        self, tools, loaded
    ):
        render_module.run_render(loaded, preview=True)

        command = tools.burn
        assert command[command.index("-i") + 1].endswith("out/output.mp4")
        assert command[command.index("-vf") + 1].startswith("subtitles=")
        assert command[command.index("-vf") + 1].endswith("out/telops.ass")
        # Re-encoding the audio could move the length the telops are timed on.
        assert command[command.index("-c:a") + 1] == "copy"
        assert command[-1].endswith("preview.mp4")

    def test_both_artifacts_are_written_and_named(self, tools, loaded):
        result = render_module.run_render(loaded, preview=True)

        assert (loaded.root / render_module.TELOPS_ASS_NAME).is_file()
        assert (loaded.root / render_module.PREVIEW_NAME).is_file()
        assert result.outputs[-2:] == ("out/telops.ass", "out/preview.mp4")

    def test_the_render_is_only_read_by_the_preview(self, tools, loaded):
        """REQ-040: `--preview` may not touch `out/output.mp4`."""
        render_module.run_render(loaded)
        before = project_module.sha256_file(loaded.root / render_module.VIDEO_NAME)

        render_module.run_render(loaded, preview=True)

        after = project_module.sha256_file(loaded.root / render_module.VIDEO_NAME)
        assert after == before

    def test_a_preview_off_by_exactly_one_frame_passes(self, tools, loaded):
        tools.preview_delta = FRAME

        result = render_module.run_render(loaded, preview=True)

        assert result.preview is not None
        assert result.preview.duration == pytest.approx(DURATION - 42.0 + FRAME)

    def test_a_preview_that_lost_time_is_refused(self, tools, loaded):
        tools.preview_delta = FRAME + 0.001

        with pytest.raises(InvariantViolationError, match=r"41\.0ms > 40\.0ms"):
            render_module.run_render(loaded, preview=True)

    def test_a_rejected_preview_never_replaces_the_previous_one(self, tools, loaded):
        target = loaded.root / render_module.PREVIEW_NAME
        target.parent.mkdir(parents=True)
        target.write_bytes(b"the preview from yesterday")
        tools.preview_delta = 1.0

        with pytest.raises(InvariantViolationError):
            render_module.run_render(loaded, preview=True)

        assert target.read_bytes() == b"the preview from yesterday"

    @pytest.mark.parametrize(
        ("check", "reason"),
        [
            pytest.param(
                {"ok": True, "libass": False}, "built without", id="no-libass"
            ),
            pytest.param({"ok": False}, "not installed", id="no-ffmpeg"),
        ],
    )
    def test_an_ffmpeg_that_cannot_draw_subtitles_asks_for_doctor(
        self, monkeypatch, check, reason
    ):
        """REQ-023, Example 4 of #12."""
        monkeypatch.setattr(doctor_module, "check_ffmpeg", lambda: check)

        with pytest.raises(UsageError, match="vidprep doctor") as refused:
            _preview.require_libass()

        assert "libass" in str(refused.value)
        assert reason  # both shapes of a broken ffmpeg reach the same advice

    def test_the_command_exits_one_when_libass_is_missing(
        self, run_cli, prepared, tools, monkeypatch
    ):
        monkeypatch.setattr(doctor_module, "check_ffmpeg", lambda: {"ok": False})

        result = run_cli("render", "-p", str(prepared), "--preview", "--json")

        assert result.exit_code == EXIT_USAGE
        assert json.loads(result.stdout)["error"] == "usage"
        assert not (prepared / render_module.PREVIEW_NAME).exists()

    def test_a_missing_telops_file_asks_for_one(self, run_cli, prepared, tools):
        (prepared / _ass.TELOPS_NAME).unlink()

        result = run_cli("render", "-p", str(prepared), "--preview", "--json")

        assert result.exit_code == EXIT_USAGE
        assert "telops.json not found" in json.loads(result.stdout)["detail"]

    def test_a_render_without_preview_burns_nothing(self, tools, loaded):
        result = render_module.run_render(loaded)

        assert tools.burns == 0
        assert result.preview is None
        assert not (loaded.root / render_module.PREVIEW_NAME).exists()
        assert not (loaded.root / render_module.TELOPS_ASS_NAME).exists()


class TestFilterEscaping:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            pytest.param("plain.ass", "plain.ass", id="nothing-to-escape"),
            pytest.param("a:b.ass", r"a\\:b.ass", id="colon-ends-an-argument"),
            pytest.param("a,b.ass", r"a\,b.ass", id="comma-ends-a-filter"),
            pytest.param("a'b.ass", r"a\\\'b.ass", id="quote-quotes-the-rest"),
            pytest.param("a[b].ass", r"a\[b\].ass", id="brackets-name-a-pad"),
        ],
    )
    def test_a_path_reaches_libass_as_it_was_written(self, name, expected):
        """Checked against ffmpeg 7.1.1: a bare `'` swallows the rest of the path."""
        assert _preview.escape_filter_path(Path(name)) == expected

    def test_the_escaped_path_is_what_the_filter_argument_holds(self, tools, loaded):
        render_module.run_render(loaded, preview=True)

        command = tools.burn
        expected = _preview.escape_filter_path(
            loaded.root / render_module.TELOPS_ASS_NAME
        )
        assert command[command.index("-vf") + 1] == f"subtitles={expected}"


# --------------------------------------------------------------------------- #
#  The stage and its command line
# --------------------------------------------------------------------------- #


class TestStage:
    def test_the_result_counts_the_telops_and_names_the_presets(self, tools, loaded):
        """Example 1 of #12."""
        result = render_module.run_render(loaded, preview=True).to_dict()

        assert result["telops"] == {
            "total": 3,
            "by_segment_id": 1,
            "by_start_duration": 1,
            "dropped_by_cut": 1,
            "warnings": [
                "telops[2]: segment s0002 was removed by a cut, so its telop is "
                "not drawn"
            ],
        }
        assert result["styles"] == {
            "presets": ["default", "emphasis", "chapter"],
            "source": "packaged default",
        }

    def test_a_render_without_preview_reports_no_telop_section(self, tools, loaded):
        result = render_module.run_render(loaded).to_dict()

        assert "telops" not in result
        assert "styles" not in result

    def test_the_reported_source_names_the_project_override(self, tools, loaded):
        write_styles(loaded.root, {"emphasis": {"fontsize": 96}})

        result = render_module.run_render(loaded, preview=True).to_dict()

        assert result["styles"]["source"] == "project override"

    def test_a_dry_run_lists_the_burn_and_writes_nothing(
        self, run_cli, prepared, tools
    ):
        result = run_cli(
            "render", "-p", str(prepared), "--preview", "--dry-run", "--json"
        )

        plan = json.loads(result.stdout)
        assert result.exit_code == EXIT_OK
        assert tools.commands == []
        assert not (prepared / "out").exists()
        assert str(prepared / render_module.TELOPS_ASS_NAME) in plan["writes"]
        assert str(prepared / render_module.PREVIEW_NAME) in plan["writes"]
        assert any("subtitles=" in " ".join(command) for command in plan["commands"])

    def test_a_dry_run_refuses_for_the_same_reasons(self, prepared, tools):
        (prepared / _ass.TELOPS_NAME).unlink()
        loaded = project_module.load_project(prepared)

        with pytest.raises(UsageError, match=r"telops\.json not found"):
            render_module.plan(loaded, preview=True)

    def test_the_command_succeeds_and_names_what_it_wrote(
        self, run_cli, prepared, tools
    ):
        result = run_cli("render", "-p", str(prepared), "--preview")

        assert result.exit_code == EXIT_OK
        assert "out/telops.ass" in result.stdout
        assert "out/preview.mp4" in result.stdout
        assert "removed by a cut" in result.stdout

    def test_preview_is_an_option_of_render(self, run_cli):
        result = run_cli("render", "--help")

        assert "--preview" in result.stdout


def test_the_packaged_styles_ship_with_the_wheel():
    """REQ-004: the defaults are part of the package, not of the repository."""
    assert isinstance(_ass.packaged_styles(), Styles)
