"""Test-only factories derived from the complete checked-in Pipeline TOML."""
from __future__ import annotations

from dataclasses import replace
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
    return replace(pipeline_config().refine, **changes)


def intersect_config(**changes: Any):
    return replace(pipeline_config().detect, **changes)


def detect_context(**changes: Any) -> DetectRuntimeContext:
    values = {
        "query_active_from_db": False,
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
    return _IntersectRunConfig(
        **intersect_config(**changes).as_dict(),
        **detect_context(**context_fields).__dict__,
    )


def clip_config(**changes: Any):
    return replace(pipeline_config().clip, **changes)


def fitting_config(**changes: Any):
    return replace(pipeline_config().fitting, **changes)


def network_config(**changes: Any):
    return replace(pipeline_config().network, **changes)


def runtime_config(**changes: Any):
    if "duckdb_memory_limit" in changes and "duckdb_event_memory_limit" not in changes:
        changes["duckdb_event_memory_limit"] = changes["duckdb_memory_limit"]
    return replace(pipeline_config().runtime, **changes)
