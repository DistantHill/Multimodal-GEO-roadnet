"""Stage-level persistence seam for the v2 pipeline.

ZH: 以阶段语义包装现有 PostgreSQL writer/query 行为。事务仍由 runner
    明确控制；此模块不在隐式 helper 中提交或回滚。
EN: Provide a stage-level wrapper around the existing PostgreSQL writers and
    queries. The runner still owns transaction boundaries; no persistence
    method commits or rolls back implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from ..clip import extract_clips_for_detect
from .queries import fetch_detect_row
from ..db_writer import (
    CleanRunRecord,
    create_clean_run,
    create_detect_run,
    create_fit_run,
    fetch_clean_run,
    finish_clean_run,
    finish_detect_run,
    finish_fit_run,
    insert_candidate_entry_exit_points,
    insert_candidate_intersection_events,
    insert_candidate_intersections,
    insert_centerline_bundle,
    insert_entry_exit_points,
    insert_excluded_points,
    insert_injected_points,
    insert_intersections,
    insert_kept_trajectory_points,
    insert_tracks_cross,
    insert_trajectory_points,
    insert_turn_events,
)
from ..intersect2 import IntersectResult
from ..models import (
    CenterlineFitBundle,
    ClipConfig,
    ClipResult,
    ExcludedIndex,
    ExcludedPoint,
    FittingConfig,
    RefinementSubtrack,
    SpatialContext,
    TrajectoryPoint,
)


_DEFAULT_BATCH_SIZE = 5000


@dataclass(frozen=True, slots=True)
class CleanBatchResult:
    """Counts and cursor state produced by one persisted Clean batch."""

    next_subtrack_index: int
    kept_count: int


@dataclass(frozen=True, slots=True)
class DetectPersistenceResult:
    """Counts and database-id maps needed by the rest of the runner."""

    tracks_cross_count: int
    turn_event_ids: dict[int, int]
    candidate_intersection_ids: dict[int, int]
    candidate_entry_exit_ids: dict[tuple[int, int], int]
    intersection_ids: dict[int, int]
    turn_event_count: int
    candidate_intersection_count: int
    candidate_intersection_event_count: int
    candidate_entry_exit_point_count: int
    intersection_count: int
    entry_exit_point_count: int


class PipelineStore(Protocol):
    """Small stage-oriented persistence interface used by the pipeline runner."""

    @property
    def connection(self) -> Any:
        """Return the wrapped connection for stage queries that need it."""

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def get_clean_run(self, clean_id: str) -> CleanRunRecord: ...

    def start_clean_run(
        self,
        *,
        source_id: str,
        source_kind: str,
        parameters: dict[str, Any],
        spatial_context: SpatialContext,
    ) -> str: ...

    def finish_clean(self, clean_id: str, *, status: str) -> None: ...

    def write_raw_points(
        self,
        points: list[TrajectoryPoint],
        *,
        table: str = "trajectory_points",
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> dict[tuple[str, str], int]: ...

    def persist_clean_batch(
        self,
        clean_id: str,
        table: str,
        point_id_map: dict[tuple[str, str], int],
        pending_excluded: list[ExcludedPoint],
        pending_subtracks: list[RefinementSubtrack],
        pending_point_keys: list[tuple[str, str]],
        subtrack_index: int,
        *,
        spatial_context: SpatialContext,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> CleanBatchResult: ...

    def start_detect_run(
        self,
        *,
        clean_id: str,
        parameters: dict[str, Any],
    ) -> str: ...

    def persist_detect(
        self,
        detect_id: str,
        result: IntersectResult,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> DetectPersistenceResult: ...

    def finish_detect(self, detect_id: str, *, status: str) -> None: ...

    def load_clip(
        self,
        detect_id: str,
        *,
        config: ClipConfig | None = None,
    ) -> ClipResult: ...

    def start_fit_run(
        self,
        *,
        detect_id: str,
        clip_config: ClipConfig,
        fitting_config: FittingConfig,
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
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def get_clean_run(self, clean_id: str) -> CleanRunRecord:
        return fetch_clean_run(self._conn, clean_id)

    def start_clean_run(
        self,
        *,
        source_id: str,
        source_kind: str,
        parameters: dict[str, Any],
        spatial_context: SpatialContext,
    ) -> str:
        return create_clean_run(
            self._conn,
            source_id=source_id,
            source_kind=source_kind,
            parameters=parameters,
            spatial_context=spatial_context,
        )

    def finish_clean(self, clean_id: str, *, status: str) -> None:
        finish_clean_run(self._conn, clean_id, status=status)

    def write_raw_points(
        self,
        points: list[TrajectoryPoint],
        *,
        table: str = "trajectory_points",
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> dict[tuple[str, str], int]:
        return insert_trajectory_points(
            self._conn,
            points,
            table=table,
            batch_size=batch_size,
        )

    def persist_clean_batch(
        self,
        clean_id: str,
        table: str,
        point_id_map: dict[tuple[str, str], int],
        pending_excluded: list[ExcludedPoint],
        pending_subtracks: list[RefinementSubtrack],
        pending_point_keys: list[tuple[str, str]],
        subtrack_index: int,
        *,
        spatial_context: SpatialContext,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> CleanBatchResult:
        run_row = fetch_clean_run(self._conn, clean_id)
        if run_row.status != "running":
            raise ValueError(f"Clean Run {clean_id} is not running")
        if run_row.spatial_context != spatial_context:
            raise ValueError(
                f"Clean Run {clean_id} SpatialContext does not match the batch"
            )
        if pending_excluded:
            insert_excluded_points(
                self._conn,
                ExcludedIndex(items=list(pending_excluded)),
                clean_id=clean_id,
                batch_size=batch_size,
            )
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
                point_id_map,
                clean_id=clean_id,
            )
            insert_injected_points(
                self._conn,
                assigned_subtracks,
                clean_id=clean_id,
                spatial_context=spatial_context,
                batch_size=batch_size,
            )
            subtrack_index += len(assigned_subtracks)
            pending_subtracks.clear()

        for key in pending_point_keys:
            point_id_map.pop(key, None)
        pending_point_keys.clear()
        return CleanBatchResult(
            next_subtrack_index=subtrack_index,
            kept_count=kept_count,
        )

    def start_detect_run(
        self,
        *,
        clean_id: str,
        parameters: dict[str, Any],
    ) -> str:
        return create_detect_run(
            self._conn,
            clean_id=clean_id,
            parameters=parameters,
        )

    def persist_detect(
        self,
        detect_id: str,
        result: IntersectResult,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> DetectPersistenceResult:
        spatial_context = result.spatial_context
        run_row = fetch_detect_row(self._conn, detect_id)
        if run_row.status != "running":
            raise ValueError(f"Detect Run {detect_id} is not running")
        if run_row.spatial_context != spatial_context:
            raise ValueError(
                f"Detect Run {detect_id} SpatialContext does not match its Clean Run"
            )
        tracks_cross_count = insert_tracks_cross(
            self._conn,
            result.cross_track_candidates,
            detect_id=detect_id,
            spatial_context=spatial_context,
            batch_size=batch_size,
        )
        turn_event_ids = insert_turn_events(
            self._conn,
            result.turn_events,
            detect_id=detect_id,
            spatial_context=spatial_context,
            batch_size=batch_size,
        )
        candidate_intersection_ids = insert_candidate_intersections(
            self._conn,
            result.clusters,
            detect_id=detect_id,
            spatial_context=spatial_context,
        )
        insert_candidate_intersection_events(
            self._conn,
            result.clusters,
            candidate_intersection_ids,
            turn_event_ids,
            detect_id=detect_id,
            batch_size=batch_size,
        )
        candidate_entry_exit_ids = insert_candidate_entry_exit_points(
            self._conn,
            result.clusters,
            candidate_intersection_ids,
            detect_id=detect_id,
            spatial_context=spatial_context,
            batch_size=batch_size,
        )
        clusters_by_id = {cluster.cluster_id: cluster for cluster in result.clusters}
        intersection_ids = insert_intersections(
            self._conn,
            result.topologies,
            candidate_intersection_ids,
            clusters_by_id,
            detect_id=detect_id,
            spatial_context=spatial_context,
        )
        insert_entry_exit_points(
            self._conn,
            result.observations,
            intersection_ids,
            candidate_entry_exit_ids,
            detect_id=detect_id,
            batch_size=batch_size,
        )
        return DetectPersistenceResult(
            tracks_cross_count=tracks_cross_count,
            turn_event_ids=turn_event_ids,
            candidate_intersection_ids=candidate_intersection_ids,
            candidate_entry_exit_ids=candidate_entry_exit_ids,
            intersection_ids=intersection_ids,
            turn_event_count=len(result.turn_events),
            candidate_intersection_count=len(result.clusters),
            candidate_intersection_event_count=sum(
                len(cluster.events) for cluster in result.clusters
            ),
            candidate_entry_exit_point_count=sum(
                len(cluster.crossing_points) for cluster in result.clusters
            ),
            intersection_count=len(intersection_ids),
            entry_exit_point_count=sum(
                1
                for observation in result.observations
                if observation.cluster_id in intersection_ids
            ),
        )

    def finish_detect(self, detect_id: str, *, status: str) -> None:
        finish_detect_run(self._conn, detect_id, status=status)

    def load_clip(
        self,
        detect_id: str,
        *,
        config: ClipConfig | None = None,
    ) -> ClipResult:
        return extract_clips_for_detect(self._conn, detect_id, config=config)

    def start_fit_run(
        self,
        *,
        detect_id: str,
        clip_config: ClipConfig,
        fitting_config: FittingConfig,
    ) -> str:
        return create_fit_run(
            self._conn,
            detect_id=detect_id,
            clip_config=clip_config,
            fitting_config=fitting_config,
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
