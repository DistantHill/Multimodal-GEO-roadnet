from unittest.mock import MagicMock, patch

from track.v2.detect import _run_columnar_db_detect

from .config_factory import intersect_run_config
from .test_detect import _TEST_SPATIAL_CONTEXT


def test_db_detect_uses_grid_task_and_keeps_turn_reader():
    reader = MagicMock()
    extent = (0.0, 0.0, 10.0, 10.0)
    reader.extent.return_value = extent
    reader.iter_turn_candidates.return_value = iter(())
    reader.track_count.return_value = 0
    cfg = intersect_run_config(
        refine_id="11111111-1111-1111-1111-111111111111",
        source_table="public.source_points",
        cross_track_task_grid_m=2500.0,
        cross_track_task_threads=2,
    )
    connector = lambda: object()
    main_conn = object()
    with (
        patch("track.v2.detect.run_grid_cross_track_task", return_value=[]) as grid_task,
        patch("track.v2.detect.extract_columnar_cross_track_candidates") as old_pairs,
        patch("track.v2.detect._cluster_pipeline", return_value=([], [])),
        patch("track.v2.detect._build_topologies_from_tracks", return_value=([], [], [])),
    ):
        result = _run_columnar_db_detect(
            cfg, _TEST_SPATIAL_CONTEXT, reader,
            conn=main_conn, refine_id=cfg.refine_id,
            connect_worker=connector,
        )

    assert result.cross_track_candidates == []
    reader.iter_turn_candidates.assert_called_once()
    old_pairs.assert_not_called()
    assert grid_task.call_args.args[0] == cfg.refine_id
    assert grid_task.call_args.args[1] == extent
    assert grid_task.call_args.kwargs == {
        "connect_worker": connector,
        "grid_cell_size_m": 2500.0,
        "thread_count": 2,
        "max_segments": 200_000,
        "max_segment_bytes": 268_435_456,
        "max_grid_memberships": 2_000_000,
        "max_overflow_pair_space": 20_000_000,
        "max_candidates": 500_000,
        "max_result_bytes": 536_870_912,
        "candidate_store": None,
        "live_status": cfg.live_status,
    }
