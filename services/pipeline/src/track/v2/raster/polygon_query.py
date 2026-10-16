"""Unsorted spatially filtered kept-plus-injected raster input."""
from __future__ import annotations

import uuid

from ..db.source_table import quote_table_name


def polygon_points_sql(source_table: str) -> str:
    table = quote_table_name(source_table)
    # Match the persisted UTM expression indexes before membership lookup.
    # The supplied geometry is the rectangular image window in processing CRS.
    return f"""SELECT ST_X(ST_Transform(s.geom, %(srid)s)) AS x,
                     ST_Y(ST_Transform(s.geom, %(srid)s)) AS y, 'kept' AS kind
        FROM {table} s
        JOIN kept_trajectory_points k ON k.source_row_key = s.id
        WHERE k.refine_id = %(refine_id)s
          AND ST_Transform(s.geom, %(srid)s) && ST_GeomFromWKB(%(polygon)s, %(srid)s)
          AND ST_Covers(ST_GeomFromWKB(%(polygon)s, %(srid)s), ST_Transform(s.geom, %(srid)s))
          AND s.timestamp >= %(start)s AND s.timestamp < %(end)s
        UNION ALL
        SELECT i.x, i.y, 'injected' AS kind
        FROM injected_points i
        WHERE i.refine_id = %(refine_id)s
          AND ST_Transform(i.geom, %(srid)s) && ST_GeomFromWKB(%(polygon)s, %(srid)s)
          AND ST_Covers(ST_GeomFromWKB(%(polygon)s, %(srid)s), ST_Transform(i.geom, %(srid)s))
          AND i.timestamp >= %(start)s AND i.timestamp < %(end)s"""


def iter_polygon_points(conn, query: str, params: dict, batch_size: int):
    with conn.cursor(name=f'polygon_raster_{uuid.uuid4().hex}') as cursor:
        cursor.execute(query, params)
        while rows := cursor.fetchmany(batch_size):
            yield rows
