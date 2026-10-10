"""Turn business batches retain Detect output ownership and failure cleanup."""
from dataclasses import replace
import gc
import math
import weakref
from unittest.mock import MagicMock,patch
import numpy as np
import pytest
from .config_factory import intersect_config,runtime_config
from .detect_columns_helpers import CONTEXT as _CONTEXT,TRANSFORMER as _TRANSFORMER,subtrack,sql_turns_from_subtrack
from .writer_relation_helpers import turn_batch
from .test_turn_event_relation import concatenate,assert_batch_equal
from track.v2.candidates.turn_event_relation import TurnEventRelation
from track.v2.candidates.detect_turn_events import empty_turn_batch
from track.v2.candidates.detect_cross_track_pairs import empty_cross_batch
from track.v2.intersections.detect_intersections import (IntersectConfig,_IntersectRunConfig,
    _compute_getis_ord_g_star,_select_hotspot_events,_detect_intersections_db)
from track.v2.orchestration.run_pipeline import PipelineInput,run_pipeline
from . import test_run_pipeline_lifecycle as lifecycle
from .test_run_pipeline_lifecycle import REFINE_ID,_empty_intersect_result


def _events(xy,*,start=0):
    xy=np.asarray(xy,dtype=np.float64)
    return turn_batch(len(xy),start=start,xy=xy,lonlat=_TRANSFORMER.inverse_many(xy),score=np.full(len(xy),90.))


def _turn_tracks():
    return {i:subtrack([[i*100.,0.],[i*100.,20.],[i*100.+20.,20.]],subtrack_index=i,headings=[0,90,90]) for i in range(1,5)}


def _run_config(config):
    return _IntersectRunConfig(**config.as_dict(),db_table='raw_points',
        kept_table='kept_trajectory_points',source_table='raw_points',refine_id=REFINE_ID,live_status=None)


def _assert_no_turn_relations(data_root):
    assert not list(data_root.glob('duckdb/turn-event-relations/*'))


class TrackingSequence(TurnEventRelation):
    def __init__(self,*args,**kwargs):
        if len(args)==1: args=(runtime_config(),args[0])
        super().__init__(*args,**kwargs); self.written=[]
    def append_batch(self,value):
        self.written.append(weakref.ref(value.xy)); super().append_batch(value)


@pytest.mark.parametrize('radius,xy',[
    (1.,[(0.,0.),(1.,0.),(math.nextafter(1.,math.inf),0.)]),
    (0.,[(0.,0.),(0.,0.),(1.,1.)]),(float('inf'),[(0.,0.),(1.,1.),(2.,2.)]),
    (4.5,[(float(i%17),float((i*7)%13)) for i in range(300)])])
def test_compact_gstar_matches_full_event_scores_and_hotspots(tmp_path,radius,xy):
    events=_events(xy); cfg=intersect_config(g_star_radius_m=radius,g_star_min_z=-100.)
    expected_scores=_compute_getis_ord_g_star(events,radius)
    expected_hotspots=_select_hotspot_events(events,cfg)
    with TurnEventRelation(runtime_config(),tmp_path,batch_size=17) as spool:
        spool.append_batch(events); spool.seal()
        actual_scores=_compute_getis_ord_g_star(spool,radius)
        assert actual_scores==expected_scores
        assert_batch_equal(_select_hotspot_events(spool,cfg),expected_hotspots)


def test_db_result_survives_columnar_run_and_cross_holds_no_full_turns(tmp_path,monkeypatch):
    tracks=_turn_tracks(); cfg=intersect_config(stream_batch_size=1000,turn_window=1,min_leg_m=10,g_star_min_z=-100.)
    from track.v2.candidates.detect_turn_events import concatenate_turn_batches
    expected_turns = concatenate_turn_batches([
        sql_turns_from_subtrack(track, cfg, first_index=i)
        for i, track in enumerate(tracks.values())
    ])
    from .detect_columns_helpers import cluster_candidate_batches
    expected_hotspots,expected_clusters=cluster_candidate_batches(expected_turns,empty_cross_batch(),_TRANSFORMER,cfg)
    stored=[]
    class OwnedSequence(TrackingSequence):
        def __init__(self,*args,**kwargs): super().__init__(*args,**kwargs); stored.append(self)
    def cross(*args,**kwargs):
        gc.collect(); assert len(stored[0])==len(expected_turns)
        # Reader fixture owns its input batch; no event row wrappers exist.
        assert all(batch() is expected_turns.xy for batch in stored[0].written if batch() is not None)
        return empty_cross_batch()
    reader=type('Reader',(),{'extent':lambda self:(0.,0.,1.,1.),
        'iter_turn_candidates':lambda self,*a,**kw:iter([expected_turns]),'track_count':lambda self:len(tracks)})()
    with patch('track.v2.intersections.detect_intersections.TurnEventRelation',OwnedSequence),patch('track.v2.intersections.detect_intersections.run_grid_cross_track_task',side_effect=cross),patch('track.v2.intersections.detect_intersections._build_topologies_from_reader',side_effect=lambda clusters,*a,**kw:([],clusters,[])):
        result=_detect_intersections_db(object(),_run_config(cfg),_CONTEXT,runtime=runtime_config(),data_root=tmp_path,refine_reader=reader,connect_worker=lambda:object())
    assert result.candidate_results[0].events is stored[0]
    assert_batch_equal(concatenate(result.turn_events.iter_batches()),expected_turns)
    assert_batch_equal(concatenate(result.hotspot_events.iter_batches()),expected_hotspots)
    assert [cluster.members.calculation_index.tolist() for cluster in result.clusters]==[cluster.members.calculation_index.tolist() for cluster in expected_clusters]
    assert all(cluster.algorithm_sources == ("g_star:default",) for cluster in result.clusters)
    assert stored[0]._connection is not None
    result.close(); result.close(); _assert_no_turn_relations(tmp_path)


