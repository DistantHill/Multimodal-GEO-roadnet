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
# 3  Cleaner 点按 generated_reason   kept / injected writer 消费同一批子轨迹，
#    在 writer 内分类                 共享已分配的 subtrack_index
# 4  恢复 excluded_points 写入       4326 Geometry 作为逐点审计真值，与
#                                   trajectory_points.is_active 状态同时保留
# 5  _flush_clean_batch 统一分配     分批流式写入时保持 subtrack_index 全局递增
# ────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .models import (
    CenterlineFitBundle,
    ClipConfig,
    ClipResult,
    CrossTrackCandidate,
    GeneratedReason,
    RefinementSubtrack,
    EntryExitObservation,
    ExcludedIndex,
    ExcludedPoint,
    HotspotCluster,
    IntersectionTopology,
    PointRef,
    SpatialContext,
    TrajectoryPoint,
    TurnEvent,
    FittingConfig,
)
from .projection import CoordinateTransformer, spatial_context_from_values

# ── Batch size for multi-row INSERT ───────────────────────────────────
_BATCH = 5000
_CANONICAL_INJECTED_SOURCE_ID = "public.injected_points"
_INJECTED_SOURCE_ID_ALIASES = frozenset({
    "injected_points",
    _CANONICAL_INJECTED_SOURCE_ID,
})
_SEQUENCE_ID_TABLES = frozenset({
    "candidate_intersections",
    "candidate_entry_exit_points",
    "intersections",
})


@dataclass(frozen=True, slots=True)
class CleanRunRecord:
    id: str
    source_id: str
    source_kind: str
    parameters: dict[str, Any]
    status: str
    spatial_context: SpatialContext


def create_clean_run(
    conn: Any,
    *,
    source_id: str,
    source_kind: str,
    parameters: dict[str, Any],
    spatial_context: SpatialContext,
) -> str:
    """Create one append-only Clean Run and return its UUID."""
    # Keep the persistence boundary fail-closed even when called outside the
    # normal pipeline configuration path.
    CoordinateTransformer(spatial_context)
    clean_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO clean_runs "
            "(id, source_id, source_kind, source_srid, processing_srid, "
            "projection_config, parameters, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'running')",
            (
                clean_id,
                source_id,
                source_kind,
                spatial_context.source_srid,
                spatial_context.processing_srid,
                json.dumps(spatial_context.projection_config, ensure_ascii=False),
                json.dumps(parameters, ensure_ascii=False),
            ),
        )
    return clean_id


def create_detect_run(
    conn: Any,
    *,
    clean_id: str,
    parameters: dict[str, Any],
) -> str:
    """Create one Detect Run only from a completed fixed-CRS Clean Run."""
    detect_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO detect_runs (id, clean_id, parameters, status) "
            "SELECT %s, c.id, %s, 'running' FROM clean_runs AS c "
            "WHERE c.id = %s AND c.status = 'completed' "
            "AND c.processing_srid IS NOT NULL "
            "AND c.projection_config->>'coordinate_semantics_version' = '2' "
            "RETURNING id",
            (detect_id, json.dumps(parameters, ensure_ascii=False), clean_id),
        )
        if cur.fetchone() is None:
            raise ValueError(
                f"Clean Run {clean_id} is not completed with fixed-CRS coordinates"
            )
    return detect_id


def create_fit_run(
    conn: Any,
    *,
    detect_id: str,
    clip_config: ClipConfig,
    fitting_config: FittingConfig,
) -> str:
    """Create one append-only Fit Run and return its UUID."""
    fit_id = str(uuid.uuid4())
    parameters = {
        "clip": clip_config.as_dict(),
        "fitting": fitting_config.as_dict(),
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fit_runs "
            "(id, detect_id, clip_algorithm, clip_version, fitting_algorithm, "
            "fitting_version, parameters, status) "
            "SELECT %s, d.id, 'circle_segment_clip', 'v2', "
            "'normalized_arc_coordinate_median', 'v2', %s, 'running' "
            "FROM detect_runs AS d "
            "JOIN clean_runs AS c ON c.id = d.clean_id "
            "WHERE d.id = %s AND d.status = 'completed' "
            "AND c.processing_srid IS NOT NULL "
            "AND c.projection_config->>'coordinate_semantics_version' = '2' "
            "RETURNING id",
            (fit_id, json.dumps(parameters, ensure_ascii=False), detect_id),
        )
        if cur.fetchone() is None:
            raise ValueError(f"Detect Run {detect_id} is not completed")
    return fit_id


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


