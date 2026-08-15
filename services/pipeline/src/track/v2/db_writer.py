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

# ── 改动清单 / Change Log ──────────────────────────────────────────────
# #  改动                             效果
# 1  _BATCH 500 → 5000               所有 INSERT 批量写入往返次数减少 10 倍
# 2  insert_kept_trajectory_points   16M 行从 ~32,000 次 INSERT 往返变成
#    改用 COPY FROM STDIN             一次 COPY 流式写入，走 PG 原生批量协议
# 3  insert_injected_points          调用方 run.py 已按 orthogonal_heading
#    去掉重复过滤                     预过滤，函数内无需再筛
# 4  移除 excluded_points 写入       排除信息仅反映在 trajectory_points.is_active，
#                                   不再单独写 excluded_points 表（表结构保留）
# 5  insert_kept_trajectory_points   支持 start_subtrack_index 并返回下一序号，
#    分批流式写入                    供 run.py 逐批 COPY，不再整轮累积 kept 点
# ────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .models import (
    CrossTrackCandidate,
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
_BATCH = 5000


@dataclass(frozen=True, slots=True)
class CleanRunRecord:
    id: str
    source_id: str
    source_kind: str
    parameters: dict[str, Any]
    status: str


def create_clean_run(
    conn: Any,
    *,
    source_id: str,
    source_kind: str,
    parameters: dict[str, Any],
) -> str:
    """Create one append-only Clean Run and return its UUID."""
    clean_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO clean_runs (id, source_id, source_kind, parameters, status) "
            "VALUES (%s, %s, %s, %s, 'running')",
            (clean_id, source_id, source_kind, json.dumps(parameters, ensure_ascii=False)),
        )
    return clean_id


def create_detect_run(
    conn: Any,
    *,
    clean_id: str,
    parameters: dict[str, Any],
) -> str:
    """Create one Detect Run owned by a Clean Run."""
    detect_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO detect_runs (id, clean_id, parameters, status) "
            "VALUES (%s, %s, %s, 'running')",
            (detect_id, clean_id, json.dumps(parameters, ensure_ascii=False)),
        )
    return detect_id


def finish_clean_run(conn: Any, clean_id: str, *, status: str) -> None:
    """Finish a Clean Run in the caller's transaction."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE clean_runs SET status = %s, finished_at = NOW() WHERE id = %s",
            (status, clean_id),
        )


def finish_detect_run(conn: Any, detect_id: str, *, status: str) -> None:
    """Finish a Detect Run in the caller's transaction."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE detect_runs SET status = %s, finished_at = NOW() WHERE id = %s",
            (status, detect_id),
        )


