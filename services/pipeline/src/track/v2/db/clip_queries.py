"""Database queries used by the Clip stage."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from ..models import ClipIntersection, PointRef, TrajectoryPoint


@dataclass(frozen=True, slots=True)
class _RunRow:
    """One Detect Run together with its parent Clean Run metadata."""

    clean_id: str
    source_id: str
    parameters: dict[str, object]
    status: str


def fetch_detect_row(conn: object, detect_id: str) -> _RunRow:
    """Fetch a Detect Run and the source metadata of its parent Clean Run."""
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT d.clean_id, c.source_id, c.parameters, d.status "
            "FROM detect_runs AS d JOIN clean_runs AS c ON c.id = d.clean_id "
            "WHERE d.id = %s",
            (detect_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Detect Run: {detect_id}")
    clean_id, source_id, raw_parameters, status = row
    return _RunRow(
        clean_id=str(clean_id),
        source_id=str(source_id),
        parameters=_decode_json_object(raw_parameters),
        status=str(status),
    )


def fetch_intersections(conn: object, detect_id: str) -> list[ClipIntersection]:
    """Load verified intersections belonging to one Detect Run."""
    intersections: list[ClipIntersection] = []
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT i.cluster_id, ci.centroid_lon, ci.centroid_lat, i.clip_radius_m "
            "FROM intersections AS i "
            "JOIN candidate_intersections AS ci "
            "ON ci.id = i.candidate_intersection_id AND ci.detect_id = i.detect_id "
            "WHERE i.detect_id = %s AND i.is_complete IS TRUE "
            "ORDER BY i.cluster_id",
            (detect_id,),
        )
        for cluster_id, lon, lat, radius_m in cur:
            if radius_m is None:
                raise ValueError(
                    f"Detect Run {detect_id} predates Clip radius persistence"
                )
            if not math.isfinite(float(radius_m)) or float(radius_m) <= 0.0:
                raise ValueError(f"Detect Run {detect_id} has an invalid Clip radius")
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
    clean_id: str,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Load one Clean Run's frozen kept members and injected points."""
    safe_table = _quoted_identifier(table)
    sql = (
        "SELECT vehicle_id, subtrack_index, anchor_sequence, "
        "source_id, source_row_key, timestamp, lon, lat, heading, speed, extra, "
        "from_source_row_key, to_source_row_key, generated_reason "
        "FROM ("
        "  SELECT ktp.vehicle_id, ktp.subtrack_index, ktp.sequence_in_subtrack AS anchor_sequence, "
        "         ktp.source_id, ktp.source_row_key, "
        "         tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, tp.extra, "
        "         NULL::text AS from_source_row_key, "
        "         NULL::text AS to_source_row_key, ''::text AS generated_reason, "
        "         0 AS point_kind, ktp.id AS row_id "
        "  FROM kept_trajectory_points AS ktp "
        f"  LEFT JOIN {safe_table} AS tp ON tp.id = ktp.trajectory_point_id "
        "  WHERE ktp.clean_id = %s "
        "  UNION ALL "
        "  SELECT ip.vehicle_id, ip.subtrack_index, ip.after_sequence_in_subtrack, "
        "         ip.source_id, ip.source_row_key, "
        "         ip.timestamp, ip.lon, ip.lat, ip.heading, ip.speed, '{}'::jsonb, "
        "         ip.from_source_row_key, ip.to_source_row_key, ip.generated_reason, "
        "         1 AS point_kind, ip.id AS row_id "
        "  FROM injected_points AS ip WHERE ip.clean_id = %s"
        ") AS clean_points "
        "ORDER BY vehicle_id, subtrack_index, anchor_sequence, point_kind, row_id"
    )
    return _group_points(conn, sql, (clean_id, clean_id), clean_id)


def _group_points(
    conn: object,
    sql: str,
    params: tuple[object, ...],
    clean_id: str,
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
                    f"Clean Run {clean_id} predates persisted source references"
                )
            generated_reason = str(row[13])
            if any(row[index] is None for index in range(5, 10)):
                raise ValueError(
                    f"Clean Run {clean_id} references a missing source row"
                )
            generated_reason = str(row[13])
            grouped.setdefault((vehicle_id, subtrack_index), []).append(
                TrajectoryPoint(
                    point_ref=PointRef(
                        str(row[3]),
                        str(row[4]),
                        stable=not generated_reason,
                        from_ref=str(row[11]) if row[11] is not None else None,
                        to_ref=str(row[12]) if row[12] is not None else None,
                        generated_reason=generated_reason,
                    ),
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
