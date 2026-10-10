"""Test-only factories derived from the complete checked-in Pipeline TOML."""
from __future__ import annotations

from dataclasses import replace, fields, make_dataclass, asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from track.v2.config import PipelineConfig, PostgresConfig, load_pipeline_config
from track.v2.intersections.detect_intersections import DetectRuntimeContext, _IntersectRunConfig


@lru_cache(maxsize=1)
def pipeline_config() -> PipelineConfig:
    path = Path(__file__).resolve().parents[2] / "config" / "pipeline.toml"
    return load_pipeline_config(path, postgres_password="test-password")


def postgres_config(**changes: Any) -> PostgresConfig:
    return replace(pipeline_config().postgres, **changes)


def projection_config(**changes: Any):
    return replace(pipeline_config().projection, **changes)


def refine_config(**changes: Any):
    return replace(pipeline_config().refine, **{"min_subtrack_points": 1, **changes})


def detect_config(**changes: Any):
    """Return the production plugin configuration without a legacy view."""
    return replace(pipeline_config().detect, **changes)


def intersect_config(**changes: Any):
    """Test-only flat parameter view for existing isolated algorithm tests."""
    from track.v2.config.detect_config import DetectConfig
    from track.v2.config.migrate_detect import GSTAR_FIELDS, CROSS_FIELDS
    base = pipeline_config().detect
    flat = {}
    for identity in ("g_star", "cross"):
        flat.update(asdict(base.candidate[identity].params["default"]))
    flat.update(use_turn_events=True, use_cross_track_candidates=True,
                candidate_dbscan_eps_m=base.cluster["dbscan"].params.eps_m,
                candidate_dbscan_min_samples=base.cluster["dbscan"].params.min_samples)
    common = {item.name: getattr(base, item.name) for item in fields(DetectConfig)}
    common.update({key: value for key, value in changes.items() if key in common})
    flat.update({key: value for key, value in changes.items() if key not in common})
    candidate = dict(common["candidate"])
    for identity in ("g_star", "turn_threshold", "cross"):
        algorithm = candidate[identity]
        params = algorithm.params["default"]
        overrides = {item.name: flat[item.name] for item in fields(params) if item.name in flat}
        try:
            updated_params = replace(params, **overrides)
        except ValueError:
            # Isolated kernel tests exercise their own invalid/fallback inputs.
            updated_params = params
        candidate[identity] = replace(algorithm, params={**algorithm.params, "default": updated_params})
    selected = tuple(identity for identity, enabled in (("g_star", flat["use_turn_events"]), ("cross", flat["use_cross_track_candidates"])) if enabled)
    common.update(candidate=candidate, candidates=selected)
    clustering = dict(common["cluster"])
    algorithm = clustering["dbscan"]
    clustering["dbscan"] = replace(algorithm, params=replace(algorithm.params,
        eps_m=flat["candidate_dbscan_eps_m"], min_samples=flat["candidate_dbscan_min_samples"]))
    common["cluster"] = clustering
    # as_dict used by runtime fixtures preserves typed plugin objects.
    def as_dict(self):
        return {item.name: getattr(self, item.name) for item in fields(DetectConfig)}
    cls = make_dataclass("AlgorithmTestConfig", [(key, type(value)) for key, value in flat.items()],
                         bases=(DetectConfig,), namespace={"as_dict": as_dict}, frozen=True)
    return cls(**common, **flat)


def detect_context(**changes: Any) -> DetectRuntimeContext:
    values = {
        "db_table": "trajectory_points",
        "kept_table": None,
        "source_table": None,
        "refine_id": None,
        "live_status": None,
    }
    values.update(changes)
    return DetectRuntimeContext(**values)


def intersect_run_config(**changes: Any) -> _IntersectRunConfig:
    context_fields = {
        key: changes.pop(key)
        for key in tuple(changes)
        if key in DetectRuntimeContext.__dataclass_fields__
    }
    flat = intersect_config(**changes)
    base_fields = set(_IntersectRunConfig.__dataclass_fields__)
    extras = {item.name: getattr(flat, item.name) for item in fields(flat) if item.name not in base_fields}
    cls = make_dataclass("AlgorithmRunTestConfig", [(key, type(value)) for key, value in extras.items()],
                         bases=(_IntersectRunConfig,), frozen=True, kw_only=True)
    return cls(**flat.as_dict(), **detect_context(**context_fields).__dict__, **extras)



def clip_config(**changes: Any):
    return replace(pipeline_config().clip, **changes)


def fitting_config(**changes: Any):
    return replace(pipeline_config().fitting, **changes)


def network_config(**changes: Any):
    return replace(pipeline_config().network, **changes)


def runtime_config(**changes: Any):
    if "duckdb_memory_limit" in changes and "duckdb_event_memory_limit" not in changes:
        changes["duckdb_event_memory_limit"] = changes["duckdb_memory_limit"]
    if "duckdb_memory_limit" in changes and "duckdb_grid_memory_limit" not in changes:
        changes["duckdb_grid_memory_limit"] = changes["duckdb_memory_limit"]
    return replace(pipeline_config().runtime, **changes)
