"""Compact index storage for the existing exact DBSCAN expansion order."""
from __future__ import annotations

from array import array
from collections.abc import Iterator, Mapping, Sequence

Cell = tuple[int, int]


def index_typecode(count: int) -> str:
    """Choose a signed index width supporting every index and -1/-2 sentinels."""
    if count < 0:
        raise ValueError("point count must be nonnegative")
    for code in ("i", "q"):
        maximum = (1 << (8 * array(code).itemsize - 1)) - 1
        if count - 1 <= maximum:
            return code
    raise OverflowError("point count exceeds signed 64-bit index capacity")


class ActiveGridLinks:
    """Each cell owns an ascending doubly linked list of unqueued point indices.

    No per-point Python integers, cell references or hash-set slots are retained.
    The immutable grid still serves core classification; these links only track
    the shrinking frontier, so dense cores do not rescan already queued points.
    """
    def __init__(self, grid: Mapping[Cell, Sequence[int]], count: int) -> None:
        self.typecode = index_typecode(count)
        self.previous = array(self.typecode, [-2]) * count
        self.following = array(self.typecode, [-2]) * count
        self.heads: dict[Cell, int] = {}
        for cell, indices in grid.items():
            if not indices:
                continue
            self.heads[cell] = indices[0]
            previous = -1
            for index in indices:
                self.previous[index] = previous
                if previous >= 0:
                    self.following[previous] = index
                previous = index
            self.following[previous] = -1

    def remove(self, cell: Cell, index: int) -> None:
        previous = self.previous[index]
        following = self.following[index]
        if previous == -2:
            raise KeyError(index)
        if previous < 0:
            if self.heads.get(cell, -1) != index:
                raise KeyError((cell, index))
            self.heads[cell] = following
        else:
            self.following[previous] = following
        if following >= 0:
            self.previous[following] = previous
        self.previous[index] = self.following[index] = -2

    def iter_cell(self, cell: Cell) -> Iterator[int]:
        index = self.heads.get(cell, -1)
        while index >= 0:
            # The caller may unlink the yielded node before this generator
            # resumes; capture its successor before handing it to the caller.
            following = self.following[index]
            yield index
            index = following
