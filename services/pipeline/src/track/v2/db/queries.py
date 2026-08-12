"""SQL query templates for DB-mode intersection detection.

ZH: DB 模式交叉口检测的 SQL 查询模板与辅助函数。
    所有 SQL 字符串集中管理，业务代码（intersect2.py）通过模块函数获取，
    不再硬编码查询字符串。

SQL convention
  - Table names are interpolated via f-string (validated by callers before use).
  - All value placeholders use ``%s`` (Psycopg parameter binding), never f-string.
  - ``_TP_UNIFIED_COLS`` and ``_IP_UNIFIED_COLS`` produce a unified 11-column
    result so consumers share a single ``row_to_dict()`` mapping.
"""

from __future__ import annotations

from typing import Any


# ═══════════════════════════════════════════════════════════════════════
# Column select fragments
# ═══════════════════════════════════════════════════════════════════════
# trajectory_points and injected_points have different column names;
# these fragments alias them into a shared 11-column result:
#   (vehicle_id, timestamp, lon, lat, heading, speed,
#    source_id, source_row_key, extra, from_source_row_key,
#    to_source_row_key, generated_reason)

_TP_UNIFIED_COLS = (
    "vehicle_id, timestamp, lon, lat, heading, speed, "
    "%s::text AS source_id, id::text AS source_row_key, extra, "
    "NULL::text AS from_source_row_key, NULL::text AS to_source_row_key, "
    "''::text AS generated_reason"
)

_IP_UNIFIED_COLS = (
    "vehicle_id, timestamp, lon, lat, heading, speed, "
    "from_source_id AS source_id, "
    "from_source_row_key AS source_row_key, "
    "'{}'::jsonb AS extra, "
    "from_source_row_key, to_source_row_key, generated_reason"
)


# ═══════════════════════════════════════════════════════════════════════
# Row mapping helper
# ═══════════════════════════════════════════════════════════════════════

def row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a DB cursor row (unified 11-column format) to a dict.

    ZH: 将统一 11 列格式的 DB 游标行转为 dict。
        替代 intersct2.py 中 3 处重复的 ``rows.append({...})``。
    """
    return {
        "vehicle_id": row[0],
        "timestamp": float(row[1]),
        "lon": float(row[2]),
        "lat": float(row[3]),
        "heading": float(row[4]),
        "speed": float(row[5]),
        "source_id": row[6],
        "source_row_key": row[7],
        "extra": row[8] if isinstance(row[8], dict) else {},
        "from_source_row_key": row[9],
        "to_source_row_key": row[10],
        "generated_reason": row[11],
    }


# ═══════════════════════════════════════════════════════════════════════
# Query builders
# ═══════════════════════════════════════════════════════════════════════

def build_spatial_extent_sql(table: str = "trajectory_points") -> str:
    """MIN/MAX lon/lat of active points.

    ZH: 查询 active points 的空间范围 (lon_min, lat_min, lon_max, lat_max)。
        使用 ``is_active = TRUE`` 以匹配 partial index ``idx_tp_active``。
    """
    return (
        f"SELECT MIN(lon), MAX(lon), MIN(lat), MAX(lat) "
        f"FROM {table} WHERE is_active = TRUE"
    )


def build_active_in_window_sql(
    table: str = "trajectory_points",
    *,
    has_run_id: bool = False,
) -> str:
    """UNION ALL query for active + injected points inside a spatial bbox.

    ZH: 空间窗口内的 active + injected points UNION ALL 查询。
        使用 ``geom && ST_MakeEnvelope()`` 走 GiST R-tree 2D 空间索引。

    Parameter placeholders (in order, caller must bind):
        1.  %s::text                         — source_id literal
        2-5. ST_MakeEnvelope (traj. bbox)    — x_min, y_min, x_max, y_max
        6-9. ST_MakeEnvelope (inj. bbox)     — x_min, y_min, x_max, y_max
        10.  run_id (optional, only when ``has_run_id=True``)
    """
    injected_run_clause = " AND run_id = %s" if has_run_id else ""
    return (
        f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
        f"       source_id, source_row_key, extra, "
        f"       NULL::text AS from_source_row_key, NULL::text AS to_source_row_key, "
        f"       ''::text AS generated_reason "
        f"FROM ("
        f"  SELECT {_TP_UNIFIED_COLS} "
        f"  FROM {table} "
        f"  WHERE is_active = TRUE "
        f"    AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326) "
        f"  UNION ALL "
        f"  SELECT {_IP_UNIFIED_COLS} "
        f"  FROM injected_points "
        f"  WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326){injected_run_clause} "
        f") AS combined "
        f"ORDER BY vehicle_id, timestamp"
    )


def build_stream_all_sql(
    table: str = "trajectory_points",
    *,
    has_run_id: bool = False,
) -> str:
    """UNION ALL query to stream all active + injected points (no spatial filter).

    ZH: 全量 active + injected points UNION ALL 查询（无空间过滤）。
        用于 server-side named cursor 逐车流式读取。
        使用 ``is_active = TRUE`` 以匹配 partial index ``idx_tp_active``。

    Parameter placeholders:
        1.  %s::text          — source_id literal
        2.  run_id (optional) — only when ``has_run_id=True``
    """
    injected_run_clause = " WHERE run_id = %s" if has_run_id else ""
    return (
        f"SELECT {_TP_UNIFIED_COLS} "
        f"FROM {table} "
        f"WHERE is_active = TRUE "
        f"UNION ALL "
        f"SELECT {_IP_UNIFIED_COLS} "
        f"FROM injected_points{injected_run_clause} "
        f"ORDER BY vehicle_id, timestamp"
    )
