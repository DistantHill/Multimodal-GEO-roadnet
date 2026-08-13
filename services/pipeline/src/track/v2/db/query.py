"""DB-mode trajectory point reads for intersect2 spatial sliding windows.

ZH: DB 模式轨迹点读取——空间范围、窗口查询、逐车流式、簇邻域查询，以及
    行物化/分组辅助。与 ``queries.py``（原始 SQL 模板）配套：本模块负责
    执行查询并物化 ``TrajectoryPoint``，SQL 字符串集中在 ``queries.py``。
EN: DB-mode trajectory point reads — spatial extent, per-window query,
    per-vehicle streaming, cluster-neighborhood query, plus row-materialization
    and grouping helpers. Complements ``queries.py`` (raw SQL templates): this
    module executes queries and materializes ``TrajectoryPoint``.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

from ..models import HotspotCluster, PointRef, TrajectoryPoint
from .queries import (
    build_active_in_window_sql,
    build_spatial_extent_sql,
    build_stream_all_sql,
    row_to_dict,
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

    ZH: 将 lon/lat 范围划分为重叠的空间滑窗。overlap 是四向扩展比例
        （每侧扩展 overlap%），相邻窗口间实际重叠 = 2 × overlap。

    Parameters
    ----------
    extent : (lon_min, lat_min, lon_max, lat_max)
        Full spatial extent of the data.
    size : (width_m, height_m)
        Window size in meters (approximate, converted to degrees).
    overlap : float
        Per-side expansion ratio (default 0.15 = 15% each side).
    """

    def __init__(
        self,
        extent: tuple[float, float, float, float],
        size: tuple[float, float] = (2000.0, 2000.0),
        overlap: float = 0.15,
    ) -> None:
        self._lon_min, self._lat_min, self._lon_max, self._lat_max = extent
        self._width_m, self._height_m = size
        self._overlap = overlap

        # Approximate metres → degrees (1° ≈ 111,320 m at equator)
        self._step_lon = self._width_m / 111320.0
        self._step_lat = self._height_m / 111320.0
        self._pad_lon = self._step_lon * overlap
        self._pad_lat = self._step_lat * overlap

    def iter_windows(self) -> Iterator[SpatialWindow]:
        """Yield overlapping spatial windows covering the full extent."""
        lon = self._lon_min
        while lon < self._lon_max:
            lat = self._lat_min
            while lat < self._lat_max:
                yield SpatialWindow(
                    x_min=max(self._lon_min, lon - self._pad_lon),
                    y_min=max(self._lat_min, lat - self._pad_lat),
                    x_max=min(self._lon_max, lon + self._step_lon + self._pad_lon),
                    y_max=min(self._lat_max, lat + self._step_lat + self._pad_lat),
                )
                lat += self._step_lat
            lon += self._step_lon

    def count_windows(self) -> int:
        """Return the number of windows that :meth:`iter_windows` will yield."""
        if self._lon_max <= self._lon_min or self._lat_max <= self._lat_min:
            return 0
        lon_count = math.ceil((self._lon_max - self._lon_min) / self._step_lon)
        lat_count = math.ceil((self._lat_max - self._lat_min) / self._step_lat)
        return lon_count * lat_count


def _query_spatial_extent(
    conn: Any,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_id: str | None = None,
    clean_id: str | None = None,
) -> tuple[float, float, float, float] | None:
    """Query MIN/MAX lon/lat of active trajectory points.

    ZH: kept 模式 (``kept_table`` 非空) 时 JOIN 清洗结果表并绑定 ``ktp.clean_id``。
    Returns ``(lon_min, lat_min, lon_max, lat_max)`` compatible with
    ``SpatialWindowIterator``.
    """
    if kept_table is not None and clean_id is None:
        raise ValueError("kept_table requires clean_id")
    sql = build_spatial_extent_sql(table, kept_table=kept_table)
    params: tuple[Any, ...] = (clean_id,) if kept_table is not None else ()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    # SQL returns (min_lon, max_lon, min_lat, max_lat)
    # SpatialWindowIterator expects (lon_min, lat_min, lon_max, lat_max)
    return float(row[0]), float(row[2]), float(row[1]), float(row[3])


