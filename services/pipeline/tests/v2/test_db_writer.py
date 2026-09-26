from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from track.v2.db.write_detect_results import _RefineMember, _refine_key
from track.v2.db.write_refine_results import finish_refine_run
from track.v2.models import RefinedPointRef, CrossingPoint, HotspotCluster, PointRef


# Materialized pre-streaming writer retained only as a regression oracle.
def _build_resolved_rows(
    detect_id: str,
    refine_members: list[_RefineMember],
    refined_ids: tuple[int, ...],
    complete_crossings: list[tuple[HotspotCluster, Any]],
    crossing_resolved_ids: tuple[int, ...],
    crossing_id_by_calculation: dict[tuple[int, int], int],
) -> list[tuple[Any, ...]]:
    by_subtrack: dict[int, list[tuple[tuple[Any, ...], tuple[Any, ...]]]] = {}
    for member, resolved_id in zip(refine_members, refined_ids, strict=True):
        identity = member.identity
        kept_id = identity.point_id if identity.point_kind == "kept" else None
        injected_id = identity.point_id if identity.point_kind == "injected" else None
        by_subtrack.setdefault(identity.subtrack_index, []).append((
            (
                float(identity.refined_sequence_in_subtrack),
                1,
                -1,
                -1,
                resolved_id,
            ),
            (
                resolved_id,
                detect_id,
                identity.subtrack_index,
                identity.refined_sequence_in_subtrack,
                kept_id,
                injected_id,
                None,
            ),
        ))
    for (cluster, crossing), resolved_id in zip(
        complete_crossings,
        crossing_resolved_ids,
        strict=True,
    ):
        if crossing.segment_start is None:
            raise ValueError("a persisted crossing requires a stable segment start")
        identity = crossing.segment_start
        key = (cluster.cluster_calculate_index, crossing.crossing_calculate_index)
        crossing_id = crossing_id_by_calculation[key]
        # A root at t=1 belongs immediately before the segment endpoint; a
        # t=0 root belongs immediately after its segment start. Interior roots
        # cannot tie a refined point. Within one event batch, exits precede
        # entries as required by the active-set sequence contract.
        endpoint_placement = 0 if crossing.segment_fraction >= 1.0 - 1e-12 else 2
        by_subtrack.setdefault(identity.subtrack_index, []).append((
            (
                identity.refined_sequence_in_subtrack + crossing.segment_fraction,
                endpoint_placement,
                0 if not crossing.is_entry else 1,
                cluster.cluster_calculate_index,
                crossing_id,
            ),
            (
                resolved_id,
                detect_id,
                identity.subtrack_index,
                None,
                None,
                None,
                crossing_id,
            ),
        ))
    rows: list[tuple[Any, ...]] = []
    for subtrack_index in sorted(by_subtrack):
        ordered = sorted(by_subtrack[subtrack_index], key=lambda item: item[0])
        for sequence, (_sort_key, row) in enumerate(ordered):
            rows.append((*row[:4], sequence, *row[4:]))
    return rows


def _crossing(
    calculation_index: int,
    *,
    segment_start: RefinedPointRef,
    fraction: float,
    is_entry: bool,
) -> CrossingPoint:
    return CrossingPoint(
        crossing_calculate_index=calculation_index,
        cluster_ref="cluster",
        vehicle_id="v1",
        segment_start=segment_start,
        subtrack_index=segment_start.subtrack_index,
        point_ref=PointRef("source", "1", True),
        crossing_x=fraction,
        crossing_y=0.0,
        crossing_lon=fraction,
        crossing_lat=0.0,
        is_entry=is_entry,
        branch_angle_deg=0.0,
        segment_fraction=fraction,
    )


def test_resolved_rows_order_four_crossings_on_one_refined_segment() -> None:
    start = RefinedPointRef("kept", 101, 1, 0)
    end = RefinedPointRef("kept", 102, 1, 1)
    members = [_RefineMember(start, "v1"), _RefineMember(end, "v1")]
    cluster_a = HotspotCluster(0, 0, 0, 0, 0, 1)
    cluster_b = replace(cluster_a, cluster_calculate_index=1)
    crossings = [
        (cluster_a, _crossing(0, segment_start=start, fraction=0.2, is_entry=True)),
        (cluster_a, _crossing(1, segment_start=start, fraction=0.4, is_entry=False)),
        (cluster_b, _crossing(0, segment_start=start, fraction=0.6, is_entry=True)),
        (cluster_b, _crossing(1, segment_start=start, fraction=0.8, is_entry=False)),
    ]
    crossing_ids = {(0, 0): 201, (0, 1): 202, (1, 0): 203, (1, 1): 204}

    rows = _build_resolved_rows(
        "detect",
        members,
        (301, 302),
        crossings,
        (303, 304, 305, 306),
        crossing_ids,
    )

    assert [row[0] for row in rows] == [301, 303, 304, 305, 306, 302]
    assert [row[4] for row in rows] == list(range(6))
    assert [row[3] for row in rows] == [0, None, None, None, None, 1]
    assert [row[7] for row in rows] == [None, 201, 202, 203, 204, None]


def test_resolved_rows_place_t1_before_endpoint_and_t0_after_start() -> None:
    start = RefinedPointRef("kept", 1, 7, 0)
    end = RefinedPointRef("kept", 2, 7, 1)
    cluster = HotspotCluster(0, 0, 0, 0, 0, 1)
    rows = _build_resolved_rows(
        "detect",
        [_RefineMember(start, "v"), _RefineMember(end, "v")],
        (10, 11),
        [
            (cluster, _crossing(0, segment_start=start, fraction=0.0, is_entry=True)),
            (cluster, _crossing(1, segment_start=start, fraction=1.0, is_entry=False)),
        ],
        (12, 13),
        {(0, 0): 20, (0, 1): 21},
    )

    assert [row[0] for row in rows] == [10, 12, 13, 11]


def test_same_progress_crossings_sort_exit_before_entry() -> None:
    start = RefinedPointRef("kept", 1, 1, 0)
    end = RefinedPointRef("kept", 2, 1, 1)
    base = HotspotCluster(0, 0, 0, 0, 0, 1)
    rows = _build_resolved_rows(
        "detect",
        [_RefineMember(start, "v"), _RefineMember(end, "v")],
        (1, 2),
        [
            (replace(base, cluster_calculate_index=1), _crossing(0, segment_start=start, fraction=0.5, is_entry=True)),
            (base, _crossing(0, segment_start=start, fraction=0.5, is_entry=False)),
        ],
        (3, 4),
        {(1, 0): 31, (0, 0): 30},
    )

    assert [row[7] for row in rows] == [None, 30, 31, None]


def test_refine_key_uses_stable_internal_identity_not_sequence() -> None:
    left = RefinedPointRef("injected", 8, 4, 2)
    right = replace(left, refined_sequence_in_subtrack=99)
    assert _refine_key(left) == _refine_key(right) == ("injected", 8, 4)


def test_completed_refine_requires_a_source_fingerprint() -> None:
    with pytest.raises(ValueError, match="requires source_fingerprint"):
        finish_refine_run(object(), "refine", status="completed")
