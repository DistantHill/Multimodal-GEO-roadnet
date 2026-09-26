"""Parse source rows into validated trajectory points."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from ..models import FieldMapping, FieldSpec, ParseIssue


class _SkipRow(Exception):
    def __init__(self, issue: ParseIssue) -> None:
        self.issue = issue
        super().__init__(issue.reason)


def _parse_row_from_tuple(
    row: tuple,
    ordinal: int,
    col_index: dict[str, int],       # canonical → tuple position
    specs: dict[str, FieldSpec],
    source_id: str,
    pk_idx: int | None,
) -> tuple[tuple, list[ParseIssue]]:
    """ZH: PG 元组快速解析路径——跳过 _row_to_dict dict 分配 + _raw_value 双 dict 查找，
        直接用 tuple[row_idx] 索引。保留 parse_row 全部校验语义。
    EN: Fast-path tuple parser for PG rows — skips per-row dict allocation and
        double dict lookup in _raw_value. Preserves all parse_row validation.
    """
    issues: list[ParseIssue] = []

    # ── str from tuple PK column ──
    if pk_idx is not None:
        pk_val = row[pk_idx]
        if pk_val not in (None, ""):
            point_ref = str(pk_val)
        else:
            point_ref = str(ordinal)
    else:
        point_ref = str(ordinal)

    def _fast_issue(level: str, reason: str) -> ParseIssue:
        return ParseIssue(
            point_ref=point_ref, row_number=ordinal,
            level=level, reason=reason, raw_values={},
        )

    # ── vehicle_id (required str) ──
    vid_spec = specs.get("vehicle_id")
    if vid_spec is None:
        raise _SkipRow(_fast_issue("ERROR", "vehicle_id spec not found"))
    vid_raw = row[col_index["vehicle_id"]] if "vehicle_id" in col_index else None
    vehicle_id = "" if vid_raw is None else str(vid_raw).strip()
    if not vehicle_id:
        raise _SkipRow(_fast_issue("ERROR", "vehicle_id missing"))

    # ── timestamp (required) ──
    ts_spec = specs.get("timestamp")
    if ts_spec is None:
        raise _SkipRow(_fast_issue("ERROR", "timestamp spec not found"))
    ts_raw = row[col_index["timestamp"]] if "timestamp" in col_index else None
    try:
        timestamp = _parse_timestamp(ts_raw) * ts_spec.unit_scale
    except (TypeError, ValueError, OverflowError) as exc:
        raise _SkipRow(_fast_issue("ERROR", "timestamp invalid")) from exc
    if not math.isfinite(timestamp):
        raise _SkipRow(_fast_issue("ERROR", "timestamp non-finite"))

    # ── lon (required float) ──
    lon_spec = specs.get("lon")
    if lon_spec is None:
        raise _SkipRow(_fast_issue("ERROR", "lon spec not found"))
    lon_raw = row[col_index["lon"]] if "lon" in col_index else None
    try:
        lon = float(lon_raw) * lon_spec.unit_scale
    except (TypeError, ValueError) as exc:
        raise _SkipRow(_fast_issue("ERROR", "lon invalid")) from exc
    if not math.isfinite(lon):
        raise _SkipRow(_fast_issue("ERROR", "lon non-finite"))
    if not -180.0 <= lon <= 180.0:
        raise _SkipRow(_fast_issue("ERROR", "lon out of bounds"))

    # ── lat (required float) ──
    lat_spec = specs.get("lat")
    if lat_spec is None:
        raise _SkipRow(_fast_issue("ERROR", "lat spec not found"))
    lat_raw = row[col_index["lat"]] if "lat" in col_index else None
    try:
        lat = float(lat_raw) * lat_spec.unit_scale
    except (TypeError, ValueError) as exc:
        raise _SkipRow(_fast_issue("ERROR", "lat invalid")) from exc
    if not math.isfinite(lat):
        raise _SkipRow(_fast_issue("ERROR", "lat non-finite"))
    if not -90.0 <= lat <= 90.0:
        raise _SkipRow(_fast_issue("ERROR", "lat out of bounds"))

    # ── heading (optional float) ──
    hdg_spec = specs.get("heading")
    heading: float = 0.0
    if hdg_spec is not None and "heading" in col_index:
        hdg_raw = row[col_index["heading"]]
        if hdg_raw in (None, ""):
            heading = _float_default(hdg_spec)
        else:
            try:
                heading = float(hdg_raw) * hdg_spec.unit_scale
            except (TypeError, ValueError):
                heading = _float_default(hdg_spec)
                issues.append(_fast_issue("WARN", "heading invalid; using default"))
    if not math.isfinite(heading):
        heading = _float_default(hdg_spec) if hdg_spec else 0.0
        issues.append(_fast_issue("WARN", "heading non-finite; using default"))
    elif not 0.0 <= heading < 360.0:
        heading %= 360.0
        issues.append(_fast_issue("WARN", "heading normalized to [0, 360)"))

    # ── speed (optional float) ──
    spd_spec = specs.get("speed")
    speed: float = 0.0
    if spd_spec is not None and "speed" in col_index:
        spd_raw = row[col_index["speed"]]
        if spd_raw in (None, ""):
            speed = _float_default(spd_spec)
        else:
            try:
                speed = float(spd_raw) * spd_spec.unit_scale
            except (TypeError, ValueError):
                speed = _float_default(spd_spec)
                issues.append(_fast_issue("WARN", "speed invalid; using default"))
    if not math.isfinite(speed) or speed < 0.0:
        speed = _float_default(spd_spec) if spd_spec else 0.0
        issues.append(_fast_issue("WARN", "speed invalid; using default"))

    return (vehicle_id, timestamp, lon, lat, heading, speed, None), issues


def parse_row(
    row: dict[str, Any],
    point_ref: str,
    row_number: int,
    mapping: FieldMapping,
) -> tuple[tuple, list[ParseIssue]]:
    specs = mapping.specs_by_canonical
    issues: list[ParseIssue] = []

    vehicle_id = _required_str(row, specs["vehicle_id"], mapping, point_ref, row_number)
    timestamp = _required_timestamp(row, specs["timestamp"], mapping, point_ref, row_number)
    lon = _required_float(row, specs["lon"], mapping, point_ref, row_number)
    lat = _required_float(row, specs["lat"], mapping, point_ref, row_number)
    if not -180.0 <= lon <= 180.0:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", "lon out of bounds", row))
    if not -90.0 <= lat <= 90.0:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", "lat out of bounds", row))

    heading = _optional_float(row, specs.get("heading"), mapping, point_ref, row_number, issues)
    if not math.isfinite(heading):
        heading = _float_default(specs.get("heading"))
        issues.append(_issue(point_ref, row_number, "WARN", "heading non-finite; using default", row))
    elif not 0.0 <= heading < 360.0:
        heading %= 360.0
        issues.append(_issue(point_ref, row_number, "WARN", "heading normalized to [0, 360)", row))

    speed = _optional_float(row, specs.get("speed"), mapping, point_ref, row_number, issues)
    if not math.isfinite(speed) or speed < 0.0:
        speed = _float_default(specs.get("speed"))
        issues.append(_issue(point_ref, row_number, "WARN", "speed invalid; using default", row))

    extra = _parse_extra(row, mapping, point_ref, row_number, issues)
    return (vehicle_id, timestamp, lon, lat, heading, speed, extra), issues



def _required_str(
    row: dict[str, Any],
    spec: FieldSpec,
    mapping: FieldMapping,
    point_ref: str,
    row_number: int,
) -> str:
    raw = _raw_value(row, spec, mapping)
    value = "" if raw is None else str(raw).strip()
    if not value:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", f"{spec.canonical} missing", row))
    return value


def _required_timestamp(
    row: dict[str, Any],
    spec: FieldSpec,
    mapping: FieldMapping,
    point_ref: str,
    row_number: int,
) -> float:
    raw = _raw_value(row, spec, mapping)
    try:
        value = _parse_timestamp(raw) * spec.unit_scale
    except (TypeError, ValueError, OverflowError) as exc:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", "timestamp invalid", row)) from exc
    if not math.isfinite(value):
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", "timestamp non-finite", row))
    return value


def _required_float(
    row: dict[str, Any],
    spec: FieldSpec,
    mapping: FieldMapping,
    point_ref: str,
    row_number: int,
) -> float:
    raw = _raw_value(row, spec, mapping)
    try:
        value = float(raw) * spec.unit_scale
    except (TypeError, ValueError) as exc:
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", f"{spec.canonical} invalid", row)) from exc
    if not math.isfinite(value):
        raise _SkipRow(_issue(point_ref, row_number, "ERROR", f"{spec.canonical} non-finite", row))
    return value


def _optional_float(
    row: dict[str, Any],
    spec: FieldSpec | None,
    mapping: FieldMapping,
    point_ref: str,
    row_number: int,
    issues: list[ParseIssue],
) -> float:
    if spec is None:
        return 0.0
    raw = _raw_value(row, spec, mapping)
    if raw in (None, ""):
        if spec.required:
            issues.append(_issue(point_ref, row_number, "WARN", f"{spec.canonical} missing; using default", row))
        return _float_default(spec)
    try:
        return float(raw) * spec.unit_scale
    except (TypeError, ValueError):
        issues.append(_issue(point_ref, row_number, "WARN", f"{spec.canonical} invalid; using default", row))
        return _float_default(spec)


def _parse_extra(
    row: dict[str, Any],
    mapping: FieldMapping,
    point_ref: str,
    row_number: int,
    issues: list[ParseIssue],
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for spec in mapping.specs:
        if not spec.extra:
            continue
        raw = _raw_value(row, spec, mapping)
        if raw in (None, ""):
            if spec.default not in (None, ""):
                extra[spec.canonical] = spec.default
            continue
        try:
            extra[spec.canonical] = _parse_scalar(raw, spec)
        except (TypeError, ValueError, OverflowError):
            issues.append(_issue(point_ref, row_number, "WARN", f"{spec.canonical} extra invalid; skipped", row))
    return extra


def _parse_scalar(raw: Any, spec: FieldSpec) -> Any:
    if spec.type == "str":
        return str(raw).strip()
    if spec.type == "int":
        value = int(float(raw) * spec.unit_scale)
        if not math.isfinite(float(value)):
            raise ValueError("non-finite int")
        return value
    if spec.type == "float":
        value = float(raw) * spec.unit_scale
        if not math.isfinite(value):
            raise ValueError("non-finite float")
        return value
    if spec.type == "timestamp":
        return _parse_timestamp(raw) * spec.unit_scale
    raise ValueError(f"unsupported field type: {spec.type}")


def _parse_timestamp(raw: Any) -> float:
    if raw in (None, ""):
        raise ValueError("missing timestamp")
    if isinstance(raw, (int, float)):
        return _timestamp_from_number(float(raw))
    text = str(raw).strip()
    if not text:
        raise ValueError("missing timestamp")
    try:
        return _timestamp_from_number(float(text))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return datetime.fromisoformat(text).timestamp()


def _timestamp_from_number(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("timestamp non-finite")
    if abs(value) > 10_000_000_000:
        return value / 1000.0
    return value


def _raw_value(row: dict[str, Any], spec: FieldSpec, mapping: FieldMapping) -> Any:
    column = mapping.canonical_to_source.get(spec.canonical)
    if column is None:
        return spec.default
    return row.get(column, spec.default)


def _float_default(spec: FieldSpec | None) -> float:
    if spec is None:
        return 0.0
    try:
        return float(spec.default or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _issue(
    point_ref: str,
    row_number: int,
    level: str,
    reason: str,
    row: dict[str, Any],
) -> ParseIssue:
    return ParseIssue(
        point_ref=point_ref,
        row_number=row_number,
        level=level,
        reason=reason,
        raw_values={str(k): "" if v is None else str(v) for k, v in row.items()},
    )
