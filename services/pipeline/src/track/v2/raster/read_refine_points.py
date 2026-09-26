"""Read exactly the Detect kept-plus-injected input for one Refine Run."""

from __future__ import annotations

import uuid
from typing import Iterator

from ..trajectories.query_refine_points import build_stream_all_sql, fetch_refine_run_row
from ..db.source_table import validate_source_table
from ..models import SpatialContext


def load_refine_spatial_context(
    conn: object, refine_id: str,
) -> tuple[str, SpatialContext, dict[str, object] | None]:
    row = fetch_refine_run_row(conn, refine_id)
    if row.status != "completed":
        raise ValueError(f"Refine Run must be completed: {row.status}")
    return row.source_table, row.spatial_context, row.source_fingerprint


def iter_refined_points(conn: object, *, refine_id: str, source_table: str,
                        spatial_context: SpatialContext,
                        batch_size: int) -> Iterator[list[tuple[float, float]]]:
    contract = validate_source_table(conn, source_table)
    query = build_stream_all_sql(
        contract.quoted_table, has_refine_id=True,
        kept_table="kept_trajectory_points", spatial_context=spatial_context,
        source_key_sql_type=contract.key_sql_type,
        source_has_extra=contract.has_extra,
    )
    # Same source label and two Refine Run bindings as Detect's stream_tracks.
    params = (source_table, refine_id, "public.injected_points", refine_id)
    with conn.cursor(name=f"raster_points_{uuid.uuid4().hex}") as cur:
        cur.execute(query, params)
        while rows := cur.fetchmany(batch_size):
            yield [(float(row[10]), float(row[11])) for row in rows]
