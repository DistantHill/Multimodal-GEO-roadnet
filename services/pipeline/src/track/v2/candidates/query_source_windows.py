"""Read refined source points in Detect's spatial candidate windows."""

from __future__ import annotations

import math
import time
import numpy as np
from ..trajectories.subtrack_columns import refinement_subtrack_from_columns
from typing import Any, Iterator

from ..telemetry.heartbeat import observed_operation, heartbeat_progress
from ..models import (
    HotspotCluster,
    SpatialContext,
    RefinementSubtrack,
)
from ..projection import (
    CoordinateTransformer,
    QueryWindow,
    SpatialWindow,
    query_window_from_processing_bounds,
)
from ..db.source_table import validate_source_table
from ..db._point_query_fragments import (
    _INJECTED_SOURCE_ID,
    _IP_UNIFIED_COLS,
    _tp_unified_cols,
)



def build_spatial_extent_sql(
    table: str = "trajectory_points",
    *,
    has_refine_id: bool = False,
    kept_table: str | None = None,
    spatial_context: SpatialContext,
    source_key_sql_type: str = "bigint",
) -> str:
    """MIN/MAX processing-CRS x/y of active points.

    ZH: 查询 active points 的空间范围 (lon_min, lat_min, lon_max, lat_max)。
        ``kept_table`` 非空时 JOIN 精炼结果表（几何列取自 ``table``），
        按 ``ktp.refine_id`` 只读本次 Refine Run 的 kept 结果。
    EN: When ``kept_table`` is set, JOIN the refine-result table against
        ``table`` for the geometry columns, filtered by ``ktp.refine_id``.

    Parameter placeholders (in order, caller must bind):
        Kept mode only:
            1. %s — ktp.refine_id
            2. %s — injected_points.refine_id
    """
    if has_refine_id and kept_table is None:
        raise ValueError("refine_id requires kept_table for complete Run isolation")
    processing_srid = int(spatial_context.processing_srid)
    if kept_table is not None:
        return (
            "SELECT MIN(x), MAX(x), MIN(y), MAX(y) FROM ("
            f"  SELECT ST_X(ST_Transform(tp.geom, {processing_srid})) AS x, "
            f"         ST_Y(ST_Transform(tp.geom, {processing_srid})) AS y "
            f"  FROM {kept_table} AS ktp "
            f"  JOIN {table} AS tp "
            f"    ON tp.id = ktp.source_row_key "
            f"  WHERE ktp.refine_id = %s "
            "  UNION ALL "
            "  SELECT ip.x, ip.y FROM injected_points AS ip "
            "  WHERE ip.refine_id = %s"
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
    has_refine_id: bool = False,
    kept_table: str | None = None,
    spatial_context: SpatialContext,
    query_srid: int,
    source_key_sql_type: str = "bigint",
    source_has_extra: bool = True,
) -> str:
    """UNION ALL query for active + injected points inside a spatial bbox.

    ZH: 空间窗口内的 active + injected points UNION ALL 查询。
        原始 Point 先以 4326 ``geom`` bbox 使用现有 GiST 粗筛，再以
        ``ST_Transform(geom, query_srid)`` 做数值闭区间精确判断；
        若该查询成为热点，可为同一固定转换建立匹配的函数 GiST 索引。
        ``kept_table`` 非空时，trajectory 分支改为 JOIN 精炼结果表
        (``kept_table`` 存 vehicle_id/subtrack 切分，几何列取自 ``table``)，
        且 ``ktp.refine_id`` 必填，只读本次 Refine Run 的 kept 结果。

    EN: When ``kept_table`` is set, the trajectory branch JOINs the refine-result
        table (which carries the _seg-suffixed vehicle_id / subtrack splits)
        against ``table`` for geometry columns, filtered by ``ktp.refine_id``.

    Parameter placeholders (in order, caller must bind):
        Raw mode without ``refine_id``:
            1.  %s::text                         — source_id literal
            2-5. source ST_MakeEnvelope          — lon_min, lat_min, lon_max, lat_max
            6-9. query-CRS numeric bounds        — x_min, y_min, x_max, y_max
            injected_points are deliberately excluded because their x/y CRS
            cannot be identified without the owning Refine Run.
        Kept mode (``kept_table`` set):
            1.  %s                              — ktp.refine_id
            2-5. raw source ST_MakeEnvelope      — lon_min, lat_min, lon_max, lat_max
            6-9. raw query-CRS numeric bounds    — x_min, y_min, x_max, y_max
            10. %s::text                         — canonical injected source ID
            11-14. injected source envelope      — lon_min, lat_min, lon_max, lat_max
            15-18. injected query-CRS bounds     — x_min, y_min, x_max, y_max
            19. %s                               — injected_points.refine_id
    """
    if has_refine_id and kept_table is None:
        raise ValueError("refine_id requires kept_table for complete Run isolation")
    processing_srid = int(spatial_context.processing_srid)
    selected_query_srid = int(query_srid)
    source_envelope = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
    projected_tp = f"ST_Transform(tp.geom, {processing_srid})"
    query_tp = f"ST_Transform(tp.geom, {selected_query_srid})"
    query_ip = f"ST_Transform(geom, {selected_query_srid})"
    tp_window = (
        f"ST_X({query_tp}) >= %s AND ST_Y({query_tp}) >= %s AND "
        f"ST_X({query_tp}) <= %s AND ST_Y({query_tp}) <= %s"
    )
    injected_window = (
        f"ST_X({query_ip}) >= %s AND ST_Y({query_ip}) >= %s AND "
        f"ST_X({query_ip}) <= %s AND ST_Y({query_ip}) <= %s"
    )
    if kept_table is not None:
        extra = "tp.extra" if source_has_extra else "'{}'::jsonb"
        return (
            f"SELECT vehicle_id, timestamp, lon, lat, heading, speed, "
            f"       source_id, source_row_key, extra, generated_reason, "
            f"       x, y, point_kind, row_id, subtrack_index, anchor_sequence "
            f"FROM ("
            f"  SELECT ktp.vehicle_id, tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, "
            f"         %s::text AS source_id, ktp.source_row_key::bigint AS source_row_key, {extra} AS extra, "
            f"         ''::text AS generated_reason, "
            f"         ST_X({projected_tp}) AS x, ST_Y({projected_tp}) AS y, "
            f"         0 AS point_kind, ktp.id AS row_id, ktp.subtrack_index, "
            f"         ktp.sequence_in_subtrack AS anchor_sequence "
            f"  FROM {kept_table} AS ktp "
            f"  JOIN {table} AS tp "
            f"    ON tp.id = ktp.source_row_key "
            f"  WHERE ktp.refine_id = %s "
            f"    AND tp.geom && {source_envelope} "
            f"    AND {tp_window} "
            f"  UNION ALL "
            f"  SELECT {_IP_UNIFIED_COLS} "
            f"  FROM injected_points "
            f"  WHERE geom && {source_envelope} "
            f"    AND {injected_window} AND refine_id = %s "
            f") AS combined "
            f"ORDER BY subtrack_index, anchor_sequence, point_kind, row_id"
        )
    return (
        f"SELECT {_tp_unified_cols(processing_srid)} "
        f"FROM {table} "
        f"WHERE geom && {source_envelope} "
        f"  AND ST_X(ST_Transform(geom, {selected_query_srid})) >= %s "
        f"  AND ST_Y(ST_Transform(geom, {selected_query_srid})) >= %s "
        f"  AND ST_X(ST_Transform(geom, {selected_query_srid})) <= %s "
        f"  AND ST_Y(ST_Transform(geom, {selected_query_srid})) <= %s "
        f"ORDER BY vehicle_id, timestamp, id::text"
    )

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