def fetch_clean_run(conn: Any, clean_id: str) -> CleanRunRecord:
    """Load one Clean Run for explicit Detect reuse."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_id, source_kind, parameters, status "
            "FROM clean_runs WHERE id = %s",
            (clean_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Clean Run: {clean_id}")
    parameters = row[3] if isinstance(row[3], dict) else json.loads(row[3])
    if not isinstance(parameters, dict):
        raise ValueError("Clean Run parameters must be a JSON object")
    return CleanRunRecord(
        id=str(row[0]),
        source_id=str(row[1]),
        source_kind=str(row[2]),
        parameters=parameters,
        status=str(row[4]),
    )


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
    # ZH: PG 协议层参数上限 65535，按列数动态限制每批行数。
    #     _BATCH=5000 * 15列 = 75,000 > 65,535 → 需要缩批。
    # EN: PG wire protocol caps params at 65535; limit rows per batch by column count.
    _max_rows = max(1, 65535 // len(columns))
    _effective_batch = min(_BATCH, _max_rows)
    all_returned: list[tuple[Any, ...]] = []
    for start in range(0, len(rows), _effective_batch):
        batch = rows[start : start + _effective_batch]
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
    # ZH: PG 协议层参数上限 65535，按列数动态限制每批行数。
    # EN: PG wire protocol caps params at 65535; limit rows per batch by column count.
    _max_rows = max(1, 65535 // len(columns))
    _effective_batch = min(_BATCH, _max_rows)
    for start in range(0, len(rows), _effective_batch):
        batch = rows[start : start + _effective_batch]
        # Build multi-row VALUES
        batch_strs = []
        params: list[Any] = []
        for row in batch:
            batch_strs.append(f"({', '.join('%s' for _ in row)})")
            params.extend(row)
        sql = f"INSERT INTO {table} ({col_list}) VALUES {', '.join(batch_strs)}"
        cur.execute(sql, params)


def _with_stage_id(
    columns: list[str],
    rows: list[tuple[Any, ...]],
    column_name: str,
    stage_id: str | None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Attach the mandatory stage id to newly persisted derived records."""
    if stage_id is None:
        raise ValueError(f"{column_name} is required for derived v2 persistence")
    return [column_name, *columns], [(stage_id, *row) for row in rows]


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
        "vehicle_id", "timestamp", "lon", "lat",
        "heading", "speed", "extra",
    ]
    rows: list[tuple[Any, ...]] = []
    for p in points:
        rows.append((
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
            cur, table, columns, rows, ["id"],
        )
    for (db_id,), p in zip(returned, points):
        mapping[(p.point_ref.source_id, p.point_ref.source_row_key)] = db_id
    return mapping


def mark_excluded_points(
    conn: Any,
    excluded: list,
    *,
    table: str = "trajectory_points",
    tp_id_map: dict | None = None,
) -> int:
    """Set ``is_active = FALSE`` for points removed by Cleaner 1.

    ZH: 批量 UPDATE trajectory_points，将被清洗排除的点的 is_active 标记为 FALSE。
        返回标记行数。通过 ``tp_id_map`` 将 PointRef 翻译为 DB id；
        若无映射（原地场景），``source_row_key`` 本身就是 id。
    """
    if not excluded:
        print("  mark_excluded_points: 没有点被标记排除 / no points to mark")
        return 0

    ids: list[int] = []
    for item in excluded:
        key = (item.point_ref.source_id, item.point_ref.source_row_key)
        if tp_id_map and key in tp_id_map:
            ids.append(tp_id_map[key])
        else:
            ids.append(int(item.point_ref.source_row_key))
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET is_active = FALSE WHERE id = ANY(%s::bigint[])",
            (ids,),
        )
    return cur.rowcount


def reset_active_points(
    conn: Any,
    *,
    table: str = "trajectory_points",
) -> int:
    """Reset all raw points' is_active flag before deriving a new run.

    ZH: 将全表的 is_active 重置为 TRUE。
    EN: Reset is_active to TRUE for all rows in the table.

    Derived cleaner decisions are stored per ``clean_id``. The raw table keeps
    a convenience active flag for DB-mode processing, so it must be restored
    to the complete source before the next run is calculated.
    """
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {table} SET is_active = TRUE")
    return cur.rowcount


def fetch_trajectory_point_ids(
    conn: Any,
    source_id: str | None,
    *,
    table: str = "trajectory_points",
) -> dict[tuple[str, str], int]:
    """Fetch id → (source_id, source_row_key) mapping from the DB.

    ZH: 从已有表中查询 id，构造 (source_id, str(id)) → id 映射。
        ``source_id`` 参数用于构造映射 key，不用于 SQL 过滤。
    EN: Fetch ids from the table, build (source_id, str(id)) → id mapping.
        ``source_id`` is used only for constructing the map key, not for filtering.
    """
    mapping: dict[tuple[str, str], int] = {}
    src = source_id or ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT id FROM {table}")
        for (db_id,) in cur:
            mapping[(src, str(db_id))] = db_id
    return mapping


# ── Cleaner 1 ──────────────────────────────────────────────────────────

def _point_ref_key(point_ref: PointRef) -> tuple[str, str]:
    return (point_ref.source_id, point_ref.source_row_key)


