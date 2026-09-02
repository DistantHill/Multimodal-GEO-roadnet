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
    insert_candidate_intersections,
    insert_centerline_bundle,
    insert_entry_exit_points,
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
    FittingConfig,
    RefinementSubtrack,
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
    candidate_entry_exit_point_count: int
    intersection_count: int
    entry_exit_point_count: int


def _candidate_memberships(
    clusters: list[Any],
    candidate_intersection_ids: dict[int, int],
) -> tuple[
    dict[int, int],
    dict[tuple[str, str, str, str, str], int],
]:
    """Build exclusive A/C child-to-candidate assignments."""
    turn_membership: dict[int, int] = {}
    cross_membership: dict[tuple[str, str, str, str, str], int] = {}

    def assign(mapping: dict[Any, int], key: Any, candidate_id: int) -> None:
        previous = mapping.get(key)
        if previous is not None and previous != candidate_id:
            raise ValueError(
                f"event {key!r} belongs to multiple candidate intersections: "
                f"{previous}, {candidate_id}"
            )
        mapping[key] = candidate_id

    for cluster in clusters:
        candidate_id = candidate_intersection_ids.get(cluster.cluster_id)
        if candidate_id is None:
            continue
        for event in cluster.events:
            if event.source == "turn":
                assign(turn_membership, event.event_id, candidate_id)
            elif event.source == "orthogonal_cross_track":
                key = (
                    event.vehicle_id,
                    str(event.from_source_id or ""),
                    str(event.from_source_row_key or ""),
                    str(event.to_source_id or ""),
                    str(event.to_source_row_key or ""),
                )
                assign(cross_membership, key, candidate_id)
            # ``orthogonal`` is an in-memory Method 2 observation. It has no
            # Detect-level A/C row on which to store reverse membership.
            elif event.source != "orthogonal":
                raise ValueError(f"unsupported candidate event source: {event.source}")
    return turn_membership, cross_membership


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
        pending_excluded: list[Any],
        pending_subtracks: list[RefinementSubtrack],
        pending_point_keys: list[tuple[str, str]],
        subtrack_index: int,
        *,
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
    ) -> str:
        return create_clean_run(
            self._conn,
            source_id=source_id,
            source_kind=source_kind,
            parameters=parameters,
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
        pending_excluded: list[Any],
        pending_subtracks: list[RefinementSubtrack],
        pending_point_keys: list[tuple[str, str]],
        subtrack_index: int,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> CleanBatchResult:
        if pending_excluded:
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
        candidate_intersection_ids = insert_candidate_intersections(
            self._conn,
            result.clusters,
            detect_id=detect_id,
        )
        turn_membership, cross_membership = _candidate_memberships(
            result.clusters,
            candidate_intersection_ids,
        )
        tracks_cross_count = insert_tracks_cross(
            self._conn,
            result.cross_track_candidates,
            detect_id=detect_id,
            candidate_intersection_ids=cross_membership,
            batch_size=batch_size,
        )
        turn_event_ids = insert_turn_events(
            self._conn,
            result.turn_events,
            detect_id=detect_id,
            candidate_intersection_ids=turn_membership,
            batch_size=batch_size,
        )
        candidate_entry_exit_ids = insert_candidate_entry_exit_points(
            self._conn,
            result.clusters,
            candidate_intersection_ids,
            detect_id=detect_id,
            batch_size=batch_size,
        )
        clusters_by_id = {cluster.cluster_id: cluster for cluster in result.clusters}
        intersection_ids = insert_intersections(
            self._conn,
            result.topologies,
            candidate_intersection_ids,
            clusters_by_id,
            detect_id=detect_id,
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
