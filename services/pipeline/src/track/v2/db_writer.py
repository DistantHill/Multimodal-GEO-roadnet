"""Database writer for track.v2 pipeline stages.

ZH: 将流水线各阶段产物写入 PostgreSQL 数据库。每个函数对应一张表，
    严格遵守 FK 依赖顺序。调用方负责事务管理。
EN: Write each pipeline stage's output to PostgreSQL. One function per table,
    strictly following FK dependency order. Caller manages transactions.

Usage / 用法::

    from track.v2.db_writer import (
        insert_trajectory_points,
        insert_excluded_points,
        insert_kept_trajectory_points,
        insert_injected_points,
        insert_turn_events,
        insert_candidate_intersections,
        insert_candidate_intersection_events,
        insert_candidate_entry_exit_points,
        insert_intersections,
        insert_entry_exit_points,
    )
"""

from __future__ import annotations

import json
from typing import Any

from .models import (
    EntryExitObservation,
    ExcludedIndex,
    ExcludedPoint,
    HotspotCluster,
    IntersectionTopology,
    PointRef,
    TrajectoryPoint,
    TurnEvent,
)

# ── Batch size for multi-row INSERT ───────────────────────────────────
_BATCH = 500


# ── Helpers ────────────────────────────────────────────────────────────

_POLYGON_EXPR = (
    "ST_Buffer("
    "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
    "%s"
    ")::geometry"
)


def _chunked_insert_returning(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    returning_cols: list[str],
    *,
    on_conflict: str = "",
) -> list[tuple[Any, ...]]:
    """Multi-row INSERT with RETURNING, processed in batches.

    ZH: 分批执行多行 INSERT ... RETURNING，返回所有结果行。
        可选 ``on_conflict`` 子句（如 ``"ON CONFLICT DO NOTHING"``）。
    EN: Execute multi-row INSERT ... RETURNING in batches, return all result rows.
        Optional ``on_conflict`` clause (e.g. ``"ON CONFLICT DO NOTHING"``).
    """
    if not rows:
        return []
    col_list = ", ".join(columns)
    ret_list = ", ".join(returning_cols)
    all_returned: list[tuple[Any, ...]] = []
    for start in range(0, len(rows), _BATCH):
        batch = rows[start : start + _BATCH]
        # psycopg 3: build parameterized multi-row VALUES instead of mogrify
        row_placeholders: list[str] = []
        params: list[Any] = []
        for row in batch:
            row_placeholders.append(f"({', '.join('%s' for _ in row)})")
            params.extend(row)
        sql = (
            f"INSERT INTO {table} ({col_list}) "
            f"VALUES {', '.join(row_placeholders)} "
            f"{on_conflict} "
            f"RETURNING {ret_list}"
        )
        cur.execute(sql, params)
        all_returned.extend(cur.fetchall())
    return all_returned