@observed_operation("查询轨迹空间范围")
def _query_spatial_extent(
    conn: Any,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_table: str | None = None,
    refine_id: str | None = None,
    spatial_context: SpatialContext,
) -> tuple[float, float, float, float] | None:
    """Query MIN/MAX processing-CRS x/y of active trajectory points.

    ZH: kept 模式 (``kept_table`` 非空) 时 JOIN 精炼结果表并绑定
        ``ktp.refine_id``。
    Returns ``(x_min, y_min, x_max, y_max)`` compatible with
    ``SpatialWindowIterator``.
    """
    if kept_table is not None and refine_id is None:
        raise ValueError("kept_table requires refine_id")
    if kept_table is None and refine_id is not None:
        raise ValueError("refine_id requires kept_table for complete Run isolation")
    contract = validate_source_table(conn, table)
    sql = build_spatial_extent_sql(
        contract.quoted_table,
        has_refine_id=refine_id is not None,
        kept_table=kept_table,
        spatial_context=spatial_context,
        source_key_sql_type=contract.key_sql_type,
    )
    if kept_table is not None:
        params: tuple[Any, ...] = (refine_id, refine_id)
    else:
        params = ()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    # SQL returns (min_x, max_x, min_y, max_y).
    return float(row[0]), float(row[2]), float(row[1]), float(row[3])

