"""Tests for the original/cut timeline mapping (design.md §4, issue #4).

The invariant tests at the bottom stand in for the property tests
verification-plan.md §9 asks for: instead of a generator library they sweep a
deterministic family of cut sets, which keeps failures reproducible without
adding a dependency.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from vidprep import timeline as timeline_module
from vidprep.timeline import Timeline, normalize_cuts

DURATION = 298.920
SAMPLE_CUTS = [(10.500, 13.240), (45.100, 45.900)]

#: Modules that would make the mapping impure (REQ-044).
FORBIDDEN_IMPORTS = {
    "asyncio",
    "io",
    "multiprocessing",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "subprocess",
    "tempfile",
    "urllib",
}


@pytest.fixture
def timeline() -> Timeline:
    return Timeline(cuts=SAMPLE_CUTS, duration=DURATION)


def _pseudo_random_cuts(
    seed: int, count: int, duration: float
) -> list[tuple[float, float]]:
    """Return *count* interval candidates from a deterministic generator."""
    value = seed * 7919 + 1
    points = []
    for _ in range(count * 2):
        value = (value * 1103515245 + 12345) % (2**31)
        points.append(round(duration * value / (2**31), 3))
    points.sort()
    return [
        (points[index], points[index + 1])
        for index in range(0, len(points) - 1, 2)
        if points[index] < points[index + 1]
    ]


CUT_SETS = [
    [],
    [(0.0, 1.0)],
    [(9.0, 10.0)],
    [(0.0, 2.0), (8.0, 10.0)],
    [(2.0, 3.0), (3.0, 4.0)],
    [(1.234, 2.345), (5.5, 5.501), (7.0, 9.999)],
    *(_pseudo_random_cuts(seed, 6, 10.0) for seed in range(12)),
]


# --------------------------------------------------------------------------- #
#  REQ-001 — normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cuts", "expected"),
    [
        pytest.param([], (), id="empty"),
        pytest.param([(5.0, 8.0)], ((5.0, 8.0),), id="single"),
        pytest.param(
            [(5.0, 8.0), (7.0, 10.0), (10.0, 12.0)], ((5.0, 12.0),), id="issue-example"
        ),
        pytest.param(
            [(8.0, 10.0), (5.0, 6.0)], ((5.0, 6.0), (8.0, 10.0)), id="unsorted"
        ),
        pytest.param(
            [(5.0, 8.0), (8.0, 10.0)], ((5.0, 10.0),), id="touching-endpoints"
        ),
        pytest.param([(5.0, 12.0), (6.0, 7.0)], ((5.0, 12.0),), id="nested"),
        pytest.param([(5.0, 8.0), (5.0, 9.0)], ((5.0, 9.0),), id="same-start"),
        pytest.param([(5.0, 8.0), (8.0004, 10.0)], ((5.0, 10.0),), id="gap-under-a-ms"),
    ],
)
def test_normalize_cuts_returns_disjoint_intervals(
    cuts: list[tuple[float, float]], expected: tuple[tuple[float, float], ...]
) -> None:
    assert normalize_cuts(cuts, duration=60.0) == expected


def test_timeline_exposes_normalized_cuts() -> None:
    assert Timeline(cuts=[(5.0, 8.0), (7.0, 10.0)], duration=60.0).cuts == (
        (5.0, 10.0),
    )


# --------------------------------------------------------------------------- #
#  REQ-002 / REQ-003 — the forward mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("t", "expected"),
    [
        pytest.param(0.0, 0.0, id="origin"),
        pytest.param(10.0, 10.0, id="before-any-cut"),
        pytest.param(10.500, 10.500, id="at-cut-start"),
        pytest.param(12.000, 10.500, id="inside-a-cut"),
        pytest.param(13.240, 10.500, id="at-cut-end"),
        pytest.param(20.000, 17.260, id="between-cuts"),
        pytest.param(45.500, 42.360, id="inside-the-second-cut"),
        pytest.param(DURATION, 295.380, id="source-end"),
    ],
)
def test_forward_matches_the_hand_computed_value(
    timeline: Timeline, t: float, expected: float
) -> None:
    assert timeline.forward(t) == pytest.approx(expected)


@pytest.mark.parametrize("t", [10.501, 11.0, 12.5, 13.239])
def test_forward_inside_a_cut_collapses_onto_its_end(
    timeline: Timeline, t: float
) -> None:
    assert timeline.forward(t) == pytest.approx(timeline.forward(13.240))


def test_forward_without_cuts_is_the_identity() -> None:
    identity = Timeline(cuts=[], duration=60.0)
    assert [identity.forward(t) for t in (0.0, 12.5, 60.0)] == [0.0, 12.5, 60.0]


def test_forward_at_the_source_end_matches_the_total_removed(
    timeline: Timeline,
) -> None:
    assert timeline.forward(DURATION) == pytest.approx(DURATION - 3.540)
    assert timeline.cut_duration == pytest.approx(295.380)
    assert timeline.removed_duration == pytest.approx(3.540)


# --------------------------------------------------------------------------- #
#  REQ-004 — the inverse mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("t", [0.0, 5.0, 10.500, 13.240, 20.0, 45.100, 100.0, DURATION])
def test_inverse_undoes_forward_outside_the_cuts(timeline: Timeline, t: float) -> None:
    round_trip = timeline.inverse(timeline.forward(t))
    expected = 10.500 if t == 13.240 else t
    assert round_trip == pytest.approx(expected, abs=1e-3)


def test_inverse_without_cuts_is_the_identity() -> None:
    identity = Timeline(cuts=[], duration=60.0)
    assert [identity.inverse(u) for u in (0.0, 12.5, 60.0)] == [0.0, 12.5, 60.0]


def test_inverse_of_a_collapsed_point_returns_the_cut_start(timeline: Timeline) -> None:
    assert timeline.inverse(timeline.forward(12.0)) == pytest.approx(10.500)


def test_inverse_of_the_cut_duration_returns_the_source_end() -> None:
    trailing = Timeline(cuts=[(50.0, 60.0)], duration=60.0)
    assert trailing.inverse(trailing.cut_duration) == pytest.approx(60.0)


# --------------------------------------------------------------------------- #
#  REQ-020 / REQ-021 — rejected input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("t", [-0.001, -10.0, 298.921, 400.0])
def test_forward_out_of_range_raises_value_error(timeline: Timeline, t: float) -> None:
    with pytest.raises(ValueError, match=r"t must be in \[0, 298.92\], got"):
        timeline.forward(t)


@pytest.mark.parametrize("u", [-0.001, 295.381])
def test_inverse_out_of_range_raises_value_error(timeline: Timeline, u: float) -> None:
    with pytest.raises(ValueError, match=r"u must be in \[0, "):
        timeline.inverse(u)


@pytest.mark.parametrize(
    "cut",
    [
        pytest.param((5.0, 5.0), id="empty"),
        pytest.param((8.0, 5.0), id="inverted"),
    ],
)
def test_empty_or_inverted_cut_raises_value_error(cut: tuple[float, float]) -> None:
    with pytest.raises(
        ValueError, match=r"invalid interval: start\(.*\) must be < end"
    ):
        Timeline(cuts=[cut], duration=60.0)


@pytest.mark.parametrize(
    "cut",
    [
        pytest.param((-1.0, 5.0), id="before-the-source"),
        pytest.param((55.0, 60.001), id="past-the-source"),
    ],
)
def test_out_of_range_cut_raises_value_error(cut: tuple[float, float]) -> None:
    with pytest.raises(ValueError, match=r"interval must lie within \[0, 60.0\]"):
        Timeline(cuts=[cut], duration=60.0)


@pytest.mark.parametrize("duration", [0.0, -1.0])
def test_non_positive_duration_raises_value_error(duration: float) -> None:
    with pytest.raises(ValueError, match=r"duration must be positive"):
        Timeline(cuts=[], duration=duration)


def test_map_segments_rejects_an_out_of_range_segment(timeline: Timeline) -> None:
    with pytest.raises(ValueError, match=r"interval must lie within"):
        timeline.map_segments([("s0001", 290.0, 400.0)])


# --------------------------------------------------------------------------- #
#  REQ-005 — the five subtitle mapping cases (design.md §4)
# --------------------------------------------------------------------------- #


@pytest.fixture
def subtitle_timeline() -> Timeline:
    return Timeline(cuts=[(10.0, 13.0)], duration=60.0)


def test_segment_contained_in_a_cut_is_dropped_and_warned(
    subtitle_timeline: Timeline,
) -> None:
    mapped, warnings = subtitle_timeline.map_segments([("s0002", 10.5, 12.5)])
    assert mapped == []
    assert warnings == [{"segment_id": "s0002", "kind": "dropped_by_cut"}]


def test_segment_matching_a_cut_exactly_is_dropped_and_warned(
    subtitle_timeline: Timeline,
) -> None:
    mapped, warnings = subtitle_timeline.map_segments([("s0002", 10.0, 13.0)])
    assert mapped == []
    assert warnings == [{"segment_id": "s0002", "kind": "dropped_by_cut"}]


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        pytest.param(("s0001", 8.0, 12.0), ("s0001", 8.0, 10.0), id="tail-overlaps"),
        pytest.param(("s0001", 11.0, 20.0), ("s0001", 10.0, 17.0), id="head-overlaps"),
    ],
)
def test_segment_overlapping_a_cut_end_is_clipped_before_mapping(
    subtitle_timeline: Timeline,
    segment: tuple[str, float, float],
    expected: tuple[str, float, float],
) -> None:
    mapped, warnings = subtitle_timeline.map_segments([segment])
    assert mapped == [expected]
    assert warnings == []


def test_segment_containing_a_cut_is_not_split(subtitle_timeline: Timeline) -> None:
    mapped, warnings = subtitle_timeline.map_segments([("s0003", 9.0, 20.0)])
    assert mapped == [("s0003", 9.0, 17.0)]
    assert warnings == []


def test_segment_shorter_than_min_display_is_kept_and_warned(
    subtitle_timeline: Timeline,
) -> None:
    mapped, warnings = subtitle_timeline.map_segments([("s0004", 13.0, 13.6)])
    assert mapped == [("s0004", 10.0, 10.6)]
    assert warnings == [
        {"segment_id": "s0004", "kind": "min_display", "value": 0.6, "threshold": 0.8}
    ]


def test_touching_entries_are_separated_so_time_stays_strictly_increasing(
    subtitle_timeline: Timeline,
) -> None:
    # The first segment ends inside the cut and the second starts at its end, so
    # both land on 10.0 without the separation step.
    mapped, _ = subtitle_timeline.map_segments(
        [("s0001", 5.0, 11.0), ("s0002", 13.0, 20.0)]
    )
    assert mapped == [("s0001", 5.0, 9.999), ("s0002", 10.0, 17.0)]


def test_map_segments_returns_entries_in_cut_timeline_order(
    subtitle_timeline: Timeline,
) -> None:
    mapped, _ = subtitle_timeline.map_segments(
        [("s0002", 20.0, 25.0), ("s0001", 1.0, 5.0)]
    )
    assert [entry.segment_id for entry in mapped] == ["s0001", "s0002"]


def test_warnings_follow_the_input_order(subtitle_timeline: Timeline) -> None:
    mapped, warnings = subtitle_timeline.map_segments(
        [
            ("s0001", 8.0, 12.0),
            ("s0002", 10.5, 12.5),
            ("s0003", 13.0, 13.6),
            ("s0004", 20.0, 30.0),
        ]
    )
    assert [entry.segment_id for entry in mapped] == ["s0001", "s0003", "s0004"]
    assert [warning["segment_id"] for warning in warnings] == ["s0002", "s0003"]


def test_map_segments_without_cuts_only_rounds() -> None:
    identity = Timeline(cuts=[], duration=60.0)
    mapped, warnings = identity.map_segments([("s0001", 1.2345, 5.6789)])
    assert mapped == [("s0001", 1.234, 5.679)]
    assert warnings == []


def test_map_segments_accepts_no_segments(subtitle_timeline: Timeline) -> None:
    assert subtitle_timeline.map_segments([]) == ([], [])


# --------------------------------------------------------------------------- #
#  Boundary values around min_display (design.md §3.6)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("end", "is_warned"),
    [
        pytest.param(0.800, False, id="exactly-min-display"),
        pytest.param(0.799, True, id="one-ms-under"),
        pytest.param(0.801, False, id="one-ms-over"),
    ],
)
def test_min_display_uses_a_strict_comparison(end: float, is_warned: bool) -> None:
    identity = Timeline(cuts=[], duration=60.0)
    _, warnings = identity.map_segments([("s0001", 0.0, end)], min_display=0.8)
    assert bool(warnings) is is_warned


def test_min_display_threshold_is_configurable() -> None:
    identity = Timeline(cuts=[], duration=60.0)
    _, warnings = identity.map_segments([("s0001", 0.0, 0.5)], min_display=0.4)
    assert warnings == []


# --------------------------------------------------------------------------- #
#  REQ-006 — rounding stays at the output boundary
# --------------------------------------------------------------------------- #


def test_a_hundred_cuts_stay_within_one_millisecond_of_the_exact_total() -> None:
    cuts = [(index * 2.0 + 0.1234567, index * 2.0 + 0.4567891) for index in range(100)]
    duration = 250.0
    timeline = Timeline(cuts=cuts, duration=duration)
    removed = sum(end - start for start, end in cuts)
    assert abs(timeline.forward(duration) - (duration - removed)) <= 0.001


def test_forward_keeps_sub_millisecond_precision(timeline: Timeline) -> None:
    assert timeline.forward(20.0001) != timeline.forward(20.0)


# --------------------------------------------------------------------------- #
#  REQ-040..REQ-044 — invariants over a family of cut sets
# --------------------------------------------------------------------------- #


def _sample_times(timeline: Timeline) -> list[float]:
    """Return probe times: a grid plus both sides of every cut boundary."""
    times = [index * timeline.duration / 40 for index in range(41)]
    for start, end in timeline.cuts:
        times.extend(
            [start, end, max(0.0, start - 0.002), min(timeline.duration, end + 0.002)]
        )
    return sorted(times)


@pytest.mark.parametrize("cuts", CUT_SETS)
def test_forward_is_monotonic(cuts: list[tuple[float, float]]) -> None:
    timeline = Timeline(cuts=cuts, duration=10.0)
    times = _sample_times(timeline)
    values = [timeline.forward(t) for t in times]
    assert all(earlier <= later for earlier, later in itertools.pairwise(values))


@pytest.mark.parametrize("cuts", CUT_SETS)
def test_forward_is_continuous_at_every_cut_boundary(
    cuts: list[tuple[float, float]],
) -> None:
    timeline = Timeline(cuts=cuts, duration=10.0)
    epsilon = 1e-6
    for start, end in timeline.cuts:
        assert timeline.forward(start) == pytest.approx(timeline.forward(end))
        if start >= epsilon:
            assert timeline.forward(start - epsilon) == pytest.approx(
                timeline.forward(start), abs=1e-5
            )
        if end + epsilon <= timeline.duration:
            assert timeline.forward(end + epsilon) == pytest.approx(
                timeline.forward(end), abs=1e-5
            )


@pytest.mark.parametrize("cuts", CUT_SETS)
def test_total_duration_matches_the_removed_length(
    cuts: list[tuple[float, float]],
) -> None:
    timeline = Timeline(cuts=cuts, duration=10.0)
    removed = sum(end - start for start, end in timeline.cuts)
    assert timeline.forward(10.0) == pytest.approx(10.0 - removed)
    assert timeline.cut_duration == pytest.approx(10.0 - removed)


@pytest.mark.parametrize("cuts", CUT_SETS)
def test_inverse_recovers_every_time_outside_a_cut(
    cuts: list[tuple[float, float]],
) -> None:
    timeline = Timeline(cuts=cuts, duration=10.0)
    for t in _sample_times(timeline):
        # A cut collapses its whole closed interval onto one point, so only
        # times strictly outside every cut have a unique preimage.
        if any(start <= t <= end for start, end in timeline.cuts):
            continue
        assert timeline.inverse(timeline.forward(t)) == pytest.approx(t, abs=1e-3)


@pytest.mark.parametrize("cuts", CUT_SETS)
def test_forward_undoes_inverse_everywhere(cuts: list[tuple[float, float]]) -> None:
    timeline = Timeline(cuts=cuts, duration=10.0)
    limit = timeline.cut_duration
    for index in range(41):
        u = limit * index / 40
        assert timeline.forward(timeline.inverse(u)) == pytest.approx(u, abs=1e-3)


@pytest.mark.parametrize("cuts", CUT_SETS)
def test_mapped_entries_never_overlap(cuts: list[tuple[float, float]]) -> None:
    timeline = Timeline(cuts=cuts, duration=10.0)
    segments = [
        (f"s{index:04d}", index * 0.5, index * 0.5 + 0.5) for index in range(20)
    ]
    mapped, _ = timeline.map_segments(segments)
    assert all(entry.start <= entry.end for entry in mapped)
    assert all(
        earlier.end <= later.start for earlier, later in itertools.pairwise(mapped)
    )


@pytest.mark.parametrize("cuts", CUT_SETS)
def test_every_kept_segment_is_either_mapped_or_warned_about(
    cuts: list[tuple[float, float]],
) -> None:
    timeline = Timeline(cuts=cuts, duration=10.0)
    segments = [
        (f"s{index:04d}", index * 0.5, index * 0.5 + 0.5) for index in range(20)
    ]
    mapped, warnings = timeline.map_segments(segments)
    dropped = {
        warning["segment_id"]
        for warning in warnings
        if warning["kind"] == "dropped_by_cut"
    }
    assert dropped | {entry.segment_id for entry in mapped} == {
        segment[0] for segment in segments
    }


def test_timeline_module_imports_nothing_impure() -> None:
    """REQ-044: the mapping must not reach for processes or the filesystem."""
    tree = ast.parse(Path(timeline_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    assert imported & FORBIDDEN_IMPORTS == set()
    assert "open" not in called
