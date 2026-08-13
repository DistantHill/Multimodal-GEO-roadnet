"""Database queries for the Clip stage.

ZH: Clip stage 专用的数据库查询函数。每个函数对应一次 DB 往返，
    返回上层可直接组装的数据结构，不包含任何业务逻辑。
EN: Database query functions for the Clip stage. Each function is one DB
    round-trip and returns ready-to-assemble data structures with zero
    business logic.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from ..models import ClipIntersection, PointRef, TrajectoryPoint


@dataclass(frozen=True, slots=True)
class _RunRow:
    """Decoded pipeline_runs row for the Clip stage."""

    source_id: str
    parameters: dict[str, object]
    status: str


def fetch_run_row(conn: object, run_id: str) -> _RunRow:
    """Fetch and decode the pipeline_runs row for *run_id*.

    Returns a fully-decoded ``_RunRow`` so the caller never touches raw
    JSON or DB row tuples.
    """
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT source_id, source_kind, parameters, status "
            "FROM pipeline_runs WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown pipeline run: {run_id}")
    source_id, _source_kind, raw_parameters, status = row
    return _RunRow(
        source_id=str(source_id),
        parameters=_decode_json_object(raw_parameters),
        status=str(status),
    )


def fetch_intersections(
    conn: object,
    run_id: str,
) -> list[ClipIntersection]:
    """Load all verified intersections for *run_id* from the Clip schema.

    Joins ``intersections`` → ``candidate_intersections`` to resolve
    centroid coordinates. Only rows with ``is_complete IS TRUE`` are
    returned.
    """
    intersections: list[ClipIntersection] = []
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT i.cluster_id, ci.centroid_lon, ci.centroid_lat, i.clip_radius_m "
            "FROM intersections AS i "
            "JOIN candidate_intersections AS ci "
            "ON ci.id = i.candidate_intersection_id "
            "WHERE i.run_id = %s AND i.is_complete IS TRUE "
            "ORDER BY i.cluster_id",
            (run_id,),
        )
        for cluster_id, lon, lat, radius_m in cur:
            if radius_m is None:
                raise ValueError(
                    f"pipeline run {run_id} predates Clip radius persistence"
                )
            if not math.isfinite(float(radius_m)) or float(radius_m) <= 0.0:
                raise ValueError(
                    f"pipeline run {run_id} has an invalid Clip radius"
                )
            intersections.append(
                ClipIntersection(
                    cluster_id=int(cluster_id),
                    lon=float(lon),
                    lat=float(lat),
                    radius_m=float(radius_m),
                )
            )
    return intersections


def fetch_clean_points(
    conn: object,
    table: str,
    run_id: str,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Load trajectory points through the Clean stage's kept-points table.

    Returns points grouped by ``(vehicle_id, subtrack_index)``.
    """
    safe_table = _quoted_identifier(table)
    sql = (
        "SELECT ktp.vehicle_id, ktp.subtrack_index, ktp.sequence_in_subtrack, "
        "ktp.source_id, ktp.source_row_key, "
        "tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, tp.extra "
        "FROM kept_trajectory_points AS ktp "
        f"JOIN {safe_table} AS tp ON tp.id = ktp.trajectory_point_id "
        "WHERE ktp.run_id = %s AND tp.is_active IS NOT FALSE "
        "ORDER BY ktp.vehicle_id, ktp.subtrack_index, "
        "ktp.sequence_in_subtrack, tp.id"
    )
    return _group_points(conn, sql, (run_id,), run_id)


def fetch_raw_points(
    conn: object,
    table: str,
    source_id: str,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Load trajectory points directly from the raw-points table.

    Used when the Clean stage is skipped (``skip_clean=True``).
    Returns points grouped by ``(vehicle_id, 0)`` — caller should
    split by time gaps afterwards.
    """
    safe_table = _quoted_identifier(table)
    sql = (
        "SELECT tp.vehicle_id, 0 AS subtrack_index, tp.id AS sequence_in_subtrack, "
        "%s::text AS point_source_id, tp.id::text AS source_row_key, "
        "tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, tp.extra "
        f"FROM {safe_table} AS tp "
        "WHERE tp.is_active IS NOT FALSE "
        "ORDER BY tp.vehicle_id, tp.timestamp, tp.id"
    )
    return _group_points(conn, sql, (source_id,), source_id)


# ── Internal helpers ──────────────────────────────────────────────────────


def _group_points(
    conn: object,
    sql: str,
    params: tuple[object, ...],
    run_id: str,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Execute *sql* and group rows by ``(vehicle_id, subtrack_index)``."""
    grouped: dict[tuple[str, int], list[TrajectoryPoint]] = {}
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(sql, params)
        for row in cur:
            vehicle_id = str(row[0])
            subtrack_index = int(row[1])
            if row[3] is None or row[4] is None:
                raise ValueError(
                    f"pipeline run {run_id} predates persisted source references"
                )
            grouped.setdefault((vehicle_id, subtrack_index), []).append(
                TrajectoryPoint(
                    point_ref=PointRef(str(row[3]), str(row[4]), stable=True),
                    vehicle_id=vehicle_id,
                    timestamp=float(row[5]),
                    lon=float(row[6]),
                    lat=float(row[7]),
                    heading=float(row[8]),
                    speed=float(row[9]),
                    extra=row[10] if isinstance(row[10], dict) else {},
                )
            )
    return grouped


def _decode_json_object(value: object) -> dict[str, object]:
    """Decode *value* into a dict, accepting either a dict or a JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("pipeline run parameters must be a JSON object")


def _quoted_identifier(value: str) -> str:
    """Quote a (possibly schema-qualified) SQL identifier for safe interpolation."""
    parts = value.split(".")
    if not parts or any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None for part in parts
    ):
        raise ValueError(f"invalid trajectory table name: {value!r}")
    return ".".join(f'"{part}"' for part in parts)
