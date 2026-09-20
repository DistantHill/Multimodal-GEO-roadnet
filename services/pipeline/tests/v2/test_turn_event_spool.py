"""DB turn output stays on disk through Cross and owns its output lifetime."""
from dataclasses import replace
import gc
import json
import math
from pathlib import Path
import weakref
from unittest.mock import patch

import pytest

from track.v2.candidate_extraction import extract_turn_candidates
from track.v2.candidate_patches import iter_track_patches
from track.v2.clip_spool import DiskSequence
from track.v2.detect import (
    IntersectConfig, _GStarCoordinate, _cluster_pipeline, _compute_getis_ord_g_star,
    _detect_intersections_db, _select_hotspot_events,
)
from track.v2.pipeline import PipelineInput, run_pipeline
from .test_candidate_patches import _turn_tracks, _CONTEXT, _TRANSFORMER
from . import test_run_pipeline_lifecycle as lifecycle
from .test_run_pipeline_lifecycle import REFINE_ID, _empty_intersect_result
from .test_ablation_centroid_memory import _event


class TrackingSequence(DiskSequence):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.written = []

    def append(self, value, **kwargs):
        self.written.append(weakref.ref(value))
        super().append(value, **kwargs)


@pytest.mark.parametrize("batch_size", [1, 2, 1000])
def test_direct_spool_matches_memory_events_and_releases_patch_outputs(tmp_path, batch_size):
    tracks = _turn_tracks()
    cfg = IntersectConfig(stream_batch_size=batch_size, turn_window=1, min_leg_m=10)
    expected = extract_turn_candidates(iter_track_patches(tracks.items(), batch_size), cfg)
    assert isinstance(expected, list)
    with TrackingSequence(directory=tmp_path) as spool:
        assert Path(spool._directory.name).parent == tmp_path
        actual = extract_turn_candidates(
            iter_track_patches(tracks.items(), batch_size), cfg, event_store=spool,
        )
        assert actual is spool
        gc.collect()
        assert all(reference() is None for reference in spool.written)
        assert list(spool) == expected
        assert list(spool) == expected
        assert [value.event_calculate_index for value in spool] == list(range(len(expected)))


@pytest.mark.parametrize("radius,xy", [
    (1., [(0., 0.), (1., 0.), (math.nextafter(1., math.inf), 0.)]),
    (0., [(0., 0.), (0., 0.), (1., 1.)]),
    (float("inf"), [(0., 0.), (1., 1.), (2., 2.)]),
    (1., [(float("nan"), 0.), (1., 1.), (float("inf"), 2.)]),
    (4.5, [(float(i % 17), float((i * 7) % 13)) for i in range(300)]),
])
def test_compact_gstar_matches_full_event_scores_and_hotspots(tmp_path, radius, xy):
    events = [replace(_event(i, 0., 0.), event_calculate_index=identity, x=x, y=y)
              for i, (identity, (x, y)) in enumerate(zip((7 + i * 101 for i in range(len(xy))), xy))]
    cfg = IntersectConfig(g_star_radius_m=radius, g_star_min_z=-100.)
    expected_scores = _compute_getis_ord_g_star(events, radius)
    expected_hotspots = _select_hotspot_events(events, cfg)
    real_compute = _compute_getis_ord_g_star

    def compact_compute(coordinates, radius_m, **kwargs):
        assert all(type(point) is _GStarCoordinate for point in coordinates)
        assert all(not hasattr(point, "point_ref") for point in coordinates)
        scores = real_compute(coordinates, radius_m, **kwargs)
        assert scores.keys() == expected_scores.keys()
        for index, value in scores.items():
            assert value == expected_scores[index] or (
                math.isnan(value) and math.isnan(expected_scores[index])
            )
        return scores

    with DiskSequence(directory=tmp_path) as spool:
        for event in events:
            spool.append(event, key=str(event.event_calculate_index))
        with patch("track.v2.detect._compute_getis_ord_g_star", side_effect=compact_compute):
            actual = _select_hotspot_events(spool, cfg)
            assert [json.dumps(value.as_dict(), sort_keys=True) for value in actual] == [
                json.dumps(value.as_dict(), sort_keys=True) for value in expected_hotspots
            ]


def test_db_result_survives_track_workspace_and_cross_holds_no_full_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    tracks = _turn_tracks()
    cfg = IntersectConfig(stream_batch_size=1000, turn_window=1, min_leg_m=10, g_star_min_z=-100.)
    expected_turns = extract_turn_candidates(iter_track_patches(tracks.items(), 1000), cfg)
    expected_hotspots, expected_clusters = _cluster_pipeline(expected_turns, [], _TRANSFORMER, cfg)
    stored = []

    class OwnedSequence(TrackingSequence):
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            stored.append(self)

    def cross(*_args, **_kwargs):
        gc.collect()
        assert len(stored[0]) == len(expected_turns)
        assert all(reference() is None for reference in stored[0].written)
        return []

    with (
        patch("track.v2.detect.DiskSequence", OwnedSequence),
        patch("track.v2.detect._query_spatial_extent", return_value=(0., 0., 1., 1.)),
        patch("track.v2.detect._stream_vehicle_tracks_from_db", return_value=iter(tracks.values())),
        patch("track.v2.detect._extract_cross_track_db_workspace", side_effect=cross),
        patch("track.v2.detect._build_topologies_from_tracks", side_effect=lambda clusters, *_a, **_kw: ([], clusters, [])),
    ):
        result = _detect_intersections_db(object(), cfg, _CONTEXT)
    assert result.turn_events is stored[0]
    assert list(result.turn_events) == expected_turns
    assert result.hotspot_events == expected_hotspots
    assert result.clusters == expected_clusters
    assert list(tmp_path.iterdir()) == [Path(stored[0]._directory.name)]
    result.close()
    result.close()
    assert not list(tmp_path.iterdir())



