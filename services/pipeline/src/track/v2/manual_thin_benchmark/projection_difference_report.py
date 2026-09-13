"""Auditable legacy-to-fixed projection difference reports and approvals.

Snapshots are JSON objects. Stages are optional and omitted stages are reported
as ``not_evaluated`` rather than fabricated as empty/stable. Supported keys are
``points``, ``candidates``, ``clusters``, ``clips``, and ``fittings``; every
record must have an ``id``. Point records additionally carry lon/lat, clusters
carry ordered ``members``, clips carry ordered ``members`` and ``length_m``,
and fittings carry a GeoJSON-like ``coordinates`` list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..projection import QUERY_WINDOW_RULE_VERSION, _wgs84_geod


REPORT_SCHEMA = "track.v2-projection-difference-v1"
APPROVAL_SCHEMA = "track.v2-projection-difference-approval-v1"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _finite(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _lonlat(record: dict[str, Any], label: str) -> None:
    lon, lat = _finite(record.get("lon"), f"{label}.lon"), _finite(record.get("lat"), f"{label}.lat")
    if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
        raise ValueError(f"{label} longitude/latitude is outside EPSG:4326")


def _validate_snapshot(snapshot: dict[str, Any], label: str) -> None:
    if snapshot.get("schema") != "track.v2-projection-snapshot-v1":
        raise ValueError(f"{label} snapshot requires schema track.v2-projection-snapshot-v1")
    if not isinstance(snapshot.get("provenance"), dict) or not snapshot["provenance"].get("source") or not snapshot["provenance"].get("algorithm"):
        raise ValueError(f"{label} snapshot requires provenance.source and provenance.algorithm")
    if "crs" not in snapshot:
        raise ValueError(f"{label} snapshot requires crs")


def _records(snapshot: dict[str, Any], stage: str) -> dict[str, dict[str, Any]] | None:
    raw = snapshot.get(stage)
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"snapshot stage {stage!r} must be a list")
    result: dict[str, dict[str, Any]] = {}
    for record in raw:
        if not isinstance(record, dict) or "id" not in record:
            raise ValueError(f"snapshot stage {stage!r} records require id")
        identity = str(record["id"])
        if identity in result:
            raise ValueError(f"duplicate {stage} identity: {identity}")
        label = f"{stage}[{identity}]"
        if stage == "points":
            _lonlat(record, label)
        elif stage in {"candidates", "clusters", "clips"}:
            if not isinstance(record.get("members"), list):
                raise ValueError(f"{label}.members must be an ordered list")
            if stage == "clips":
                length = _finite(record.get("length_m"), f"{label}.length_m")
                if length < 0.0:
                    raise ValueError(f"{label}.length_m must be non-negative")
        elif stage == "fittings":
            coordinates = record.get("coordinates")
            if not isinstance(coordinates, list) or len(coordinates) < 2:
                raise ValueError(f"{label}.coordinates requires at least two vertices")
            for index, coordinate in enumerate(coordinates):
                if not isinstance(coordinate, list) or len(coordinate) != 2:
                    raise ValueError(f"{label}.coordinates[{index}] must be [lon, lat]")
                _lonlat({"lon": coordinate[0], "lat": coordinate[1]}, f"{label}.coordinates[{index}]")
        result[identity] = record
    return result


def _identity_diff(
    legacy: dict[str, dict[str, Any]], fixed: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    legacy_ids, fixed_ids = set(legacy), set(fixed)
    return {
        "legacy_count": len(legacy),
        "fixed_count": len(fixed),
        "missing_in_fixed": sorted(legacy_ids - fixed_ids),
        "added_in_fixed": sorted(fixed_ids - legacy_ids),
        "common_count": len(legacy_ids & fixed_ids),
    }


def _percentiles(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    result: dict[str, float | int] = {"count": len(ordered)}
    for label, fraction in (("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99)):
        if not ordered:
            result[label] = 0.0
            continue
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        result[label] = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    result["max"] = max(ordered, default=0.0)
    return result


def _point_diff(legacy: dict[str, dict[str, Any]], fixed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = _identity_diff(legacy, fixed)
    distances: list[float] = []
    geod = _wgs84_geod()
    for identity in sorted(set(legacy) & set(fixed)):
        left, right = legacy[identity], fixed[identity]
        _az1, _az2, distance = geod.inv(
            float(left["lon"]), float(left["lat"]),
            float(right["lon"]), float(right["lat"]),
        )
        distances.append(abs(float(distance)))
    result["ellipsoidal_displacement_m"] = _percentiles(distances)
    return result


def _structural_stage(
    legacy: dict[str, dict[str, Any]],
    fixed: dict[str, dict[str, Any]],
    *,
    member_field: str,
) -> dict[str, Any]:
    result = _identity_diff(legacy, fixed)
    changed = []
    for identity in sorted(set(legacy) & set(fixed)):
        if list(legacy[identity].get(member_field, [])) != list(fixed[identity].get(member_field, [])):
            changed.append(identity)
    result["changed_ordered_membership"] = changed
    return result


def _clip_diff(legacy: dict[str, dict[str, Any]], fixed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = _structural_stage(legacy, fixed, member_field="members")
    length_deltas = [
        abs(float(legacy[key]["length_m"]) - float(fixed[key]["length_m"]))
        for key in sorted(set(legacy) & set(fixed))
        if "length_m" in legacy[key] and "length_m" in fixed[key]
    ]
    result["absolute_length_delta_m"] = _percentiles(length_deltas)
    return result


def _fitting_diff(legacy: dict[str, dict[str, Any]], fixed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    result = _identity_diff(legacy, fixed)
    changed, vertex_distances = [], []
    geod = _wgs84_geod()
    for identity in sorted(set(legacy) & set(fixed)):
        left = legacy[identity].get("coordinates")
        right = fixed[identity].get("coordinates")
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            changed.append(identity)
            continue
        for left_point, right_point in zip(left, right):
            _a, _b, distance = geod.inv(*map(float, left_point), *map(float, right_point))
            vertex_distances.append(abs(float(distance)))
    result["changed_geometry_structure"] = changed
    result["paired_vertex_displacement_m"] = _percentiles(vertex_distances)
    return result


def build_projection_difference_report(
    legacy: dict[str, Any],
    fixed: dict[str, Any],
    *,
    parameters: dict[str, Any] | None = None,
    version: str,
) -> dict[str, Any]:
    """Return a complete report without treating normal coordinate drift as failure."""
    _validate_snapshot(legacy, "legacy")
    _validate_snapshot(fixed, "fixed")
    if not version.strip():
        raise ValueError("version is required")
    stage_records = {
        stage: (_records(legacy, stage), _records(fixed, stage))
        for stage in ("points", "candidates", "clusters", "clips", "fittings")
    }
    input_hashes = {"legacy": _sha256(legacy), "fixed": _sha256(fixed)}
    stages: dict[str, Any] = {}
    for stage, (left, right) in stage_records.items():
        if left is None or right is None:
            stages[stage] = {"status": "not_evaluated"}
        elif stage == "points":
            stages[stage] = {"status": "evaluated", **_point_diff(left, right)}
        elif stage in {"candidates", "clusters"}:
            stages[stage] = {
                "status": "evaluated",
                **_structural_stage(left, right, member_field="members"),
            }
        elif stage == "clips":
            stages[stage] = {"status": "evaluated", **_clip_diff(left, right)}
        else:
            stages[stage] = {"status": "evaluated", **_fitting_diff(left, right)}
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "review_status": "pending_review",
        "input_hashes": input_hashes,
        "input_pair_hash": _sha256(input_hashes),
        "crs": {
            "legacy": legacy.get("crs", "legacy_local_origin"),
            "fixed": fixed.get("crs", "unknown"),
            "comparison": "WGS84_ellipsoidal_distance",
            "query_window_rule_version": QUERY_WINDOW_RULE_VERSION,
        },
        "parameters": parameters or {},
        "version": version,
        "snapshot_provenance": {
            "legacy": legacy["provenance"],
            "fixed": fixed["provenance"],
        },
        "comparison_policy": {
            "identity_and_order": "exact",
            "coordinate_difference": "reported_not_gated",
            "distance_unit": "metre",
        },
        "stages": stages,
    }
    report["report_hash"] = _sha256(report)
    return report


def accept_projection_report(
    report: dict[str, Any],
    *,
    accepted_by: str,
    reason: str,
) -> dict[str, Any]:
    """Create an explicit approval bound to exact report and input hashes."""
    _validate_report_integrity(report)
    if not accepted_by.strip() or not reason.strip():
        raise ValueError("approval requires accepted_by and reason")
    approval = {
        "schema": APPROVAL_SCHEMA,
        "report_hash": report["report_hash"],
        "input_pair_hash": report["input_pair_hash"],
        "accepted_by": accepted_by,
        "reason": reason,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }
    approval["approval_hash"] = _sha256(approval)
    return approval


def verify_projection_approval(report: dict[str, Any], approval: dict[str, Any]) -> None:
    _validate_report_integrity(report)
    expected = _sha256({key: value for key, value in approval.items() if key != "approval_hash"})
    if approval.get("schema") != APPROVAL_SCHEMA or approval.get("approval_hash") != expected:
        raise ValueError("approval record is invalid")
    if approval.get("report_hash") != report.get("report_hash") or approval.get(
        "input_pair_hash"
    ) != report.get("input_pair_hash"):
        raise ValueError("approval is bound to a different report or input pair")


def _validate_report_integrity(report: dict[str, Any]) -> None:
    calculated = _sha256({key: value for key, value in report.items() if key != "report_hash"})
    if report.get("schema") != REPORT_SCHEMA or report.get("report_hash") != calculated:
        raise ValueError("report hash does not match report content")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(payload)
    except FileExistsError as exc:
        raise ValueError(f"refusing to overwrite existing artifact: {path}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--legacy", type=Path, required=True)
    report_parser.add_argument("--fixed", type=Path, required=True)
    report_parser.add_argument("--output", type=Path, required=True)
    report_parser.add_argument("--version", required=True)
    report_parser.add_argument("--parameters-json", type=Path)
    accept_parser = subparsers.add_parser("accept")
    accept_parser.add_argument("--report", type=Path, required=True)
    accept_parser.add_argument("--output", type=Path, required=True)
    accept_parser.add_argument("--accepted-by", required=True)
    accept_parser.add_argument("--reason", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--report", type=Path, required=True)
    verify_parser.add_argument("--approval", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "report":
        _write_new(args.output, build_projection_difference_report(
            _read(args.legacy), _read(args.fixed),
            parameters=_read(args.parameters_json) if args.parameters_json else {},
            version=args.version,
        ))
    elif args.command == "accept":
        _write_new(args.output, accept_projection_report(
            _read(args.report), accepted_by=args.accepted_by, reason=args.reason,
        ))
    else:
        verify_projection_approval(_read(args.report), _read(args.approval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
