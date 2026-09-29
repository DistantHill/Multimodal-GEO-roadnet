"""Run identity is shared by Refine persistence and all Detect transports."""
from unittest.mock import Mock, patch
import numpy as np
import pytest
from .config_factory import intersect_config, runtime_config
from .detect_columns_helpers import CONTEXT, TRANSFORMER, subtrack
from .test_refine_parquet_reader import _published_tracks
from .test_grid_segments import _batch, _row
from track.v2.db.pipeline_store import PostgresPipelineStore
from track.v2.trajectories.results.read_parquet import RefineDatasetReader
from track.v2.candidates.grid_segments import build_grid_segments
from track.v2.candidates.detect_grid_crossings import iter_grid_crossings, iter_grid_candidates
from track.v2.candidates.detect_cross_track_pairs import concatenate_cross_batches
from track.v2.candidates.collect_candidates import extract_cross_track_candidates, extract_columnar_cross_track_candidates


def _grid_columns(tracks):
    rows=[]
    next_id=0
    for track in tracks:
        points=[]
        for i in range(len(track)):
            points.append(dict(point_id=next_id,point_kind=0,refined_sequence_in_subtrack=i,
                               source_id=track.source_id,source_row_key=int(track.source_row_key[i]),generated_reason=None,
                               timestamp=float(track.timestamp[i]),lon=float(track.lonlat[i,0]),lat=float(track.lonlat[i,1]),
                               heading=float(track.heading[i]),speed=float(track.speed[i]),x=float(track.xy[i,0]),y=float(track.xy[i,1])))
            next_id+=1
        rows.extend(_row(track.vehicle_id,point,next_=points[i+1],subtrack=track.subtrack_index)
                    for i,point in enumerate(points[:-1]))
    return build_grid_segments([_batch(*rows)],max_segments=100,max_bytes=1_000_000)


@pytest.mark.parametrize("same_subtrack",[False,True])
def test_grid_parquet_and_eager_use_identical_run_partition_rule(tmp_path,same_subtrack):
    if same_subtrack:
        tracks=[subtrack([[-20.,0.],[20.,0.],[0.,-20.],[0.,20.]],subtrack_index=212,headings=[90.,90.,0.,0.])]
    else:
        tracks=[subtrack([[-20.,0.],[20.,0.]],subtrack_index=212,headings=[90.,90.]),
                subtrack([[0.,-20.],[0.,20.]],subtrack_index=37,headings=[0.,0.])]
    cfg=intersect_config()
    eager=extract_cross_track_candidates({track.subtrack_index:track for track in tracks},TRANSFORMER,cfg)[0]
    segments=_grid_columns(tracks)
    grid=concatenate_cross_batches(list(iter_grid_candidates(segments,iter_grid_crossings(segments,cfg,max_grid_memberships=10000,max_overflow_pair_space=10000),TRANSFORMER)))
    published=_published_tracks(tmp_path,[(track.vehicle_id,track.subtrack_index,list(zip(track.timestamp,track.xy[:,0],track.xy[:,1],track.heading))) for track in tracks])
    with RefineDatasetReader(tmp_path,published,"present",runtime_config()) as reader:
        parquet=extract_columnar_cross_track_candidates(reader,"source",TRANSFORMER,cfg)
    for result in (eager,grid,parquet):
        if same_subtrack:
            assert len(result)==0
        else:
            assert result.from_subtrack_index.tolist()==[37]
            assert result.to_subtrack_index.tolist()==[212]
            assert result.from_vehicle_id.tolist()==result.to_vehicle_id.tolist()==["original_vehicle"]
            np.testing.assert_array_equal(result.xy,[[0.,0.]])