def finish_fit_run(
    conn: Any,
    fit_id: str,
    *,
    status: str,
    report: dict[str, Any] | None = None,
) -> None:
    """Finish a running Fit Run exactly once."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fit_runs SET status = %s, report = %s, finished_at = NOW() "
            "WHERE id = %s AND status = 'running'",
            (
                status,
                json.dumps(report, ensure_ascii=False) if report is not None else None,
                fit_id,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Fit Run {fit_id} is not running")


def insert_centerline_bundle(
    conn: Any,
    bundle: CenterlineFitBundle,
    clips: ClipResult,
    *,
    fit_id: str,
    detect_id: str,
) -> tuple[int, ...]:
    """Persist successful centerlines and their ordered source-clip manifests.

    Centerlines and manifests are intentionally batched across the complete
    three-variant bundle.  A real run commonly produces hundreds of lines;
    issuing one ``INSERT RETURNING`` plus one manifest ``INSERT`` for every
    line turns local database latency into the dominant Fitting cost.
    """
    if clips.detect_id is not None and clips.detect_id != detect_id:
        raise ValueError("ClipResult belongs to a different Detect Run")
    for result in (bundle.combined, bundle.a_to_b, bundle.b_to_a):
        if result.spatial_context != clips.spatial_context:
            raise ValueError("FitResult and ClipResult SpatialContext must match")
        if result.detect_id is not None and result.detect_id != detect_id:
            raise ValueError("FitResult belongs to a different Detect Run")
    clips_by_id = {clip.clip_id: clip for clip in clips.clips}
    ordered_centerlines = [
        centerline
        for result in (bundle.combined, bundle.a_to_b, bundle.b_to_a)
        for centerline in result.centerlines
    ]
    if not ordered_centerlines:
        return ()

    centerline_columns = [
        "fit_id", "detect_id", "variant",
        "from_intersection_id", "to_intersection_id",
        "geom", "length_m", "forward_clip_count", "reverse_clip_count",
    ]
    centerline_rows: list[tuple[Any, ...]] = []
    for centerline in ordered_centerlines:
        points_wkt = ", ".join(f"{lon} {lat}" for lon, lat in centerline.points)
        centerline_rows.append((
            fit_id,
            detect_id,
            centerline.variant,
            centerline.from_intersection_id,
            centerline.to_intersection_id,
            f"LINESTRING({points_wkt})",
            centerline.length_m,
            centerline.forward_clip_count,
            centerline.reverse_clip_count,
        ))

    value_template = ", ".join(
        ["%s"] * 5 + ["ST_GeomFromText(%s, 4326)"] + ["%s"] * 3
    )
    with conn.cursor() as cur:
        returned = _chunked_insert_template_returning(
            cur,
            "centerlines",
            centerline_columns,
            centerline_rows,
            value_template,
            ["id", "variant", "from_intersection_id", "to_intersection_id"],
        )

    id_by_key = {
        (str(variant), int(from_id), int(to_id)): int(centerline_id)
        for centerline_id, variant, from_id, to_id in returned
    }
    ordered_keys = [
        (
            centerline.variant,
            centerline.from_intersection_id,
            centerline.to_intersection_id,
        )
        for centerline in ordered_centerlines
    ]
    if len(id_by_key) != len(ordered_centerlines) or any(
        key not in id_by_key for key in ordered_keys
    ):
        raise RuntimeError("centerline batch INSERT returned an incomplete id mapping")
    centerline_ids = [id_by_key[key] for key in ordered_keys]

    manifest_rows: list[tuple[Any, ...]] = []
    for centerline_id, centerline in zip(centerline_ids, ordered_centerlines):
        for input_ordinal, clip_id in enumerate(centerline.source_clip_ids):
            clip = clips_by_id[clip_id]
            start_intersection_id = clip.start.intersection_id
            end_intersection_id = clip.end.intersection_id
            assert start_intersection_id is not None and end_intersection_id is not None
            manifest_rows.append((
                centerline_id, input_ordinal, clip.clip_id, clip.vehicle_id,
                clip.subtrack_index, clip.clip_ordinal_in_track,
                start_intersection_id, clip.start.segment_index,
                clip.start.segment_fraction, clip.interior_start_index,
                clip.interior_end_index_exclusive, end_intersection_id,
                clip.end.segment_index, clip.end.segment_fraction,
                start_intersection_id != centerline.from_intersection_id,
                clip.point_count,
            ))

    manifest_columns = [
        "centerline_id", "input_ordinal", "clip_id", "vehicle_id",
        "subtrack_index", "clip_ordinal_in_track", "start_intersection_id",
        "start_segment_index", "start_segment_fraction",
        "interior_start_index", "interior_end_index_exclusive",
        "end_intersection_id", "end_segment_index", "end_segment_fraction",
        "reverse_to_centerline_direction", "input_point_count",
    ]
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "centerline_source_clips",
            manifest_columns,
            manifest_rows,
        )
    return tuple(centerline_ids)


def fetch_clean_run(conn: Any, clean_id: str) -> CleanRunRecord:
    """Load one Clean Run for explicit Detect reuse."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_id, source_kind, parameters, status, "
            "source_srid, processing_srid, projection_config "
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
        spatial_context=spatial_context_from_values(row[5], row[6], row[7]),
    )


