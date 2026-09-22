"""Controlled Network replay from bounded persisted Fitting inputs."""

from __future__ import annotations

from dataclasses import MISSING, dataclass, fields
import hashlib
import json
import math
from pathlib import Path
from typing import Any, get_type_hints
from uuid import UUID, uuid4

from ..models import Centerline, ClipIntersection, NetworkConfig
from ..network import build_network
from ..db.cursors import declare_full_scan_cursor


EXPECTED_NETWORK_SHA256 = "4986ebfd9cc1f481f488b86f22c59384c363aded58b391f240f47ab6d220fbef"


class NetworkReplayError(RuntimeError):
    """Persisted input or the controlled replay contract is unreliable."""


@dataclass(frozen=True, slots=True)
class ReplayResult:
    tables: dict[str, list[tuple[Any, ...]]]
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _FitAdapter:
    centerlines: tuple[Centerline, ...]
    detect_id: str


def _network_hash() -> str:
    return hashlib.sha256((Path(__file__).parents[1] / "network.py").read_bytes()).hexdigest()


def _validate_contract() -> str:
    actual = _network_hash()
    if actual != EXPECTED_NETWORK_SHA256:
        raise NetworkReplayError("network algorithm anchor mismatch")
    config_fields = fields(NetworkConfig)
    if (len(config_fields) != 1 or config_fields[0].name != "include_isolated_nodes"
            or get_type_hints(NetworkConfig).get(config_fields[0].name) is not bool
            or config_fields[0].default is not MISSING):
        raise NetworkReplayError("NetworkConfig domain changed")
    return actual


