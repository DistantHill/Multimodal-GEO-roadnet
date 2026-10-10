import numpy as np
from track.v2.models import SourceBatch, RefineConfig, SpatialContext
from track.v2.trajectories.refine_trajectories import refine_vehicle


def source(times, speeds, headings=None):
    n = len(times)
    return SourceBatch('fixture', 'original', np.column_stack((np.full(n, 120.), np.linspace(30, 30.001, n))),
                       np.array(times, dtype=np.float64), np.array(headings or [0] * n, dtype=np.float64),
                       np.array(speeds, dtype=np.float64), np.arange(n, dtype=np.int64),
                       np.ones(n, dtype=bool), np.full(n, '', dtype=object))


def test_gap_and_stationary_columns_preserve_vehicle_and_source_keys():
    result = refine_vehicle(source([0, 1, 30, 31, 100, 101], [0] * 6),
                            spatial_context=SpatialContext(4326, 32651, {"coordinate_semantics_version": 2, "enforce_area_of_use": False, "max_scale_error": 0.1}),
                            config=RefineConfig(100, 45, 135, 50, 1, 20, 10, 1))
    assert [t.vehicle_id for t in result.subtracks] == ['original', 'original']
    assert [t.subtrack_index for t in result.subtracks] == [1, 2]
    assert result.excluded.source_row_key.tolist() == [1, 2]
    assert result.subtracks[0].source_row_key.tolist() == [0, 3]
    assert result.subtracks[0].source_row_key_valid.all()
    for track in result.subtracks:
        assert track.xy.shape == (len(track), 2)
        assert track.xy.dtype == np.float64
        assert not track.xy.flags.writeable
        assert not track.lonlat.flags.writeable


def test_zero_and_negative_source_keys_are_valid():
    batch = source([0, 1], [1, 1])
    batch.source_row_key.flags.writeable = True
    batch.source_row_key[:] = [0, -1]
    batch.source_row_key.flags.writeable = False
    result = refine_vehicle(batch, spatial_context=SpatialContext(4326, 32651, {"coordinate_semantics_version": 2, "enforce_area_of_use": False, "max_scale_error": 0.1}),
                            config=RefineConfig(100, 45, 135, 50, 1, 20, 10, 1))
    assert result.subtracks[0].source_row_key.tolist() == [0, -1]
    assert result.subtracks[0].source_row_key_valid.tolist() == [True, True]


def test_extra_column_preserves_nested_json_values_as_one_dimensional():
    from track.v2.trajectories.read_source import _extra_column
    values = [[1, 2], [3, 4]]
    column = _extra_column(values)
    assert column.shape == (2,)
    assert column.tolist() == values
