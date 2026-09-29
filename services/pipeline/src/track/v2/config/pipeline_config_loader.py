"""Strict, single-source Pipeline TOML configuration loader."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from ..intersections.detect_intersections import IntersectConfig
from ..errors import ConfigError
from .field_mapping import load_field_config
from ..models import (ClipConfig, FieldConfig, FittingConfig, NetworkConfig,
                      ProjectionConfig, RefineConfig, RuntimeConfig)
from ..raster.models import RasterConfig
from . import PostgresConfig


@dataclass(frozen=True, slots=True)
class PathsConfig:
    data_root: Path
    parquet_directory: Path
    duckdb_postgres_extension_path: Path


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    directory: Path


@dataclass(frozen=True, slots=True)
class ParserConfig:
    field_specs_path: Path
    fields: FieldConfig


@dataclass(frozen=True, slots=True)
class ResourcesConfig:
    verify_artifact_sha256: bool


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    postgres: PostgresConfig
    paths: PathsConfig
    logging: LoggingConfig
    projection: ProjectionConfig
    parser: ParserConfig
    refine: RefineConfig
    detect: IntersectConfig
    clip: ClipConfig
    fitting: FittingConfig
    network: NetworkConfig
    runtime: RuntimeConfig
    resources: ResourcesConfig
    raster: RasterConfig

    def effective_snapshot(self) -> dict[str, Any]:
        """Return the complete effective non-secret configuration."""
        return {
            "semantic_config": {
                "projection": self.projection.as_dict(),
                "parser": {
                    "field_specs": self.parser.fields.as_dict(),
                },
                "refine": self.refine.as_dict(),
                "detect": self.detect.as_dict(),
                "clip": self.clip.as_dict(),
                "fitting": self.fitting.as_dict(),
                "network": self.network.as_dict(),
                "raster": self.raster.semantic_dict(),
            },
            "execution_config": {
                "postgres": {
                    "host": self.postgres.host,
                    "port": self.postgres.port,
                    "database": self.postgres.database,
                    "user": self.postgres.user,
                    "connect_timeout": self.postgres.connect_timeout,
                    "sslmode": self.postgres.sslmode,
                    "keepalives": self.postgres.keepalives,
                    "keepalives_idle": self.postgres.keepalives_idle,
                    "keepalives_interval": self.postgres.keepalives_interval,
                    "keepalives_count": self.postgres.keepalives_count,
                    "tcp_user_timeout": self.postgres.tcp_user_timeout,
                },
                "paths": {
                    "data_root": str(self.paths.data_root),
                    "parquet_directory": str(self.paths.parquet_directory),
                    "duckdb_postgres_extension_path": str(
                        self.paths.duckdb_postgres_extension_path
                    ),
                },
                "logging": {"directory": str(self.logging.directory)},
                "runtime": self.runtime.as_dict(),
                "resources": dataclasses.asdict(self.resources),
                "raster": self.raster.execution_dict(),
            },
        }

    def snapshot_parameters(self) -> dict[str, Any]:
        """Return persisted configuration values plus a stable SHA-256 digest."""
        snapshot = self.effective_snapshot()
        canonical = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return {**snapshot, "config_digest": hashlib.sha256(canonical.encode()).hexdigest()}


_SECTION_TYPES: dict[str, type[Any]] = {
    "postgres": PostgresConfig, "paths": PathsConfig, "logging": LoggingConfig,
    "projection": ProjectionConfig, "refine": RefineConfig, "detect": IntersectConfig,
    "clip": ClipConfig, "fitting": FittingConfig, "network": NetworkConfig,
    "runtime": RuntimeConfig, "resources": ResourcesConfig, "raster": RasterConfig,
}


def _matches(value: Any, annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        return any(_matches(value, item) for item in get_args(annotation))
    if origin is tuple:
        args = get_args(annotation)
        return isinstance(value, list) and (len(args) == 2 and args[1] is Ellipsis
            and all(_matches(v, args[0]) for v in value)
            or len(args) == len(value) and all(_matches(v, t) for v, t in zip(value, args)))
    if annotation is bool:
        return type(value) is bool
    if annotation is int:
        return type(value) is int
    if annotation is float:
        return type(value) in (int, float)
    if annotation is str:
        return isinstance(value, str)
    if annotation is Path:
        return isinstance(value, str)
    return True


def _convert(value: Any, annotation: Any, base_dir: Path) -> Any:
    if get_origin(annotation) is tuple:
        return tuple(value)
    if annotation is float:
        return float(value)
    if annotation is Path:
        path = Path(value)
        return path if path.is_absolute() else (base_dir / path).resolve()
    return value


def _collect_section(name: str, cls: type[Any], raw: Any, base_dir: Path,
                     errors: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        errors.append(f"{name}: section must be a table")
        return {}
    fields = {item.name: item for item in dataclasses.fields(cls)}
    if cls is PostgresConfig:
        fields.pop("password")
    errors.extend(f"{name}.{key}: unknown key" for key in sorted(set(raw) - set(fields)))
    missing = set(fields) - set(raw)
    if cls is RuntimeConfig:
        missing.discard("duckdb_event_memory_limit")
        missing.discard("duckdb_grid_memory_limit")
    errors.extend(f"{name}.{key}: missing required key" for key in sorted(missing))
    hints = get_type_hints(cls)
    result = {}
    for key in sorted(set(raw) & set(fields)):
        annotation = hints[key]
        if not _matches(raw[key], annotation):
            errors.append(f"{name}.{key}: expected {annotation!s}")
        else:
            result[key] = _convert(raw[key], annotation, base_dir)
    if cls is RuntimeConfig and "duckdb_event_memory_limit" not in raw and "duckdb_memory_limit" in result:
        result["duckdb_event_memory_limit"] = result["duckdb_memory_limit"]
    if cls is RuntimeConfig and "duckdb_grid_memory_limit" not in raw and "duckdb_memory_limit" in result:
        result["duckdb_grid_memory_limit"] = result["duckdb_memory_limit"]
    return result


def _validate(values: dict[str, dict[str, Any]], errors: list[str]) -> None:
    raster = values.get("raster", {})
    if raster and len(raster) == len(dataclasses.fields(RasterConfig)):
        try:
            RasterConfig(**raster)
        except ValueError as exc:
            errors.append(str(exc))
    positive = {"postgres.port", "postgres.connect_timeout", "postgres.keepalives_idle",
        "postgres.keepalives_interval", "postgres.keepalives_count", "projection.source_srid",
        "projection.processing_srid", "runtime.db_insert_batch", "runtime.db_stream_batch_size",
        "runtime.relation_batch_size", "runtime.parquet_read_batch_size",
        "runtime.validation_batch_size", "runtime.import_batch_size",
        "runtime.csv_max_field_size", "runtime.encoding_sample_size", "runtime.progress_interval",
        "runtime.duckdb_threads", "runtime.polars_threads", "runtime.artifact_gc_grace_days"}
    non_negative = {"postgres.tcp_user_timeout", "runtime.parquet_target_file_size_bytes",
                    "runtime.parquet_row_group_size_rows"}
    for path in positive | non_negative:
        section, key = path.split(".")
        value = values.get(section, {}).get(key)
        minimum = 0 if path in non_negative else 1
        if value is not None and value < minimum:
            errors.append(f"{path}: must be >= {minimum}")
    strictly_positive = {
        "refine.max_extension_m", "refine.max_gap_s", "detect.min_leg_m",
        "detect.g_star_radius_m", "detect.candidate_dbscan_eps_m",
        "detect.cross_track_grid_cell_m",
        "detect.cross_track_task_grid_m",
        "detect.min_intersection_radius_m", "fitting.point_spacing_m",
        "fitting.fit_sample_spacing_m", "runtime.progress_time_threshold_s",
    }
    non_negative_numbers = {
        "refine.min_speed_mps", "refine.max_stationary_duration_s",
        "detect.min_movement_speed_mps", "detect.merge_centroid_distance_m",
        "detect.cross_track_min_avg_speed",
        "detect.topology_radius_padding_m",
    }
    positive_integers = {
        "refine.excluded_sample_max", "detect.turn_window",
        "detect.candidate_dbscan_min_samples",
        "detect.cross_track_pair_batch_size", "detect.cross_track_max_grid_cells_per_segment",
        "detect.cross_track_task_threads",
        "detect.min_branches", "detect.min_observations", "detect.min_directed_transitions",
        "detect.bearing_window", "detect.stream_batch_size", "fitting.min_clips_per_pair",
        "runtime.mark_flush_every_n", "runtime.excluded_flush_threshold",
        "runtime.refine_flush_point_threshold",
    }
    for path in strictly_positive | non_negative_numbers | positive_integers:
        section, key = path.split(".")
        value = values.get(section, {}).get(key)
        minimum = 0 if path in non_negative_numbers else 0
        if value is not None and (not math.isfinite(value) or
                                  (value < minimum if path in non_negative_numbers else value <= minimum)):
            operator = ">= 0" if path in non_negative_numbers else "> 0"
            errors.append(f"{path}: must be finite and {operator}")
    for path in ("projection.max_scale_error", "runtime.encoding_confidence"):
        section, key = path.split(".")
        value = values.get(section, {}).get(key)
        if value is not None and not 0.0 <= value <= 1.0:
            errors.append(f"{path}: must be in [0, 1]")
    detect = values.get("detect", {})
    if detect.get("cross_track_task_threads", 4) > 8:
        errors.append("detect.cross_track_task_threads: must be <= 8")
    angle_paths = ("refine.min_heading_delta_deg", "refine.max_heading_delta_deg",
                   "detect.min_turn_deg", "detect.max_turn_deg",
                   "detect.cross_track_straightness_deg", "detect.cross_track_min_angle_deg",
                   "detect.cross_track_max_angle_deg", "detect.branch_threshold_deg")
    for path in angle_paths:
        section, key = path.split(".")
        value = values.get(section, {}).get(key)
        if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 180.0):
            errors.append(f"{path}: must be finite and in [0, 180]")
    for path in ("detect.spatial_window_overlap",):
        section, key = path.split(".")
        value = values.get(section, {}).get(key)
        if value is not None and not 0.0 <= value <= 1.0:
            errors.append(f"{path}: must be in [0, 1]")
    window = detect.get("spatial_window_size")
    if window is not None and (len(window) != 2 or any(not math.isfinite(v) or v <= 0 for v in window)):
        errors.append("detect.spatial_window_size: must contain two finite positive numbers")
    for low, high in (("min_turn_deg", "max_turn_deg"),
                      ("cross_track_min_angle_deg", "cross_track_max_angle_deg")):
        if low in detect and high in detect and detect[low] > detect[high]:
            errors.append(f"detect.{low}: must be <= detect.{high}")
    refine = values.get("refine", {})
    if ("min_heading_delta_deg" in refine and "max_heading_delta_deg" in refine
            and refine["min_heading_delta_deg"] > refine["max_heading_delta_deg"]):
        errors.append("refine.min_heading_delta_deg: must be <= refine.max_heading_delta_deg")
    postgres = values.get("postgres", {})
    if "port" in postgres and not 1 <= postgres["port"] <= 65535:
        errors.append("postgres.port: must be in [1, 65535]")
    for section, keys in {"postgres": ("host", "database", "user"),
                          "runtime": ("default_text_encoding",)}.items():
        for key in keys:
            value = values.get(section, {}).get(key)
            if value is not None and not value.strip():
                errors.append(f"{section}.{key}: must be non-empty")
    encodings = values.get("runtime", {}).get("fallback_encodings")
    if encodings is not None and (not encodings or any(not item.strip() for item in encodings)):
        errors.append("runtime.fallback_encodings: must be a non-empty list of non-empty strings")
    paths = values.get("paths", {})
    for key in ("data_root", "parquet_directory", "duckdb_postgres_extension_path"):
        value = paths.get(key)
        if value is not None and not value.is_absolute():
            errors.append(f"paths.{key}: must be absolute")
    if paths.get("data_root") == Path("/"):
        errors.append("paths.data_root: must not be filesystem root")


def load_pipeline_config(path: str | Path, *, postgres_password: str) -> PipelineConfig:
    """Load one complete config file; never merge files or Python defaults."""
    resolved = Path(path).resolve()
    errors = ([] if postgres_password.strip() and postgres_password.strip() != "change-me" else
              ["postgres.password: POSTGRES_PASSWORD is required and cannot be a placeholder"])
    try:
        with resolved.open("rb") as stream:
            payload = tomllib.load(stream)
    except FileNotFoundError:
        raise ConfigError(f"pipeline config not found: {resolved}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML pipeline config: {resolved}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read pipeline config: {resolved}: {exc}") from exc
    expected = set(_SECTION_TYPES) | {"parser"}
    errors.extend(f"{name}: unknown section" for name in sorted(set(payload) - expected))
    errors.extend(f"{name}: missing required section" for name in sorted(expected - set(payload)))
    values = {name: _collect_section(name, cls, payload.get(name), resolved.parent, errors)
              for name, cls in _SECTION_TYPES.items()}
    parser_raw = payload.get("parser")
    parser_values = {}
    if not isinstance(parser_raw, dict):
        errors.append("parser: section must be a table")
    else:
        errors.extend(f"parser.{key}: unknown key"
                      for key in sorted(set(parser_raw) - {"field_specs_path"}))
        field_path = parser_raw.get("field_specs_path")
        if not isinstance(field_path, str) or not field_path:
            errors.append("parser.field_specs_path: expected non-empty string")
        else:
            parser_values["field_specs_path"] = (resolved.parent / field_path).resolve()
    _validate(values, errors)
    fields = None
    if "field_specs_path" in parser_values:
        try:
            fields = load_field_config(parser_values["field_specs_path"])
        except ConfigError as exc:
            errors.append(f"parser.field_specs_path: {exc}")
    values["postgres"]["password"] = postgres_password
    built = {}
    for name, cls in _SECTION_TYPES.items():
        expected_count = len(dataclasses.fields(cls))
        if cls is PostgresConfig:
            expected_count -= 1
        if len(values[name]) == expected_count + (1 if cls is PostgresConfig else 0):
            try:
                built[name] = cls(**values[name])
            except (TypeError, ValueError) as exc:
                errors.append(f"{name}: {exc}")
    if errors:
        raise ConfigError("invalid pipeline configuration:\n- " + "\n- ".join(errors))
    return PipelineConfig(parser=ParserConfig(fields=fields, **parser_values), **built)
