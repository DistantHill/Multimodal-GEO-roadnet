from __future__ import annotations

from dataclasses import replace

import pytest

from track.v2.db_writer import (
    _CleanMember,
    _build_resolved_rows,
    _clean_key,
    finish_clean_run,
)
from track.v2.models import CleanedPointRef, CrossingPoint, HotspotCluster, PointRef


def _crossing(
    calculation_index: int,
    *,
    segment_start: CleanedPointRef,
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


def test_resolved_rows_order_four_crossings_on_one_cleaned_segment() -> None:
    start = CleanedPointRef("kept", 101, 1, 0)
    end = CleanedPointRef("kept", 102, 1, 1)
    members = [_CleanMember(start, "v1"), _CleanMember(end, "v1")]
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
    start = CleanedPointRef("kept", 1, 7, 0)
    end = CleanedPointRef("kept", 2, 7, 1)
    cluster = HotspotCluster(0, 0, 0, 0, 0, 1)
    rows = _build_resolved_rows(
        "detect",
        [_CleanMember(start, "v"), _CleanMember(end, "v")],
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
    start = CleanedPointRef("kept", 1, 1, 0)
    end = CleanedPointRef("kept", 2, 1, 1)
    base = HotspotCluster(0, 0, 0, 0, 0, 1)
    rows = _build_resolved_rows(
        "detect",
        [_CleanMember(start, "v"), _CleanMember(end, "v")],
        (1, 2),
        [
            (replace(base, cluster_calculate_index=1), _crossing(0, segment_start=start, fraction=0.5, is_entry=True)),
            (base, _crossing(0, segment_start=start, fraction=0.5, is_entry=False)),
        ],
        (3, 4),
        {(1, 0): 31, (0, 0): 30},
    )

    assert [row[7] for row in rows] == [None, 30, 31, None]


def test_clean_key_uses_stable_internal_identity_not_sequence() -> None:
    left = CleanedPointRef("injected", 8, 4, 2)
    right = replace(left, cleaned_sequence_in_subtrack=99)
    assert _clean_key(left) == _clean_key(right) == ("injected", 8, 4)


def test_completed_clean_requires_a_source_fingerprint() -> None:
    with pytest.raises(ValueError, match="requires source_fingerprint"):
        finish_clean_run(object(), "clean", status="completed")
