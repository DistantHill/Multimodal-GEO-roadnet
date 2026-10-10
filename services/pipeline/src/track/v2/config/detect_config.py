"""Strongly typed Detect orchestration and algorithm-owned parameter instances."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateAlgorithmConfig:
    output_instances: tuple[str, ...]
    params: dict[str, Any]


@dataclass(frozen=True)
class ClusterAlgorithmConfig:
    params: Any


@dataclass(frozen=True)
class DetectConfig:
    candidates: tuple[str, ...]
    cluster_algorithm: str
    candidate: dict[str, CandidateAlgorithmConfig]
    cluster: dict[str, ClusterAlgorithmConfig]
    merge_centroid_distance_m: float
    min_intersection_radius_m: float
    topology_radius_padding_m: float
    branch_threshold_deg: float
    min_branches: int
    min_observations: int
    min_directed_transitions: int
    bearing_window: int
    spatial_window_size: tuple[float, float]
    spatial_window_overlap: float
    stream_batch_size: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


IntersectConfig = DetectConfig
