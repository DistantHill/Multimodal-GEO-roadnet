"""Strict spatial selection preserves source identity and rejects gap bridges."""
from types import SimpleNamespace
import pytest
from track.v2.trajectories.results.read_parquet import RefineDatasetReader, TopologyTrackSelection
from track.v2.projection import SpatialWindow
from .config_factory import runtime_config, intersect_config
from .test_refine_parquet_reader import _published_tracks
from .test_refine_time_selection import at


def test_bbox_turn_windows_and_topology_do_not_bridge_reentry(tmp_path):
    # Two three-point inside runs, separated by an outside point in one subtrack.
    points = [(0,0,0,0),(1,1,0,90),(2,1,1,0),(3,20,20,90),
              (4,2,0,0),(5,3,0,90),(6,3,1,0)]
    descriptor = _published_tracks(tmp_path, [('v',1,points)])
    bbox=(120.,30.,120.000005,30.000005)
    cfg=intersect_config(turn_window=1,min_turn_deg=1,max_turn_deg=180,
                         min_leg_m=0,min_movement_speed_mps=0)
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config(),bbox=bbox) as reader:
        batches=list(reader.iter_turn_candidates('source',cfg))
        assert [i for b in batches for i in b.point_index] == [1,5]
        assert [i for b in batches for i in b.refine_point_id] == [2,6]
        assert reader.extent() == (0,0,3,1)
        selected=list(reader.iter_topology_tracks('source',[SpatialWindow(-1,-1,30,30)]))
        assert [s.point_offset for s in selected] == [0,4]
        assert [s.points.refine_point_id.tolist() for s in selected] == [[1,2,3],[5,6,7]]
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config(),bbox=bbox,
                             time_start=at(4),time_end=at(7)) as reader:
        assert [i for b in reader.iter_turn_candidates('source',cfg) for i in b.point_index] == [1]
    with RefineDatasetReader(tmp_path,descriptor,'present',runtime_config(),bbox=(0,0,1,1)) as reader:
        assert reader.extent() is None
        assert list(reader.iter_turn_candidates('source',cfg)) == []
        assert list(reader.iter_topology_tracks('source',[SpatialWindow(-1,-1,30,30)])) == []


def test_topology_crossings_retain_parent_sequence_offset():
    from .test_detect_topology_regressions import cluster,cfg
    from .detect_columns_helpers import subtrack,TRANSFORMER
    from track.v2.intersections.extract_topology import _build_topologies_from_reader
    track=subtrack([[-250.,0.],[0.,0.],[250.,0.]],subtrack_index=123)
    reader=SimpleNamespace(iter_topology_tracks=lambda *a,**kw:iter([TopologyTrackSelection((0,),'123',track,7)]))
    _,annotated,obs=_build_topologies_from_reader([cluster()],cfg(),TRANSFORMER,refine_reader=reader)
    assert annotated[0].crossing_points.point_index.tolist() == [7,8]
    assert annotated[0].crossing_points.refine_point_id.tolist() == track.refine_point_id[:2].tolist()
    assert len(obs[0]) == 1


@pytest.mark.parametrize('bbox',[(0,0,0,1),(0,0,181,1),(0,float('nan'),1,2),(False,0,1,2)])
def test_invalid_bbox_rejected(bbox):
    from track.v2.trajectories.results.read_parquet import validate_bbox
    with pytest.raises(ValueError):validate_bbox(bbox)
