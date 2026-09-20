"""Detect TurnEvent columnar relation wiring and output ownership."""
from dataclasses import replace
import gc
import math
from pathlib import Path
import weakref
from unittest.mock import patch

import pytest

from track.v2.candidate_extraction import extract_turn_candidates
from track.v2.candidate_patches import iter_track_patches
from track.v2.models import RuntimeConfig
from track.v2.turn_event_relation import TurnEventRelation
from track.v2.detect import (
    IntersectConfig, _GStarCoordinate, _cluster_pipeline, _compute_getis_ord_g_star,
    _detect_intersections_db, _select_hotspot_events,
)
from track.v2.pipeline import PipelineInput, run_pipeline
from .test_candidate_patches import _turn_tracks, _CONTEXT, _TRANSFORMER
from . import test_run_pipeline_lifecycle as lifecycle
from .test_run_pipeline_lifecycle import REFINE_ID, _empty_intersect_result
from .test_ablation_centroid_memory import _event


def _assert_no_turn_relations(data_root: Path) -> None:
    assert not list(data_root.glob("duckdb/turn-event-relations/*"))


class TrackingSequence(TurnEventRelation):
    def __init__(self, *args, **kwargs):
        if len(args) == 1:
            args = (RuntimeConfig(), args[0])
        super().__init__(*args, **kwargs)
        self.written = []

    def append(self, value):
        self.written.append(weakref.ref(value))
        super().append(value)


