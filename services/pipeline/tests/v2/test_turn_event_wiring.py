from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)
"""Detect TurnEvent columnar relation wiring and output ownership."""
from dataclasses import replace
import gc
import math
from pathlib import Path
import weakref
from unittest.mock import MagicMock, patch

import pytest

from track.v2.candidates.collect_candidates import extract_turn_candidates
from track.v2.candidates.build_patches import iter_track_patches
from track.v2.models import RuntimeConfig
from track.v2.candidates.turn_event_relation import TurnEventRelation
from track.v2.intersections.detect_intersections import (
    IntersectConfig, _IntersectRunConfig, _GStarCoordinate,
    _cluster_pipeline, _compute_getis_ord_g_star,
    _detect_intersections_db, _select_hotspot_events,
)
from track.v2.orchestration.run_pipeline import PipelineInput, run_pipeline
from .test_candidate_patches import _turn_tracks, _CONTEXT, _TRANSFORMER
from . import test_run_pipeline_lifecycle as lifecycle
from .test_run_pipeline_lifecycle import REFINE_ID, _empty_intersect_result
from .test_ablation_centroid_memory import _event


def _run_config(config: IntersectConfig) -> _IntersectRunConfig:
    return _IntersectRunConfig(
        **config.as_dict(),
        query_active_from_db=True,
        db_table="raw_points",
        kept_table="kept_trajectory_points",
        source_table="raw_points",
        refine_id=REFINE_ID,
        live_status=None,
    )


def _assert_no_turn_relations(data_root: Path) -> None:
    assert not list(data_root.glob("duckdb/turn-event-relations/*"))


class TrackingSequence(TurnEventRelation):
    def __init__(self, *args, **kwargs):
        if len(args) == 1:
            args = (runtime_config(), args[0])
        super().__init__(*args, **kwargs)
        self.written = []

    def append(self, value):
        self.written.append(weakref.ref(value))
        super().append(value)


@pytest.mark.parametrize("batch_size", [1, 2, 1000])
def test_direct_relation_matches_memory_events_and_releases_patch_outputs(tmp_path, batch_size):
    tracks = _turn_tracks()
    cfg = intersect_config(stream_batch_size=batch_size, turn_window=1, min_leg_m=10)
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
    cfg = intersect_config(g_star_radius_m=radius, g_star_min_z=-100.)
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

    with TurnEventRelation(runtime_config(), tmp_path) as spool:
        for event in events:
            spool.append(event)
        spool.seal()
        with patch("track.v2.intersections.detect_intersections._compute_getis_ord_g_star", side_effect=compact_compute):
            actual = _select_hotspot_events(spool, cfg)
            assert [value.as_dict() for value in actual] == [
                value.as_dict() for value in expected_hotspots
            ]


def test_db_result_survives_columnar_run_and_cross_holds_no_full_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    tracks = _turn_tracks()
    cfg = intersect_config(stream_batch_size=1000, turn_window=1, min_leg_m=10, g_star_min_z=-100.)
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

    reader = type("Reader", (), {
        "extent": lambda self: (0., 0., 1., 1.),
        "iter_turn_candidates": lambda self, *_args, **_kwargs: (
            replace(event) for event in expected_turns
        ),
        "track_count": lambda self: len(tracks),
    })()

    with (
        patch("track.v2.intersections.detect_intersections.TurnEventRelation", OwnedSequence),
        patch("track.v2.intersections.detect_intersections.run_grid_cross_track_task", side_effect=cross),
        patch("track.v2.intersections.detect_intersections._build_topologies_from_tracks", side_effect=lambda clusters, *_a, **_kw: ([], clusters, [])),
    ):
        result = _detect_intersections_db(
            object(), _run_config(cfg), _CONTEXT,
            runtime=runtime_config(), data_root=tmp_path,
            refine_reader=reader, connect_worker=lambda: object(),
        )
    assert result.turn_events is stored[0]
    assert list(result.turn_events) == expected_turns
    assert result.hotspot_events == expected_hotspots
    assert result.clusters == expected_clusters
    assert stored[0]._connection is not None
    result.close()
    result.close()
    _assert_no_turn_relations(tmp_path)


