"""Patch boundaries must not change candidates, provenance or reader lifetime."""
from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

from dataclasses import replace
from unittest.mock import patch

import pytest

from track.v2.candidates.collect_candidates import extract_cross_track_candidates, extract_turn_candidates
from track.v2.candidates.build_patches import (
    build_cross_track_index,
    iter_cross_track_patches,
    iter_track_patches,
)
from track.v2.candidates.detect_cross_track_pairs import _prepare_segments, extract_cross_track_candidates_reference
from track.v2.intersections.detect_intersections import IntersectConfig
from track.v2.models import PointRef, RefinedPointRef, SpatialContext, TrajectoryPoint
from track.v2.projection import CoordinateTransformer
from track.v2.candidates.detect_turn_events import detect_turn_patch


_CONTEXT = SpatialContext(
    4326, 32650,
    {"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": .01},
)
_TRANSFORMER = CoordinateTransformer(_CONTEXT)


def _track(vehicle, subtrack, coordinates):
    points = []
    for position, (dx, dy, heading) in enumerate(coordinates):
        x, y = 500000.0 + dx, 4320000.0 + dy
        lon, lat = _TRANSFORMER.inverse(x, y)
        points.append(TrajectoryPoint(
            PointRef("fixture", f"{vehicle}:{subtrack}:{position}", True),
            vehicle, float(position), lon, lat, heading, 20.0, x=x, y=y,
            refined_point_ref=RefinedPointRef("kept", subtrack * 10 + position, subtrack, position),
        ))
    return points


def _turn_tracks():
    tracks = {}
    for vehicle, subtrack in [("A", 1), ("A", 2), ("B", 1), ("C", 1), ("D", 1)]:
        track = _track(vehicle, subtrack, [
            (0, 0, 0), (0, 20, 0), (0, 40, 90), (20, 40, 90), (40, 40, 90),
        ])
        tracks[f"{vehicle}\x00{subtrack}"] = track
    # Preserve an injected point's lineage as well as kept-point references.
    track = tracks["A\x002"]
    track[2] = replace(track[2], point_ref=PointRef(
        "injected", "injected-1", False, from_ref="a", to_ref="b",
    ))
    return tracks


def test_track_patches_are_lazy_keep_tracks_intact_and_emit_tail():
    tracks = _turn_tracks()
    consumed = []

    def source():
        for key, track in tracks.items():
            consumed.append(key)
            yield key, track

    patches = iter_track_patches(source(), 2)
    first = next(patches)
    assert consumed == list(tracks)[:2]
    assert all(first[key] is tracks[key] for key in first)
    rest = list(patches)
    assert [len(first), *(len(p) for p in rest)] == [2, 2, 1]
    assert [key for p in [first, *rest] for key in p] == list(tracks)
    assert len(first) == 2  # Later iterations must not clear an emitted patch.


@pytest.mark.parametrize("batch_size", [1, 2, 5, 100])
def test_turn_candidates_are_independent_of_patch_size(batch_size):
    tracks = _turn_tracks()
    cfg = intersect_config(stream_batch_size=batch_size, turn_window=1, min_leg_m=10)
    expected = detect_turn_patch(tracks, cfg)
    assert len(expected) == len(tracks)
    assert [e.event_calculate_index for e in expected] == list(range(len(tracks)))
    actual = extract_turn_candidates(iter_track_patches(tracks.items(), batch_size), cfg)
    assert actual == expected


def _cross_tracks():
    return {
        "A\x001": _track("A", 1, [(-20, 0, 90), (20, 0, 90)]),
        "A\x002": _track("A", 2, [(-2000, 10, 90), (2000, 10, 90)]),
        "B\x001": _track("B", 1, [(0, -20, 0), (0, 20, 0)]),
        "C\x001": _track("C", 1, [(10, -2000, 0), (10, 2000, 0)]),
    }


@pytest.mark.parametrize("batch_size", [1, 2, 65536])
@pytest.mark.parametrize("cell_m", [10.0, 100.0, 1000.0])
def test_grid_and_overflow_patches_preserve_reference_candidates(batch_size, cell_m):
    tracks = _cross_tracks()
    cfg = intersect_config(
        cross_track_pair_batch_size=batch_size,
        cross_track_grid_cell_m=cell_m,
        cross_track_max_grid_cells_per_segment=8,
    )
    expected = extract_cross_track_candidates_reference(tracks, _TRANSFORMER, cfg)
    actual = extract_cross_track_candidates(tracks, _TRANSFORMER, cfg)
    assert len(expected[0]) == 4
    assert actual[0] == expected[0]
    # The historical oracle counts track keys (including subtracks); the
    # production extractor reports unique vehicle pairs: A/B, A/C and B/C.
    assert actual[1:] == (3, 3)


def test_mixed_grid_and_overflow_schedule_pairs_once_under_the_patch_bound():
    cfg = intersect_config(cross_track_max_grid_cells_per_segment=8)
    segments, _ = _prepare_segments(_cross_tracks(), _TRANSFORMER, cfg)
    index = build_cross_track_index(segments, cell_m=100.0, max_cells=8)
    assert index.grid
    assert index.overflow_segment_ids == [1, 3]
    pairs = []
    for ids_a, ids_b in iter_cross_track_patches(index, 2):
        assert 0 < len(ids_a) == len(ids_b) <= 2
        pairs.extend(zip(ids_a.tolist(), ids_b.tolist()))
    assert len(pairs) == len(set(pairs))
    assert set(pairs) == {(0, 2), (0, 3), (1, 2), (1, 3), (2, 3)}


def test_consumed_turn_patch_is_released_before_producer_builds_next_patch():
    import gc
    import weakref

    references = []

    def source():
        for index in range(4):
            if index == 2:
                gc.collect()
                # The scheduler may still hold the final track from the old
                # patch, but its first track must no longer be retained by the
                # outer consumer while this next patch is being populated.
                assert references[0]() is None
            points = _track(str(index), index + 1, [
                (0, 0, 0), (0, 20, 0), (0, 40, 90), (20, 40, 90), (40, 40, 90),
            ])
            references.append(weakref.ref(points[2]))
            yield str(index), points

    cfg = intersect_config(stream_batch_size=2, turn_window=1, min_leg_m=10)
    events = extract_turn_candidates(iter_track_patches(source(), cfg.stream_batch_size), cfg)
    gc.collect()
    assert len(events) == 4
    assert [event.event_calculate_index for event in events] == [0, 1, 2, 3]
    assert all(event.refined_point_ref is not None for event in events)
    assert all(ref() is None for ref in references)
