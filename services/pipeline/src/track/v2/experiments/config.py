"""One editable TOML with validated cases and named parameter overrides."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import PipelineConfig
from ..config.pipeline_config_loader import load_pipeline_payload
from ..intersections.detect_intersections import IntersectConfig
from ..db.source_table import SourceTableContract, quote_table_name

EXPERIMENT_SECTIONS = {"experiment", "cases", "schemes", "evaluation", "archive"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, (str, datetime)):
        raise ValueError(f"{name}: expected ISO timestamp with timezone")
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name}: expected ISO timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name}: timezone is required")
    return parsed.astimezone(timezone.utc)


def polygon_geometry(raw: dict[str, Any]) -> dict[str, Any]:
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    if not isinstance(raw, dict):
        raise ValueError("region file must contain a GeoJSON object")
    if raw.get("crs") is not None:
        raise ValueError("region must use WGS84 GeoJSON without legacy crs")
    items = raw.get("features", []) if raw.get("type") == "FeatureCollection" else [raw]
    geometries = []
    if not isinstance(items, list):
        raise ValueError("region features must be an array")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("region features must be GeoJSON objects")
        geometry = item.get("geometry") if item.get("type") == "Feature" else item
        if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError("region must contain only Polygon/MultiPolygon features")
        geom = shape(geometry)
        if geom.is_empty or not geom.is_valid:
            raise ValueError("region polygon is empty or invalid")
        low_x, low_y, high_x, high_y = geom.bounds
        if not all(math.isfinite(v) for v in geom.bounds) or not (-180 <= low_x <= high_x <= 180 and -90 <= low_y <= high_y <= 90):
            raise ValueError("region coordinates must be WGS84 longitude/latitude")
        geometries.append(geom)
    if not geometries:
        raise ValueError("region file has no polygons")
    return json.loads(json.dumps(mapping(unary_union(geometries))))


@dataclass(frozen=True)
class ExperimentCase:
    name: str
    source_table: str
    time_start: datetime | None
    time_end: datetime | None
    geometry: dict[str, Any]
    region_file: Path | None = None
    truth_file: Path | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"name": self.name, "source_table": self.source_table,
                "time_start": self.time_start.isoformat() if self.time_start else None, "time_end": self.time_end.isoformat() if self.time_end else None,
                "geometry": self.geometry}

    def source_query(self, contract: SourceTableContract) -> tuple[str, tuple[Any, ...]]:
        # Find relevant vehicles through the spatial index before reading their
        # full time slices. The lateral window keeps each slice on the
        # (vehicle_id, timestamp, id) index rather than sorting the whole day.
        # Outside rows still contribute to the segment counter before filtering.
        query = f"""WITH candidates AS MATERIALIZED (
            SELECT DISTINCT vehicle_id FROM {contract.quoted_table}
            WHERE ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), geom)
              AND timestamp >= %s AND timestamp < %s
        ) SELECT segmented.* FROM candidates AS candidate
          CROSS JOIN LATERAL (
            SELECT *, SUM(CASE WHEN _experiment_inside THEN 0 ELSE 1 END)
                OVER (ORDER BY timestamp, id::text ROWS UNBOUNDED PRECEDING)
                AS _experiment_segment
            FROM (
                SELECT *, ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), geom)
                    AS _experiment_inside
                FROM {contract.quoted_table}
                WHERE vehicle_id = candidate.vehicle_id
                  AND timestamp >= %s AND timestamp < %s
            ) AS timed
          ) AS segmented WHERE _experiment_inside
          ORDER BY vehicle_id, timestamp, id::text"""
        bounds = (canonical(self.geometry),
                  self.time_start.timestamp() if self.time_start else float("-inf"),
                  self.time_end.timestamp() if self.time_end else float("inf"))
        return query, bounds + bounds


def load_case(name: str, raw: dict[str, Any], base: Path) -> ExperimentCase:
    if not isinstance(raw, dict):
        raise ValueError(f"case {name}: expected table")
    allowed = {"source_table", "time_start", "time_end", "region_file", "bbox", "truth_file"}
    if set(raw) - allowed:
        raise ValueError(f"case {name}: unknown keys {sorted(set(raw) - allowed)}")
    source = raw.get("source_table")
    if not isinstance(source, str):
        raise ValueError(f"case {name}: source_table is required")
    quote_table_name(source)
    from ..time_selection import time_bounds
    start, end = time_bounds(raw.get("time_start"), raw.get("time_end"))
    def is_empty(value: Any) -> bool:
        return value is None or (isinstance(value, str) and not value.strip()) or value == []

    region_value = raw.get("region_file")
    bbox_value = raw.get("bbox")
    if is_empty(region_value) == is_empty(bbox_value):
        raise ValueError(f"case {name}: provide exactly one region_file or bbox")
    region_file = None
    if not is_empty(region_value):
        if not isinstance(region_value, str):
            raise ValueError(f"case {name}: region_file must be a path string")
        region_file = (base / region_value).resolve()
        geometry = polygon_geometry(json.loads(region_file.read_text(encoding="utf-8")))
    else:
        from shapely.geometry import box, mapping
        corners = bbox_value
        if not isinstance(corners, list) or len(corners) != 2 or any(not isinstance(p, list) or len(p) != 2 for p in corners):
            raise ValueError("bbox must contain exactly two [longitude, latitude] corners")
        if any(type(v) not in (int, float) or not math.isfinite(v) for point in corners for v in point):
            raise ValueError("bbox coordinates must be finite numbers")
        (x1, y1), (x2, y2) = corners
        if x1 == x2 or y1 == y2:
            raise ValueError("bbox must have positive area")
        geometry = polygon_geometry(mapping(box(min(x1,x2), min(y1,y2), max(x1,x2), max(y1,y2))))
    truth = (base / raw["truth_file"]).resolve() if raw.get("truth_file") else None
    if truth is not None and not truth.is_file():
        raise ValueError(f"case {name}: truth_file does not exist")
    return ExperimentCase(name, source, start, end, geometry, region_file, truth)


def merge_parameters(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if key not in result:
            raise ValueError(f"unknown parameter override: {key}")
        if isinstance(value, dict) and isinstance(result[key], dict):
            result[key] = merge_parameters(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_experiment_payload(base: dict[str, Any], overrides: dict[str, Any] | None = None,
                            **kwargs: Any) -> PipelineConfig:
    """Use shared validation; each scheme supplies a complete Detect config."""
    changes = copy.deepcopy(overrides or {})
    detect = changes.pop("detect", None)
    effective = merge_parameters(base, changes)
    if detect is not None:
        effective["detect"] = copy.deepcopy(detect)
    return load_pipeline_payload(effective, **kwargs)


def independent_instance_configs(config: PipelineConfig):
    """One independent clustering run for each declared instance, without combinations."""
    for algorithm in config.detect.candidates:
        definition = config.detect.candidate[algorithm]
        for instance, params in definition.params.items():
            selected = replace(definition, output_instances=(instance,))
            detect = replace(config.detect, candidates=(algorithm,), candidate={algorithm: selected})
            yield algorithm, instance, params, replace(config, detect=detect)


@dataclass(frozen=True)
class ExperimentWorkspace:
    config_path: Path
    root: Path
    payload: dict[str, Any]
    case: ExperimentCase | None
    schemes: tuple[str, ...]
    configs: dict[str, PipelineConfig]

    @property
    def evaluation(self) -> dict[str, Any]:
        return self.payload.get("evaluation", {})

    @property
    def archive_settings(self) -> dict[str, Any]:
        from dataclasses import asdict
        settings = asdict(self.configs[self.schemes[0]].postgres)
        settings.update(self.payload.get("archive", {}))
        settings["database"] = self.payload.get("archive", {}).get("database", "roadnet_eval")
        if settings["database"] in {"roadnet", "roadnet_test"}:
            raise ValueError("archive database must be independent of development databases")
        return settings


def load_workspace(path: str | Path, *, password: str, case_name: str | None = None,
                   schemes: list[str] | None = None, validate_run: bool = True) -> ExperimentWorkspace:
    path = Path(path).resolve()
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    selection = payload.get("experiment", {})
    if set(selection) - {"case", "schemes", "workspace", "note"}:
        raise ValueError("unknown experiment settings")
    if set(payload.get("archive", {})) - {"database", "host", "port", "user", "sslmode"}:
        raise ValueError("archive accepts only connection settings without credentials")
    if not validate_run:
        base = {k: v for k, v in payload.items() if k not in EXPERIMENT_SECTIONS}
        config = load_experiment_payload(base, base_directory=path.parent, postgres_password=password)
        root = (path.parent / selection["workspace"]).resolve() if selection.get("workspace") else config.paths.data_root / "experiments"
        if not root.is_relative_to(config.paths.data_root.resolve()):
            raise ValueError("experiment workspace must be under configured paths.data_root")
        return ExperimentWorkspace(path, root, payload, None, ("connection",), {"connection": config})
    case_name = case_name or selection.get("case")
    if not case_name or case_name not in payload.get("cases", {}):
        raise ValueError("select a defined case using experiment.case or --case")
    case = load_case(case_name, payload["cases"][case_name], path.parent)
    if payload.get("evaluation") or case.truth_file:
        from pipeline.experiments.evaluation import EvaluationRule, validated_features
        EvaluationRule.parse(payload.get("evaluation", {}))
        if case.truth_file:
            validated_features(json.loads(case.truth_file.read_text(encoding="utf-8")), truth=True)
    selected = schemes or selection.get("schemes")
    if not isinstance(selected, list) or not selected or any(not isinstance(s, str) for s in selected):
        raise ValueError("experiment.schemes must be a nonempty list of names")
    if len(set(selected)) != len(selected):
        raise ValueError("scheme names must not repeat within a batch")
    base = {k: v for k, v in payload.items() if k not in EXPERIMENT_SECTIONS}
    configs = {}
    for name in selected:
        if name not in payload.get("schemes", {}):
            raise ValueError(f"unknown scheme: {name}")
        overrides = payload["schemes"][name]
        if set(overrides) - {"projection", "refine", "detect", "runtime", "resources"}:
            raise ValueError("schemes may override projection/refine/detect/runtime/resources only")
        configs[name] = load_experiment_payload(base, overrides, base_directory=path.parent, postgres_password=password)
    root = (path.parent / selection["workspace"]).resolve() if selection.get("workspace") else configs[selected[0]].paths.data_root / "experiments"
    data_root = configs[selected[0]].paths.data_root.resolve()
    if not root.is_relative_to(data_root):
        raise ValueError("experiment workspace must be under configured paths.data_root")
    return ExperimentWorkspace(path, root, payload, case, tuple(selected), configs)


def refine_identity(case: ExperimentCase, config: PipelineConfig) -> dict[str, Any]:
    semantic = config.effective_snapshot()["semantic_config"]
    return {"case": case.snapshot(), "database": config.postgres.database,
            "host": config.postgres.host, "port": config.postgres.port,
            "projection": semantic["projection"], "parser": semantic["parser"], "refine": semantic["refine"]}


def refine_key(case: ExperimentCase, config: PipelineConfig) -> str:
    return hashlib.sha256(canonical(refine_identity(case, config)).encode()).hexdigest()


@dataclass(frozen=True)
class TimeSelection:
    """Optional time slice for the ordinary full Pipeline CLI."""
    time_start: datetime
    time_end: datetime

    def __post_init__(self) -> None:
        if self.time_start >= self.time_end:
            raise ValueError("time_start must precede time_end")

    def snapshot(self) -> dict[str, str]:
        return {"time_start": self.time_start.isoformat(), "time_end": self.time_end.isoformat()}

    def source_query(self, contract: SourceTableContract) -> tuple[str, tuple[Any, ...]]:
        return (f"SELECT * FROM {contract.quoted_table} WHERE timestamp >= %s AND timestamp < %s "
                "ORDER BY vehicle_id, timestamp, id::text", (self.time_start.timestamp(), self.time_end.timestamp()))