def test_columnar_detect_feeds_turn_relation_through_bounded_extend(tmp_path):
    calls = []

    class ObservedRelation(TurnEventRelation):
        def extend(self, events, *, batch_size=None):
            calls.append(batch_size)
            return super().extend(events, batch_size=batch_size)

    reader = type("Reader", (), {
        "extent": lambda self: (0., 0., 1., 1.),
        "iter_turn_candidates": lambda self, *_args, **_kwargs: iter(()),
        "track_count": lambda self: 0,
    })()
    cfg = intersect_config(stream_batch_size=7, use_cross_track_candidates=False)
    with (
        patch("track.v2.intersections.detect_intersections.TurnEventRelation", ObservedRelation),
        patch("track.v2.intersections.detect_intersections._build_topologies_from_tracks", return_value=([], [], [])),
    ):
        result = _detect_intersections_db(
            object(), _run_config(cfg), _CONTEXT,
            runtime=runtime_config(), data_root=tmp_path,
            refine_reader=reader,
        )
    assert calls == [7]
    result.close()



@pytest.mark.parametrize("failure", [RuntimeError("failed"), KeyboardInterrupt()])
def test_detect_internal_failure_closes_output_relation(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    def fail(*_args, event_store, **_kwargs):
        event_store.append(_event(0, 0., 0.))
        raise failure

    with patch("track.v2.intersections.detect_intersections._run_columnar_db_detect", side_effect=fail):
        with pytest.raises(type(failure)):
            _detect_intersections_db(
                object(), _run_config(intersect_config()), _CONTEXT,
                runtime=runtime_config(), data_root=tmp_path, refine_reader=MagicMock(),
            )
    _assert_no_turn_relations(tmp_path)


@pytest.mark.parametrize("failure", [None, RuntimeError("write failed"), KeyboardInterrupt()])
def test_pipeline_closes_turn_storage_after_commit_or_failure(tmp_path, failure):
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    spool = TurnEventRelation(runtime_config(), tmp_path)
    spool.seal()
    result = replace(_empty_intersect_result(), turn_events=spool)
    load_clip = fixture.store.load_clip

    def check_clip(*args, **kwargs):
        assert spool._connection is None
        return load_clip(*args, **kwargs)

    try:
        with (
            patch("track.v2.stages.detect.detect_intersections", return_value=result),
            patch.object(fixture.store, "load_clip", side_effect=check_clip),
        ):
            if failure is None:
                run_pipeline(PipelineInput(refine_id=REFINE_ID), fixture.config)
            else:
                with patch.object(fixture.store, "persist_detect", side_effect=failure):
                    with pytest.raises(type(failure)):
                        run_pipeline(PipelineInput(refine_id=REFINE_ID), fixture.config)
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

    with ObservedSequence(runtime_config(), tmp_path) as spool:
        spool.append(_event(0, 0., 0.))
        spool.append(_event(1, 1., 1.))
        spool.seal()
        with pytest.raises(type(failure)):
            _select_hotspot_events(spool, intersect_config(g_star_min_z=-100.))
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

    with ObservedSequence(runtime_config(), tmp_path) as relation:
        relation.append(_event(0, 0., 0.))
        relation.seal()
        with patch("track.v2.intersections.detect_intersections.replace", side_effect=failure):
            with pytest.raises(type(failure)):
                _select_hotspot_events(relation, intersect_config(g_star_min_z=-100.))
        assert relation.active_readers == 0
        assert len(list(relation)) == 1


@pytest.mark.parametrize("failure", [None, RuntimeError("commit failed"), KeyboardInterrupt()])
def test_turn_relation_closes_after_result_rows_commit_before_artifact_completion(tmp_path, failure):
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    spool = TurnEventRelation(runtime_config(), tmp_path)
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
            patch("track.v2.stages.detect.detect_intersections", return_value=result),
            patch.object(fixture.store, "commit", side_effect=commit),
        ):
            if failure is None:
                run_pipeline(PipelineInput(refine_id=REFINE_ID), fixture.config)
            else:
                with pytest.raises(type(failure)):
                    run_pipeline(PipelineInput(refine_id=REFINE_ID), fixture.config)
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
    spool = TurnEventRelation(runtime_config(), tmp_path)
    spool.seal()
    result = replace(_empty_intersect_result(), turn_events=spool)

    def emit(event):
        if event.detail == "detect_persist":
            assert spool._connection is None
            raise KeyboardInterrupt()

    try:
        with patch("track.v2.stages.detect.detect_intersections", return_value=result):
            with pytest.raises(KeyboardInterrupt):
                run_pipeline(PipelineInput(refine_id=REFINE_ID), fixture.config, emit=emit)
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
        with patch("track.v2.stages.detect.detect_intersections", side_effect=KeyboardInterrupt()):
            with pytest.raises(KeyboardInterrupt):
                run_pipeline(PipelineInput(refine_id=REFINE_ID), fixture.config)
        assert "finish_detect:completed" not in fixture.events
        failed_index = fixture.events.index("finish_detect:failed")
        assert fixture.events[failed_index - 1] == "rollback"
        assert fixture.events[failed_index + 1] == "commit"
    finally:
        fixture.doCleanups()
