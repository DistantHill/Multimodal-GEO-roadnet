"""Read and stream point sequences belonging to persisted Refine Runs."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Iterator

from ..telemetry.heartbeat import observed_operation, heartbeat_progress
from ..models import PointRef, RefinedPointRef, SpatialContext, TrajectoryPoint
from ..projection import spatial_context_from_values
from ..db.source_table import validate_source_table
from ..db._point_query_fragments import _INJECTED_SOURCE_ID, _IP_UNIFIED_COLS, _tp_unified_cols


def build_stream_all_sql(
    table: str = "trajectory_points",
    *,
    has_refine_id: bool = False,
    kept_table: str | None = None,
    spatial_context: SpatialContext,
    source_key_sql_type: str = "bigint",
    source_has_extra: bool = True,
) -> str:
    """Stream raw or one Refine Run's kept + injected points without a bbox.

    ZH: 全量来源点，或指定 Refine Run 的 kept + injected UNION ALL 查询。
        用于 server-side named cursor 逐车流式读取。
        未提供 ``refine_id`` 时不读取 injected_points，避免混入其它 Run。
        ``kept_table`` 非空时，trajectory 分支改为 JOIN 精炼结果表
        (``kept_table`` 的 vehicle_id 带 _seg 后缀，几何列取自 ``table``)，
        并按 ``ktp.refine_id`` 只读本次 Refine Run。

    EN: When ``kept_table`` is set, the trajectory branch JOINs the refine-result
        table (suffixed vehicle_id) against ``table`` for geometry columns,
        filtered by ``ktp.refine_id``.

    Parameter placeholders (in order, caller must bind):
        Raw mode (``kept_table`` is None):
            no parameters
        Kept mode (``kept_table`` set):
            1.  %s::text         — source table label for the in-memory PointRef
            2.  %s               — ktp.refine_id
            3.  %s               — injected_points.refine_id
    """
    if has_refine_id and kept_table is None:
        raise ValueError("refine_id requires kept_table for complete Run isolation")
    processing_srid = int(spatial_context.processing_srid)
    projected_tp = f"ST_Transform(tp.geom, {processing_srid})"
    if kept_table is not None:
        extra = "tp.extra" if source_has_extra else "'{}'::jsonb"
        return (
            f"SELECT ktp.vehicle_id, tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, "
            f"       %s::text AS source_id, ktp.source_row_key::text AS source_row_key, {extra} AS extra, "
            f"       ''::text AS generated_reason, "
            f"       ST_X({projected_tp}) AS x, ST_Y({projected_tp}) AS y, "
            f"       0 AS point_kind, ktp.id AS row_id, ktp.subtrack_index, "
            f"       ktp.sequence_in_subtrack AS anchor_sequence "
            f"FROM {kept_table} AS ktp "
            f"JOIN {table} AS tp "
            f"  ON tp.id = ktp.source_row_key "
            f"WHERE ktp.refine_id = %s "
            f"UNION ALL "
            f"SELECT {_IP_UNIFIED_COLS} "
            f"FROM injected_points WHERE refine_id = %s "
            f"ORDER BY subtrack_index, anchor_sequence, point_kind, row_id"
        )
    return (
        f"SELECT {_tp_unified_cols(processing_srid)} "
        f"FROM {table} "
        f"ORDER BY vehicle_id, timestamp, id::text"
    )

@observed_operation("读取轨迹与逐车处理")
def _stream_vehicle_tracks_from_db(
    conn: Any,
    *,
    table: str = "trajectory_points",
    kept_table: str | None = None,
    source_table: str | None = None,
    refine_id: str | None = None,
    spatial_context: SpatialContext,
) -> Iterator[list[TrajectoryPoint]]:
    """Stream tracks one vehicle at a time from DB (global, no spatial filter).

    ZH: 从 DB 逐车流式读取轨迹点。使用 UNION ALL 合并 active points 和
        injected_points，按 (vehicle_id, timestamp) 排序后检测 vehicle_id
        变化来分组。内存占用 = 单车辆最大点数。
        kept 模式 (``kept_table`` 非空) 时 trajectory 分支 JOIN 精炼结果表，
        绑定 ``ktp.refine_id`` 与 ``injected_points.refine_id``。
    """
    has_refine_id = refine_id is not None
    if kept_table is not None and refine_id is None:
        raise ValueError("kept_table requires refine_id")
    if kept_table is None and refine_id is not None:
        raise ValueError("refine_id requires kept_table for complete Run isolation")
    if kept_table is not None:
        params: list[Any] = [
            source_table or table,
            refine_id,
            _INJECTED_SOURCE_ID,
            refine_id,
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
                has_refine_id=has_refine_id,
                kept_table=kept_table,
                spatial_context=spatial_context,
                source_key_sql_type=contract.key_sql_type,
                source_has_extra=contract.has_extra,
            ),
            params,
        )
        current_subtrack: int | None = None
        current_track: list[TrajectoryPoint] = []
        refined_sequence = 0
        for row_count, row in enumerate(cur, 1):
            if row_count % 10_000 == 0:
                heartbeat_progress(row_count)
            vid = str(row[0])
            subtrack_index = int(row[14])
            if current_subtrack is not None and subtrack_index != current_subtrack:
                yield current_track
                current_track = []
                refined_sequence = 0
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
                refined_point_ref=RefinedPointRef(
                    point_kind="injected" if is_injected else "kept",
                    point_id=int(row[13]),
                    subtrack_index=subtrack_index,
                    refined_sequence_in_subtrack=refined_sequence,
                ),
            ))
            refined_sequence += 1
        if current_track:
            yield current_track

@dataclass(frozen=True, slots=True)
class _RefineRunRow:
    """Authoritative metadata required before a formal DB Detect reads points."""

    refine_id: str
    source_table: str
    status: str
    spatial_context: SpatialContext
    source_fingerprint: dict[str, object] | None

def fetch_refine_run_row(conn: object, refine_id: str) -> _RefineRunRow:
    """Fetch and validate one Refine Run's persisted projection context."""
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT id, source_table, status, source_srid, processing_srid, "
            "projection_config, source_fingerprint "
            "FROM refine_runs WHERE id = %s",
            (refine_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Refine Run: {refine_id}")
    fingerprint = row[6]
    if isinstance(fingerprint, str):
        fingerprint = json.loads(fingerprint)
    if fingerprint is not None and not isinstance(fingerprint, dict):
        raise ValueError("Refine Run source_fingerprint is malformed")
    return _RefineRunRow(
        refine_id=str(row[0]),
        source_table=str(row[1]),
        status=str(row[2]),
        spatial_context=spatial_context_from_values(row[3], row[4], row[5]),
        source_fingerprint=fingerprint,
    )