def _read_named(
    conn: Any, name: str, query: str, params: tuple[Any, ...], batch_size: int,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    with conn.cursor(name=f"network_replay_{name}_{uuid4().hex}") as cursor:
        cursor.itersize = batch_size
        declare_full_scan_cursor(conn, cursor, query, params)
        while batch := cursor.fetchmany(batch_size):
            rows.extend(batch)
    return rows


def _identity_map(rows: list[tuple[Any, ...]]) -> dict[int, str]:
    result: dict[int, str] = {}
    identities: set[str] = set()
    for raw_id, raw_identity in rows:
        if (isinstance(raw_id, bool) or not isinstance(raw_id, int)
                or not isinstance(raw_identity, str) or not raw_identity
                or "__UNCOMPARABLE_" in raw_identity):
            raise NetworkReplayError("intersection identity is invalid")
        identity = raw_identity
        if raw_id in result or identity in identities:
            raise NetworkReplayError("intersection identity is not unique")
        result[raw_id] = identity
        identities.add(identity)
    return result


def _intersections(rows: list[tuple[Any, ...]]) -> tuple[ClipIntersection, ...]:
    result = []
    seen: set[int] = set()
    for raw_id, *raw_values in rows:
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id in seen:
            raise NetworkReplayError("intersection surrogate identity is invalid")
        try:
            lon, lat, radius = (float(value) for value in raw_values)
        except (TypeError, ValueError) as exc:
            raise NetworkReplayError("intersection geometry is invalid") from exc
        if (not all(math.isfinite(value) for value in (lon, lat, radius))
                or not -180 <= lon <= 180 or not -90 <= lat <= 90 or radius <= 0):
            raise NetworkReplayError("intersection geometry is invalid")
        seen.add(raw_id)
        result.append(ClipIntersection(raw_id, lon, lat, radius))
    return tuple(result)


def _centerlines(rows: list[tuple[Any, ...]]) -> tuple[Centerline, ...]:
    result = []
    seen: set[tuple[int, int]] = set()
    for raw_from, raw_to, raw_geometry, raw_length, raw_forward, raw_reverse in rows:
        if (isinstance(raw_from, bool) or not isinstance(raw_from, int)
                or isinstance(raw_to, bool) or not isinstance(raw_to, int)
                or raw_from >= raw_to or (raw_from, raw_to) in seen):
            raise NetworkReplayError("combined centerline identity is invalid")
        if (isinstance(raw_forward, bool) or not isinstance(raw_forward, int)
                or isinstance(raw_reverse, bool) or not isinstance(raw_reverse, int)
                or raw_forward < 0 or raw_reverse < 0 or raw_forward + raw_reverse <= 0):
            raise NetworkReplayError("combined centerline counts are invalid")
        try:
            geometry = json.loads(raw_geometry)
            coordinates = tuple(tuple(float(value) for value in point)
                                for point in geometry["coordinates"])
            length = float(raw_length)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NetworkReplayError("combined centerline geometry is invalid") from exc
        if (geometry.get("type") != "LineString" or len(coordinates) < 2
                or any(len(point) != 2 or not all(math.isfinite(value) for value in point)
                       for point in coordinates)
                or not math.isfinite(length) or length <= 0):
            raise NetworkReplayError("combined centerline geometry is invalid")
        seen.add((raw_from, raw_to))
        result.append(Centerline(
            raw_from, raw_to, coordinates, length, (), raw_forward, raw_reverse,
        ))
    return tuple(result)


def _key(*values: object) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _normalized_tables(
    result: Any, identities: dict[int, str], include_isolated: bool,
) -> dict[str, list[tuple[Any, ...]]]:
    suffix = "true" if include_isolated else "false"
    nodes = sorted((identities[node.intersection_id], node.lon, node.lat,
                    node.radius_m, node.degree) for node in result.nodes)
    edges = []
    for edge in result.edges:
        left, right = identities[edge.from_intersection_id], identities[edge.to_intersection_id]
        reverse = left > right
        pair = (right, left) if reverse else (left, right)
        points = tuple(reversed(edge.points)) if reverse else edge.points
        forward = edge.reverse_clip_count if reverse else edge.forward_clip_count
        backward = edge.forward_clip_count if reverse else edge.reverse_clip_count
        geometry = json.dumps({"type": "LineString", "coordinates": points},
                              separators=(",", ":"))
        edges.append((_key("edge", *pair), geometry, edge.length_m,
                      edge.source_clip_count, forward, backward))
    issues = sorted((_key("issue", *sorted((identities[issue.from_intersection_id],
                                             identities[issue.to_intersection_id])), issue.reason),)
                    for issue in result.issues)
    report = result.report
    return {
        f"network.replay.{suffix}.nodes": nodes,
        f"network.replay.{suffix}.edges": sorted(edges),
        f"network.replay.{suffix}.issues": issues,
        f"network.replay.{suffix}.report": [(
            "report", report.input_centerline_count, report.node_count,
            report.edge_count, report.isolated_node_count,
            report.skipped_centerline_count,
        )],
    }


def replay_network(
    conn: Any, detect_id: UUID, fit_id: UUID, identity_rows: list[tuple[Any, ...]],
    *, batch_size: int,
) -> ReplayResult:
    """Replay both values of the anchored Network configuration domain."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise NetworkReplayError("batch_size must be positive")
    actual_hash = _validate_contract()
    status = conn.execute("""
      SELECT f.status,d.status
      FROM fit_runs f JOIN detect_runs d ON d.id=f.detect_id
      WHERE f.id=%s AND f.detect_id=%s AND d.id=%s
    """, (fit_id, detect_id, detect_id)).fetchone()
    if status != ("completed", "completed"):
        raise NetworkReplayError("Fit/Detect lineage is not completed")
    identities = _identity_map(identity_rows)
    intersection_rows = _read_named(conn, "intersections", """
      SELECT i.id,c.centroid_lon,c.centroid_lat,i.clip_radius_m
      FROM intersections i JOIN candidate_intersections c
        ON (c.id,c.detect_id)=(i.candidate_intersection_id,i.detect_id)
      WHERE i.detect_id=%s ORDER BY i.id
    """, (detect_id,), batch_size)
    centerline_rows = _read_named(conn, "centerlines", """
      SELECT from_intersection_id,to_intersection_id,ST_AsGeoJSON(geom,15),
             length_m,forward_clip_count,reverse_clip_count
      FROM centerlines
      WHERE fit_id=%s AND detect_id=%s AND variant='combined'
      ORDER BY from_intersection_id,to_intersection_id
    """, (fit_id, detect_id), batch_size)
    intersections = _intersections(intersection_rows)
    if set(identities) != {item.intersection_id for item in intersections}:
        raise NetworkReplayError("intersection identity mapping is incomplete")
    centerlines = _centerlines(centerline_rows)
    if any(line.from_intersection_id not in identities
           or line.to_intersection_id not in identities for line in centerlines):
        raise NetworkReplayError("centerline endpoint identity is missing")
    adapter = _FitAdapter(centerlines, str(detect_id))
    tables: dict[str, list[tuple[Any, ...]]] = {}
    for include_isolated in (False, True):
        replay = build_network(
            adapter, intersections,
            config=NetworkConfig(include_isolated_nodes=include_isolated),
        )
        tables.update(_normalized_tables(replay, identities, include_isolated))
    return ReplayResult(tables, {
        "kind": "controlled_replay",
        "historical_output_reconstructed": False,
        "configs": [{"include_isolated_nodes": False}, {"include_isolated_nodes": True}],
        "source_hash_expected": EXPECTED_NETWORK_SHA256,
        "source_hash_actual": actual_hash,
        "detect_id": str(detect_id),
        "fit_id": str(fit_id),
        "intersection_rows_read": len(intersection_rows),
        "centerline_rows_read": len(centerline_rows),
        "identity_rows_read": len(identity_rows),
    })