def insert_excluded_points(
    conn: Any,
    excluded: ExcludedIndex,
    tp_ref_to_id: dict[tuple[str, str], int] | None = None,
    *,
    clean_id: str | None = None,
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

    columns, rows = _with_stage_id(columns, rows, "clean_id", clean_id)
    with conn.cursor() as cur:
        _chunked_insert(cur, "excluded_points", columns, rows)


def insert_kept_trajectory_points(
    conn: Any,
    kept_subtracks: list[list[TrajectoryPoint]],
    tp_ref_to_id: dict[tuple[str, str], int],
    *,
    clean_id: str | None = None,
    start_subtrack_index: int = 1,
) -> int:
    """Persist references for points that survived ``_exclude_stationary``.

    ZH: 写入保留点引用——每个子轨迹的保留点通过 trajectory_point_id 指向源数据行。
        CSV 模式：tp_ref_to_id 存 trajectory_points 自增 ID → 走 FK 映射。
        PG 模式：tp_ref_to_id 为空 → 直接用 source_row_key（即源表主键）作为 trajectory_point_id 值。
        使用 PostgreSQL COPY FROM STDIN 流式写入，避免逐批 INSERT 的网络往返开销。
        返回下一个待分配的子轨迹序号（``start_subtrack_index`` + 写入子轨迹数），
        供调用方分批流式写入时保持 ``subtrack_index`` 全局递增。
    EN: Persist per-point references for kept subtracks.  CSV mode uses
        ``tp_ref_to_id`` to map to ``trajectory_points`` auto-increment IDs.
        PG mode (empty map) stores the source PK (``source_row_key``) directly
        as ``trajectory_point_id``.  Uses COPY FROM STDIN for streaming write.
        Returns the next subtrack index to assign (``start_subtrack_index`` plus
        the number of subtracks written), so callers can stream successive
        batches while keeping ``subtrack_index`` globally increasing.
    """
    if clean_id is None:
        raise ValueError("clean_id is required for derived v2 persistence")
    if not kept_subtracks:
        return start_subtrack_index

    st_idx = start_subtrack_index
    with conn.cursor() as cur:
        with cur.copy(
            "COPY kept_trajectory_points "
            "(clean_id, trajectory_point_id, source_id, source_row_key, "
            "vehicle_id, subtrack_index, sequence_in_subtrack) "
            "FROM STDIN"
        ) as copy:
            for subtrack in kept_subtracks:
                for seq_idx, point in enumerate(subtrack):
                    key = _point_ref_key(point.point_ref)
                    tp_id = tp_ref_to_id.get(key) if tp_ref_to_id else None
                    if tp_id is None:
                        # Fallback: source_row_key is the DB primary key in PG mode
                        tp_id = int(point.point_ref.source_row_key)
                    copy.write_row((
                        clean_id,
                        tp_id,
                        point.point_ref.source_id,
                        point.point_ref.source_row_key,
                        point.vehicle_id,
                        st_idx,
                        seq_idx,
                    ))
                st_idx += 1
    return st_idx


def insert_injected_points(
    conn: Any,
    active_points: list[TrajectoryPoint],
    *,
    clean_id: str | None = None,
) -> None:
    """Insert synthetic points injected by ``_inject_orthogonal``.

    ZH: 写入注入的合成点。调用方已按 generated_reason="orthogonal_heading" 预过滤。
    """
    if not active_points:
        return

    columns = [
        "vehicle_id", "timestamp", "lon", "lat", "x", "y",
        "heading", "speed",
        "from_source_id", "from_source_row_key",
        "to_source_id", "to_source_row_key",
        "generated_reason", "origin_lon", "origin_lat",
    ]
    rows: list[tuple[Any, ...]] = []
    for p in active_points:
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

    columns, rows = _with_stage_id(columns, rows, "clean_id", clean_id)
    if rows:
        with conn.cursor() as cur:
            _chunked_insert(cur, "injected_points", columns, rows)


# ── Intersect2 ─────────────────────────────────────────────────────────

def insert_tracks_cross(
    conn: Any,
    candidates: list[CrossTrackCandidate],
    *,
    detect_id: str | None = None,
) -> int:
    """Persist deduplicated cross-track candidates and return inserted rows."""
    if not candidates:
        return 0

    columns = [
        "vehicle_id", "timestamp", "lon", "lat", "x", "y",
        "heading", "speed",
        "from_source_id", "from_source_row_key",
        "to_source_id", "to_source_row_key",
        "generated_reason", "origin_lon", "origin_lat",
    ]
    rows = [
        (
            candidate.vehicle_id,
            candidate.timestamp,
            candidate.lon,
            candidate.lat,
            candidate.x,
            candidate.y,
            candidate.heading,
            candidate.speed,
            candidate.from_source_id,
            candidate.from_source_row_key,
            candidate.to_source_id,
            candidate.to_source_row_key,
            "orthogonal_cross_track",
            candidate.origin_lon,
            candidate.origin_lat,
        )
        for candidate in candidates
    ]
    columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
    with conn.cursor() as cur:
        inserted = _chunked_insert_returning(
            cur,
            "tracks_cross",
            columns,
            rows,
            ["id"],
            on_conflict=(
                "ON CONFLICT (detect_id, vehicle_id, from_source_id, from_source_row_key, "
                "to_source_id, to_source_row_key) DO NOTHING"
            ),
        )
    return len(inserted)


def insert_turn_events(
    conn: Any,
    turn_events: list[TurnEvent],
    origin_lon: float = 0.0,
    origin_lat: float = 0.0,
    *,
    detect_id: str | None = None,
) -> dict[int, int]:
    """Insert turn events, return ``{event_id: db_id}`` mapping."""
    if not turn_events:
        return {}

    columns = [
        "event_id", "vehicle_id", "timestamp", "lon", "lat",
        "x", "y", "in_bearing", "out_bearing", "turn_deg", "score",
        "source", "tp_source_id", "tp_source_row_key",
        "from_source_id", "from_source_row_key", "to_source_id", "to_source_row_key",
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
            te.from_source_id,
            te.from_source_row_key,
            te.to_source_id,
            te.to_source_row_key,
            origin_lon,
            origin_lat,
        ))

    columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
    mapping: dict[int, int] = {}
    with conn.cursor() as cur:
        returned = _chunked_insert_returning(
            cur, "turn_events", columns, rows, ["id", "event_id"],
            on_conflict=(
                "ON CONFLICT (detect_id, vehicle_id, timestamp) DO NOTHING"
            ),
        )
    for db_id, event_id in returned:
        mapping[event_id] = db_id
    return mapping