# ── Helpers ────────────────────────────────────────────────────────────

_POLYGON_EXPR = (
    "ST_Transform("
    "ST_Buffer(ST_SetSRID(ST_MakePoint(%s, %s), %s::integer), %s), "
    "4326)"
)


def _reserve_ids(cur: Any, table: str, count: int) -> tuple[int, ...]:
    """Reserve database-generated BIGSERIAL IDs in a deterministic input order."""
    if table not in _SEQUENCE_ID_TABLES:
        raise ValueError(f"unsupported sequence-backed table: {table}")
    if count <= 0:
        return ()
    cur.execute(
        "SELECT ordinal, nextval(pg_get_serial_sequence("
        f"'{table}', 'id')::regclass) "
        "FROM generate_series(1, %s) AS reserved(ordinal) ORDER BY ordinal",
        (count,),
    )
    rows = list(cur.fetchall())
    if [int(row[0]) for row in rows] != list(range(1, count + 1)):
        raise RuntimeError(f"failed to reserve ordered IDs for {table}")
    ids = tuple(int(row[1]) for row in rows)
    if len(ids) != count or len(set(ids)) != count:
        raise RuntimeError(f"failed to reserve {count} unique IDs for {table}")
    return ids


def _candidate_crossing_source_ref(
    point_ref: PointRef | None,
) -> tuple[str | None, str | None]:
    """Return a durable source-table reference for one crossing endpoint."""
    if point_ref is None:
        return None, None
    source_id = str(point_ref.source_id)
    source_row_key = str(point_ref.source_row_key)
    if source_id in _INJECTED_SOURCE_ID_ALIASES:
        try:
            injected_point_id = int(source_row_key)
        except ValueError as exc:
            raise ValueError(
                "injected crossing endpoints must reference injected_points.id"
            ) from exc
        if injected_point_id <= 0:
            raise ValueError(
                "injected crossing endpoints must reference a positive "
                "injected_points.id"
            )
        return _CANONICAL_INJECTED_SOURCE_ID, str(injected_point_id)
    if point_ref.generated_reason:
        raise ValueError(
            "injected crossing endpoints must be reloaded from "
            "public.injected_points before persistence"
        )
    return source_id, source_row_key


def _validate_candidate_crossing_injected_refs(
    cur: Any,
    detect_id: str,
    injected_refs: set[tuple[int, str]],
) -> None:
    """Require injected endpoint IDs to belong to the Detect Run's Clean Run."""
    if not injected_refs:
        return
    ordered_refs = sorted(injected_refs)
    injected_ids = [injected_id for injected_id, _vehicle_id in ordered_refs]
    vehicle_ids = [vehicle_id for _injected_id, vehicle_id in ordered_refs]
    cur.execute(
        "SELECT requested.injected_id, requested.vehicle_id "
        "FROM unnest(%s::bigint[], %s::text[]) "
        "AS requested(injected_id, vehicle_id) "
        "JOIN detect_runs AS detect ON detect.id = %s "
        "JOIN injected_points AS injected "
        "ON injected.id = requested.injected_id "
        "AND injected.clean_id = detect.clean_id "
        "AND injected.vehicle_id = requested.vehicle_id",
        (injected_ids, vehicle_ids, detect_id),
    )
    found = {(int(row[0]), str(row[1])) for row in cur.fetchall()}
    expected = set(ordered_refs)
    if found != expected:
        missing = sorted(expected - found)
        preview = ", ".join(
            f"{injected_id}@{vehicle_id}"
            for injected_id, vehicle_id in missing[:5]
        )
        suffix = ", ..." if len(missing) > 5 else ""
        raise ValueError(
            "candidate crossing injected-point references do not belong to "
            f"Detect Run {detect_id}: {preview}{suffix}"
        )