def _chunked_insert(cur: Any, table: str, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    """Multi-row INSERT without RETURNING, processed in batches."""
    if not rows:
        return
    col_list = ", ".join(columns)
    placeholders = ", ".join(f"%({i})s" if isinstance(row, dict) else "%s" for i, row in enumerate(rows[:1]))
    for start in range(0, len(rows), _BATCH):
        batch = rows[start : start + _BATCH]
        # Build multi-row VALUES
        batch_strs = []
        params: list[Any] = []
        for row in batch:
            batch_strs.append(f"({', '.join('%s' for _ in row)})")
            params.extend(row)
        sql = f"INSERT INTO {table} ({col_list}) VALUES {', '.join(batch_strs)}"
        cur.execute(sql, params)


# ── Parser 0 ───────────────────────────────────────────────────────────

def insert_trajectory_points(
    conn: Any,
    points: list[TrajectoryPoint],
    *,
    table: str = "trajectory_points",
) -> dict[tuple[str, str], int]:
    """Insert all trajectory points and return a ``{(source_id, source_row_key): db_id}`` map.

    ZH: 批量插入轨迹点，返回 PointRef → DB ID 映射供后续阶段使用。
        ``table`` 指定目标表名，默认 ``trajectory_points``。
    """
    if not points:
        return {}

    columns = [
        "source_id", "source_row_key", "stable",
        "vehicle_id", "timestamp", "lon", "lat",
        "heading", "speed", "extra",
    ]
    rows: list[tuple[Any, ...]] = []
    for p in points:
        rows.append((
            p.point_ref.source_id,
            p.point_ref.source_row_key,
            p.point_ref.stable,
            p.vehicle_id,
            p.timestamp,
            p.lon,
            p.lat,
            p.heading,
            p.speed,
            json.dumps(p.extra, ensure_ascii=False),
        ))

    mapping: dict[tuple[str, str], int] = {}
    with conn.cursor() as cur:
        returned = _chunked_insert_returning(
            cur, table, columns, rows, ["id", "source_id", "source_row_key"],
            on_conflict="ON CONFLICT (source_id, source_row_key) DO NOTHING",
        )
    for db_id, sid, srk in returned:
        mapping[(sid, srk)] = db_id
    return mapping


def mark_excluded_points(
    conn: Any,
    excluded: list,
    *,
    table: str = "trajectory_points",
) -> int:
    """Set ``is_active = FALSE`` for points removed by Cleaner 1.

    ZH: 批量 UPDATE trajectory_points，将被清洗排除的点的 is_active 标记为 FALSE。
        返回标记行数。
    """
    if not excluded:
        return 0

    keys = [(item.point_ref.source_id, item.point_ref.source_row_key) for item in excluded]
    total = 0
    with conn.cursor() as cur:
        for start in range(0, len(keys), _BATCH):
            batch = keys[start : start + _BATCH]
            values_clause = ", ".join("(%s, %s)" for _ in batch)
            params: list[Any] = []
            for sid, srk in batch:
                params.extend([sid, srk])
            cur.execute(
                f"UPDATE {table} SET is_active = FALSE "
                f"FROM (VALUES {values_clause}) AS t(sid, srk) "
                f"WHERE {table}.source_id = t.sid AND {table}.source_row_key = t.srk",
                params,
            )
            total += cur.rowcount
    return total


def fetch_trajectory_point_ids(
    conn: Any,
    source_id: str,
    *,
    table: str = "trajectory_points",
) -> dict[tuple[str, str], int]:
    """Fetch existing (source_id, source_row_key) → id mapping from the DB.

    ZH: 从已有表中查询 (source_id, source_row_key) → id 映射。
        当数据源为 PostgreSQL 表时使用，避免重复插入已存在的数据。
    """
    mapping: dict[tuple[str, str], int] = {}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, source_id, source_row_key FROM {table} WHERE source_id = %s",
            (source_id,),
        )
        for db_id, sid, srk in cur:
            mapping[(sid, srk)] = db_id
    return mapping


# ── Cleaner 1 ──────────────────────────────────────────────────────────

def _point_ref_key(point_ref: PointRef) -> tuple[str, str]:
    return (point_ref.source_id, point_ref.source_row_key)


def insert_excluded_points(
    conn: Any,
    excluded: ExcludedIndex,
    tp_ref_to_id: dict[tuple[str, str], int] | None = None,
) -> None:
    """Insert points removed by Cleaner 1.

    ZH: 写入被清洗排除的点（含 lon/lat，geom 由 DB 自动生成）。
    """
    if not excluded.items:
        return

    columns = ["source_id", "source_row_key", "reason", "lon", "lat"]
    rows: list[tuple[Any, ...]] = []
    for item in excluded.items:
        rows.append((
            item.point_ref.source_id,
            item.point_ref.source_row_key,
            item.reason,
            item.lon,
            item.lat,
        ))

    with conn.cursor() as cur:
        _chunked_insert(cur, "excluded_points", columns, rows)