def test_refine_run_renumbering_is_detects_authoritative_partition(tmp_path):
    pending=[subtrack([[-20.,0.],[20.,0.]],subtrack_index=1,headings=[90.,90.]),
             subtrack([[0.,-20.],[0.,20.]],subtrack_index=1,headings=[0.,0.])]
    run=Mock(status="running",spatial_context=CONTEXT)
    with patch("track.v2.db.pipeline_store.fetch_refine_run",return_value=run),patch("track.v2.db.pipeline_store.insert_kept_trajectory_points",return_value=4) as kept,patch("track.v2.db.pipeline_store.insert_injected_points"):
        result=PostgresPipelineStore(object()).persist_refine_batch("refine",[],pending,41,spatial_context=CONTEXT,batch_size=2)
    assigned=kept.call_args.args[1]
    assert result.next_subtrack_index==43 and pending==[]
    assert [track.subtrack_index for track in assigned]==[41,42]
    assert [track.vehicle_id for track in assigned]==["original_vehicle","original_vehicle"]
    published=_published_tracks(tmp_path,[(track.vehicle_id,track.subtrack_index,list(zip(track.timestamp,track.xy[:,0],track.xy[:,1],track.heading))) for track in assigned])
    with RefineDatasetReader(tmp_path,published,"present",runtime_config()) as reader:
        loaded=list(reader.iter_tracks("source",batch_size=1))
        assert [track.subtrack_index for track in loaded]==[41,42]
        candidates=extract_columnar_cross_track_candidates(reader,"source",TRANSFORMER,intersect_config())
    assert candidates.from_subtrack_index.tolist()==[41]
    assert candidates.to_subtrack_index.tolist()==[42]


@pytest.mark.parametrize("speeds,expected", [([0., 40.], 1), ([40., 0.], 1),
    ([20., 20.], 1), ([0., 39.], 0), ([39., 0.], 0), ([19.9, 20.], 0)])
def test_cross_track_endpoint_average_speed_is_shared_by_all_routes(tmp_path, speeds, expected):
    from dataclasses import replace
    from track.v2.candidates.grid_segments import GridSegmentRelation
    from track.v2.candidates.detect_grid_crossings import iter_spilled_grid_candidates
    from track.v2.candidates.cross_track_timing import CrossTrackTiming

    tracks = [replace(subtrack([[-20., 0.], [20., 0.]], subtrack_index=1,
                              headings=[90., 90.]), speed=np.asarray(speeds)),
              subtrack([[0., -20.], [0., 20.]], subtrack_index=2)]
    cfg = intersect_config()
    eager = extract_cross_track_candidates({t.subtrack_index: t for t in tracks}, TRANSFORMER, cfg)[0]
    segments = _grid_columns(tracks)
    assert segments['end_speed'].tolist() == [speeds[1], 20.]
    timing = CrossTrackTiming()
    crossings = iter_grid_crossings(segments, cfg, max_grid_memberships=10000,
                                    max_overflow_pair_space=10000, timing=timing)
    grid = concatenate_cross_batches(list(iter_grid_candidates(segments, crossings, TRANSFORMER)))
    assert timing.values['speed_rejected_segments'] == 1 - expected
    assert timing.values['pair_count'] == expected
    relation = GridSegmentRelation(runtime_config(), tmp_path)
    try:
        relation.append(segments)
        relation.finish()
        spilled = concatenate_cross_batches(list(iter_spilled_grid_candidates(
            relation, cfg, TRANSFORMER, block_size=1)))
    finally:
        relation.close()
    published = _published_tracks(tmp_path, [(t.vehicle_id, t.subtrack_index,
        list(zip(t.timestamp, t.xy[:, 0], t.xy[:, 1], t.heading))) for t in tracks],
        speeds=[*speeds, 20., 20.])
    with RefineDatasetReader(tmp_path, published, "present", runtime_config()) as reader:
        parquet = extract_columnar_cross_track_candidates(reader, "source", TRANSFORMER, cfg)
    for result in (eager, grid, spilled, parquet):
        assert len(result) == expected
        if expected:
            np.testing.assert_array_equal(result.xy, [[0., 0.]])
            assert result.from_subtrack_index.tolist() == [1]
            assert result.to_subtrack_index.tolist() == [2]