def test_columnar_detect_feeds_turn_relation_through_bounded_batches(tmp_path):
    calls=[]
    class ObservedRelation(TurnEventRelation):
        def append_batch(self,event):
            calls.append(self._batch_size); return super().append_batch(event)
    reader=type('Reader',(),{'extent':lambda self:(0.,0.,1.,1.),'iter_turn_candidates':lambda self,*a,**kw:iter([_events([[0., 0.]])]),'track_count':lambda self:0})()
    cfg=intersect_config(stream_batch_size=7,use_cross_track_candidates=False)
    with patch('track.v2.intersections.detect_intersections.TurnEventRelation',ObservedRelation),patch('track.v2.intersections.detect_intersections._build_topologies_from_reader',return_value=([],[],[])):
        result=_detect_intersections_db(object(),_run_config(cfg),_CONTEXT,runtime=runtime_config(relation_batch_size=7),data_root=tmp_path,refine_reader=reader)
    assert calls==[7]; result.close()


def test_columnar_turn_detection_emits_heartbeat_and_batch_counts(tmp_path):
    live = MagicMock()
    batches = [_events([[0., 0.]], start=0), _events([[10., 0.], [20., 0.]], start=1)]
    reader = type('Reader', (), {
        'extent': lambda self: (0., 0., 1., 1.),
        'iter_turn_candidates': lambda self, *args, **kwargs: iter(batches),
        'track_count': lambda self: 0,
    })()
    cfg = replace(_run_config(intersect_config(use_cross_track_candidates=False)), live_status=live)
    with patch('track.v2.intersections.detect_intersections.heartbeat_progress') as progress, \
         patch('track.v2.intersections.detect_intersections._build_candidate_clusters', return_value=[]), \
         patch('track.v2.intersections.detect_intersections._build_topologies_from_reader', return_value=([], [], [])):
        result = _detect_intersections_db(
            object(), cfg, _CONTEXT, runtime=runtime_config(), data_root=tmp_path,
            refine_reader=reader,
        )
    assert len(result.turn_events) == 3
    assert '转向事件检测' in live.update.call_args_list[0].kwargs['detail']
    assert [call.args[0] for call in progress.call_args_list] == [1, 3]
    assert all(call.kwargs['unit'] == '转向事件' for call in progress.call_args_list)
    result.close()


@pytest.mark.parametrize('failure',[RuntimeError('failed'),KeyboardInterrupt()])
def test_detect_internal_failure_closes_output_relation(tmp_path,monkeypatch,failure):
    def fail(config,execution):
        relation = execution.own(TurnEventRelation(runtime_config(), tmp_path))
        relation.append_batch(_events([[0.,0.]]))
        raise failure
    with patch('track.v2.intersections.detect_intersections.execute_candidates',side_effect=fail):
        with pytest.raises(type(failure)):
            _detect_intersections_db(object(),_run_config(intersect_config()),_CONTEXT,runtime=runtime_config(),data_root=tmp_path,refine_reader=MagicMock())
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

        def iter_coordinate_batches(self, *, include_source=True):
            self.active_coordinates += 1
            try:
                yield next(super().iter_coordinate_batches(include_source=include_source))
                raise failure
            finally:
                self.active_coordinates -= 1

    with ObservedSequence(runtime_config(), tmp_path) as spool:
        spool.append_batch(_events([[0.,0.]]))
        spool.append_batch(_events([[1.,1.]],start=1))
        spool.seal()
        with pytest.raises(type(failure)):
            _select_hotspot_events(spool, intersect_config(g_star_min_z=-100.))
        assert spool.active_coordinates == 0
        assert len(concatenate(spool.iter_batches())) == 2


@pytest.mark.parametrize("failure", [RuntimeError("decode failed"), KeyboardInterrupt()])
def test_hotspot_output_iteration_closes_on_error(tmp_path, failure):
    class ObservedSequence(TurnEventRelation):
        active_readers = 0

        def iter_batches(self,**kwargs):
            self.active_readers += 1
            try:
                yield from super().iter_batches(**kwargs)
            finally:
                self.active_readers -= 1

    with ObservedSequence(runtime_config(), tmp_path) as relation:
        relation.append_batch(_events([[0.,0.]]))
        relation.seal()
        with patch("track.v2.intersections.detect_intersections.replace", side_effect=failure):
            with pytest.raises(type(failure)):
                _select_hotspot_events(relation, intersect_config(g_star_min_z=-100.))
        assert relation.active_readers == 0
        assert len(concatenate(relation.iter_batches())) == 1


@pytest.mark.parametrize("failure", [None, RuntimeError("commit failed"), KeyboardInterrupt()])
def test_turn_relation_closes_after_result_rows_commit_before_artifact_completion(tmp_path, failure):
    fixture = lifecycle.RunPipelineLifecycleTest()
    fixture.setUp()
    spool = TurnEventRelation(runtime_config(), tmp_path)
    spool.append_batch(_events([[0.,0.]]))
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
