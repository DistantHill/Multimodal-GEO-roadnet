"""Detect publication coordinates are read as complete columnar subtracks."""
from types import SimpleNamespace

import duckdb
import numpy as np

from track.v2.intersections.results.read_parquet import DetectDatasetReader


def test_reader_batch_one_preserves_xy_order_nullable_events_and_source_vehicle():
    reader = object.__new__(DetectDatasetReader)
    reader._runtime = SimpleNamespace(parquet_read_batch_size=1)
    reader._connection = duckdb.connect()
    try:
        reader._connection.execute("""
            CREATE TABLE resolved_points (
                resolved_point_id BIGINT, subtrack_index BIGINT,
                sequence_in_subtrack BIGINT, vehicle_id VARCHAR,
                lon DOUBLE, lat DOUBLE, x DOUBLE, y DOUBLE,
                crossing_id BIGINT, intersection_id BIGINT, crossing_type SMALLINT
            );
            INSERT INTO resolved_points VALUES
                (3,7,2,'original',3,4,30,40,NULL,NULL,NULL),
                (1,7,0,'original',1,2,10,20,NULL,NULL,NULL),
                (2,7,1,'original',2,3,20,30,0,0,1),
                (4,8,0,'original',4,5,40,50,NULL,NULL,NULL)
        """)
        tracks = tuple(reader.iter_tracks(batch_size=1))
        assert [track.subtrack_index for track in tracks] == [7, 8]
        assert [track.vehicle_id for track in tracks] == ["original", "original"]
        np.testing.assert_array_equal(tracks[0].resolved_point_ids, [1, 2, 3])
        np.testing.assert_array_equal(tracks[0].xy, [[10.,20.], [20.,30.], [30.,40.]])
        np.testing.assert_array_equal(tracks[0].lonlat, [[1.,2.], [2.,3.], [3.,4.]])
        np.testing.assert_array_equal(tracks[0].crossing_valid, [False, True, False])
        np.testing.assert_array_equal(tracks[0].intersection_valid, [False, True, False])
        assert tracks[0].crossing_ids[1] == tracks[0].intersection_ids[1] == 0
        assert tracks[0].xy.dtype == np.float64
        assert tracks[0].xy.flags.c_contiguous
        assert not tracks[0].xy.flags.writeable
        assert not tracks[0].crossing_ids.flags.writeable
        assert not hasattr(tracks[0], "points")
    finally:
        reader.__exit__()
