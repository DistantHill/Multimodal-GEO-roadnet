from unittest.mock import MagicMock,patch
from .config_factory import runtime_config
from .config_factory import intersect_run_config
from .detect_columns_helpers import CONTEXT
from track.v2.intersections.detect_intersections import _run_columnar_db_detect
from track.v2.candidates.detect_cross_track_pairs import empty_cross_batch
from track.v2.candidates.detect_turn_events import empty_turn_batch


def test_db_detect_uses_grid_task_and_turn_reader_with_explicit_runtime_bounds(tmp_path):
    reader=MagicMock(); reader.extent.return_value=(0.,0.,10.,10.); reader.iter_turn_candidates.return_value=iter(()); reader.track_count.return_value=0
    cfg=intersect_run_config(refine_id="run",source_table="trajectory_points",cross_track_task_grid_m=2500.,cross_track_task_threads=2)
    connector=lambda:object()
    with patch("track.v2.intersections.detect_intersections.run_grid_cross_track_task",return_value=empty_cross_batch()) as grid,patch("track.v2.intersections.detect_intersections._cluster_pipeline",return_value=(empty_turn_batch(),[])),patch("track.v2.intersections.detect_intersections._build_topologies_from_reader",return_value=([],[],[])):
        result=_run_columnar_db_detect(cfg,CONTEXT,reader,conn=object(),refine_id=cfg.refine_id,connect_worker=connector,runtime=runtime_config(),data_root=tmp_path)
    assert len(result.cross_track_candidates)==0
    reader.iter_turn_candidates.assert_called_once()
    assert grid.call_args.args[:2]==("run",(0.,0.,10.,10.))
    assert grid.call_args.kwargs["thread_count"]==2
    assert grid.call_args.kwargs["max_segments"]==200_000
