"""Explicit conversion of legacy Detect snapshots; never invoked by loaders."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..errors import ConfigError


TURN_FIELDS = ("turn_window", "min_turn_deg", "max_turn_deg", "min_leg_m", "min_movement_speed_mps")
GSTAR_FIELDS = TURN_FIELDS + ("g_star_radius_m", "g_star_min_z")
CROSS_FIELDS = tuple("cross_track_" + name for name in (
    "min_avg_speed", "straightness_deg", "min_angle_deg", "max_angle_deg", "grid_cell_m",
    "pair_batch_size", "max_grid_cells_per_segment", "task_grid_m", "task_threads"))


def convert_legacy_detect(raw: dict[str, Any], *, turn_algorithm: str, cluster_algorithm: str) -> dict[str, Any]:
    """Return a new snapshot while preserving the original historical dictionary.

    Legacy algorithm choice lived outside Detect in experimental plans, so callers
    must provide that choice explicitly; conversion does not guess it.
    """
    if turn_algorithm not in {"g_star", "turn_threehold", "turn_angle"}:
        raise ConfigError(f"cannot convert legacy turn algorithm: {turn_algorithm}")
    if cluster_algorithm not in {"dbscan", "hdbscan"}:
        raise ConfigError(f"cannot convert legacy clustering algorithm: {cluster_algorithm}")
    turn_algorithm = "turn_threehold" if turn_algorithm == "turn_angle" else turn_algorithm
    required = {"use_turn_events", "use_cross_track_candidates", "candidate_dbscan_eps_m", "candidate_dbscan_min_samples", *GSTAR_FIELDS, *CROSS_FIELDS}
    missing = required - raw.keys()
    if missing:
        raise ConfigError("legacy detect missing required keys: " + ", ".join(sorted(missing)))
    if type(raw["use_turn_events"]) is not bool or type(raw["use_cross_track_candidates"]) is not bool:
        raise ConfigError("legacy Detect enable flags must be boolean")
    output = deepcopy(raw)
    output.pop("turn_candidate_algorithm", None)
    output.pop("clustering_algorithm", None)
    for key in required:
        output.pop(key)
    output.update(candidates=[], candidate={}, cluster_algorithm=cluster_algorithm,
                  cluster={cluster_algorithm: {"params": {"eps_m": raw["candidate_dbscan_eps_m"],
                                                        "min_samples": raw["candidate_dbscan_min_samples"]}}})
    for enabled, identity, keys in ((raw["use_turn_events"], turn_algorithm, GSTAR_FIELDS if turn_algorithm == "g_star" else TURN_FIELDS),
                                    (raw["use_cross_track_candidates"], "cross", CROSS_FIELDS)):
        if enabled:
            output["candidates"].append(identity)
            output["candidate"][identity] = {"output_instances": ["default"], "params": {"default": {key: raw[key] for key in keys}}}
    if not output["candidates"]:
        raise ConfigError("legacy Detect with no enabled candidates cannot be converted")
    return output