def insert_kept_trajectory_points(
    conn: Any,
    kept_subtracks: list[list[TrajectoryPoint]],
    tp_ref_to_id: dict[tuple[str, str], int],
) -> None:
    """Insert FK references for points that survived ``_exclude_stationary``.

    ZH: 写入保留点引用——每个子轨迹的保留点通过 FK 指向 trajectory_points。
        ``kept_subtracks`` 的每个元素是 Cleaner 1 的一个 subtrack 的保留点列表。
    """
    columns = ["trajectory_point_id", "vehicle_id", "subtrack_index", "sequence_in_subtrack"]
    rows: list[tuple[Any, ...]] = []
    for st_idx, subtrack in enumerate(kept_subtracks, start=1):
        for seq_idx, point in enumerate(subtrack):
            tp_id = tp_ref_to_id.get(_point_ref_key(point.point_ref))
            if tp_id is not None:
                rows.append((tp_id, point.vehicle_id, st_idx, seq_idx))

    if rows:
        with conn.cursor() as cur:
            _chunked_insert(cur, "kept_trajectory_points", columns, rows)


def insert_injected_points(
    conn: Any,
    active_points: list[TrajectoryPoint],
) -> None:
    """Insert synthetic points injected by ``_inject_orthogonal``.

    ZH: 从 active 列表中筛选 generated_reason="orthogonal_heading" 的点写入注入表。
    """
    injected = [p for p in active_points if p.point_ref.generated_reason == "orthogonal_heading"]
    if not injected:
        return

    columns = [
        "vehicle_id", "timestamp", "lon", "lat", "x", "y",
        "heading", "speed",
        "from_source_id", "from_source_row_key",
        "to_source_id", "to_source_row_key",
        "generated_reason", "origin_lon", "origin_lat",
    ]
    rows: list[tuple[Any, ...]] = []
    for p in injected:
        ref = p.point_ref
        # x/y are not stored directly on TrajectoryPoint — compute from first point as origin
        # The cleaner doesn't store x/y either, so we leave them as 0.0
        rows.append((
            p.vehicle_id,
            p.timestamp,
            p.lon,
            p.lat,
            0.0,  # x — computed on read
            0.0,  # y — computed on read
            p.heading,
            p.speed,
            ref.source_id,
            ref.from_ref or "",
            ref.source_id,
            ref.to_ref or "",
            ref.generated_reason,
            0.0,  # origin_lon — set on read
            0.0,  # origin_lat — set on read
        ))

    if rows:
        with conn.cursor() as cur:
            _chunked_insert(cur, "injected_points", columns, rows)


# ── Intersect2 ─────────────────────────────────────────────────────────

def insert_turn_events(
    conn: Any,
    turn_events: list[TurnEvent],
    origin_lon: float = 0.0,
    origin_lat: float = 0.0,
) -> dict[int, int]:
    """Insert turn events, return ``{event_id: db_id}`` mapping."""
    if not turn_events:
        return {}

    columns = [
        "event_id", "vehicle_id", "timestamp", "lon", "lat",
        "x", "y", "in_bearing", "out_bearing", "turn_deg", "score",
        "source", "tp_source_id", "tp_source_row_key",
        "origin_lon", "origin_lat",
    ]
    rows: list[tuple[Any, ...]] = []
    for te in turn_events:
        rows.append((
            te.event_id,
            te.vehicle_id,
            te.timestamp,
            te.lon,
            te.lat,
            te.x,
            te.y,
            te.in_bearing,
            te.out_bearing,
            te.turn_deg,
            te.score,
            te.source,
            te.point_ref.source_id if te.point_ref else None,
            te.point_ref.source_row_key if te.point_ref else None,
            origin_lon,
            origin_lat,
        ))

    mapping: dict[int, int] = {}
    with conn.cursor() as cur:
        returned = _chunked_insert_returning(
            cur, "turn_events", columns, rows, ["id", "event_id"],
            on_conflict="ON CONFLICT (vehicle_id, timestamp) DO NOTHING",
        )
    for db_id, event_id in returned:
        mapping[event_id] = db_id
    return mapping