@pytest.mark.parametrize("batch_size", [1, 2, 1000])
def test_direct_relation_matches_memory_events_and_releases_patch_outputs(tmp_path, batch_size):
    tracks = _turn_tracks()
    cfg = IntersectConfig(stream_batch_size=batch_size, turn_window=1, min_leg_m=10)
    expected = extract_turn_candidates(iter_track_patches(tracks.items(), batch_size), cfg)
    assert isinstance(expected, list)
    with TrackingSequence(tmp_path) as spool:
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
    (4.5, [(float(i % 17), float((i * 7) % 13)) for i in range(300)]),
])
def test_compact_gstar_matches_full_event_scores_and_hotspots(tmp_path, radius, xy):
    events = [replace(_event(i, 0., 0.), event_calculate_index=i, x=x, y=y)
              for i, (x, y) in enumerate(xy)]
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

    with TurnEventRelation(RuntimeConfig(), tmp_path) as spool:
        for event in events:
            spool.append(event)
        spool.seal()
        with patch("track.v2.detect._compute_getis_ord_g_star", side_effect=compact_compute):
            actual = _select_hotspot_events(spool, cfg)
            assert [value.as_dict() for value in actual] == [
                value.as_dict() for value in expected_hotspots
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

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            stored.append(self)

    def cross(*_args, **_kwargs):
        gc.collect()
        assert len(stored[0]) == len(expected_turns)
        assert all(reference() is None for reference in stored[0].written)
        return []

    with (
        patch("track.v2.detect.TurnEventRelation", OwnedSequence),
        patch("track.v2.detect._query_spatial_extent", return_value=(0., 0., 1., 1.)),
        patch("track.v2.detect._stream_vehicle_tracks_from_db", return_value=iter(tracks.values())),
        patch("track.v2.detect._extract_cross_track_db_workspace", side_effect=cross),
        patch("track.v2.detect._build_topologies_from_tracks", side_effect=lambda clusters, *_a, **_kw: ([], clusters, [])),
    ):
        result = _detect_intersections_db(
            object(), cfg, _CONTEXT, runtime=RuntimeConfig(), data_root=tmp_path,
        )
    assert result.turn_events is stored[0]
    assert list(result.turn_events) == expected_turns
    assert result.hotspot_events == expected_hotspots
    assert result.clusters == expected_clusters
    assert stored[0]._connection is not None
    result.close()
    result.close()
    _assert_no_turn_relations(tmp_path)



@pytest.mark.parametrize("failure", [RuntimeError("failed"), KeyboardInterrupt()])
def test_detect_internal_failure_closes_output_relation(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    def fail(*_args, event_store, **_kwargs):
        event_store.append(_event(0, 0., 0.))
        raise failure

    with patch("track.v2.detect._detect_intersections_db_workspace", side_effect=fail):
        with pytest.raises(type(failure)):
            _detect_intersections_db(
                object(), IntersectConfig(), _CONTEXT,
                runtime=RuntimeConfig(), data_root=tmp_path,
            )
    _assert_no_turn_relations(tmp_path)


@pytest.mark.parametrize("failure", [None, RuntimeError("write failed"), KeyboardInterrupt()])
def test_pipeline_closes_turn_storage_after_commit_or_failure(tmp_path, failure):
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    spool = TurnEventRelation(RuntimeConfig(), tmp_path)
    spool.seal()
    result = replace(_empty_intersect_result(), turn_events=spool)
    load_clip = fixture.store.load_clip

    def check_clip(*args, **kwargs):
        assert spool._connection is None
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
        assert spool._connection is None
        if failure is not None:
            assert "finish_detect:failed" in fixture.events
        _assert_no_turn_relations(tmp_path)
        result.close()
        empty = _empty_intersect_result()
        empty.close()
        empty.close()
    finally:
        spool.close()
        fixture.doCleanups()


@pytest.mark.parametrize("failure", [RuntimeError("decode failed"), KeyboardInterrupt()])
def test_hotspot_coordinate_iteration_closes_on_error(tmp_path, failure):
    class ObservedSequence(TurnEventRelation):
        active_coordinates = 0

        def iter_coordinates(self):
            self.active_coordinates += 1
            try:
                yield next(super().iter_coordinates())
                raise failure
            finally:
                self.active_coordinates -= 1

    with ObservedSequence(RuntimeConfig(), tmp_path) as spool:
        spool.append(_event(0, 0., 0.))
        spool.append(_event(1, 1., 1.))
        spool.seal()
        with pytest.raises(type(failure)):
            _select_hotspot_events(spool, IntersectConfig(g_star_min_z=-100.))
        assert spool.active_coordinates == 0
        assert len(list(spool)) == 2


@pytest.mark.parametrize("failure", [RuntimeError("decode failed"), KeyboardInterrupt()])
def test_hotspot_output_iteration_closes_on_error(tmp_path, failure):
    class ObservedSequence(TurnEventRelation):
        active_readers = 0

        def __iter__(self):
            self.active_readers += 1
            try:
                yield from super().__iter__()
            finally:
                self.active_readers -= 1

    with ObservedSequence(RuntimeConfig(), tmp_path) as relation:
        relation.append(_event(0, 0., 0.))
        relation.seal()
        with patch("track.v2.detect.replace", side_effect=failure):
            with pytest.raises(type(failure)):
                _select_hotspot_events(relation, IntersectConfig(g_star_min_z=-100.))
        assert relation.active_readers == 0
        assert len(list(relation)) == 1


@pytest.mark.parametrize("failure", [None, RuntimeError("commit failed"), KeyboardInterrupt()])
def test_turn_relation_closes_after_result_rows_commit_before_artifact_completion(tmp_path, failure):
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    spool = TurnEventRelation(RuntimeConfig(), tmp_path)
    event = _event(7, 0., 0.)
    spool.append(replace(event, event_calculate_index=0))
    spool.seal()
    result = replace(_empty_intersect_result(), turn_events=spool)
    original_commit = fixture.store.commit
    completing = []

    def commit():
        if fixture.events[-1] == "finish_detect:completed":
            # Detect Parquet reads committed PostgreSQL rows, so the transient
            # calculation result must already be released before registration.
            assert spool._connection is None
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
        assert spool._connection is None
        _assert_no_turn_relations(tmp_path)
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
    spool = TurnEventRelation(RuntimeConfig(), tmp_path)
    spool.seal()
    result = replace(_empty_intersect_result(), turn_events=spool)

    def emit(event):
        if event.detail == "detect_persist":
            assert spool._connection is None
            raise KeyboardInterrupt()

    try:
        with patch("track.v2.pipeline.detect_intersections", return_value=result):
            with pytest.raises(KeyboardInterrupt):
                run_pipeline(PipelineInput(fixture.config, refine_id=REFINE_ID), emit=emit)
        assert "finish_detect:completed" in fixture.events
        assert "finish_detect:failed" not in fixture.events
        assert spool._connection is None
        _assert_no_turn_relations(tmp_path)
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
