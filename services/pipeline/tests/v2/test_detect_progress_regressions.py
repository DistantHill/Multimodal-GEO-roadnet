"""Eager and columnar entry points preserve diagnostics and cursor lifetimes."""
from unittest.mock import Mock, patch
import numpy as np
import pytest
from .config_factory import intersect_config, intersect_run_config
from .detect_columns_helpers import CONTEXT, TRANSFORMER, subtrack
from .test_detect_identity_routes import _grid_columns
from track.v2.candidates import collect_candidates as collect
from track.v2.candidates.grid_segments import build_grid_segments
from track.v2.intersections.detect_intersections import _detect_intersections_handle
from track.v2.intersections.performance_timing import PerfTracker


def _tracks():
    return {1:subtrack([[-20.,0.],[20.,0.]],subtrack_index=1,headings=[90.,90.]),
            2:subtrack([[0.,-20.],[0.,20.]],subtrack_index=2,headings=[0.,0.])}


def test_eager_cross_track_live_perf_and_final_progress_are_restored():
    clock=[0.]
    live=Mock()
    perf=PerfTracker("eager-progress")
    telemetry={}
    kernel=collect.detect_cross_track_patch
    def timed_kernel(*args,**kwargs):
        output=kernel(*args,**kwargs)
        clock[0]+=4.
        return output
    with patch.object(collect.time,"perf_counter",side_effect=lambda:clock[0]),patch.object(collect,"detect_cross_track_patch",side_effect=timed_kernel):
        result,pairs,total=collect.extract_cross_track_candidates(_tracks(),TRANSFORMER,intersect_config(),live_status=live,perf=perf,telemetry=telemetry)
    assert len(result)==pairs==total==1
    calls=[call.kwargs for call in live.update.call_args_list]
    assert any("线段对" in call.get("detail","") and call["elapsed"]==4. for call in calls)
    assert calls[-1]["progress"]==1.
    assert "1/1" in calls[-1]["detail"]
    assert telemetry["segments"]==2 and telemetry["output_candidates"]==1
    assert telemetry["cross_track_seconds"]==4.
    assert "segment_preparation" in perf.report()


def test_turn_candidates_disabled_still_consume_patches_and_report_tracks():
    clock=[0.]
    consumed=[]
    live=Mock()
    def patches():
        for key,track in _tracks().items():
            clock[0]+=4.
            consumed.append(key)
            yield {key:track}
    with patch.object(collect.time,"perf_counter",side_effect=lambda:clock[0]),patch.object(collect,"log_info") as logs:
        result=collect.extract_turn_candidates(patches(),intersect_config(use_turn_events=False),live_status=live)
    assert len(result)==0 and consumed==[1,2]
    assert len(live.update.call_args_list)==2
    assert "2 轨迹" in live.update.call_args_list[-1].kwargs["detail"]
    assert "2 轨迹/tracks" in logs.call_args_list[-1].args[0]


def test_public_eager_entry_forwards_live_and_shared_perf_tracker():
    live=Mock()
    config=intersect_run_config(use_turn_events=False,live_status=live,cross_track_dbscan_min_samples=1)
    with patch("track.v2.intersections.detect_intersections.extract_turn_candidates",wraps=collect.extract_turn_candidates) as turns,patch("track.v2.intersections.detect_intersections.extract_cross_track_candidates",wraps=collect.extract_cross_track_candidates) as crosses:
        result=_detect_intersections_handle(_tracks(),config,CONTEXT)
    assert len(result.cross_track_candidates)==1
    assert turns.call_args.kwargs["live_status"] is live
    assert crosses.call_args.kwargs["live_status"] is live
    assert isinstance(turns.call_args.kwargs["perf"],PerfTracker)
    assert turns.call_args.kwargs["perf"] is crosses.call_args.kwargs["perf"]
    assert live.update.called


def test_owned_eager_live_status_stops_on_geometry_failure():
    live=Mock()
    with patch("track.v2.telemetry.console_logging.LiveStatus",return_value=live),patch("track.v2.intersections.detect_intersections.extract_cross_track_candidates",side_effect=RuntimeError("geometry failed")),pytest.raises(RuntimeError,match="geometry failed"):
        _detect_intersections_handle(_tracks(),intersect_run_config(use_turn_events=False),CONTEXT)
    live.start.assert_called_once()
    live.stop.assert_called_once()


def test_columnar_pair_iterator_closes_on_geometry_failure():
    closed=[]
    def pairs():
        try:
            yield {},np.asarray([0],dtype=np.int64),np.asarray([1],dtype=np.int64)
        finally:
            closed.append(True)
    reader=Mock()
    reader.iter_cross_track_pair_batches.return_value=pairs()
    with pytest.raises(KeyError):
        collect.extract_columnar_cross_track_candidates(reader,"source",TRANSFORMER,intersect_config())
    assert closed==[True]


@pytest.mark.parametrize("empty",[False,True])
def test_grid_segment_publication_freezes_every_column(empty):
    columns=build_grid_segments([],max_segments=10,max_bytes=10000) if empty else _grid_columns(list(_tracks().values()))
    assert columns and all(not values.flags.writeable for values in columns.values())
    if not empty:
        with pytest.raises(ValueError):
            columns["x_start"][0]=999.


def test_straight_parallel_tracks_have_empty_complete_detect_result():
    tracks={1:subtrack([[0.,0.],[20.,0.],[40.,0.]],subtrack_index=1,headings=[90.,90.,90.]),
            2:subtrack([[0.,100.],[20.,100.],[40.,100.]],subtrack_index=2,headings=[90.,90.,90.])}
    result=_detect_intersections_handle(tracks,intersect_run_config(),CONTEXT)
    assert result.report.track_count==2
    assert result.report.turn_event_count==result.report.hotspot_event_count==result.report.cross_track_candidate_count==0
    assert result.report.cluster_count==result.report.complete_intersection_count==0
    assert result.clusters==result.topologies==result.observations==[]
