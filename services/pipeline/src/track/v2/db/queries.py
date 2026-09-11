"""Database queries for Pipeline intersection, Clip, and Replay stages.

This module owns SQL builders, DB execution, row materialization, spatial-window
iteration, and persisted Clean/Detect Run reads. Business modules import query
operations from this single infrastructure boundary.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

from ..models import (
    CleanedPointRef,
    ClipIntersection,
    ClipTrack,
    HotspotCluster,
    PointRef,
    ResolvedTrackPoint,
    SpatialContext,
    TrajectoryPoint,
)
from ..projection import CoordinateTransformer, spatial_context_from_values
from .source_table import quote_table_name, validate_source_table


# ═══════════════════════════════════════════════════════════════════════
# Column select fragments
# ═══════════════════════════════════════════════════════════════════════
# Source rows and injected points are mapped into one p/i row shape.  Parent
# source pairs were removed in v3: order is the stable Clean sequence contract.

_IP_UNIFIED_COLS = (
    "vehicle_id, timestamp, lon, lat, heading, speed, "
    "%s::text AS source_id, "
    "id::text AS source_row_key, "
    "'{}'::jsonb AS extra, "
    "generated_reason, x, y, 1 AS point_kind, id AS row_id, "
    "subtrack_index, after_sequence_in_subtrack AS anchor_sequence"
)

_INJECTED_SOURCE_ID = "public.injected_points"


def _tp_unified_cols(processing_srid: int, *, alias: str = "") -> str:
    """Return raw-point columns plus fixed processing-CRS metre coordinates."""
    prefix = f"{alias}." if alias else ""
    projected = f"ST_Transform({prefix}geom, {int(processing_srid)})"
    return (
        f"{prefix}vehicle_id, {prefix}timestamp, {prefix}lon, {prefix}lat, "
        f"{prefix}heading, {prefix}speed, %s::text AS source_id, "
        f"{prefix}id::text AS source_row_key, {prefix}extra, "
        f"''::text AS generated_reason, ST_X({projected}) AS x, "
        f"ST_Y({projected}) AS y, 0 AS point_kind, {prefix}id AS row_id, "
        "1 AS subtrack_index, 0 AS anchor_sequence"
    )


# ═══════════════════════════════════════════════════════════════════════
# Row mapping helper
# ═══════════════════════════════════════════════════════════════════════
def row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a DB cursor row (unified schema-v3 p/i format) to a dict.

    ZH: 将统一 14 列格式的 DB 游标行转为 dict。
        替代 intersect2.py 中重复的 ``rows.append({...})``。
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
        "generated_reason": row[9],
        "x": float(row[10]),
        "y": float(row[11]),
        "point_kind": int(row[12]),
        "row_id": int(row[13]),
        "subtrack_index": int(row[14]),
        "anchor_sequence": int(row[15]),
    }


# ═══════════════════════════════════════════════════════════════════════
# Query builders
# ═══════════════════════════════════════════════════════════════════════
def build_spatial_extent_sql(
    table: str = "trajectory_points",
    *,
    has_clean_id: bool = False,
    kept_table: str | None = None,
    spatial_context: SpatialContext,
    source_key_sql_type: str = "bigint",
) -> str:
    """MIN/MAX processing-CRS x/y of active points.

    ZH: 查询 active points 的空间范围 (lon_min, lat_min, lon_max, lat_max)。
        ``kept_table`` 非空时 JOIN 清洗结果表（几何列取自 ``table``），
        按 ``ktp.clean_id`` 只读本次 Clean Run 的 kept 结果。
    EN: When ``kept_table`` is set, JOIN the clean-result table against
        ``table`` for the geometry columns, filtered by ``ktp.clean_id``.

    Parameter placeholders (in order, caller must bind):
        Kept mode only:
            1. %s — ktp.clean_id
            2. %s — injected_points.clean_id
    """
    if has_clean_id and kept_table is None:
        raise ValueError("clean_id requires kept_table for complete Run isolation")
    processing_srid = int(spatial_context.processing_srid)
    if kept_table is not None:
        return (
            "SELECT MIN(x), MAX(x), MIN(y), MAX(y) FROM ("
            f"  SELECT ST_X(ST_Transform(tp.geom, {processing_srid})) AS x, "
            f"         ST_Y(ST_Transform(tp.geom, {processing_srid})) AS y "
            f"  FROM {kept_table} AS ktp "
            f"  JOIN {table} AS tp "
            f"    ON tp.id = ktp.source_row_key::{source_key_sql_type} "
            f"  WHERE ktp.clean_id = %s "
            "  UNION ALL "
            "  SELECT ip.x, ip.y FROM injected_points AS ip "
            "  WHERE ip.clean_id = %s"
            ") AS projected_points"
        )
    raw_extent = (
        f"SELECT MIN(ST_X(ST_Transform(geom, {processing_srid}))), "
        f"MAX(ST_X(ST_Transform(geom, {processing_srid}))), "
        f"MIN(ST_Y(ST_Transform(geom, {processing_srid}))), "
        f"MAX(ST_Y(ST_Transform(geom, {processing_srid}))) "
        f"FROM {table}"
    )
    return raw_extent


def build_active_in_window_sql(
    table: str = "trajectory_points",
    *,
    has_clean_id: bool = False,
    kept_table: str | None = None,
    spatial_context: SpatialContext,
    source_key_sql_type: str = "bigint",
    source_has_extra: bool = True,
) -> str:
    """UNION ALL query for active + injected points inside a spatial bbox.

    ZH: 空间窗口内的 active + injected points UNION ALL 查询。
        原始 Point 先以 4326 ``geom`` bbox 使用现有 GiST 粗筛，再以
        ``ST_Transform(geom, processing_srid)`` 做精确窗口判断；
        若该查询成为热点，可为同一固定转换建立匹配的函数 GiST 索引。
        ``kept_table`` 非空时，trajectory 分支改为 JOIN 清洗结果表
        (``kept_table`` 存 vehicle_id/subtrack 切分，几何列取自 ``table``)，
        且 ``ktp.clean_id`` 必填，只读本次 Clean Run 的 kept 结果。

    EN: When ``kept_table`` is set, the trajectory branch JOINs the clean-result
        table (which carries the _seg-suffixed vehicle_id / subtrack splits)
        against ``table`` for geometry columns, filtered by ``ktp.clean_id``.

    Parameter placeholders (in order, caller must bind):
        Raw mode without ``clean_id``:
            1.  %s::text                         — source_id literal
            2-5. source ST_MakeEnvelope          — lon_min, lat_min, lon_max, lat_max
            6-9. processing ST_MakeEnvelope      — x_min, y_min, x_max, y_max
            injected_points are deliberately excluded because their x/y CRS
            cannot be identified without the owning Clean Run.
        Kept mode (``kept_table`` set):
            1.  %s                              — ktp.clean_id
            2-5. raw source ST_MakeEnvelope      — lon_min, lat_min, lon_max, lat_max
            6-9. raw processing ST_MakeEnvelope  — x_min, y_min, x_max, y_max
            10. %s::text                         — canonical injected source ID
            11-14. injected source envelope      — lon_min, lat_min, lon_max, lat_max
            15-18. injected x/y bounds           — x_min, y_min, x_max, y_max
            19. %s                               — injected_points.clean_id
    """
    if has_clean_id and kept_table is None:
        raise ValueError("clean_id requires kept_table for complete Run isolation")
    processing_srid = int(spatial_context.processing_srid)
    source_envelope = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
    projected_envelope = (
        f"ST_MakeEnvelope(%s, %s, %s, %s, {processing_srid})"
    )
    projected_tp = f"ST_Transform(tp.geom, {processing_srid})"
    injected_window = "x >= %s AND y >= %s AND x <= %s AND y <= %s"
    if kept_table is not None:
        extra = "tp.extra" if source_has_extra else "'{}'::jsonb"
        return (
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       source_id, source_row_key, extra, generated_reason, "
            f"       x, y, point_kind, row_id, subtrack_index, anchor_sequence "
            f"FROM ("
            f"  SELECT ktp.vehicle_id, tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, "
            f"         %s::text AS source_id, ktp.source_row_key, {extra} AS extra, "
            f"         ''::text AS generated_reason, "
            f"         ST_X({projected_tp}) AS x, ST_Y({projected_tp}) AS y, "
            f"         0 AS point_kind, ktp.id AS row_id, ktp.subtrack_index, "
            f"         ktp.sequence_in_subtrack AS anchor_sequence "
            f"  FROM {kept_table} AS ktp "
            f"  JOIN {table} AS tp "
            f"    ON tp.id = ktp.source_row_key::{source_key_sql_type} "
            f"  WHERE ktp.clean_id = %s "
            f"    AND tp.geom && {source_envelope} "
            f"    AND {projected_tp} && {projected_envelope} "
            f"  UNION ALL "
            f"  SELECT {_IP_UNIFIED_COLS} "
            f"  FROM injected_points "
            f"  WHERE geom && {source_envelope} "
            f"    AND {injected_window} AND clean_id = %s "
            f") AS combined "
            f"ORDER BY subtrack_index, anchor_sequence, point_kind, row_id"
        )
    return (
        f"SELECT {_tp_unified_cols(processing_srid)} "
        f"FROM {table} "
        f"WHERE geom && {source_envelope} "
        f"  AND ST_Transform(geom, {processing_srid}) && {projected_envelope} "
        f"ORDER BY vehicle_id, timestamp, id::text"
    )


def build_stream_all_sql(
    table: str = "trajectory_points",
    *,
    has_clean_id: bool = False,
    kept_table: str | None = None,
    spatial_context: SpatialContext,
    source_key_sql_type: str = "bigint",
    source_has_extra: bool = True,
) -> str:
    """Stream raw or one Clean Run's kept + injected points without a bbox.

    ZH: 全量来源点，或指定 Clean Run 的 kept + injected UNION ALL 查询。
        用于 server-side named cursor 逐车流式读取。
        未提供 ``clean_id`` 时不读取 injected_points，避免混入其它 Run。
        ``kept_table`` 非空时，trajectory 分支改为 JOIN 清洗结果表
        (``kept_table`` 的 vehicle_id 带 _seg 后缀，几何列取自 ``table``)，
        并按 ``ktp.clean_id`` 只读本次 Clean Run。

    EN: When ``kept_table`` is set, the trajectory branch JOINs the clean-result
        table (suffixed vehicle_id) against ``table`` for geometry columns,
        filtered by ``ktp.clean_id``.

    Parameter placeholders (in order, caller must bind):
        Raw mode (``kept_table`` is None):
            no parameters
        Kept mode (``kept_table`` set):
            1.  %s::text         — source table label for the in-memory PointRef
            2.  %s               — ktp.clean_id
            3.  %s               — injected_points.clean_id
    """
    if has_clean_id and kept_table is None:
        raise ValueError("clean_id requires kept_table for complete Run isolation")
    processing_srid = int(spatial_context.processing_srid)
    projected_tp = f"ST_Transform(tp.geom, {processing_srid})"
    if kept_table is not None:
        extra = "tp.extra" if source_has_extra else "'{}'::jsonb"
        return (
            f"SELECT ktp.vehicle_id, tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, "
            f"       %s::text AS source_id, ktp.source_row_key, {extra} AS extra, "
            f"       ''::text AS generated_reason, "
            f"       ST_X({projected_tp}) AS x, ST_Y({projected_tp}) AS y, "
            f"       0 AS point_kind, ktp.id AS row_id, ktp.subtrack_index, "
            f"       ktp.sequence_in_subtrack AS anchor_sequence "
            f"FROM {kept_table} AS ktp "
            f"JOIN {table} AS tp "
            f"  ON tp.id = ktp.source_row_key::{source_key_sql_type} "
            f"WHERE ktp.clean_id = %s "
            f"UNION ALL "
            f"SELECT {_IP_UNIFIED_COLS} "
            f"FROM injected_points WHERE clean_id = %s "
            f"ORDER BY subtrack_index, anchor_sequence, point_kind, row_id"
        )
    return (
        f"SELECT {_tp_unified_cols(processing_srid)} "
        f"FROM {table} "
        f"ORDER BY vehicle_id, timestamp, id::text"
    )


@dataclass
class SpatialWindow:
    """A single spatial cell with optional overlap padding."""
    x_min: float
    y_min: float
    x_max: float
    y_max: float


class SpatialWindowIterator:
    """Divide a spatial extent into overlapping windows.

    ZH: 将 processing CRS 米制范围划分为重叠的空间滑窗。overlap 是四向扩展比例
        （每侧扩展 overlap%），相邻窗口间实际重叠 = 2 × overlap。
    Parameters
    ----------
    extent : (x_min, y_min, x_max, y_max)
        Full processing-CRS spatial extent in metres.
    size : (width_m, height_m)
        Window size in processing-CRS metres.
    overlap : float
        Per-side expansion ratio (default 0.15 = 15% each side).
    """

    def __init__(
        self,
        extent: tuple[float, float, float, float],
        size: tuple[float, float] = (2000.0, 2000.0),
        overlap: float = 0.15,
    ) -> None:
        self._x_min, self._y_min, self._x_max, self._y_max = extent
        self._width_m, self._height_m = size
        self._overlap = overlap

        self._step_x = self._width_m
        self._step_y = self._height_m
        self._pad_x = self._step_x * overlap
        self._pad_y = self._step_y * overlap

    def iter_windows(self) -> Iterator[SpatialWindow]:
        """Yield overlapping spatial windows covering the full extent."""
        x_count, y_count = self.window_shape()
        for x_index in range(x_count):
            x = self._x_min + x_index * self._step_x
            for y_index in range(y_count):
                y = self._y_min + y_index * self._step_y
                yield SpatialWindow(
                    x_min=max(self._x_min, x - self._pad_x),
                    y_min=max(self._y_min, y - self._pad_y),
                    x_max=min(
                        self._x_max,
                        self._x_min + (x_index + 1) * self._step_x + self._pad_x,
                    ),
                    y_max=min(
                        self._y_max,
                        self._y_min + (y_index + 1) * self._step_y + self._pad_y,
                    ),
                )

    def window_shape(self) -> tuple[int, int]:
        """Return deterministic ``(x_count, y_count)``."""
        if self._x_max <= self._x_min or self._y_max <= self._y_min:
            return 0, 0
        return (
            self._axis_count(self._x_min, self._x_max, self._step_x),
            self._axis_count(self._y_min, self._y_max, self._step_y),
        )

    @staticmethod
    def _axis_count(axis_min: float, axis_max: float, step: float) -> int:
        """Return the smallest count whose indexed cells cover ``axis_max``."""
        count = max(1, math.ceil((axis_max - axis_min) / step))
        while count > 1 and axis_min + (count - 1) * step >= axis_max:
            count -= 1
        while axis_min + count * step < axis_max:
            count += 1
        return count

    def count_windows(self) -> int:
        """Return the number of windows that :meth:`iter_windows` will yield."""
        x_count, y_count = self.window_shape()
        return x_count * y_count


def _query_spatial_extent(
    conn: Any,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_table: str | None = None,
    clean_id: str | None = None,
    spatial_context: SpatialContext,
) -> tuple[float, float, float, float] | None:
    """Query MIN/MAX processing-CRS x/y of active trajectory points.

    ZH: kept 模式 (``kept_table`` 非空) 时 JOIN 清洗结果表并绑定
        ``ktp.clean_id``。
    Returns ``(x_min, y_min, x_max, y_max)`` compatible with
    ``SpatialWindowIterator``.
    """
    if kept_table is not None and clean_id is None:
        raise ValueError("kept_table requires clean_id")
    if kept_table is None and clean_id is not None:
        raise ValueError("clean_id requires kept_table for complete Run isolation")
    contract = validate_source_table(conn, table)
    sql = build_spatial_extent_sql(
        contract.quoted_table,
        has_clean_id=clean_id is not None,
        kept_table=kept_table,
        spatial_context=spatial_context,
        source_key_sql_type=contract.key_sql_type,
    )
    if kept_table is not None:
        params: tuple[Any, ...] = (clean_id, clean_id)
    else:
        params = ()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    # SQL returns (min_x, max_x, min_y, max_y).
    return float(row[0]), float(row[2]), float(row[1]), float(row[3])


def _query_active_points_in_window(
    conn: Any,
    window: SpatialWindow,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_table: str | None = None,
    clean_id: str | None = None,
    spatial_context: SpatialContext,
    telemetry: dict[str, int | float] | None = None,
    _transformer: CoordinateTransformer | None = None,
) -> list[TrajectoryPoint]:
    """Query active trajectory points + injected points in a spatial window.

    ZH: 先以反投影并加密边界的 4326 bbox 使用原始 Geometry GiST 粗筛，
        再在 processing CRS 中对 Point 执行精确窗口判断，并以 UNION ALL
        合并两表、一次往返。只有性能分析确认热点后，才应为完全相同的
        ST_Transform 表达式建立函数索引。
        kept 模式 (``kept_table`` 非空) 时 trajectory 分支 JOIN 清洗结果表，
        首参绑定 ``ktp.clean_id``（而非 raw 模式的 source_id）。
    """
    rows: list[dict[str, Any]] = []
    has_clean_id = clean_id is not None
    if kept_table is not None and clean_id is None:
        raise ValueError("kept_table requires clean_id")
    if kept_table is None and clean_id is not None:
        raise ValueError("clean_id requires kept_table for complete Run isolation")
    transformer = _transformer or CoordinateTransformer(spatial_context)
    contract = validate_source_table(conn, table)
    source_bounds = transformer.inverse_bounds(
        (window.x_min, window.y_min, window.x_max, window.y_max)
    )
    if kept_table is not None:
        params: list[Any] = [source_table or table, clean_id]
        params.extend(source_bounds)
        params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
        params.append(_INJECTED_SOURCE_ID)
        params.extend(source_bounds)
        params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
        params.append(clean_id)
    else:
        params = [source_table or ""]
        params.extend(source_bounds)
        params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
    query_started = time.perf_counter() if telemetry is not None else 0.0
    with conn.cursor() as cur:
        cur.execute(
            build_active_in_window_sql(
                contract.quoted_table,
                has_clean_id=has_clean_id,
                kept_table=kept_table,
                spatial_context=spatial_context,
                source_key_sql_type=contract.key_sql_type,
                source_has_extra=contract.has_extra,
            ),
            params,
        )
        for row in cur:
            rows.append(row_to_dict(row))

    # DB ORDER BY 已保证 (vehicle_id, timestamp) 有序，无需 Python 侧再排
    if telemetry is not None:
        telemetry["query_seconds"] = telemetry.get("query_seconds", 0.0) + (
            time.perf_counter() - query_started
        )
        telemetry["queried_rows"] = telemetry.get("queried_rows", 0) + len(rows)
        materialization_started = time.perf_counter()
    points = _build_points_from_rows(rows)
    if telemetry is not None:
        telemetry["materialization_seconds"] = telemetry.get(
            "materialization_seconds", 0.0
        ) + (time.perf_counter() - materialization_started)
    return points


def _stream_vehicle_tracks_from_db(
    conn: Any,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_table: str | None = None,
    clean_id: str | None = None,
    spatial_context: SpatialContext,
) -> Iterator[list[TrajectoryPoint]]:
    """Stream tracks one vehicle at a time from DB (global, no spatial filter).

    ZH: 从 DB 逐车流式读取轨迹点。使用 UNION ALL 合并 active points 和
        injected_points，按 (vehicle_id, timestamp) 排序后检测 vehicle_id
        变化来分组。内存占用 = 单车辆最大点数。
        kept 模式 (``kept_table`` 非空) 时 trajectory 分支 JOIN 清洗结果表，
        绑定 ``ktp.clean_id`` 与 ``injected_points.clean_id``。
    """
    has_clean_id = clean_id is not None
    if kept_table is not None and clean_id is None:
        raise ValueError("kept_table requires clean_id")
    if kept_table is None and clean_id is not None:
        raise ValueError("clean_id requires kept_table for complete Run isolation")
    if kept_table is not None:
        params: list[Any] = [
            source_table or table,
            clean_id,
            _INJECTED_SOURCE_ID,
            clean_id,
        ]
    else:
        params = [source_table or ""]
    contract = validate_source_table(conn, table)
    # ZH: 服务器端命名游标 — PG 按需 FETCH，避免全量物化到 portal。
    # EN: Server-side named cursor — FETCHes on demand, no full portal materialization.
    with conn.cursor(
        name=f"stream_tracks_{uuid.uuid4().hex}",
    ) as cur:
        cur.execute(
            build_stream_all_sql(
                contract.quoted_table,
                has_clean_id=has_clean_id,
                kept_table=kept_table,
                spatial_context=spatial_context,
                source_key_sql_type=contract.key_sql_type,
                source_has_extra=contract.has_extra,
            ),
            params,
        )
        current_subtrack: int | None = None
        current_track: list[TrajectoryPoint] = []
        cleaned_sequence = 0
        for row in cur:
            vid = str(row[0])
            subtrack_index = int(row[14])
            if current_subtrack is not None and subtrack_index != current_subtrack:
                yield current_track
                current_track = []
                cleaned_sequence = 0
            current_subtrack = subtrack_index
            is_injected = int(row[12]) == 1
            current_track.append(TrajectoryPoint(
                point_ref=PointRef(
                    source_id=str(row[6] or ""),
                    source_row_key=str(row[7] or ""),
                    stable=not is_injected,
                    generated_reason=str(row[9] or ""),
                ),
                vehicle_id=vid,
                timestamp=float(row[1]),
                lon=float(row[2]),
                lat=float(row[3]),
                heading=float(row[4]),
                speed=float(row[5]),
                extra=row[8] if isinstance(row[8], dict) else {},
                x=float(row[10]),
                y=float(row[11]),
                cleaned_point_ref=CleanedPointRef(
                    point_kind="injected" if is_injected else "kept",
                    point_id=int(row[13]),
                    subtrack_index=subtrack_index,
                    cleaned_sequence_in_subtrack=cleaned_sequence,
                ),
            ))
            cleaned_sequence += 1
        if current_track:
            yield current_track


def _query_active_points_near_cluster(
    conn: Any,
    cluster: HotspotCluster,
    radius_m: float,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_table: str | None = None,
    clean_id: str | None = None,
    spatial_context: SpatialContext,
) -> list[TrajectoryPoint]:
    """Query active points within *radius_m* of a cluster centroid.

    ZH: 委托 ``_query_active_points_in_window``，将 cluster centroid + radius_m
        转换为 SpatialWindow 后在 processing CRS 中查询。自动获得 UNION ALL
        一次往返、DB 侧 ORDER BY（无需 Python 排序），且 telemetry 可选跟踪。
    """
    window = SpatialWindow(
        x_min=cluster.centroid_x - radius_m,
        y_min=cluster.centroid_y - radius_m,
        x_max=cluster.centroid_x + radius_m,
        y_max=cluster.centroid_y + radius_m,
    )
    return _query_active_points_in_window(
        conn,
        window,
        table=table,
        kept_table=kept_table,
        source_table=source_table,
        clean_id=clean_id,
        spatial_context=spatial_context,
    )


def _build_points_from_rows(rows: list[dict[str, Any]]) -> list[TrajectoryPoint]:
    """Convert DB row dicts to TrajectoryPoint objects."""
    points: list[TrajectoryPoint] = []
    cleaned_sequences: dict[int, int] = {}
    for r in rows:
        subtrack_index = int(r["subtrack_index"])
        cleaned_sequence = cleaned_sequences.get(subtrack_index, 0)
        is_injected = int(r["point_kind"]) == 1
        points.append(TrajectoryPoint(
            point_ref=PointRef(
                source_id=r["source_id"],
                source_row_key=r["source_row_key"],
                stable=not is_injected,
                generated_reason=str(r.get("generated_reason") or ""),
            ),
            vehicle_id=r["vehicle_id"],
            timestamp=r["timestamp"],
            lon=r["lon"],
            lat=r["lat"],
            heading=r["heading"],
            speed=r["speed"],
            extra=r["extra"],
            x=r["x"],
            y=r["y"],
            cleaned_point_ref=CleanedPointRef(
                point_kind="injected" if is_injected else "kept",
                point_id=int(r["row_id"]),
                subtrack_index=subtrack_index,
                cleaned_sequence_in_subtrack=cleaned_sequence,
            ),
        ))
        cleaned_sequences[subtrack_index] = cleaned_sequence + 1
    return points


def _tracks_from_points(points: list[TrajectoryPoint]) -> dict[str, list[TrajectoryPoint]]:
    """Group points by globally unique Clean subtrack, retaining vehicle IDs."""
    tracks: dict[str, list[TrajectoryPoint]] = {}
    for tp in points:
        subtrack_index = (
            tp.cleaned_point_ref.subtrack_index
            if tp.cleaned_point_ref is not None
            else 1
        )
        key = f"{tp.vehicle_id}\x00{subtrack_index}"
        tracks.setdefault(key, []).append(tp)
    return tracks


@dataclass(frozen=True, slots=True)
class _RunRow:
    """One Detect Run together with its parent Clean Run metadata."""

    clean_id: str
    source_table: str
    parameters: dict[str, object]
    status: str
    spatial_context: SpatialContext


def fetch_detect_row(conn: object, detect_id: str) -> _RunRow:
    """Fetch a Detect Run and the source metadata of its parent Clean Run."""
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT d.clean_id, c.source_table, c.parameters, d.status, "
            "c.source_srid, c.processing_srid, c.projection_config "
            "FROM detect_runs AS d JOIN clean_runs AS c ON c.id = d.clean_id "
            "WHERE d.id = %s",
            (detect_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Detect Run: {detect_id}")
    clean_id, source_table, raw_parameters, status, source_srid, processing_srid, projection_config = row
    return _RunRow(
        clean_id=str(clean_id),
        source_table=str(source_table),
        parameters=_decode_json_object(raw_parameters),
        status=str(status),
        spatial_context=spatial_context_from_values(
            source_srid,
            processing_srid,
            projection_config,
        ),
    )


def fetch_intersections(conn: object, detect_id: str) -> list[ClipIntersection]:
    """Load verified intersections belonging to one Detect Run."""
    intersections: list[ClipIntersection] = []
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT i.id, ci.centroid_lon, ci.centroid_lat, i.clip_radius_m "
            "FROM intersections AS i "
            "JOIN candidate_intersections AS ci "
            "ON ci.id = i.candidate_intersection_id AND ci.detect_id = i.detect_id "
            "WHERE i.detect_id = %s "
            "ORDER BY i.id",
            (detect_id,),
        )
        for intersection_id, lon, lat, radius_m in cur:
            if radius_m is None:
                raise ValueError(
                    f"Detect Run {detect_id} is missing its Clip radius"
                )
            if not math.isfinite(float(radius_m)) or float(radius_m) <= 0.0:
                raise ValueError(f"Detect Run {detect_id} has an invalid Clip radius")
            intersections.append(
                ClipIntersection(
                    intersection_id=int(intersection_id),
                    lon=float(lon),
                    lat=float(lat),
                    radius_m=float(radius_m),
                )
            )
    return intersections


def fetch_resolved_clip_tracks(
    conn: object,
    detect_id: str,
) -> list[ClipTrack]:
    """Load the final p/i/c sequence for Clip without reconstructing roots."""
    run = fetch_detect_row(conn, detect_id)
    if run.status != "completed":
        raise ValueError(f"Detect Run {detect_id} is not completed")
    contract = validate_source_table(conn, run.source_table)
    query = f"""
        WITH subtrack_vehicles AS (
            SELECT resolved.subtrack_index,
                   MIN(COALESCE(kept.vehicle_id, injected.vehicle_id)) AS vehicle_id,
                   COUNT(DISTINCT COALESCE(kept.vehicle_id, injected.vehicle_id))
                       AS vehicle_count
              FROM resolved_subtrack_points AS resolved
              LEFT JOIN kept_trajectory_points AS kept
                ON kept.id = resolved.kept_point_id
              LEFT JOIN injected_points AS injected
                ON injected.id = resolved.injected_point_id
             WHERE resolved.detect_id = %s
               AND resolved.crossing_point_id IS NULL
             GROUP BY resolved.subtrack_index
        )
        SELECT resolved.id,
               resolved.subtrack_index,
               resolved.sequence_in_subtrack,
               vehicles.vehicle_id,
               COALESCE(source.lon, injected.lon, crossing.crossing_lon) AS lon,
               COALESCE(source.lat, injected.lat, crossing.crossing_lat) AS lat,
               crossing.id AS crossing_id,
               intersection.id AS intersection_id,
               crossing.crossing_type
          FROM resolved_subtrack_points AS resolved
          JOIN subtrack_vehicles AS vehicles
            ON vehicles.subtrack_index = resolved.subtrack_index
           AND vehicles.vehicle_count = 1
          LEFT JOIN kept_trajectory_points AS kept
            ON kept.id = resolved.kept_point_id
          LEFT JOIN {contract.quoted_table} AS source
            ON source.id = kept.source_row_key::{contract.key_sql_type}
          LEFT JOIN injected_points AS injected
            ON injected.id = resolved.injected_point_id
          LEFT JOIN candidate_entry_exit_points AS crossing
            ON crossing.id = resolved.crossing_point_id
           AND crossing.detect_id = resolved.detect_id
          LEFT JOIN intersections AS intersection
            ON intersection.detect_id = crossing.detect_id
           AND intersection.candidate_intersection_id =
               crossing.candidate_intersection_id
         WHERE resolved.detect_id = %s
         ORDER BY resolved.subtrack_index, resolved.sequence_in_subtrack
    """
    grouped: dict[int, tuple[str, list[ResolvedTrackPoint]]] = {}
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(query, (detect_id, detect_id))
        for row in cur:
            if row[3] is None or row[4] is None or row[5] is None:
                raise ValueError(
                    f"Detect Run {detect_id} has missing resolved point data"
                )
            subtrack_index = int(row[1])
            vehicle_id = str(row[3])
            previous = grouped.get(subtrack_index)
            if previous is None:
                points: list[ResolvedTrackPoint] = []
                grouped[subtrack_index] = (vehicle_id, points)
            else:
                if previous[0] != vehicle_id:
                    raise ValueError(
                        f"subtrack {subtrack_index} contains multiple vehicles"
                    )
                points = previous[1]
            points.append(ResolvedTrackPoint(
                resolved_point_id=int(row[0]),
                lon=float(row[4]),
                lat=float(row[5]),
                crossing_id=None if row[6] is None else int(row[6]),
                intersection_id=None if row[7] is None else int(row[7]),
                crossing_type=None if row[8] is None else int(row[8]),
            ))
    return [
        ClipTrack(
            vehicle_id=vehicle_id,
            trip_id=f"{vehicle_id}:subtrack-{subtrack_index:04d}",
            points=tuple(points),
            subtrack_index=subtrack_index,
        )
        for subtrack_index, (vehicle_id, points) in sorted(grouped.items())
    ]


def fetch_clean_points(
    conn: object,
    table: str,
    clean_id: str,
    *,
    spatial_context: SpatialContext,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Load one Clean Run's frozen kept members and injected points."""
    contract = validate_source_table(conn, table)
    processing_srid = int(spatial_context.processing_srid)
    extra = "tp.extra" if contract.has_extra else "'{}'::jsonb"
    sql = (
        "SELECT vehicle_id, subtrack_index, anchor_sequence, point_kind, row_id, "
        "source_id, source_row_key, timestamp, lon, lat, heading, speed, extra, "
        "generated_reason, x, y "
        "FROM ("
        "  SELECT ktp.vehicle_id, ktp.subtrack_index, ktp.sequence_in_subtrack AS anchor_sequence, "
        "         0 AS point_kind, ktp.id AS row_id, "
        "         %s::text AS source_id, ktp.source_row_key, "
        f"         tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, {extra} AS extra, "
        "         ''::text AS generated_reason, "
        f"         ST_X(ST_Transform(tp.geom, {processing_srid})) AS x, "
        f"         ST_Y(ST_Transform(tp.geom, {processing_srid})) AS y "
        "  FROM kept_trajectory_points AS ktp "
        f"  JOIN {contract.quoted_table} AS tp "
        f"    ON tp.id = ktp.source_row_key::{contract.key_sql_type} "
        "  WHERE ktp.clean_id = %s "
        "  UNION ALL "
        "  SELECT ip.vehicle_id, ip.subtrack_index, ip.after_sequence_in_subtrack, "
        "         1 AS point_kind, ip.id AS row_id, "
        "         %s::text AS source_id, ip.id::text AS source_row_key, "
        "         ip.timestamp, ip.lon, ip.lat, ip.heading, ip.speed, '{}'::jsonb, "
        "         ip.generated_reason, ip.x, ip.y "
        "  FROM injected_points AS ip WHERE ip.clean_id = %s"
        ") AS clean_points"
    )
    return _group_points(
        conn,
        sql,
        (table, clean_id, _INJECTED_SOURCE_ID, clean_id),
        clean_id,
    )


