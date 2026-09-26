"""Typed references and results exchanged by Pipeline stage runners."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db.artifact_registry import ArtifactDescriptor
from ..db.pipeline_store import DetectPersistenceResult
from ..models import (
    CenterlineFitBundle,
    ClipResult,
    NetworkResult,
    SpatialContext,
)


@dataclass(frozen=True, slots=True)
class RefineStageRef:
    refine_id: str
    source_table: str
    spatial_context: SpatialContext


@dataclass(frozen=True, slots=True)
class RefineStageResult:
    ref: RefineStageRef
    source_point_count: int
    kept_point_count: int
    excluded_point_count: int
    injected_point_count: int
    vehicle_count: int
    artifact: ArtifactDescriptor
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class DetectStageRef:
    detect_id: str
    refine: RefineStageRef


@dataclass(frozen=True, slots=True)
class DetectStageResult:
    ref: DetectStageRef
    artifact: ArtifactDescriptor
    persisted: DetectPersistenceResult
    compute_elapsed_s: float
    persist_elapsed_s: float
    elapsed_s: float


@dataclass(slots=True)
class ClipStageResult:
    result: ClipResult
    assembly_elapsed_s: float
    elapsed_s: float
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.result.clips, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> "ClipStageResult":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class FittingStageRef:
    fit_id: str
    detect: DetectStageRef


@dataclass(frozen=True, slots=True)
class FittingStageResult:
    ref: FittingStageRef
    bundle: CenterlineFitBundle
    centerline_ids: tuple[int, ...]
    compute_elapsed_s: float
    persist_elapsed_s: float
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class NetworkStageResult:
    result: NetworkResult
    elapsed_s: float
