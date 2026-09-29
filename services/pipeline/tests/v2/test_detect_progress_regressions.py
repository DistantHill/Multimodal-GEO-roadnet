"""Columnar cursor lifetime and grid segment immutability."""
from unittest.mock import Mock, patch
import numpy as np
import pytest
from .config_factory import intersect_config
from .detect_columns_helpers import TRANSFORMER, subtrack
from .test_detect_identity_routes import _grid_columns
from track.v2.candidates import collect_candidates as collect
from track.v2.candidates.grid_segments import build_grid_segments


def _tracks():
    return {1:subtrack([[-20.,0.],[20.,0.]],subtrack_index=1,headings=[90.,90.]),
            2:subtrack([[0.,-20.],[0.,20.]],subtrack_index=2,headings=[0.,0.])}


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