@observed_operation("查询窗口内轨迹点")
def _query_active_points_in_window(
    conn: Any,
    window: SpatialWindow | QueryWindow,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_table: str | None = None,
    refine_id: str | None = None,
    spatial_context: SpatialContext,
    telemetry: dict[str, int | float] | None = None,
    _transformer: CoordinateTransformer | None = None,
) -> list[RefinementSubtrack]:
    """Query active trajectory points + injected points in a spatial window.

    ZH: 先以反投影并加密边界的 4326 bbox 使用原始 Geometry GiST 粗筛，
        再在 processing CRS 中对 Point 执行精确窗口判断，并以 UNION ALL
        合并两表、一次往返。只有性能分析确认热点后，才应为完全相同的
        ST_Transform 表达式建立函数索引。
        kept 模式 (``kept_table`` 非空) 时 trajectory 分支 JOIN 精炼结果表，
        首参绑定 ``ktp.refine_id``（而非 raw 模式的 source_id）。
    """
    grouped_columns: dict[tuple[str, int], list[list]] = {}
    row_count = 0
    has_refine_id = refine_id is not None
    if kept_table is not None and refine_id is None:
        raise ValueError("kept_table requires refine_id")
    if kept_table is None and refine_id is not None:
        raise ValueError("refine_id requires kept_table for complete Run isolation")
    query_window = (
        window
        if isinstance(window, QueryWindow)
        else query_window_from_processing_bounds(
            (window.x_min, window.y_min, window.x_max, window.y_max),
            spatial_context,
        )
    )
    contract = validate_source_table(conn, table)
    source_bounds = query_window.prefilter_bounds
    query_bounds = (
        query_window.x_min, query_window.y_min,
        query_window.x_max, query_window.y_max,
    )
    if kept_table is not None:
        params: list[Any] = [source_table or table, refine_id]
        params.extend(source_bounds)
        params.extend(query_bounds)
        params.append(_INJECTED_SOURCE_ID)
        params.extend(source_bounds)
        params.extend(query_bounds)
        params.append(refine_id)
    else:
        params = [source_table or ""]
        params.extend(source_bounds)
        params.extend(query_bounds)
    query_started = time.perf_counter() if telemetry is not None else 0.0
    with conn.cursor() as cur:
        cur.execute(
            build_active_in_window_sql(
                contract.quoted_table,
                has_refine_id=has_refine_id,
                kept_table=kept_table,
                spatial_context=spatial_context,
                query_srid=query_window.query_srid,
                source_key_sql_type=contract.key_sql_type,
                source_has_extra=contract.has_extra,
            ),
            params,
        )
        for row_count, row in enumerate(cur, 1):
            if row_count % 10_000 == 0:
                heartbeat_progress(row_count)
            key = (str(row[0]), int(row[14]))
            columns = grouped_columns.setdefault(key, [[] for _ in range(16)])
            for column, item in zip(columns, row):
                column.append(item)

    # DB ORDER BY 已保证 (vehicle_id, timestamp) 有序，无需 Python 侧再排
    if telemetry is not None:
        telemetry["query_seconds"] = telemetry.get("query_seconds", 0.0) + (
            time.perf_counter() - query_started
        )
        telemetry["queried_rows"] = telemetry.get("queried_rows", 0) + row_count
        materialization_started = time.perf_counter()
    points = _build_tracks_from_columns(grouped_columns, raw=kept_table is None)
    _validate_materialized_points(points, spatial_context)
    if telemetry is not None:
        telemetry["materialization_seconds"] = telemetry.get(
            "materialization_seconds", 0.0
        ) + (time.perf_counter() - materialization_started)
    return points