@pytest.mark.parametrize("failure", [RuntimeError("failed"), KeyboardInterrupt()])
def test_detect_internal_failure_closes_output_spool(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    def fail(*_args, event_store, **_kwargs):
        event_store.append(_event(0, 0., 0.), key="0")
        raise failure

    with patch("track.v2.detect._detect_intersections_db_workspace", side_effect=fail):
        with pytest.raises(type(failure)):
            _detect_intersections_db(object(), IntersectConfig(), _CONTEXT)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", [None, RuntimeError("write failed"), KeyboardInterrupt()])
def test_pipeline_closes_turn_storage_after_commit_or_failure(tmp_path, failure):
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    spool = DiskSequence(directory=tmp_path)
    result = replace(_empty_intersect_result(), turn_events=spool)
    load_clip = fixture.store.load_clip

    def check_clip(*args, **kwargs):
        assert spool._closed
        return load_clip(*args, **kwargs)

    try:
        with (
            patch("track.v2.pipeline.detect_intersections", return_value=result),
            patch.object(fixture.store, "load_clip", side_effect=check_clip),
        ):
            if failure is None:
                run_pipeline(PipelineInput(fixture.config, refine_id=REFINE_ID))
            else:
                with patch.object(fixture.store, "persist_detect", side_effect=failure):
                    with pytest.raises(type(failure)):
                        run_pipeline(PipelineInput(fixture.config, refine_id=REFINE_ID))
        assert spool._closed
        if failure is not None:
            assert "finish_detect:failed" in fixture.events
        assert not list(tmp_path.iterdir())
        result.close()
        empty = _empty_intersect_result()
        empty.close()
        empty.close()
    finally:
        spool.close()
        fixture.doCleanups()


@pytest.mark.parametrize("phase", ["_GStarCoordinate", "replace"])
@pytest.mark.parametrize("failure", [RuntimeError("decode failed"), KeyboardInterrupt()])
def test_hotspot_coordinate_and_output_iteration_close_on_error(tmp_path, phase, failure):
    class ObservedSequence(DiskSequence):
        active_readers = 0

        def __iter__(self):
            self.active_readers += 1
            try:
                yield from super().__iter__()
            finally:
                self.active_readers -= 1

    with ObservedSequence(directory=tmp_path) as spool:
        spool.append(_event(7, 0., 0.), key="7")
        with patch(f"track.v2.detect.{phase}", side_effect=failure):
            with pytest.raises(type(failure)):
                _select_hotspot_events(spool, IntersectConfig(g_star_min_z=-100.))
        assert spool.active_readers == 0
        assert len(list(spool)) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", [None, RuntimeError("commit failed"), KeyboardInterrupt()])
def test_turn_spool_closes_after_result_rows_commit_before_artifact_completion(tmp_path, failure):
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    spool = DiskSequence(directory=tmp_path)
    event = _event(7, 0., 0.)
    spool.append(event, key="7")
    result = replace(_empty_intersect_result(), turn_events=spool)
    original_commit = fixture.store.commit
    completing = []

    def commit():
        if fixture.events[-1] == "finish_detect:completed":
            # Detect Parquet reads committed PostgreSQL rows, so the transient
            # calculation result must already be released before registration.
            assert spool._closed
            completing.append(True)
            if failure is not None:
                raise failure
        return original_commit()

    try:
        with (
            patch("track.v2.pipeline.detect_intersections", return_value=result),
            patch.object(fixture.store, "commit", side_effect=commit),
        ):
            if failure is None:
                run_pipeline(PipelineInput(fixture.config, refine_id=REFINE_ID))
            else:
                with pytest.raises(type(failure)):
                    run_pipeline(PipelineInput(fixture.config, refine_id=REFINE_ID))
        assert completing == [True]
        assert spool._closed
        assert not list(tmp_path.iterdir())
        if failure is not None:
            failed_index = fixture.events.index("finish_detect:failed")
            assert fixture.events[failed_index - 1] == "rollback"
            assert fixture.events[failed_index + 1] == "commit"
        else:
            assert "finish_detect:failed" not in fixture.events
    finally:
        spool.close()
        fixture.doCleanups()


def test_keyboard_interrupt_after_detect_commit_keeps_completed_status(tmp_path):
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    spool = DiskSequence(directory=tmp_path)
    result = replace(_empty_intersect_result(), turn_events=spool)

    def emit(event):
        if event.detail == "detect_persist":
            assert spool._closed
            raise KeyboardInterrupt()

    try:
        with patch("track.v2.pipeline.detect_intersections", return_value=result):
            with pytest.raises(KeyboardInterrupt):
                run_pipeline(PipelineInput(fixture.config, refine_id=REFINE_ID), emit=emit)
        assert "finish_detect:completed" in fixture.events
        assert "finish_detect:failed" not in fixture.events
        assert spool._closed
        assert not list(tmp_path.iterdir())
    finally:
        spool.close()
        fixture.doCleanups()


def test_keyboard_interrupt_during_detect_compute_marks_header_failed():
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    try:
        with patch("track.v2.pipeline.detect_intersections", side_effect=KeyboardInterrupt()):
            with pytest.raises(KeyboardInterrupt):
                run_pipeline(PipelineInput(fixture.config, refine_id=REFINE_ID))
        assert "finish_detect:completed" not in fixture.events
        failed_index = fixture.events.index("finish_detect:failed")
        assert fixture.events[failed_index - 1] == "rollback"
        assert fixture.events[failed_index + 1] == "commit"
    finally:
        fixture.doCleanups()
