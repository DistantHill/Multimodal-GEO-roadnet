from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from track.v2.time_selection import time_bounds, validate_refine_time
from track.v2.stages.refine import load_completed_refine_ref
from track.v2.trajectories.results.read_parquet import RefineDatasetReader
from track.v2.projection import SpatialWindow
from .config_factory import runtime_config
from .test_refine_parquet_reader import _published_tracks


def at(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


def record():
    return SimpleNamespace(id='week', status='completed', source_table='source', spatial_context=object(),
        parameters={'source_selection': {'time_start': at(0).isoformat(), 'time_end': at(7*86400).isoformat()}})


def test_week_refine_can_select_one_day_without_matching_historical_selection():
    store = Mock()
    store.get_refine_run.return_value = record()
    ref = load_completed_refine_ref(store=store, refine_id='week', time_start=at(86400), time_end=at(2*86400))
    assert (ref.time_start, ref.time_end) == (at(86400), at(2*86400))
    store.connection.cursor.assert_not_called()


@pytest.mark.parametrize('start,end', [(None,None), ('',''), (at(0),at(7*86400)), (at(1),None), (None,at(2))])
def test_empty_or_partial_bounds_use_refine_coverage(start,end):
    assert validate_refine_time(Mock(),record(),start,end) == time_bounds(start,end)


@pytest.mark.parametrize('start,end', [(at(-1),at(1)), (at(1),at(7*86400+1)), (at(7*86400),None), (None,at(0))])
def test_out_of_range_bounds_rejected(start,end):
    with pytest.raises(ValueError,match='exceeds Refine time range'):
        validate_refine_time(Mock(),record(),start,end)


@pytest.mark.parametrize('start,end', [('2023-03-01',None), (at(2),at(1)), (at(1),at(1))])
def test_invalid_bounds_rejected(start,end):
    with pytest.raises(ValueError):
        time_bounds(start,end)


def test_parquet_filters_before_tracks_turns_topology_and_extent(tmp_path):
    # A single Refine subtrack crosses both time boundaries.
    descriptor = _published_tracks(tmp_path, [('v',1,[(t,t,t,90) for t in range(8)])])
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config(),
                             time_start=at(2),time_end=at(5)) as reader:
        tracks = list(reader.iter_tracks('source'))
        assert len(tracks) == 1
        assert list(tracks[0].timestamp) == [2,3,4]
        assert list(tracks[0].refine_point_id) == [3,4,5]
        assert reader.extent() == (2,2,4,4)
        assert reader.track_count() == 1
        assert len(list(reader.iter_subtrack_batches('v',1))[0]) == 3
        windows = [SpatialWindow(2,2,4,4)]
        topology = list(reader.iter_topology_tracks('source',windows))
        assert len(topology) == 1
        assert list(topology[0].points.timestamp) == [2,3,4]
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config()) as reader:
        assert len(list(reader.iter_tracks('source'))[0]) == 8


def test_filter_removes_outside_neighbors_before_turn_detection(tmp_path):
    from .config_factory import intersect_config
    cfg = intersect_config(turn_window=1,min_turn_deg=1,max_turn_deg=180,
                           min_leg_m=0,min_movement_speed_mps=0)
    descriptor = _published_tracks(tmp_path, [('v',1,[(0,0,0,0),(1,1,0,0),(2,1,1,90),(3,2,1,0),(4,2,2,90)])])
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config()) as reader:
        assert sum(len(batch) for batch in reader.iter_turn_candidates('source',cfg)) > 0
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config(),time_start=at(2),time_end=at(4)) as reader:
        assert sum(len(batch) for batch in reader.iter_turn_candidates('source',cfg)) == 0


def test_kept_and_injected_points_share_half_open_filter(tmp_path):
    from .test_refine_parquet_reader import _published
    descriptor = _published(tmp_path)
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config(),time_start=at(1),time_end=at(2)) as reader:
        track = list(reader.iter_tracks('source'))[0]
        assert track.kind.tolist() == ['p','i','p']
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config(),time_end=at(1)) as reader:
        assert reader.track_count() == 0
        assert reader.extent() is None


def test_grid_time_filter_drops_hits_and_outside_neighbors(monkeypatch):
    from .test_grid_points import _Cursor
    from track.v2.db import fetch_refine_points_in_grid as module
    from uuid import uuid4
    def point(sequence, kind=0):
        return (sequence,kind,sequence+10,sequence+10 if kind==0 else None,
                float(sequence),0.,90.,float(sequence),2.,110.,20.,None,sequence,'source')
    rows = [('car',1)+point(n)+point(n-1,1)+point(n+1,1) for n in (1,2,3,4)]
    data,metadata = _Cursor(rows),_Cursor([])
    conn = SimpleNamespace(cursor=lambda **kw: data if kw else metadata)
    monkeypatch.setattr(module,'validate_source_table',lambda *_:
                        SimpleNamespace(source_table='source',quoted_table='"source"'))
    batches = list(module.fetch_points_in_grid(conn,(0,0),[[(0,0,9,9)]],uuid4(),
                   SimpleNamespace(processing_srid=32650),time_start=at(2),time_end=at(4)))
    assert batches[0]['timestamp'] == (2.,3.)
    assert batches[0]['prev_point_id'] == (None,12)
    assert batches[0]['next_point_id'] == (13,None)