def _validate_materialized_points(
    points: list[RefinementSubtrack],
    spatial_context: SpatialContext,
) -> None:
    """Validate exactly the rows read by a raw/diagnostic window query."""
    if not points:
        return
    bounds=None
    for track in points:
        if not (np.isfinite(track.xy).all() and np.isfinite(track.lonlat).all()): raise ValueError("queried geometry contains non-finite coordinates")
        lon=track.lonlat[:,0]; lat=track.lonlat[:,1]
        if np.any((lon < -180)|(lon > 180)|(lat < -90)|(lat > 90)): raise ValueError("queried coordinates outside EPSG:4326")
        if len(track):
            current=(float(lon.min()),float(lat.min()),float(lon.max()),float(lat.max()))
            bounds=current if bounds is None else (min(bounds[0],current[0]),min(bounds[1],current[1]),max(bounds[2],current[2]),max(bounds[3],current[3]))
    if bounds is not None: CoordinateTransformer(spatial_context).validate_bounds(bounds)

def _query_active_points_near_cluster(
    conn: Any,
    cluster: HotspotCluster,
    radius_m: float,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_table: str | None = None,
    refine_id: str | None = None,
    spatial_context: SpatialContext,
) -> list[RefinementSubtrack]:
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
        refine_id=refine_id,
        spatial_context=spatial_context,
    )

def _build_tracks_from_columns(grouped_columns, *, raw=False):
    """Publish grouped columns; raw diagnostic queries have a local partition."""
    tracks=[]
    seen_partitions=set()
    for ordinal, ((vehicle_id, subtrack), columns) in enumerate(grouped_columns.items(), 1):
        if raw:
            subtrack=ordinal
        elif subtrack in seen_partitions:
            raise ValueError("subtrack contains multiple vehicles")
        seen_partitions.add(subtrack)
        kind=np.where(np.asarray(columns[12],dtype=np.int8)==0,"p","i")
        source_keys=np.zeros(len(kind),dtype=np.int64)
        kept=np.flatnonzero(kind=="p")
        source_keys[kept]=np.asarray([columns[7][i] for i in kept],dtype=np.int64)
        tracks.append(refinement_subtrack_from_columns(
            vehicle_id=vehicle_id,subtrack_index=subtrack,source_id=str(columns[6][0]),
            xy=np.column_stack((columns[10],columns[11])).astype(np.float64),
            lonlat=np.column_stack((columns[2],columns[3])).astype(np.float64),
            timestamp=np.asarray(columns[1],dtype=np.float64),heading=np.asarray(columns[4],dtype=np.float64),
            speed=np.asarray(columns[5],dtype=np.float64),kind=kind,source_row_key=source_keys,
            refine_point_id=np.asarray(columns[13],dtype=np.int64),after_sequence=np.asarray(columns[15],dtype=np.int64),
            generated_reason=np.asarray([reason or "" for reason in columns[9]],dtype=object)))
    return tracks


def _tracks_from_points(points):
    return {track.subtrack_index:track for track in points}