def insert_candidate_intersections(
    conn: Any,
    clusters: list[HotspotCluster],
    origin_lon: float = 0.0,
    origin_lat: float = 0.0,
    *,
    detect_id: str | None = None,
) -> dict[int, int]:
    """Insert candidate intersections with POLYGON geometry.

    ZH: 写入候选交叉口——POLYGON geom 在 SQL 中通过 ST_Buffer 计算。
        返回 {cluster_id: db_id} 映射。
    """
    if not clusters:
        return {}
    if detect_id is None:
        raise ValueError("detect_id is required for derived v2 persistence")

    columns = [
        "cluster_id", "centroid_x", "centroid_y",
        "centroid_lon", "centroid_lat", "radius_m",
        "source", "sources", "source_counts", "source_ratios",
        "critical_sources",
        "turn_event_count", "orthogonal_count", "cross_track_count", "total_event_count",
        "origin_lon", "origin_lat",
        "geom",  # ← must be last: _POLYGON_EXPR is appended to end of placeholders
    ]
    columns = ["detect_id", *columns]
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
                json.dumps(list(c.source_ids), ensure_ascii=False),
                json.dumps(c.source_counts, ensure_ascii=False),
                json.dumps(c.source_ratios, ensure_ascii=False),
                json.dumps(list(c.critical_sources), ensure_ascii=False),
                len([e for e in c.events if e.source == "turn"]),
                len([e for e in c.events if e.source == "orthogonal"]),
                len([e for e in c.events if e.source == "orthogonal_cross_track"]),
                len(c.events),
                origin_lon,
                origin_lat,
            )
            row = (detect_id, *row)
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
    *,
    detect_id: str | None = None,
) -> None:
    """Persist every cluster member with its typed input source pair.

    ``turn_event_id`` is used only for native turn events.  Orthogonal and
    cross-track members remain distinct rows and point directly to their
    source pairs, so their local event ids cannot collide with turn-event ids.
    """
    rows: list[tuple[Any, ...]] = []
    for c in clusters:
        ci_db_id = ci_id_map.get(c.cluster_id)
        if ci_db_id is None:
            continue
        for event in c.events:
            turn_event_id = (
                te_id_map.get(event.event_id) if event.source == "turn" else None
            )
            from_source_id = event.from_source_id
            from_source_row_key = event.from_source_row_key
            if from_source_id is None and event.point_ref is not None:
                from_source_id = event.point_ref.source_id
                from_source_row_key = event.point_ref.source_row_key
            rows.append((
                ci_db_id,
                event.source,
                turn_event_id,
                from_source_id,
                from_source_row_key,
                event.to_source_id,
                event.to_source_row_key,
            ))

    if rows:
        columns = [
            "candidate_intersection_id", "event_kind", "turn_event_id",
            "from_source_id", "from_source_row_key",
            "to_source_id", "to_source_row_key",
        ]
        columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
        with conn.cursor() as cur:
            _chunked_insert(cur, "candidate_intersection_events", columns, rows)


