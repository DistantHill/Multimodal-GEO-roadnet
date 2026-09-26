"""Centerline replay retains ordered resolved IDs at the JSON boundary."""
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import numpy as np
import pytest

from track.v2.validation.replay_lineage import replay_centerline_inputs
from .test_clip import _track, _point


def test_centerline_replay_arrays_preserve_endpoint_identity_and_input_order():
    track = _track("original-vehicle", "trip", (
        _point(1, 0, crossing_id=10, intersection_id=3, crossing_type=0),
        _point(2, 10),
        _point(3, 20, crossing_id=11, intersection_id=7, crossing_type=1),
    ), 8)
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("fit", "detect", "combined", 3, 7, "LINESTRING(0 0,1 1)", 1, 0)
    cursor.__iter__.return_value = iter([(0, 10, 11, False)])
    with (
        patch("track.v2.validation.replay_lineage.fetch_detect_row", return_value=SimpleNamespace(status="completed", refine_id="refine")),
        patch("track.v2.validation.replay_lineage.verify_refine_source_fingerprint"),
        patch("track.v2.validation.replay_lineage.fetch_resolved_clip_tracks", return_value=iter((track,))),
    ):
        result = replay_centerline_inputs(conn, 99, batch_size=2)
    clip = result.clips[0]
    np.testing.assert_array_equal(clip.resolved_point_ids, [1, 2, 3])
    assert np.shares_memory(clip.lonlat, track.lonlat)
    assert not clip.lonlat.flags.writeable
    output = result.as_dict()["clips"][0]
    assert output["start"]["resolved_point_id"] == 1
    assert output["end"]["resolved_point_id"] == 3
    assert output["interior_points"][0]["resolved_point_id"] == 2
    assert output["original_from_intersection_id"] == 3
    assert output["original_to_intersection_id"] == 7


@pytest.mark.parametrize("start_type,end_type", [(1, 1), (0, 0)])
def test_centerline_replay_rejects_manifest_with_wrong_endpoint_types(start_type, end_type):
    track = _track("original-vehicle", "trip", (
        _point(1, 0, crossing_id=10, intersection_id=3, crossing_type=start_type),
        _point(2, 10),
        _point(3, 20, crossing_id=11, intersection_id=7, crossing_type=end_type),
    ), 8)
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("fit", "detect", "combined", 3, 7, "LINESTRING(0 0,1 1)", 1, 0)
    cursor.__iter__.return_value = iter([(0, 10, 11, False)])
    with (
        patch("track.v2.validation.replay_lineage.fetch_detect_row", return_value=SimpleNamespace(status="completed", refine_id="refine")),
        patch("track.v2.validation.replay_lineage.verify_refine_source_fingerprint"),
        patch("track.v2.validation.replay_lineage.fetch_resolved_clip_tracks", return_value=iter((track,))),
        pytest.raises(ValueError, match="invalid crossing type"),
    ):
        replay_centerline_inputs(conn, 99, batch_size=2)
