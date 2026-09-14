"""track.v2 — Step-based trajectory pipeline.

Architecture (linear):
    Parser  →  Refine  →  Detect  →  Clip  →  Fitting  →  Network

Each stage has a single public entry point and produces frozen intermediate
artifacts. No stage mutates another stage's output.

Detect uses two complementary methods:
  1. Turn events: bearing-change detection → Getis-Ord G* → DBSCAN
  3. Cross-track candidates: cross-vehicle segment intersections → DBSCAN
Both feed into the same topology verification (entry/exit → branch clustering).
"""

from importlib import import_module as _import_module


# Importing a lightweight coordinator must not initialize numerical libraries
# before its spawned process initializer can configure native thread counts.
# Public stage functions and model classes retain their original identities.
_MODULE_EXPORTS = {
    "refine": ("refine_vehicle",),
    "clip": ("extract_clips", "extract_clips_for_detect"),
    "fitting": ("fit_centerlines", "fit_centerline_variants"),
    "detect": ("IntersectConfig", "IntersectResult", "detect_intersections"),
    "network": ("build_network",),
    "models": (
        "Centerline", "CenterlineFitBundle", "RefineConfig", "ClipConfig",
        "ClipEndpoint", "ClipInput", "ClipIntersection", "ClipPoint", "ClipReport",
        "ClipResult", "ClipTrack", "CsvSource", "FitIssue", "FitReport", "FitResult",
        "FittingConfig", "NetworkConfig", "NetworkEdge", "NetworkIssue", "NetworkNode",
        "NetworkReport", "NetworkResult", "PointRef", "PostgresSource", "ProjectionConfig",
        "ResolvedTrackPoint", "SpatialContext", "TrackClip", "TrajectoryPoint",
    ),
    "parser": ("get_database",),
    "projection": (
        "ProjectionError", "QueryWindow", "QueryWindowRequest", "build_query_window",
        "create_spatial_context",
    ),
    "replay": ("ReplayService", "ReplayTarget", "replay_centerline_inputs"),
    "pipeline": ("PipelineInput", "PipelineResult"),
}
_EXPORT_MODULE = {
    name: module for module, names in _MODULE_EXPORTS.items() for name in names
}
__all__ = list(_EXPORT_MODULE)


def __getattr__(name: str):
    module = _EXPORT_MODULE.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