def _query_active_points_in_window(
    conn: Any,
    window: SpatialWindow,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_id: str | None = None,
    clean_id: str | None = None,
    telemetry: dict[str, int | float] | None = None,
) -> list[TrajectoryPoint]:
    """Query active trajectory points + injected points in a spatial window.

    ZH: 使用 PostGIS GiST 空间索引 (geom && ST_MakeEnvelope) 替代
        lon/lat BETWEEN，走 R-tree 2D 索引扫描。UNION ALL 合并两表一次往返。
        kept 模式 (``kept_table`` 非空) 时 trajectory 分支 JOIN 清洗结果表，
        首参绑定 ``ktp.clean_id``（而非 raw 模式的 source_id）。
    """
    rows: list[dict[str, Any]] = []
    has_clean_id = clean_id is not None
    if kept_table is not None and clean_id is None:
        raise ValueError("kept_table requires clean_id")
    if kept_table is not None:
        params: list[Any] = [clean_id]
        params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
        params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
        params.append(clean_id)
    else:
        params = [source_id or ""]
        params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
        params.extend((window.x_min, window.y_min, window.x_max, window.y_max))
        if has_clean_id:
            params.append(clean_id)
    query_started = time.perf_counter() if telemetry is not None else 0.0
    with conn.cursor() as cur:
        cur.execute(
            build_active_in_window_sql(table, has_clean_id=has_clean_id, kept_table=kept_table),
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
    source_id: str | None = None,
    clean_id: str | None = None,
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
    if kept_table is not None:
        params: list[Any] = [clean_id, clean_id]
    else:
        params = [source_id or ""]
        if has_clean_id:
            params.append(clean_id)
    # ZH: 服务器端命名游标 — PG 按需 FETCH，避免全量物化到 portal。
    # EN: Server-side named cursor — FETCHes on demand, no full portal materialization.
    with conn.cursor(
        name=f"stream_tracks_{uuid.uuid4().hex}",
    ) as cur:
        cur.execute(
            build_stream_all_sql(table, has_clean_id=has_clean_id, kept_table=kept_table),
            params,
        )
        current_vid: str | None = None
        current_track: list[TrajectoryPoint] = []
        for row in cur:
            vid = str(row[0])
            if current_vid is not None and vid != current_vid:
                yield current_track
                current_track = []
            current_vid = vid
            current_track.append(TrajectoryPoint(
                point_ref=PointRef(
                    source_id=str(row[6] or ""),
                    source_row_key=str(row[7] or ""),
                    stable=not bool(row[11]),
                    from_ref=str(row[9]) if row[9] is not None else None,
                    to_ref=str(row[10]) if row[10] is not None else None,
                    generated_reason=str(row[11] or ""),
                ),
                vehicle_id=vid,
                timestamp=float(row[1]),
                lon=float(row[2]),
                lat=float(row[3]),
                heading=float(row[4]),
                speed=float(row[5]),
                extra=row[8] if isinstance(row[8], dict) else {},
            ))
        if current_track:
            yield current_track


def _query_active_points_near_cluster(
    conn: Any,
    cluster: HotspotCluster,
    radius_m: float,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_id: str | None = None,
    clean_id: str | None = None,
) -> list[TrajectoryPoint]:
    """Query active points within *radius_m* of a cluster centroid.

    ZH: 委托 ``_query_active_points_in_window``，将 cluster centroid + radius_m
        转换为 SpatialWindow 后走 GiST 空间索引查询。自动获得 UNION ALL 一次往返、
        DB 侧 ORDER BY（无需 Python 排序），且 telemetry 可选跟踪。
    """
    radius_deg = radius_m / 111320.0
    # Window x/y are *lon/lat* degrees (not meters) — SpatialWindow stores
    # the coordinate system of the extent it was built from.
    window = SpatialWindow(
        x_min=cluster.centroid_lon - radius_deg,
        y_min=cluster.centroid_lat - radius_deg,
        x_max=cluster.centroid_lon + radius_deg,
        y_max=cluster.centroid_lat + radius_deg,
    )
    return _query_active_points_in_window(
        conn,
        window,
        table=table,
        kept_table=kept_table,
        source_id=source_id,
        clean_id=clean_id,
    )


def _build_points_from_rows(rows: list[dict[str, Any]]) -> list[TrajectoryPoint]:
    """Convert DB row dicts to TrajectoryPoint objects."""
    points: list[TrajectoryPoint] = []
    for r in rows:
        points.append(TrajectoryPoint(
            point_ref=PointRef(
                source_id=r["source_id"],
                source_row_key=r["source_row_key"],
                stable=not bool(r.get("generated_reason")),
                from_ref=(
                    str(r["from_source_row_key"])
                    if r.get("from_source_row_key") is not None else None
                ),
                to_ref=(
                    str(r["to_source_row_key"])
                    if r.get("to_source_row_key") is not None else None
                ),
                generated_reason=str(r.get("generated_reason") or ""),
            ),
            vehicle_id=r["vehicle_id"],
            timestamp=r["timestamp"],
            lon=r["lon"],
            lat=r["lat"],
            heading=r["heading"],
            speed=r["speed"],
            extra=r["extra"],
        ))
    return points


def _tracks_from_points(points: list[TrajectoryPoint]) -> dict[str, list[TrajectoryPoint]]:
    """Group points by vehicle_id into a tracks dict."""
    tracks: dict[str, list[TrajectoryPoint]] = {}
    for tp in points:
        if tp.vehicle_id not in tracks:
            tracks[tp.vehicle_id] = []
        tracks[tp.vehicle_id].append(tp)
    return tracks