def _project_lonlat(
    spatial_context: SpatialContext,
    values: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Project 4326 truth coordinates with the owning Clean Run context."""
    return CoordinateTransformer(spatial_context).forward_many(values)


def _chunked_insert_returning(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    returning_cols: list[str],
    *,
    on_conflict: str = "",
    batch_size: int = _BATCH,
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
    _effective_batch = min(batch_size, _max_rows)
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


def _chunked_insert_template_returning(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    value_template: str,
    returning_cols: list[str],
    *,
    batch_size: int = _BATCH,
) -> list[tuple[Any, ...]]:
    """Batch rows whose VALUES clause contains a trusted SQL expression."""
    if not rows:
        return []
    params_per_row = len(rows[0])
    if any(len(row) != params_per_row for row in rows):
        raise ValueError("templated insert rows must have a consistent width")
    effective_batch = min(batch_size, max(1, 65535 // params_per_row))
    returned: list[tuple[Any, ...]] = []
    for start in range(0, len(rows), effective_batch):
        batch = rows[start : start + effective_batch]
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
            f"{', '.join(f'({value_template})' for _ in batch)} "
            f"RETURNING {', '.join(returning_cols)}"
        )
        params = [value for row in batch for value in row]
        cur.execute(sql, params)
        returned.extend(cur.fetchall())
    return returned


def _chunked_insert(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    batch_size: int = _BATCH,
) -> None:
    """Multi-row INSERT without RETURNING, processed in batches."""
    if not rows:
        return
    col_list = ", ".join(columns)
    # ZH: PG 协议层参数上限 65535，按列数动态限制每批行数。
    # EN: PG wire protocol caps params at 65535; limit rows per batch by column count.
    _max_rows = max(1, 65535 // len(columns))
    _effective_batch = min(batch_size, _max_rows)
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


def _copy_insert(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
) -> None:
    """Stream append-only stage artifacts with PostgreSQL COPY.

    Detect and Fitting artifacts belong to freshly-created run ids and are
    written in one transaction.  COPY avoids constructing and parsing very
    large VALUES statements.
    """
    if not rows:
        return
    with cur.copy(
        f"COPY {table} ({', '.join(columns)}) FROM STDIN"
    ) as copy:
        for row in rows:
            copy.write_row(row)


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
    batch_size: int = _BATCH,
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
            batch_size=batch_size,
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

    Derived cleaner decisions are stored per ``clean_id``.  The raw table keeps
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
    batch_size: int = _BATCH,
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
        _chunked_insert(cur, "excluded_points", columns, rows, batch_size=batch_size)


def insert_kept_trajectory_points(
    conn: Any,
    subtracks: list[RefinementSubtrack],
    tp_ref_to_id: dict[tuple[str, str], int],
    *,
    clean_id: str | None = None,
) -> int:
    """Persist references for points that survived ``_exclude_stationary``.

    ZH: 写入保留点引用——每个子轨迹的保留点通过 trajectory_point_id 指向源数据行。
        CSV 模式：tp_ref_to_id 存 trajectory_points 自增 ID → 走 FK 映射。
        PG 模式：tp_ref_to_id 为空 → 直接用 source_row_key（即源表主键）作为 trajectory_point_id 值。
        使用 PostgreSQL COPY FROM STDIN 流式写入，避免逐批 INSERT 的网络往返开销。
    EN: Persist per-point references for kept subtracks.  CSV mode uses
        ``tp_ref_to_id`` to map to ``trajectory_points`` auto-increment IDs.
        PG mode (empty map) stores the source PK (``source_row_key``) directly
        as ``trajectory_point_id``.  Uses COPY FROM STDIN for streaming write.
    """
    if clean_id is None:
        raise ValueError("clean_id is required for derived v2 persistence")
    if not subtracks:
        return 0

    kept_count = 0
    with conn.cursor() as cur:
        with cur.copy(
            "COPY kept_trajectory_points "
            "(clean_id, trajectory_point_id, source_id, source_row_key, "
            "vehicle_id, subtrack_index, sequence_in_subtrack) "
            "FROM STDIN"
        ) as copy:
            for subtrack in subtracks:
                sequence_in_subtrack = 0
                for point in subtrack.points:
                    if point.point_ref.generated_reason:
                        continue
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
                        subtrack.subtrack_index,
                        sequence_in_subtrack,
                    ))
                    sequence_in_subtrack += 1
                    kept_count += 1
    return kept_count


def insert_injected_points(
    conn: Any,
    subtracks: list[RefinementSubtrack],
    *,
    clean_id: str | None = None,
    spatial_context: SpatialContext,
    batch_size: int = _BATCH,
) -> None:
    """Insert synthetic points injected by ``_inject_orthogonal``.

    ZH: 按 ``generated_reason`` 写入派生点，并以同一子轨迹内前一个 kept 点为锚点。
    """
    if not subtracks:
        return

    columns = [
        "vehicle_id", "subtrack_index", "after_sequence_in_subtrack",
        "source_id", "source_row_key", "timestamp", "lon", "lat", "x", "y",
        "heading", "speed",
        "from_source_id", "from_source_row_key",
        "to_source_id", "to_source_row_key",
        "generated_reason",
    ]
    injected: list[tuple[RefinementSubtrack, int, TrajectoryPoint]] = []
    for subtrack in subtracks:
        kept_sequence = -1
        for point in subtrack.points:
            if point.point_ref.generated_reason != GeneratedReason.HEADING_ORTHOGONAL:
                kept_sequence += 1
                continue
            injected.append((subtrack, kept_sequence, point))

    projected = _project_lonlat(
        spatial_context,
        [(point.lon, point.lat) for _, _, point in injected],
    )
    rows: list[tuple[Any, ...]] = []
    for (subtrack, kept_sequence, point), (x, y) in zip(injected, projected):
        ref = point.point_ref
        rows.append((
            point.vehicle_id, subtrack.subtrack_index, kept_sequence,
            ref.source_id, ref.source_row_key,
            point.timestamp,
            point.lon,
            point.lat,
            x,
            y,
            point.heading,
            point.speed,
            ref.source_id,
            ref.from_ref or "",
            ref.source_id,
            ref.to_ref or "",
            ref.generated_reason,
        ))

    columns, rows = _with_stage_id(columns, rows, "clean_id", clean_id)
    if rows:
        with conn.cursor() as cur:
            _chunked_insert(cur, "injected_points", columns, rows, batch_size=batch_size)


# ── Intersect2 ─────────────────────────────────────────────────────────

def insert_tracks_cross(
    conn: Any,
    candidates: list[CrossTrackCandidate],
    *,
    detect_id: str | None = None,
    candidate_intersection_ids: Mapping[
        tuple[str, str, str, str, str], int
    ] | None = None,
    spatial_context: SpatialContext,
    batch_size: int = _BATCH,
) -> int:
    """Persist deduplicated cross-track candidates and return inserted rows."""
    if not candidates:
        return 0

    columns = [
        "vehicle_id", "timestamp", "lon", "lat", "x", "y",
        "heading", "speed",
        "from_source_id", "from_source_row_key",
        "to_source_id", "to_source_row_key",
        "generated_reason", "candidate_intersection_id",
    ]
    membership = candidate_intersection_ids or {}
    projected = _project_lonlat(
        spatial_context,
        [(candidate.lon, candidate.lat) for candidate in candidates],
    )
    rows = [
        (
            candidate.vehicle_id,
            candidate.timestamp,
            candidate.lon,
            candidate.lat,
            x,
            y,
            candidate.heading,
            candidate.speed,
            candidate.from_source_id,
            candidate.from_source_row_key,
            candidate.to_source_id,
            candidate.to_source_row_key,
            "orthogonal_cross_track",
            membership.get((
                candidate.vehicle_id,
                candidate.from_source_id,
                candidate.from_source_row_key,
                candidate.to_source_id,
                candidate.to_source_row_key,
            )),
        )
        for candidate, (x, y) in zip(candidates, projected)
    ]
    columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
    with conn.cursor() as cur:
        _copy_insert(cur, "tracks_cross", columns, rows)
    return len(rows)


def insert_turn_events(
    conn: Any,
    turn_events: list[TurnEvent],
    *,
    detect_id: str | None = None,
    candidate_intersection_ids_by_calculate_index: Mapping[int, int] | None = None,
    spatial_context: SpatialContext,
    batch_size: int = _BATCH,
) -> int:
    """Insert turn events and return the inserted row count.

    PostgreSQL ``turn_events.id`` is the sole persisted event identifier.  The
    calculation index is used only to attach in-memory cluster membership
    before COPY and is not written to the table.
    """
    if not turn_events:
        return 0

    columns = [
        "vehicle_id", "timestamp", "lon", "lat",
        "x", "y", "in_bearing", "out_bearing", "turn_deg", "score",
        "source", "tp_source_id", "tp_source_row_key",
        "from_source_id", "from_source_row_key", "to_source_id", "to_source_row_key",
        "candidate_intersection_id",
    ]
    membership = candidate_intersection_ids_by_calculate_index or {}
    projected = _project_lonlat(
        spatial_context,
        [(event.lon, event.lat) for event in turn_events],
    )
    rows: list[tuple[Any, ...]] = []
    for te, (x, y) in zip(turn_events, projected):
        rows.append((
            te.vehicle_id,
            te.timestamp,
            te.lon,
            te.lat,
            x,
            y,
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
            membership.get(te.event_calculate_index),
        ))

    columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
    with conn.cursor() as cur:
        _copy_insert(cur, "turn_events", columns, rows)
    return len(rows)


def insert_candidate_intersections(
    conn: Any,
    clusters: list[HotspotCluster],
    *,
    detect_id: str | None = None,
    spatial_context: SpatialContext,
    batch_size: int = _BATCH,
) -> dict[int, int]:
    """Insert candidate intersections with POLYGON geometry.

    ZH: 写入候选交叉口——POLYGON geom 在 SQL 中通过 ST_Buffer 计算。
        返回 {cluster_calculate_index: candidate_intersections.id} 临时映射。
    """
    if not clusters:
        return {}
    if detect_id is None:
        raise ValueError("detect_id is required for derived v2 persistence")
    ordered_clusters = sorted(
        clusters,
        key=lambda cluster: cluster.cluster_calculate_index,
    )
    calculate_indexes = [
        cluster.cluster_calculate_index for cluster in ordered_clusters
    ]
    if len(set(calculate_indexes)) != len(calculate_indexes):
        raise ValueError("candidate cluster calculation indexes must be unique")

    columns = [
        "id", "centroid_x", "centroid_y",
        "centroid_lon", "centroid_lat", "radius_m",
        "source", "sources", "source_counts", "source_ratios",
        "critical_sources",
        "turn_event_count", "cross_track_count", "total_event_count",
        "geom",  # ← must be last: _POLYGON_EXPR is appended to end of placeholders
    ]
    columns = ["detect_id", *columns]
    projected = _project_lonlat(
        spatial_context,
        [
            (cluster.centroid_lon, cluster.centroid_lat)
            for cluster in ordered_clusters
        ],
    )
    with conn.cursor() as cur:
        database_ids = _reserve_ids(
            cur,
            "candidate_intersections",
            len(ordered_clusters),
        )
        rows = [
            (
                detect_id,
                database_id,
                x,
                y,
                cluster.centroid_lon,
                cluster.centroid_lat,
                cluster.radius_m,
                cluster.source,
                json.dumps(list(cluster.source_ids), ensure_ascii=False),
                json.dumps(cluster.source_counts, ensure_ascii=False),
                json.dumps(cluster.source_ratios, ensure_ascii=False),
                json.dumps(list(cluster.critical_sources), ensure_ascii=False),
                len([event for event in cluster.events if event.source == "turn"]),
                len([
                    event
                    for event in cluster.events
                    if event.source == "orthogonal_cross_track"
                ]),
                len(cluster.events),
                x,
                y,
                spatial_context.processing_srid,
                cluster.radius_m,
            )
            for cluster, database_id, (x, y) in zip(
                ordered_clusters,
                database_ids,
                projected,
                strict=True,
            )
        ]
        value_template = ", ".join(
            ["%s"] * (len(columns) - 1) + [_POLYGON_EXPR]
        )
        returned = _chunked_insert_template_returning(
            cur,
            "candidate_intersections",
            columns,
            rows,
            value_template,
            ["id"],
            batch_size=batch_size,
        )
    if {int(row[0]) for row in returned} != set(database_ids):
        raise RuntimeError("candidate intersection INSERT returned an incomplete ID set")
    return {
        cluster.cluster_calculate_index: database_id
        for cluster, database_id in zip(
            ordered_clusters,
            database_ids,
            strict=True,
        )
    }


def insert_candidate_entry_exit_points(
    conn: Any,
    clusters: list[HotspotCluster],
    candidate_intersection_ids_by_calculate_index: dict[int, int],
    *,
    detect_id: str | None = None,
    spatial_context: SpatialContext,
    batch_size: int = _BATCH,
) -> dict[tuple[int, int], int]:
    """Insert crossings and return a temporary calculation-index/ID mapping."""
    if detect_id is None:
        raise ValueError("detect_id is required for derived v2 persistence")
    columns = [
        "id", "candidate_intersection_id", "vehicle_id",
        "segment_index", "crossing_type",
        "crossing_x", "crossing_y", "crossing_lon", "crossing_lat",
        "branch_angle_deg",
        "from_source_id", "from_source_row_key",
        "to_source_id", "to_source_row_key",
    ]
    pending: list[tuple[Any, ...]] = []
    crossing_keys: list[tuple[int, int]] = []
    injected_refs: set[tuple[int, str]] = set()
    for cluster in sorted(
        clusters,
        key=lambda item: item.cluster_calculate_index,
    ):
        ci_db_id = candidate_intersection_ids_by_calculate_index.get(
            cluster.cluster_calculate_index
        )
        if ci_db_id is None:
            continue
        for crossing in cluster.crossing_points:
            cp_any: Any = crossing  # HotspotCluster.crossing_points typed as tuple[()]
            from_source_id, from_source_row_key = _candidate_crossing_source_ref(
                cp_any.point_ref
            )
            to_source_id, to_source_row_key = _candidate_crossing_source_ref(
                cp_any.end_point_ref
            )
            if from_source_id == _CANONICAL_INJECTED_SOURCE_ID:
                assert from_source_row_key is not None
                injected_refs.add((int(from_source_row_key), str(cp_any.vehicle_id)))
            if to_source_id == _CANONICAL_INJECTED_SOURCE_ID:
                assert to_source_row_key is not None
                injected_refs.add((int(to_source_row_key), str(cp_any.vehicle_id)))
            pending.append((
                cluster,
                ci_db_id,
                cp_any,
                from_source_id,
                from_source_row_key,
                to_source_id,
                to_source_row_key,
            ))
            crossing_keys.append(
                (cluster.cluster_calculate_index, cp_any.crossing_id)
            )
    projected = _project_lonlat(
        spatial_context,
        [
            (float(crossing.crossing_lon), float(crossing.crossing_lat))
            for _, _, crossing, *_source_refs in pending
        ],
    )
    rows: list[tuple[Any, ...]] = []
    for (
        (
            _cluster,
            ci_db_id,
            crossing,
            from_source_id,
            from_source_row_key,
            to_source_id,
            to_source_row_key,
        ),
        (x, y),
    ) in zip(pending, projected, strict=True):
        rows.append((
            ci_db_id,
            crossing.vehicle_id,
            crossing.segment_index,
            "entry" if crossing.is_entry else "exit",
            x,
            y,
            crossing.crossing_lon,
            crossing.crossing_lat,
            getattr(crossing, "branch_angle_deg", None),
            from_source_id,
            from_source_row_key,
            to_source_id,
            to_source_row_key,
        ))

    if len(set(crossing_keys)) != len(crossing_keys):
        raise ValueError("candidate crossing calculation keys must be unique")
    mapping: dict[tuple[int, int], int] = {}
    if rows:
        with conn.cursor() as cur:
            _validate_candidate_crossing_injected_refs(
                cur,
                detect_id,
                injected_refs,
            )
            database_ids = _reserve_ids(
                cur,
                "candidate_entry_exit_points",
                len(rows),
            )
            rows = [
                (database_id, *row)
                for database_id, row in zip(database_ids, rows, strict=True)
            ]
            columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
            returned = _chunked_insert_returning(
                cur, "candidate_entry_exit_points", columns, rows, ["id"],
                batch_size=batch_size,
            )
        if {int(row[0]) for row in returned} != set(database_ids):
            raise RuntimeError("candidate crossing INSERT returned an incomplete ID set")
        mapping = dict(zip(crossing_keys, database_ids, strict=True))
    return mapping


def insert_intersections(
    conn: Any,
    topologies: list[IntersectionTopology],
    candidate_intersection_ids_by_calculate_index: dict[int, int],
    clusters_by_calculate_index: dict[int, HotspotCluster],
    *,
    detect_id: str | None = None,
    spatial_context: SpatialContext,
    batch_size: int = _BATCH,
) -> dict[int, int]:
    """Insert only topology-verified intersections with POLYGON geometry.

    ZH: 写入验证通过的交叉口。POLYGON geom 从关联的 candidate_intersection
        的 centroid + radius 派生（方案A：应用层计算）。
        返回 {cluster_calculate_index: intersections.id} 临时映射。
    """
    if not topologies:
        return {}
    if detect_id is None:
        raise ValueError("detect_id is required for derived v2 persistence")

    columns = [
        "id", "candidate_intersection_id",
        "branch_centers_deg", "transition_counts",
        "observation_count", "is_complete", "reason",
        "clip_radius_m",
        "geom",
    ]
    columns = ["detect_id", *columns]
    prepared: list[tuple[IntersectionTopology, int, HotspotCluster | None, float]] = []
    for topo in topologies:
        if not topo.is_complete:
            continue
        ci_db_id = candidate_intersection_ids_by_calculate_index.get(
            topo.cluster_calculate_index
        )
        if ci_db_id is None:
            continue
        cluster = clusters_by_calculate_index.get(topo.cluster_calculate_index)
        clip_radius_m = topo.clip_radius_m or (
            cluster.radius_m if cluster is not None else 0.0
        )
        if not clip_radius_m > 0.0:
            raise ValueError(
                "complete intersection calculation index "
                f"{topo.cluster_calculate_index} requires a positive clip radius"
            )
        prepared.append((topo, ci_db_id, cluster, clip_radius_m))

    prepared.sort(key=lambda item: item[0].cluster_calculate_index)
    prepared_indexes = [item[0].cluster_calculate_index for item in prepared]
    if len(set(prepared_indexes)) != len(prepared_indexes):
        raise ValueError("intersection calculation indexes must be unique")

    projected_clusters = [
        (topology.cluster_calculate_index, cluster)
        for topology, _candidate_id, cluster, _radius in prepared
        if cluster is not None
    ]
    projected_by_calculate_index = {
        calculate_index: projected
        for (calculate_index, _cluster), projected in zip(
            projected_clusters,
            _project_lonlat(
                spatial_context,
                [
                    (cluster.centroid_lon, cluster.centroid_lat)
                    for _calculate_index, cluster in projected_clusters
                ],
            ),
            strict=True,
        )
    }

    optional_polygon = (
        "CASE WHEN %s::boolean THEN "
        "ST_Transform(ST_Buffer(ST_SetSRID(ST_MakePoint(%s::double precision, "
        "%s::double precision), %s::integer), %s::double precision), 4326) "
        "ELSE NULL END"
    )
    value_template = ", ".join(["%s"] * (len(columns) - 1) + [optional_polygon])
    with conn.cursor() as cur:
        database_ids = _reserve_ids(cur, "intersections", len(prepared))
        rows = [
            (
                detect_id,
                database_id,
                ci_db_id,
                json.dumps(topo.branch_centers_deg, ensure_ascii=False),
                json.dumps(
                    {
                        f"{key[0]},{key[1]}": value
                        for key, value in topo.transition_counts.items()
                    },
                    ensure_ascii=False,
                ),
                topo.observation_count,
                topo.is_complete,
                topo.reason,
                clip_radius_m,
                cluster is not None,
                (
                    projected_by_calculate_index[topo.cluster_calculate_index][0]
                    if cluster is not None else None
                ),
                (
                    projected_by_calculate_index[topo.cluster_calculate_index][1]
                    if cluster is not None else None
                ),
                spatial_context.processing_srid if cluster is not None else None,
                clip_radius_m if cluster is not None else None,
            )
            for database_id, (topo, ci_db_id, cluster, clip_radius_m) in zip(
                database_ids,
                prepared,
                strict=True,
            )
        ]
        returned = _chunked_insert_template_returning(
            cur,
            "intersections",
            columns,
            rows,
            value_template,
            ["id"],
            batch_size=batch_size,
        )
    if {int(row[0]) for row in returned} != set(database_ids):
        raise RuntimeError("intersection INSERT returned an incomplete ID set")
    return {
        topo.cluster_calculate_index: database_id
        for database_id, (topo, _ci_id, _cluster, _radius) in zip(
            database_ids,
            prepared,
            strict=True,
        )
    }


def insert_entry_exit_points(
    conn: Any,
    observations: list[EntryExitObservation],
    intersection_ids_by_calculate_index: dict[int, int],
    crossing_ids_by_calculate_index: dict[tuple[int, int], int] | None = None,
    *,
    detect_id: str | None = None,
    batch_size: int = _BATCH,
) -> None:
    """Insert entry/exit observations.

    ZH: 写入每辆车通过每个交叉口的出入观测，并把运行时 crossing 引用映射为
        ``candidate_entry_exit_points.id`` 外键。
    """
    if not observations:
        return

    columns = [
        "intersection_id", "vehicle_id",
        "entry_branch_angle", "exit_branch_angle",
        "inbound_bearing", "outbound_bearing", "turn_deg",
        "entry_crossing_id", "exit_crossing_id",
    ]
    rows: list[tuple[Any, ...]] = []
    for obs in observations:
        int_db_id = intersection_ids_by_calculate_index.get(
            obs.cluster_calculate_index
        )
        if int_db_id is None:
            continue
        entry_crossing_key = (
            None
            if obs.entry_crossing_id is None
            else (obs.cluster_calculate_index, obs.entry_crossing_id)
        )
        exit_crossing_key = (
            None
            if obs.exit_crossing_id is None
            else (obs.cluster_calculate_index, obs.exit_crossing_id)
        )
        rows.append((
            int_db_id,
            obs.vehicle_id,
            obs.entry_branch_angle,
            obs.exit_branch_angle,
            obs.inbound_bearing,
            obs.outbound_bearing,
            obs.turn_deg,
            (
                (crossing_ids_by_calculate_index or {}).get(entry_crossing_key)
                if entry_crossing_key is not None
                else None
            ),
            (
                (crossing_ids_by_calculate_index or {}).get(exit_crossing_key)
                if exit_crossing_key is not None
                else None
            ),
        ))

    columns, rows = _with_stage_id(columns, rows, "detect_id", detect_id)
    if rows:
        with conn.cursor() as cur:
            _copy_insert(cur, "entry_exit_points", columns, rows)