def insert_candidate_intersections(
    conn: Any,
    clusters: list[HotspotCluster],
    origin_lon: float = 0.0,
    origin_lat: float = 0.0,
) -> dict[int, int]:
    """Insert candidate intersections with POLYGON geometry.

    ZH: 写入候选交叉口——POLYGON geom 在 SQL 中通过 ST_Buffer 计算。
        返回 {cluster_id: db_id} 映射。
    """
    if not clusters:
        return {}

    columns = [
        "cluster_id", "centroid_x", "centroid_y",
        "centroid_lon", "centroid_lat", "radius_m",
        "source", "sources", "source_counts", "source_ratios",
        "critical_sources",
        "turn_event_count", "orthogonal_count", "total_event_count",
        "origin_lon", "origin_lat",
        "geom",  # ← must be last: _POLYGON_EXPR is appended to end of placeholders
    ]
    mapping: dict[int, int] = {}
    with conn.cursor() as cur:
        for c in clusters:
            row = (
                c.cluster_id,
                c.centroid_x,
                c.centroid_y,
                c.centroid_lon,
                c.centroid_lat,
                c.radius_m,
                c.source,
                json.dumps(list(c.sources), ensure_ascii=False),
                json.dumps(c.source_counts, ensure_ascii=False),
                json.dumps(c.source_ratios, ensure_ascii=False),
                json.dumps(list(c.critical_sources), ensure_ascii=False),
                len([e for e in c.events if e.source == "turn"]),
                len([e for e in c.events if e.source == "orthogonal"]),
                len(c.events),
                origin_lon,
                origin_lat,
            )
            placeholders = ", ".join(["%s"] * len(columns))
            # Replace the last placeholder (geom) with ST_Buffer expression
            placeholders_parts = ["%s"] * len(columns)
            placeholders_parts[-1] = _POLYGON_EXPR  # geom is last column
            placeholders = ", ".join(placeholders_parts)
            params = row + (c.centroid_lon, c.centroid_lat, c.radius_m)
            sql = (
                f"INSERT INTO candidate_intersections ({', '.join(columns)}) "
                f"VALUES ({placeholders}) "
                f"RETURNING id, cluster_id"
            )
            cur.execute(sql, params)
            db_id, cluster_id = cur.fetchone()
            mapping[cluster_id] = db_id
    return mapping


def insert_candidate_intersection_events(
    conn: Any,
    clusters: list[HotspotCluster],
    ci_id_map: dict[int, int],
    te_id_map: dict[int, int],
) -> None:
    """Insert many-to-many links between candidate intersections and turn events."""
    rows: list[tuple[Any, ...]] = []
    for c in clusters:
        ci_db_id = ci_id_map.get(c.cluster_id)
        if ci_db_id is None:
            continue
        for event in c.events:
            te_db_id = te_id_map.get(event.event_id)
            if te_db_id is not None:
                rows.append((ci_db_id, te_db_id))

    if rows:
        with conn.cursor() as cur:
            _chunked_insert(cur, "candidate_intersection_events",
                            ["candidate_intersection_id", "turn_event_id"], rows)


def insert_candidate_entry_exit_points(
    conn: Any,
    clusters: list[HotspotCluster],
    ci_id_map: dict[int, int],
) -> dict[int, int]:
    """Insert crossing points, return ``{crossing_id: db_id}`` mapping."""
    columns = [
        "candidate_intersection_id", "cluster_id", "vehicle_id",
        "segment_index", "crossing_type",
        "crossing_x", "crossing_y", "crossing_lon", "crossing_lat",
        "branch_angle_deg", "origin_lon", "origin_lat",
    ]
    rows: list[tuple[Any, ...]] = []
    crossing_ids: list[int] = []  # parallel to rows
    for c in clusters:
        ci_db_id = ci_id_map.get(c.cluster_id)
        if ci_db_id is None:
            continue
        for cp in c.crossing_points:
            cp_any: Any = cp  # HotspotCluster.crossing_points typed as tuple[()]
            rows.append((
                ci_db_id,
                c.cluster_id,
                cp_any.vehicle_id,
                cp_any.segment_index,
                "entry" if cp_any.is_entry else "exit",
                getattr(cp_any, "crossing_x", 0.0),
                getattr(cp_any, "crossing_y", 0.0),
                cp_any.crossing_lon,
                cp_any.crossing_lat,
                getattr(cp_any, "branch_angle_deg", None),
                getattr(cp_any, "origin_lon", 0.0),
                getattr(cp_any, "origin_lat", 0.0),
            ))
            crossing_ids.append(cp_any.crossing_id)

    mapping: dict[int, int] = {}
    if rows:
        with conn.cursor() as cur:
            returned = _chunked_insert_returning(
                cur, "candidate_entry_exit_points", columns, rows, ["id"],
            )
        for (db_id,), crossing_id in zip(returned, crossing_ids):
            mapping[crossing_id] = db_id
    return mapping