def insert_candidate_entry_exit_points(
    conn: Any,
    clusters: list[HotspotCluster],
    ci_id_map: dict[int, int],
    *,
    detect_id: str | None = None,
) -> dict[tuple[int, int], int]:
    """Insert crossing points, return ``{crossing_id: db_id}`` mapping."""
    columns = [
        "candidate_intersection_id", "cluster_id", "vehicle_id",
        "segment_index", "crossing_type",
        "crossing_x", "crossing_y", "crossing_lon", "crossing_lat",
        "branch_angle_deg", "origin_lon", "origin_lat",
        "from_source_id", "from_source_row_key",
        "to_source_id", "to_source_row_key",
    ]
    rows: list[tuple[Any, ...]] = []
    crossing_ids: list[tuple[int, int]] = []  # (cluster_id, crossing_id), parallel to rows
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
                cp_any.point_ref.source_id,
                cp_any.point_ref.source_row_key,
                cp_any.end_point_ref.source_id if cp_any.end_point_ref else None,
                cp_any.end_point_ref.source_row_key if cp_any.end_point_ref else None,
            ))
            crossing_ids.append((c.cluster_id, cp_any.crossing_id))

    columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
    mapping: dict[tuple[int, int], int] = {}
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
    *,
    detect_id: str | None = None,
) -> dict[int, int]:
    """Insert only topology-verified intersections with POLYGON geometry.

    ZH: 写入验证通过的交叉口。POLYGON geom 从关联的 candidate_intersection
        的 centroid + radius 派生（方案A：应用层计算）。
        返回 {cluster_id: db_id} 映射。
    """
    if not topologies:
        return {}
    if detect_id is None:
        raise ValueError("detect_id is required for derived v2 persistence")

    columns = [
        "candidate_intersection_id", "cluster_id",
        "branch_centers_deg", "transition_counts",
        "observation_count", "is_complete", "reason",
        "clip_radius_m",
        "geom",
    ]
    columns = ["detect_id", *columns]
    mapping: dict[int, int] = {}
    with conn.cursor() as cur:
        for topo in topologies:
            if not topo.is_complete:
                continue
            ci_db_id = ci_id_map.get(topo.cluster_id)
            if ci_db_id is None:
                continue
            cluster = clusters_by_id.get(topo.cluster_id)
            clip_radius_m = topo.clip_radius_m or (
                cluster.radius_m if cluster is not None else 0.0
            )
            if not clip_radius_m > 0.0:
                raise ValueError(
                    f"complete intersection {topo.cluster_id} requires a positive clip radius"
                )
            row = (
                ci_db_id,
                topo.cluster_id,
                json.dumps(topo.branch_centers_deg, ensure_ascii=False),
                json.dumps({f"{k[0]},{k[1]}": v for k, v in topo.transition_counts.items()},
                          ensure_ascii=False),
                topo.observation_count,
                topo.is_complete,
                topo.reason,
                clip_radius_m,
            )
            row = (detect_id, *row)
            # Build geom via ST_Buffer from candidate cluster's centroid + radius
            if cluster is not None:
                placeholders = ", ".join("%s" for _ in row)
                placeholders += f", {_POLYGON_EXPR}"
                sql = (
                    f"INSERT INTO intersections ({', '.join(columns)}) "
                    f"VALUES ({placeholders}) "
                    f"RETURNING id, cluster_id"
                )
                cur.execute(sql, row + (cluster.centroid_lon, cluster.centroid_lat, clip_radius_m))
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
    crossing_id_map: dict[tuple[int, int], int] | None = None,
    *,
    detect_id: str | None = None,
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
        "entry_crossing_id", "exit_crossing_id",
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
            (crossing_id_map or {}).get((obs.cluster_id, obs.entry_crossing_id or -1)),
            (crossing_id_map or {}).get((obs.cluster_id, obs.exit_crossing_id or -1)),
        ))

    columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
    if rows:
        with conn.cursor() as cur:
            _chunked_insert(cur, "entry_exit_points", columns, rows)
