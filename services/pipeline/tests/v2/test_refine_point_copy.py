"""Array publication preserves basic views without point-copy wrappers."""
import numpy as np
from track.v2.models import SourceBatch


def test_noncontiguous_geometry_view_is_published_without_copy():
    owner = np.arange(24, dtype=np.float64).reshape(12, 2)
    view = owner[::2]
    batch = SourceBatch("fixture", "v", view, np.arange(6, dtype=np.float64),
                        np.zeros(6), np.zeros(6), np.arange(6), np.ones(6, dtype=bool), np.full(6, ""))
    assert np.shares_memory(batch.lonlat, owner)
    assert not batch.lonlat.flags.c_contiguous
    assert not batch.lonlat.flags.writeable
    assert not hasattr(batch, "__dict__")