def insert_intersections(
    conn: Any,
    topologies: list[IntersectionTopology],
    ci_id_map: dict[int, int],
    clusters_by_id: dict[int, HotspotCluster],
) -> dict[int, int]:
    """Insert verified intersections with POLYGON geometry.

    ZH: 写入验证通过的交叉口。POLYGON geom 从关联的 candidate_intersection
        的 centroid + radius 派生（方案A：应用层计算）。
        返回 {cluster_id: db_id} 映射。
    """
    if not topologies:
        return {}

    columns = [
        "candidate_intersection_id", "cluster_id",
        "branch_centers_deg", "transition_counts",
        "observation_count", "is_complete", "reason",
        "geom",
    ]
    mapping: dict[int, int] = {}
    with conn.cursor() as cur:
        for topo in topologies:
            ci_db_id = ci_id_map.get(topo.cluster_id)
            if ci_db_id is None:
                continue
            cluster = clusters_by_id.get(topo.cluster_id)
            row = (
                ci_db_id,
                topo.cluster_id,
                json.dumps(topo.branch_centers_deg, ensure_ascii=False),
                json.dumps({f"{k[0]},{k[1]}": v for k, v in topo.transition_counts.items()},
                          ensure_ascii=False),
                topo.observation_count,
                topo.is_complete,
                topo.reason,
            )
            # Build geom via ST_Buffer from candidate cluster's centroid + radius
            if cluster is not None:
                placeholders = ", ".join("%s" for _ in row)
                placeholders += f", {_POLYGON_EXPR}"
                sql = (
                    f"INSERT INTO intersections ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) "
                    f"RETURNING id, cluster_id"
                )
                cur.execute(sql, row + (cluster.centroid_lon, cluster.centroid_lat, cluster.radius_m))
            else:
                placeholders = ", ".join("%s" for _ in row)
                placeholders += ", NULL"
                sql = (
                    f"INSERT INTO intersections ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) "
                    f"RETURNING id, cluster_id"
                )
                cur.execute(sql, row)
            db_id, cid = cur.fetchone()
            mapping[cid] = db_id
    return mapping


def insert_entry_exit_points(
    conn: Any,
    observations: list[EntryExitObservation],
    int_id_map: dict[int, int],
) -> None:
    """Insert entry/exit observations.

    ZH: 写入每辆车通过每个交叉口的出入观测。entry_crossing_id/exit_crossing_id
        当前置 NULL（模型尚未携带这些引用）。
    """
    if not observations:
        return

    columns = [
        "intersection_id", "cluster_id", "vehicle_id",
        "entry_branch_angle", "exit_branch_angle",
        "inbound_bearing", "outbound_bearing", "turn_deg",
    ]
    rows: list[tuple[Any, ...]] = []
    for obs in observations:
        int_db_id = int_id_map.get(obs.cluster_id)
        if int_db_id is None:
            continue
        rows.append((
            int_db_id,
            obs.cluster_id,
            obs.vehicle_id,
            obs.entry_branch_angle,
            obs.exit_branch_angle,
            obs.inbound_bearing,
            obs.outbound_bearing,
            obs.turn_deg,
        ))

    if rows:
        with conn.cursor() as cur:
            _chunked_insert(cur, "entry_exit_points", columns, rows)
