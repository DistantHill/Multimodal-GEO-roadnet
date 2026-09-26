"""Stage-level persistence seam for the v2 pipeline.

ZH: 以阶段语义包装现有 PostgreSQL writer/query 行为。事务仍由 runner
    明确控制；此模块不在隐式 helper 中提交或回滚。
EN: Provide a stage-level wrapper around the existing PostgreSQL writers and
    queries. The runner still owns transaction boundaries; no persistence
    method commits or rolls back implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from ..telemetry.heartbeat import validation_notices
from ..clips.extract_clips import extract_clips_for_detect
from .artifact_registry import ArtifactDescriptor, insert_present
from ..intersections.query_results import fetch_detect_row
from .write_refine_results import (
    RefineRunRecord,
    create_refine_run,
    fetch_refine_run,
    finish_refine_run,
    insert_excluded_points,
    insert_injected_points,
    insert_kept_trajectory_points,
)
from .write_detect_results import (
    create_detect_run,
    finish_detect_run,
    persist_detect_result,
)
from .write_fitting_results import (
    create_fit_run,
    finish_fit_run,
    insert_centerline_bundle,
)
from ..intersections.detect_intersections import IntersectResult
from ..models import (
    CenterlineFitBundle,
    ClipConfig,
    ClipResult,
    ExcludedBatch,
    FittingConfig,
    RefinementSubtrack,
    RuntimeConfig,
    SpatialContext,
)




@dataclass(frozen=True, slots=True)
class RefineBatchResult:
    """Counts and cursor state produced by one persisted Refine batch."""

    next_subtrack_index: int
    kept_count: int


@dataclass(frozen=True, slots=True)
class DetectPersistenceResult:
    """Persisted Detect-stage row counts."""

    resolved_point_count: int
    tracks_cross_count: int
    turn_event_count: int
    candidate_intersection_count: int
    candidate_entry_exit_point_count: int
    intersection_count: int
    entry_exit_point_count: int
    resolved_sequence_elapsed_s: float = 0.0


class PipelineStore(Protocol):
    """Small stage-oriented persistence interface used by the pipeline runner."""

    @property
    def connection(self) -> Any:
        """Return the wrapped connection for stage queries that need it."""

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def get_refine_run(self, refine_id: str) -> RefineRunRecord: ...

    def start_refine_run(
        self,
        *,
        source_table: str,
        parameters: dict[str, Any],
        spatial_context: SpatialContext,
    ) -> str: ...

    def finish_refine(
        self,
        refine_id: str,
        *,
        status: str,
        source_fingerprint: dict[str, object] | None = None,
    ) -> None: ...

    def complete_refine_with_artifact(
        self,
        refine_id: str,
        descriptor: ArtifactDescriptor,
        source_fingerprint: dict[str, object],
    ) -> None: ...

    def persist_refine_batch(
        self,
        refine_id: str,
        pending_excluded: list[ExcludedBatch],
        pending_subtracks: list[RefinementSubtrack],
        subtrack_index: int,
        *,
        spatial_context: SpatialContext,
        batch_size: int,
    ) -> RefineBatchResult: ...

    def start_detect_run(
        self,
        *,
        refine_id: str,
        parameters: dict[str, Any],
    ) -> str: ...

    def persist_detect(
        self,
        detect_id: str,
        result: IntersectResult,
        *,
        runtime: RuntimeConfig,
        data_root: Path | str,
        batch_size: int,
    ) -> DetectPersistenceResult: ...

    def finish_detect(self, detect_id: str, *, status: str) -> None: ...

    def complete_detect_with_artifact(
        self, detect_id: str, descriptor: ArtifactDescriptor,
    ) -> None: ...

    def load_clip(
        self,
        detect_id: str,
        *,
        publication_root: Path | str,
        data_root: Path | str,
        runtime: RuntimeConfig,
        config: ClipConfig,
        verify_sha256: bool,
    ) -> ClipResult: ...

    def start_fit_run(
        self,
        *,
        detect_id: str,
        clip_config: ClipConfig,
        fitting_config: FittingConfig,
        parameters: dict[str, Any],
    ) -> str: ...

    def persist_fit_bundle(
        self,
        fit_id: str,
        detect_id: str,
        bundle: CenterlineFitBundle,
        clips: ClipResult,
    ) -> tuple[int, ...]: ...

    def finish_fit(
        self,
        fit_id: str,
        *,
        status: str,
        report: dict[str, Any] | None = None,
    ) -> None: ...


class PostgresPipelineStore:
    """PostgreSQL adapter that delegates to the existing v2 persistence code."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @property
    def connection(self) -> Any:
        return self._conn

    def commit(self) -> None:
        with validation_notices(self._conn):
            self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def get_refine_run(self, refine_id: str) -> RefineRunRecord:
        return fetch_refine_run(self._conn, refine_id)

    def start_refine_run(
        self,
        *,
        source_table: str,
        parameters: dict[str, Any],
        spatial_context: SpatialContext,
    ) -> str:
        return create_refine_run(
            self._conn,
            source_table=source_table,
            parameters=parameters,
            spatial_context=spatial_context,
        )

    def finish_refine(
        self,
        refine_id: str,
        *,
        status: str,
        source_fingerprint: dict[str, object] | None = None,
    ) -> None:
        finish_refine_run(
            self._conn,
            refine_id,
            status=status,
            source_fingerprint=source_fingerprint,
        )

    def complete_refine_with_artifact(
        self,
        refine_id: str,
        descriptor: ArtifactDescriptor,
        source_fingerprint: dict[str, object],
    ) -> None:
        """Register a present artifact and complete its Run without committing."""
        if descriptor.stage != "refine" or str(descriptor.refine_id) != str(refine_id):
            raise ValueError("artifact does not belong to the Refine Run")
        with self._conn.cursor() as cur:
            insert_present(cur, descriptor)
        finish_refine_run(
            self._conn,
            refine_id,
            status="completed",
            source_fingerprint=source_fingerprint,
        )

    def persist_refine_batch(
        self,
        refine_id: str,
        pending_excluded: list[ExcludedBatch],
        pending_subtracks: list[RefinementSubtrack],
        subtrack_index: int,
        *,
        spatial_context: SpatialContext,
        batch_size: int,
    ) -> RefineBatchResult:
        run_row = fetch_refine_run(self._conn, refine_id)
        if run_row.status != "running":
            raise ValueError(f"Refine Run {refine_id} is not running")
        if run_row.spatial_context != spatial_context:
            raise ValueError(
                f"Refine Run {refine_id} SpatialContext does not match the batch"
            )
        if pending_excluded:
            for excluded in pending_excluded:
                insert_excluded_points(self._conn, excluded, refine_id=refine_id,
                                       batch_size=batch_size)
            pending_excluded.clear()

        kept_count = 0
        if pending_subtracks:
            assigned_subtracks = [
                replace(subtrack, subtrack_index=subtrack_index + offset)
                for offset, subtrack in enumerate(pending_subtracks)
            ]
            kept_count = insert_kept_trajectory_points(
                self._conn,
                assigned_subtracks,
                refine_id=refine_id,
            )
            insert_injected_points(
                self._conn,
                assigned_subtracks,
                refine_id=refine_id,
                spatial_context=spatial_context,
                batch_size=batch_size,
            )
            subtrack_index += len(assigned_subtracks)
            pending_subtracks.clear()

        return RefineBatchResult(
            next_subtrack_index=subtrack_index,
            kept_count=kept_count,
        )

    def start_detect_run(
        self,
        *,
        refine_id: str,
        parameters: dict[str, Any],
    ) -> str:
        return create_detect_run(
            self._conn,
            refine_id=refine_id,
            parameters=parameters,
        )

    def persist_detect(
        self,
        detect_id: str,
        result: IntersectResult,
        *,
        runtime: RuntimeConfig,
        data_root: Path | str,
        batch_size: int,
    ) -> DetectPersistenceResult:
        spatial_context = result.spatial_context
        run_row = fetch_detect_row(self._conn, detect_id)
        if run_row.status != "running":
            raise ValueError(f"Detect Run {detect_id} is not running")
        if run_row.spatial_context != spatial_context:
            raise ValueError(
                f"Detect Run {detect_id} SpatialContext does not match its Refine Run"
            )
        counts = persist_detect_result(
            self._conn,
            detect_id,
            result,
            runtime=runtime,
            data_root=data_root,
            batch_size=batch_size,
        )
        return DetectPersistenceResult(
            resolved_point_count=counts.resolved_point_count,
            tracks_cross_count=counts.tracks_cross_count,
            turn_event_count=counts.turn_event_count,
            candidate_intersection_count=counts.candidate_intersection_count,
            candidate_entry_exit_point_count=counts.candidate_entry_exit_point_count,
            intersection_count=counts.intersection_count,
            entry_exit_point_count=counts.entry_exit_point_count,
            resolved_sequence_elapsed_s=counts.resolved_sequence_elapsed_s,
        )

    def finish_detect(self, detect_id: str, *, status: str) -> None:
        finish_detect_run(self._conn, detect_id, status=status)

    def complete_detect_with_artifact(
        self, detect_id: str, descriptor: ArtifactDescriptor,
    ) -> None:
        """Register a present artifact and complete its Run without committing."""
        if descriptor.stage != "detect" or str(descriptor.detect_id) != str(detect_id):
            raise ValueError("artifact does not belong to the Detect Run")
        with self._conn.cursor() as cur:
            insert_present(cur, descriptor)
        finish_detect_run(self._conn, detect_id, status="completed")

    def load_clip(
        self,
        detect_id: str,
        *,
        publication_root: Path | str,
        data_root: Path | str,
        runtime: RuntimeConfig,
        config: ClipConfig,
        verify_sha256: bool,
    ) -> ClipResult:
        return extract_clips_for_detect(
            self._conn, detect_id, publication_root=publication_root, data_root=data_root,
            runtime=runtime, config=config, verify_sha256=verify_sha256,
        )

    def start_fit_run(
        self,
        *,
        detect_id: str,
        clip_config: ClipConfig,
        fitting_config: FittingConfig,
        parameters: dict[str, Any],
    ) -> str:
        return create_fit_run(
            self._conn,
            detect_id=detect_id,
            clip_config=clip_config,
            fitting_config=fitting_config,
            parameters=parameters,
        )

    def persist_fit_bundle(
        self,
        fit_id: str,
        detect_id: str,
        bundle: CenterlineFitBundle,
        clips: ClipResult,
    ) -> tuple[int, ...]:
        run_row = fetch_detect_row(self._conn, detect_id)
        if run_row.status != "completed":
            raise ValueError(f"Detect Run {detect_id} is not completed")
        if clips.spatial_context != run_row.spatial_context:
            raise ValueError(
                f"ClipResult SpatialContext does not match Detect Run {detect_id}"
            )
        if clips.detect_id is not None and clips.detect_id != detect_id:
            raise ValueError("ClipResult belongs to a different Detect Run")
        for result in (bundle.combined, bundle.a_to_b, bundle.b_to_a):
            if result.spatial_context != run_row.spatial_context:
                raise ValueError(
                    f"FitResult SpatialContext does not match Detect Run {detect_id}"
                )
            if result.detect_id is not None and result.detect_id != detect_id:
                raise ValueError("FitResult belongs to a different Detect Run")
        return insert_centerline_bundle(
            self._conn,
            bundle,
            clips,
            fit_id=fit_id,
            detect_id=detect_id,
        )

    def finish_fit(
        self,
        fit_id: str,
        *,
        status: str,
        report: dict[str, Any] | None = None,
    ) -> None:
        finish_fit_run(self._conn, fit_id, status=status, report=report)