def fetch_clean_points_for_subtracks(
    conn: object,
    table: str,
    clean_id: str,
    subtracks: set[tuple[str, int]],
    *,
    spatial_context: SpatialContext,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Load only the frozen Clean Run subtracks named by a replay manifest."""
    if not subtracks:
        return {}
    requested = sorted(subtracks)
    vehicle_ids = [vehicle_id for vehicle_id, _ in requested]
    subtrack_indexes = [subtrack_index for _, subtrack_index in requested]
    contract = validate_source_table(conn, table)
    processing_srid = int(spatial_context.processing_srid)
    extra = "tp.extra" if contract.has_extra else "'{}'::jsonb"
    sql = (
        "WITH requested(vehicle_id, subtrack_index) AS ("
        "  SELECT * FROM unnest(%s::text[], %s::integer[])"
        ") "
        "SELECT vehicle_id, subtrack_index, anchor_sequence, point_kind, row_id, "
        "source_id, source_row_key, timestamp, lon, lat, heading, speed, extra, "
        "generated_reason, x, y "
        "FROM ("
        "  SELECT ktp.vehicle_id, ktp.subtrack_index, ktp.sequence_in_subtrack AS anchor_sequence, "
        "         0 AS point_kind, ktp.id AS row_id, "
        "         %s::text AS source_id, ktp.source_row_key, "
        f"         tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, {extra} AS extra, "
        "         ''::text AS generated_reason, "
        f"         ST_X(ST_Transform(tp.geom, {processing_srid})) AS x, "
        f"         ST_Y(ST_Transform(tp.geom, {processing_srid})) AS y "
        "  FROM kept_trajectory_points AS ktp "
        "  JOIN requested AS r USING (vehicle_id, subtrack_index) "
        f"  JOIN {contract.quoted_table} AS tp "
        f"    ON tp.id = ktp.source_row_key::{contract.key_sql_type} "
        "  WHERE ktp.clean_id = %s "
        "  UNION ALL "
        "  SELECT ip.vehicle_id, ip.subtrack_index, ip.after_sequence_in_subtrack, "
        "         1 AS point_kind, ip.id AS row_id, "
        "         %s::text AS source_id, ip.id::text AS source_row_key, "
        "         ip.timestamp, ip.lon, ip.lat, ip.heading, ip.speed, '{}'::jsonb, "
        "         ip.generated_reason, ip.x, ip.y "
        "  FROM injected_points AS ip "
        "  JOIN requested AS r USING (vehicle_id, subtrack_index) "
        "  WHERE ip.clean_id = %s"
        ") AS clean_points"
    )
    return _group_points(
        conn,
        sql,
        (
            vehicle_ids,
            subtrack_indexes,
            table,
            clean_id,
            _INJECTED_SOURCE_ID,
            clean_id,
        ),
        clean_id,
    )


def _group_points(
    conn: object,
    sql: str,
    params: tuple[object, ...],
    clean_id: str,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Execute *sql* and locally order points within each subtrack.

    Avoiding a global PostgreSQL sort is material for large Clean runs: the join
    rows are wide and routinely spill hundreds of megabytes to temporary files.
    Subtracks are small, so ordering their members independently in Python has
    the same semantics with much lower sort pressure.
    """
    grouped_rows: dict[
        tuple[str, int],
        list[tuple[int, int, int, TrajectoryPoint]],
    ] = {}
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(sql, params)
        for row in cur:
            vehicle_id = str(row[0])
            subtrack_index = int(row[1])
            if row[5] is None or row[6] is None:
                raise ValueError(
                    f"Clean Run {clean_id} has missing source references"
                )
            if any(row[index] is None for index in range(7, 12)):
                raise ValueError(
                    f"Clean Run {clean_id} references a missing source row"
                )
            generated_reason = str(row[13])
            is_injected = int(row[3]) == 1
            point = TrajectoryPoint(
                point_ref=PointRef(
                    "public.injected_points" if is_injected else str(row[5]),
                    str(row[4]) if is_injected else str(row[6]),
                    stable=not generated_reason,
                    generated_reason=generated_reason,
                ),
                vehicle_id=vehicle_id,
                timestamp=float(row[7]),
                lon=float(row[8]),
                lat=float(row[9]),
                heading=float(row[10]),
                speed=float(row[11]),
                extra=row[12] if isinstance(row[12], dict) else {},
                x=float(row[14]),
                y=float(row[15]),
            )
            grouped_rows.setdefault((vehicle_id, subtrack_index), []).append(
                (int(row[2]), int(row[3]), int(row[4]), point)
            )
    grouped: dict[tuple[str, int], list[TrajectoryPoint]] = {}
    for key, rows in grouped_rows.items():
        ordered = sorted(rows, key=lambda item: item[:3])
        grouped[key] = [
            TrajectoryPoint(
                point_ref=item[3].point_ref,
                vehicle_id=item[3].vehicle_id,
                timestamp=item[3].timestamp,
                lon=item[3].lon,
                lat=item[3].lat,
                heading=item[3].heading,
                speed=item[3].speed,
                extra=item[3].extra,
                x=item[3].x,
                y=item[3].y,
                cleaned_point_ref=CleanedPointRef(
                    point_kind="injected" if item[1] == 1 else "kept",
                    point_id=item[2],
                    subtrack_index=key[1],
                    cleaned_sequence_in_subtrack=sequence,
                ),
            )
            for sequence, item in enumerate(ordered)
        ]
    return grouped


def _decode_json_object(value: object) -> dict[str, object]:
    """Decode PostgreSQL JSONB values returned as dicts or JSON strings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("Clean Run parameters must be a JSON object")


def _quoted_identifier(value: str) -> str:
    """Quote a validated, optionally schema-qualified SQL identifier."""
    parts = value.split(".")
    if not parts or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None for part in parts
    ):
        raise ValueError(f"invalid trajectory table name: {value!r}")
    return ".".join(f'"{part}"' for part in parts)
