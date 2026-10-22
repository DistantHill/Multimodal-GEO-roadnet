"""The only configuration authority is the selected pipeline.toml file."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import tomllib
from typing import Any

from pyproj import CRS


DEFAULTS: dict[str, Any] = {
    "version": 1,
    "input": {
        "kind": "parquet", "path": "", "dataset_id": "trajectories",
        "source_crs": "EPSG:4326", "processing_crs": "EPSG:32650",
        "timezone": "Asia/Shanghai", "timestamp_unit": "iso", "speed_unit": "km/h",
        "table": "public.trajectory_points", "dsn_env": "ROADNET_SOURCE_DSN",
        "columns": {"id": "id", "vehicle_id": "vehicle_id", "timestamp": "timestamp",
                    "x": "lon", "y": "lat", "heading": "heading", "speed": "speed"},
    },
    "output": {"root": "./workbench-output", "geojson": True},
    "resources": {
        "sample_interval_ms": 100, "soft_memory_mb": 4096, "max_wall_s": 0,
        "include_children": True, "heap_profile": False,
        "duckdb_memory_mb": 512, "threads": 2, "batch_rows": 65536,
        "max_vehicle_points": 1000000, "max_candidates": 1000000,
        "max_pair_checks": 100000000, "max_grid_memberships": 10000000,
        "max_fit_points": 1000000, "max_tile_segments": 500000,
    },
    "refine": {
        "max_gap_s": 120.0, "min_speed_mps": 0.5,
        "max_stationary_duration_s": 60.0, "stationary_radius_m": 15.0,
        "max_speed_mps": 70.0, "min_heading_delta_deg": 80.0,
        "max_heading_delta_deg": 100.0, "max_extension_m": 200.0,
        "min_subtrack_points": 2, "inject_orthogonal": True,
    },
    "detect": {
        "tile_size_m": 1000.0, "max_segment_length_m": 1000.0,
        "turn_window": 2, "min_turn_deg": 30.0, "max_turn_deg": 160.0,
        "min_leg_m": 5.0, "min_movement_speed_mps": 0.5,
        "cross_grid_size_m": 100.0, "min_cross_angle_deg": 30.0,
        "cross_straightness_deg": 30.0, "cross_min_speed_mps": 0.0,
        "cluster_eps_m": 25.0, "cluster_min_samples": 3,
        "topology_radius_m": 30.0, "arm_merge_angle_deg": 25.0,
        "min_arms": 3, "min_vehicles": 2,
    },
    "clip": {"include_boundary_clips": True, "min_length_m": 5.0},
    "fit": {"fit_sample_spacing_m": 5.0, "point_spacing_m": 5.0,
            "min_clips_per_pair": 2, "emit_directional": True},
    "network": {"include_isolated_nodes": True},
    "labels": {},
    "tuning": {"n_trials": 30, "seed": 42, "study_name": "roadnet",
               "objective": "intersection_f1", "startup_trials": 5,
               "warmup_steps": 0, "context_halo_m": 1200.0,
               "matching_radius_m": 40.0, "line_tolerance_m": 10.0,
               "search_space": {}},
    "database": {"enabled": False, "dsn_env": "ROADNET_RESULT_DSN",
                 "schema": "roadnet_workbench"},
}


def _merge(default: dict, supplied: dict, prefix: str = "") -> dict:
    if not isinstance(supplied, dict):
        raise ValueError(f"{prefix or 'configuration'} must be a table")
    result = deepcopy(default)
    for key, value in supplied.items():
        name = prefix + key
        if name == "tuning.timeout_s":
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("tuning.timeout_s must be positive")
            result[key] = float(value)
            continue
        if key not in default:
            raise ValueError(f"Unknown configuration key: {name}")
        if name in {"labels", "tuning.search_space"}:
            result[key] = deepcopy(value)
        elif isinstance(default[key], dict):
            result[key] = _merge(default[key], value, name + ".")
        else:
            expected = type(default[key])
            if expected is float:
                valid = type(value) in (int, float) and math.isfinite(value)
            elif expected is int and name in {"resources.soft_memory_mb", "resources.max_wall_s"}:
                valid = type(value) in (int, float) and math.isfinite(value)
            else:
                valid = type(value) is expected
            if not valid:
                raise ValueError(f"{name} expects {expected.__name__}, received {type(value).__name__}")
            result[key] = float(value) if expected is float else value
    return result


def validate_config(config: dict) -> dict:
    """Also validate sampled trial configurations, before executing native code."""
    cfg = _merge(DEFAULTS, config)
    if cfg["version"] != 1:
        raise ValueError("Unsupported configuration version; expected version=1")
    source, metric = CRS(cfg["input"]["source_crs"]), CRS(cfg["input"]["processing_crs"])
    if not metric.is_projected or any(abs(a.unit_conversion_factor - 1) > 1e-9 for a in metric.axis_info[:2]):
        raise ValueError("input.processing_crs must be a projected CRS with metre axes")
    if source is None:  # CRS construction above intentionally rejects unknown codes.
        raise ValueError("input.source_crs is required")
    if cfg["input"]["kind"] not in {"parquet", "csv", "postgres"}:
        raise ValueError("input.kind must be parquet, csv or postgres")
    if cfg["input"]["kind"] != "postgres" and not cfg["input"]["path"]:
        raise ValueError("input.path must name a file, directory or glob")
    if cfg["input"]["timestamp_unit"] not in {"iso", "s", "ms", "us", "ns"}:
        raise ValueError("input.timestamp_unit must be iso, s, ms, us or ns")
    if cfg["input"]["speed_unit"] not in {"m/s", "km/h"}:
        raise ValueError("input.speed_unit must be m/s or km/h")
    if any(not v for k, v in cfg["input"]["columns"].items() if k not in {"heading", "speed"}):
        raise ValueError("id, vehicle_id, timestamp, x and y column mappings are required")
    for section in ("refine", "detect", "clip", "fit", "resources", "tuning"):
        for key, value in cfg[section].items():
            if type(value) in (int, float) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{section}.{key} must be finite and nonnegative")
    positive = {
        "resources": ["sample_interval_ms", "duckdb_memory_mb", "threads", "batch_rows", "max_vehicle_points", "max_candidates", "max_pair_checks", "max_grid_memberships", "max_fit_points", "max_tile_segments"],
        "refine": ["max_gap_s", "max_speed_mps", "stationary_radius_m", "min_subtrack_points"],
        "detect": ["tile_size_m", "max_segment_length_m", "turn_window", "cross_grid_size_m", "cluster_eps_m", "cluster_min_samples", "topology_radius_m", "arm_merge_angle_deg", "min_arms", "min_vehicles"],
        "fit": ["fit_sample_spacing_m", "point_spacing_m", "min_clips_per_pair"],
        "tuning": ["n_trials", "matching_radius_m", "line_tolerance_m"],
    }
    for section, keys in positive.items():
        for key in keys:
            if cfg[section][key] <= 0:
                raise ValueError(f"{section}.{key} must be > 0")
    for section, lo, hi in [("refine", "min_heading_delta_deg", "max_heading_delta_deg"), ("detect", "min_turn_deg", "max_turn_deg")]:
        if not 0 <= cfg[section][lo] <= cfg[section][hi] <= 180:
            raise ValueError(f"{section}.{lo}/{hi} must satisfy 0 <= min <= max <= 180")
    for key in ("min_cross_angle_deg", "cross_straightness_deg", "arm_merge_angle_deg"):
        if cfg["detect"][key] > 180:
            raise ValueError(f"detect.{key} must be <= 180")
    if cfg["detect"]["min_cross_angle_deg"] > 90:
        raise ValueError("detect.min_cross_angle_deg must be <= 90 for undirected line intersections")
    if cfg["detect"]["arm_merge_angle_deg"] >= 180:
        raise ValueError("detect.arm_merge_angle_deg must be < 180")
    if cfg["resources"]["sample_interval_ms"] > 60000:
        raise ValueError("resources.sample_interval_ms must be <= 60000")
    if cfg["resources"]["soft_memory_mb"] and cfg["resources"]["duckdb_memory_mb"] >= cfg["resources"]["soft_memory_mb"]:
        raise ValueError("duckdb_memory_mb must leave memory for native operators and file buffers")
    if cfg["refine"]["min_subtrack_points"] < 2:
        raise ValueError("refine.min_subtrack_points must be >= 2")
    if cfg["tuning"]["objective"] not in {"intersection_f1", "centerline_f1", "combined_f1"}:
        raise ValueError("Unknown tuning.objective")
    return cfg


def load_config(path: Path | str = "pipeline.toml") -> dict:
    path = Path(path).resolve()
    with path.open("rb") as handle:
        cfg = validate_config(tomllib.load(handle))
    for section, key in (("input", "path"), ("output", "root")):
        if cfg[section][key]:
            value = Path(cfg[section][key]).expanduser()
            cfg[section][key] = str(value if value.is_absolute() else path.parent / value)
    for kind in ("intersections", "centerlines", "regions"):
        item = cfg["labels"].get(kind)
        if item and item.get("path"):
            value = Path(item["path"]).expanduser()
            item["path"] = str(value if value.is_absolute() else path.parent / value)
    return cfg
