"""PostGIS text values for point snapshots written through COPY or INSERT."""
from __future__ import annotations

from collections.abc import Sequence


def point_ewkt(lonlat: Sequence[float]) -> str:
    return f"SRID=4326;POINT({float(lonlat[0])} {float(lonlat[1])})"


def entry_exit_ewkt(entry: Sequence[float], exit: Sequence[float]) -> str:
    return (
        f"SRID=4326;MULTIPOINT(({float(entry[0])} {float(entry[1])}),"
        f"({float(exit[0])} {float(exit[1])}))"
    )
