import pytest

from track.v2.candidates.grid import create_grid


def test_create_grid_tiles_extent_with_shared_closed_edges():
    grids = create_grid((100.0, 200.0, 10100.0, 5700.0), 5000.0)

    assert len(grids) == 2
    assert [len(column) for column in grids] == [2, 2]
    assert grids[0][0].aabb == (100.0, 200.0, 5100.0, 5200.0)
    assert grids[1][0].aabb == (5100.0, 200.0, 10100.0, 5200.0)
    assert grids[0][1].aabb == (100.0, 5200.0, 5100.0, 5700.0)
    assert tuple(grids[1][1]) == (5100.0, 5200.0, 10100.0, 5700.0)
    assert all(cell.status == "todo" for column in grids for cell in column)
    grids[0][0].status = "done"
    assert grids[0][0].status == "done"
    assert grids[1][0].status == "todo"


def test_create_grid_empty_and_single_point_extent():
    assert create_grid(None, 5000.0) == []
    grids = create_grid((42.0, 24.0, 42.0, 24.0), 5000.0)
    assert len(grids) == 1 and len(grids[0]) == 1
    assert grids[0][0].aabb == (42.0, 24.0, 42.0, 24.0)


@pytest.mark.parametrize("size", [0.0, -1.0, float("inf"), float("nan")])
def test_create_grid_rejects_invalid_size(size):
    with pytest.raises(ValueError, match="cell_size_m"):
        create_grid((0.0, 0.0, 1.0, 1.0), size)
