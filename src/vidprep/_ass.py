"""Telops as an ASS subtitle track (design.md §3.5).

``telops.json`` says what to put on screen and when; ``styles.json`` says what
it looks like. This module turns the two into one ASS document, and it decides
nothing about time on its own: a telop that names a ``segment_id`` is given the
interval :mod:`vidprep.timeline` already mapped that segment onto for the SRT,
so a caption and the subtitle it belongs to cannot end up a frame apart, and a
telop timed by hand has its ``start`` put through the same mapping.

Style presets are the packaged ``styles/default.json`` merged with the
project's own file, field by field. A project that wants bigger text writes
``{"emphasis": {"fontsize": 80}}`` and keeps the packaged ``fontname`` — which
matters, because that font name is how weight is asked for at all: macOS drives
libass through CoreText, where ``Bold: 1`` has been seen to change nothing, so
the presets name a weighted family (``Hiragino Sans W6``) instead.

The module is pure: it reads JSON, returns text, and spawns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING

import pysubs2

from . import project as project_module
from .errors import TelopInvalidError, UsageError
from .models import StylePreset, Styles, Telops, to_ms

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from .models import Telop
    from .timeline import TimedSegment, Timeline

TELOPS_NAME = "telops.json"
STYLES_NAME = "styles.json"
PACKAGED_STYLES = "styles/default.json"

ASS_FORMAT = "ass"

#: Where the presets came from, as ``render --json`` reports it.
PACKAGED_SOURCE = "packaged default"
OVERRIDE_SOURCE = "project override"

#: Colours are written ``&HAABBGGRR``; a value without the alpha byte is opaque.
COLOUR_DIGITS = 8
HEX_BASE = 16


def packaged_styles() -> Styles:
    """Return the style presets shipped with vidprep (REQ-004)."""
    template = resources.files(__package__).joinpath(PACKAGED_STYLES)
    return Styles.model_validate_json(template.read_text(encoding="utf-8"))


def merge_styles(packaged: Styles, override: Styles) -> Styles:
    """Return *packaged* with the fields *override* actually states applied.

    A preset the project does not mention is left alone, a preset it does
    mention keeps every field it stays silent about, and a preset the packaged
    file has never heard of is added rather than refused (design.md §3.5).
    """
    presets = {name: preset.model_dump() for name, preset in packaged.presets.items()}
    for name, preset in override.presets.items():
        stated = preset.model_dump(exclude_unset=True)
        presets[name] = {**presets.get(name, {}), **stated}
    return Styles(
        presets={
            name: StylePreset.model_validate(values) for name, values in presets.items()
        }
    )


def load_styles(root: Path) -> tuple[Styles, str]:
    """Load the presets for the project at *root*, and say where they came from.

    Raises:
        SchemaInvalidError: If the project's ``styles.json`` violates its schema.
    """
    packaged = packaged_styles()
    path = root / STYLES_NAME
    if not path.is_file():
        return packaged, PACKAGED_SOURCE
    override = project_module.load_artifact(path, Styles)
    return merge_styles(packaged, override), OVERRIDE_SOURCE


def load_telops(root: Path, duration: float) -> Telops:
    """Load the telops of the project at *root*.

    Raises:
        UsageError: If the project has no ``telops.json`` to preview.
        SchemaInvalidError: If the file violates its schema.
    """
    path = root / TELOPS_NAME
    if not path.is_file():
        msg = (
            f"{TELOPS_NAME} not found — `--preview` burns in the telops written "
            f"there; create it next to {project_module.MANIFEST_NAME}"
        )
        raise UsageError(msg)
    return project_module.load_artifact(path, Telops, duration)


@dataclass(frozen=True, slots=True)
class TelopEvent:
    """One telop placed on the cut timeline."""

    index: int
    text: str
    style_preset: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class TelopPlan:
    """Every telop that will be drawn, and everything remarkable about the rest.

    Attributes:
        events: The telops to draw, in the order ``telops.json`` lists them.
        by_segment_id: How many of them were timed by a segment reference.
        by_start_duration: How many were timed by ``start`` and ``duration``.
        warnings: What was silently changed or left out, ready to be shown.
    """

    events: tuple[TelopEvent, ...] = ()
    by_segment_id: int = 0
    by_start_duration: int = 0
    dropped_by_cut: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Placement:
    """What a telop has to be resolved against.

    Attributes:
        timeline: The cut plan, for telops timed in original-timeline seconds.
        mapped: Segment identifier -> the interval the SRT gave that segment.
        known: Every segment identifier the transcript has, so a name that is
            merely gone with a cut can be told from one that never existed.
        presets: The style presets a telop may name.
    """

    timeline: Timeline
    mapped: Mapping[str, TimedSegment]
    known: frozenset[str]
    presets: Mapping[str, StylePreset] = field(default_factory=dict)


@dataclass(slots=True)
class _Resolution:
    """The plan being assembled, plus the complaints collected on the way."""

    events: list[TelopEvent] = field(default_factory=list)
    by_segment_id: int = 0
    by_start_duration: int = 0
    dropped_by_cut: int = 0
    warnings: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def finish(self) -> TelopPlan:
        """Return the finished plan.

        Raises:
            TelopInvalidError: If any telop named something that is not there.
        """
        if self.problems:
            raise TelopInvalidError(self.problems)
        return TelopPlan(
            events=tuple(self.events),
            by_segment_id=self.by_segment_id,
            by_start_duration=self.by_start_duration,
            dropped_by_cut=self.dropped_by_cut,
            warnings=tuple(self.warnings),
        )


def _interval(
    telop: Telop, index: int, placement: Placement, state: _Resolution
) -> tuple[float, float] | None:
    """Return the cut-timeline interval of *telop*, or ``None`` if it has none.

    A telop carrying both ways of stating its time follows its ``segment_id``,
    which is the one that keeps tracking the words when a cut moves them.
    """
    if telop.segment_id is not None:
        if telop.start is not None or telop.duration is not None:
            state.warnings.append(
                f"telops[{index}]: segment_id {telop.segment_id} wins over the "
                f"start/duration given alongside it"
            )
        if telop.segment_id not in placement.known:
            state.problems.append(
                f"unknown segment_id: {telop.segment_id} (telops[{index}])"
            )
            return None
        segment = placement.mapped.get(telop.segment_id)
        if segment is None:
            state.dropped_by_cut += 1
            state.warnings.append(
                f"telops[{index}]: segment {telop.segment_id} was removed by a "
                f"cut, so its telop is not drawn"
            )
            return None
        state.by_segment_id += 1
        return segment.start, segment.end

    # The schema guarantees both are present once segment_id is not.
    assert telop.start is not None  # noqa: S101
    assert telop.duration is not None  # noqa: S101
    start = placement.timeline.forward(telop.start)
    end = min(start + telop.duration, placement.timeline.cut_duration)
    if to_ms(end) <= to_ms(start):
        state.dropped_by_cut += 1
        state.warnings.append(
            f"telops[{index}]: starts at {telop.start:.3f}s, which the cuts "
            f"moved to the very end of the output, so its telop is not drawn"
        )
        return None
    state.by_start_duration += 1
    return start, end


def resolve(telops: Sequence[Telop], placement: Placement) -> TelopPlan:
    """Place every telop on the cut timeline (REQ-002, REQ-003, REQ-041).

    Args:
        telops: The telops of ``telops.json``, in the order they are written.
        placement: The transcript, the cut plan and the presets to resolve
            against.

    Returns:
        The telops to draw and what happened to the ones that will not be.

    Raises:
        TelopInvalidError: If a telop names a segment the transcript does not
            have, or a preset ``styles.json`` does not define. Every such name
            is reported together.
    """
    state = _Resolution()
    for index, telop in enumerate(telops):
        if telop.style_preset not in placement.presets:
            state.problems.append(
                f"unknown style_preset: {telop.style_preset} (telops[{index}])"
            )
        interval = _interval(telop, index, placement, state)
        if interval is None:
            continue
        state.events.append(
            TelopEvent(index, telop.text, telop.style_preset, *interval)
        )
    return state.finish()


def parse_colour(value: str) -> pysubs2.Color:
    """Return the ``&HAABBGGRR`` literal *value* as a pysubs2 colour."""
    digits = value[2:].rjust(COLOUR_DIGITS, "0")
    alpha, blue, green, red = (
        int(digits[position : position + 2], HEX_BASE) for position in (0, 2, 4, 6)
    )
    return pysubs2.Color(r=red, g=green, b=blue, a=alpha)


def to_style(preset: StylePreset) -> pysubs2.SSAStyle:
    """Return *preset* as the ASS style libass will render (REQ-005, REQ-012)."""
    return pysubs2.SSAStyle(
        fontname=preset.fontname,
        fontsize=preset.fontsize,
        bold=preset.bold,
        italic=preset.italic,
        alignment=pysubs2.Alignment(preset.alignment),
        primarycolor=parse_colour(preset.primary_colour),
        secondarycolor=parse_colour(preset.secondary_colour),
        outlinecolor=parse_colour(preset.outline_colour),
        backcolor=parse_colour(preset.back_colour),
        outline=preset.outline,
        shadow=preset.shadow,
        spacing=preset.spacing,
        marginl=preset.margin_l,
        marginr=preset.margin_r,
        marginv=preset.margin_v,
    )


def document(plan: TelopPlan, presets: Mapping[str, StylePreset], size: str) -> str:
    """Return the ASS document drawing *plan*.

    Every preset becomes a style, not only the ones in use, so the file stays a
    stylesheet somebody can point a telop at without editing it by hand.

    Args:
        plan: The telops to draw, already placed on the cut timeline.
        presets: The style presets to declare.
        size: Resolution of the material as ``WIDTHxHEIGHT``; ASS sizes are
            relative to it, so a mismatch would change how big the text looks.

    Returns:
        The ASS document, ready to be written and burned in.
    """
    width, _, height = size.partition("x")
    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = width
    subs.info["PlayResY"] = height
    for name, preset in presets.items():
        subs.styles[name] = to_style(preset)
    for event in plan.events:
        subs.append(
            pysubs2.SSAEvent(
                start=to_ms(event.start),
                end=to_ms(event.end),
                style=event.style_preset,
                # One layer per telop, in file order: telops that overlap are
                # stacked rather than refused (design.md §3.5 boundary table).
                layer=event.index,
                text=event.text,
            )
        )
    return subs.to_string(ASS_FORMAT)
