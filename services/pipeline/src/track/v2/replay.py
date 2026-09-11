"""Semantic replay over schema-v3 source and resolved-point identities."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .db.queries import fetch_detect_row, fetch_resolved_clip_tracks
from .db.source_table import validate_source_table, verify_clean_source_fingerprint
from .models import (
    CenterlineInputReplay,
    CleanerResult,
    ClipEndpoint,
    ClipPoint,
    CrossingPoint,
    HotspotCluster,
    InOutIntersectionPoint,
    PointRef,
    ReplayCleanResult,
    ReplayFitClip,
    ReplayFitEndpoint,
    TrackModel,
    TrajectoryPoint,
)


def replay_centerline_inputs(conn: object, centerline_id: int) -> CenterlineInputReplay:
    """Rebuild each fitting Clip from persisted crossing IDs and final order."""
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT centerline.fit_id, centerline.detect_id, centerline.variant, "
            "centerline.from_intersection_id, centerline.to_intersection_id, "
            "ST_AsText(centerline.geom), centerline.forward_clip_count, "
            "centerline.reverse_clip_count "
            "FROM centerlines AS centerline "
            "JOIN fit_runs AS fit ON fit.id = centerline.fit_id "
            "AND fit.detect_id = centerline.detect_id "
            "JOIN detect_runs AS detect ON detect.id = centerline.detect_id "
            "JOIN clean_runs AS clean ON clean.id = detect.clean_id "
            "WHERE centerline.id = %s AND fit.status = 'completed' "
            "AND detect.status = 'completed' AND clean.status = 'completed'",
            (centerline_id,),
        )
        centerline_row = cur.fetchone()
    if centerline_row is None:
        raise ValueError(f"unknown completed Centerline: {centerline_id}")
    (
        fit_id,
        detect_id,
        variant,
        from_id,
        to_id,
        centerline_wkt,
        forward_count,
        reverse_count,
    ) = centerline_row
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT input_ordinal, start_crossing_id, end_crossing_id, "
            "reverse_to_centerline_direction "
            "FROM centerline_source_clips WHERE centerline_id = %s "
            "AND detect_id = %s ORDER BY input_ordinal",
            (centerline_id, detect_id),
        )
        manifest_rows = list(cur)
    if len(manifest_rows) != int(forward_count) + int(reverse_count):
        raise ValueError(f"Centerline {centerline_id} source Clip manifest is incomplete")

    detect = fetch_detect_row(conn, str(detect_id))
    if detect.status != "completed":
        raise ValueError(f"Detect Run {detect_id} is not completed")
    verify_clean_source_fingerprint(conn, detect.clean_id)
    tracks = fetch_resolved_clip_tracks(conn, str(detect_id))
    crossing_index: dict[int, tuple[int, int]] = {}
    for track_index, track in enumerate(tracks):
        for point_index, point in enumerate(track.points):
            if point.crossing_id is not None:
                crossing_index[point.crossing_id] = (track_index, point_index)

    clips: list[ReplayFitClip] = []
    for ordinal, start_crossing_id, end_crossing_id, reverse_direction in manifest_rows:
        start_location = crossing_index.get(int(start_crossing_id))
        end_location = crossing_index.get(int(end_crossing_id))
        if start_location is None or end_location is None:
            raise ValueError(
                f"Centerline {centerline_id} references a missing resolved crossing"
            )
        if start_location[0] != end_location[0] or start_location[1] >= end_location[1]:
            raise ValueError(
                f"Centerline {centerline_id} crossing order is inconsistent"
            )
        track = tracks[start_location[0]]
        start_point = track.points[start_location[1]]
        end_point = track.points[end_location[1]]
        start = _replay_fit_endpoint(start_point, expected_type=0)
        end = _replay_fit_endpoint(end_point, expected_type=1)
        interior = tuple(
            ClipPoint(
                resolved_point_id=point.resolved_point_id,
                lon=point.lon,
                lat=point.lat,
            )
            for point in track.points[start_location[1] + 1 : end_location[1]]
        )
        clips.append(
            ReplayFitClip(
                input_ordinal=int(ordinal),
                original_from_intersection_id=start.intersection_id,
                original_to_intersection_id=end.intersection_id,
                reverse_to_centerline_direction=bool(reverse_direction),
                start=start,
                interior_points=interior,
                end=end,
            )
        )
    return CenterlineInputReplay(
        centerline_id=centerline_id,
        fit_id=str(fit_id),
        detect_id=str(detect_id),
        clean_id=detect.clean_id,
        variant=str(variant),
        from_intersection_id=int(from_id),
        to_intersection_id=int(to_id),
        centerline_wkt=str(centerline_wkt),
        value_semantics="persisted_crossings_and_verified_source_rows",
        clips=tuple(clips),
    )


def _replay_fit_endpoint(point: Any, *, expected_type: int) -> ReplayFitEndpoint:
    if (
        point.crossing_id is None
        or point.intersection_id is None
        or point.crossing_type != expected_type
    ):
        expected = "exit" if expected_type == 0 else "entry"
        raise ValueError(f"Centerline manifest endpoint is not a resolved {expected} crossing")
    return ReplayFitEndpoint(
        resolved_point_id=point.resolved_point_id,
        crossing_id=point.crossing_id,
        intersection_id=point.intersection_id,
        kind="exit" if expected_type == 0 else "entry",
        lon=point.lon,
        lat=point.lat,
    )


@dataclass(frozen=True)
class ReplayTarget(TrackModel):
    """One stable schema-v3 entity identity."""

    kind: str
    id: int | str
    clean_id: str | None = None
    detect_id: str | None = None


@dataclass(frozen=True)
class ReplayError(TrackModel):
    code: str
    message: str
    target: ReplayTarget


@dataclass(frozen=True)
class ReplayNode(TrackModel):
    kind: str
    id: int | str
    depth: int
    data: dict[str, Any]
    raw_row: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReplayEdge(TrackModel):
    child: ReplayTarget
    parent: ReplayTarget
    relation: str


@dataclass(frozen=True)
class ReplayTrace(TrackModel):
    target: ReplayTarget
    status: str
    nodes: list[ReplayNode]
    edges: list[ReplayEdge]
    errors: list[ReplayError] = field(default_factory=list)


class ReplayStore(Protocol):
    def get_entity(self, target: ReplayTarget) -> dict[str, Any] | None: ...

    def parent_refs(
        self,
        target: ReplayTarget,
        row: dict[str, Any],
    ) -> list[tuple[str, ReplayTarget]]: ...


class ReplayService:
    """Walk concrete v3 relationships until stable source rows are reached."""

    def __init__(self, store: ReplayStore) -> None:
        self._store = store

    def replay(self, target: ReplayTarget) -> ReplayTrace:
        nodes: list[ReplayNode] = []
        edges: list[ReplayEdge] = []
        errors: list[ReplayError] = []
        visited: set[tuple[str, str, str | None, str | None]] = set()

        def visit(current: ReplayTarget, depth: int) -> None:
            key = (
                current.kind,
                str(current.id),
                current.clean_id,
                current.detect_id,
            )
            if key in visited:
                return
            visited.add(key)
            try:
                row = self._store.get_entity(current)
            except ValueError as exc:
                errors.append(ReplayError("invalid_replay", str(exc), current))
                return
            if row is None:
                errors.append(
                    ReplayError(
                        "target_not_found",
                        "Persisted replay target was not found.",
                        current,
                    )
                )
                return
            nodes.append(
                ReplayNode(
                    current.kind,
                    current.id,
                    depth,
                    dict(row),
                    dict(row) if current.kind == "source_point" else None,
                )
            )
            try:
                parents = self._store.parent_refs(current, row)
            except ValueError as exc:
                errors.append(ReplayError("invalid_replay", str(exc), current))
                return
            for relation, parent in parents:
                edges.append(ReplayEdge(current, parent, relation))
                visit(parent, depth + 1)

        visit(target, 0)
        if not errors:
            status = "ok"
        elif not nodes:
            status = "not_found" if errors[0].code == "target_not_found" else "invalid"
        else:
            status = "partial"
        return ReplayTrace(target, status, nodes, edges, errors)


_ENTITY_TABLES = {
    "kept_trajectory_point": "kept_trajectory_points",
    "injected_point": "injected_points",
    "resolved_point": "resolved_subtrack_points",
    "tracks_cross": "tracks_cross",
    "turn_event": "turn_events",
    "candidate_intersection": "candidate_intersections",
    "candidate_entry_exit_point": "candidate_entry_exit_points",
    "intersection": "intersections",
    "entry_exit_point": "entry_exit_points",
}
CLEAN_REPLAY_KINDS = frozenset(
    {"source_point", "excluded_point", "kept_trajectory_point", "injected_point"}
)
DETECT_REPLAY_KINDS = frozenset(
    {
        "resolved_point",
        "tracks_cross",
        "turn_event",
        "candidate_intersection",
        "candidate_entry_exit_point",
        "intersection",
        "entry_exit_point",
    }
)


class PostgresReplayStore:
    """Read the concrete schema-v3 lineage graph from PostgreSQL."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._verified_clean_ids: set[str] = set()
        self._detect_clean_ids: dict[str, str] = {}

    def get_entity(self, target: ReplayTarget) -> dict[str, Any] | None:
        if target.kind == "centerline":
            return self._get_centerline(target)
        if target.kind in CLEAN_REPLAY_KINDS:
            clean_id = self._require_clean_target(target)
            if target.kind == "source_point":
                return self._get_source_point(clean_id, str(target.id))
            if target.kind == "excluded_point":
                return self._get_json_row(
                    "excluded_points",
                    "clean_id = %s AND source_row_key = %s",
                    (clean_id, str(target.id)),
                )
            table = _ENTITY_TABLES[target.kind]
            return self._get_json_row(
                table,
                "id = %s AND clean_id = %s",
                (int(target.id), clean_id),
            )
        if target.kind in DETECT_REPLAY_KINDS:
            detect_id = self._require_detect_target(target)
            table = _ENTITY_TABLES[target.kind]
            return self._get_json_row(
                table,
                "id = %s AND detect_id = %s",
                (int(target.id), detect_id),
            )
        raise ValueError(f"unsupported replay kind: {target.kind}")

    def parent_refs(
        self,
        target: ReplayTarget,
        row: dict[str, Any],
    ) -> list[tuple[str, ReplayTarget]]:
        kind = target.kind
        clean_id = target.clean_id
        detect_id = target.detect_id
        parents: list[tuple[str, ReplayTarget]] = []

        def add(
            kind_: str,
            entity_id: Any,
            relation: str,
            *,
            clean: str | None = clean_id,
            detect: str | None = detect_id,
        ) -> None:
            if entity_id is None:
                return
            parents.append(
                (
                    relation,
                    ReplayTarget(
                        kind_,
                        entity_id,
                        clean_id=clean if kind_ in CLEAN_REPLAY_KINDS else None,
                        detect_id=detect if kind_ in DETECT_REPLAY_KINDS else None,
                    ),
                )
            )

        if kind in {"source_point", "excluded_point"}:
            if kind == "excluded_point":
                add("source_point", row.get("source_row_key"), "excluded_source")
        elif kind == "kept_trajectory_point":
            add("source_point", row.get("source_row_key"), "kept_source")
        elif kind == "injected_point":
            parents.extend(self._injected_brackets(row, str(clean_id)))
        elif kind == "resolved_point":
            add(
                "kept_trajectory_point",
                row.get("kept_point_id"),
                "resolved_kept",
                clean=self._clean_for_detect(str(detect_id)),
            )
            add(
                "injected_point",
                row.get("injected_point_id"),
                "resolved_injected",
                clean=self._clean_for_detect(str(detect_id)),
            )
            add(
                "candidate_entry_exit_point",
                row.get("crossing_point_id"),
                "resolved_crossing",
            )
        elif kind == "turn_event":
            add("resolved_point", row.get("resolved_point_id"), "observed_point")
            add(
                "candidate_intersection",
                row.get("candidate_intersection_id"),
                "candidate",
            )
        elif kind == "tracks_cross":
            add(
                "resolved_point",
                row.get("from_segment_start_point_id"),
                "from_segment_start",
            )
            add(
                "resolved_point",
                row.get("to_segment_start_point_id"),
                "to_segment_start",
            )
            add(
                "candidate_intersection",
                row.get("candidate_intersection_id"),
                "candidate",
            )
        elif kind == "candidate_entry_exit_point":
            add(
                "resolved_point",
                row.get("segment_start_point_id"),
                "segment_start",
            )
            add(
                "candidate_intersection",
                row.get("candidate_intersection_id"),
                "candidate",
            )
        elif kind == "candidate_intersection":
            parents.extend(
                self._detect_children(
                    "turn_events",
                    "candidate_intersection_id",
                    target.id,
                    "turn_event",
                    str(detect_id),
                    "member_turn_event",
                )
            )
            parents.extend(
                self._detect_children(
                    "tracks_cross",
                    "candidate_intersection_id",
                    target.id,
                    "tracks_cross",
                    str(detect_id),
                    "member_track_cross",
                )
            )
            parents.extend(
                self._detect_children(
                    "candidate_entry_exit_points",
                    "candidate_intersection_id",
                    target.id,
                    "candidate_entry_exit_point",
                    str(detect_id),
                    "crossing",
                )
            )
        elif kind == "intersection":
            add(
                "candidate_intersection",
                row.get("candidate_intersection_id"),
                "candidate",
            )
            parents.extend(
                self._detect_children(
                    "entry_exit_points",
                    "intersection_id",
                    target.id,
                    "entry_exit_point",
                    str(detect_id),
                    "entry_exit_observation",
                )
            )
        elif kind == "entry_exit_point":
            add("intersection", row.get("intersection_id"), "intersection")
            add(
                "candidate_entry_exit_point",
                row.get("entry_crossing_id"),
                "entry_crossing",
            )
            add(
                "candidate_entry_exit_point",
                row.get("exit_crossing_id"),
                "exit_crossing",
            )
        elif kind == "centerline":
            centerline_detect = str(row["detect_id"])
            add(
                "intersection",
                row.get("from_intersection_id"),
                "from_intersection",
                detect=centerline_detect,
            )
            add(
                "intersection",
                row.get("to_intersection_id"),
                "to_intersection",
                detect=centerline_detect,
            )
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT start_crossing_id, end_crossing_id, input_ordinal "
                    "FROM centerline_source_clips WHERE centerline_id = %s "
                    "AND detect_id = %s ORDER BY input_ordinal",
                    (int(target.id), centerline_detect),
                )
                for start_id, end_id, ordinal in cur:
                    add(
                        "candidate_entry_exit_point",
                        start_id,
                        f"clip_{int(ordinal)}_start",
                        detect=centerline_detect,
                    )
                    add(
                        "candidate_entry_exit_point",
                        end_id,
                        f"clip_{int(ordinal)}_end",
                        detect=centerline_detect,
                    )
        return parents

    def _get_centerline(self, target: ReplayTarget) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT to_jsonb(centerline) FROM centerlines AS centerline "
                "JOIN fit_runs AS fit ON fit.id = centerline.fit_id "
                "AND fit.detect_id = centerline.detect_id "
                "JOIN detect_runs AS detect ON detect.id = centerline.detect_id "
                "JOIN clean_runs AS clean ON clean.id = detect.clean_id "
                "WHERE centerline.id = %s AND fit.status = 'completed' "
                "AND detect.status = 'completed' AND clean.status = 'completed'",
                (int(target.id),),
            )
            value = cur.fetchone()
        if value is None:
            return None
        result = _decode_row(value[0])
        self._verify_clean(str(result["detect_id"]), detect=True)
        return result

    def _get_source_point(self, clean_id: str, source_row_key: str) -> dict[str, Any] | None:
        source_table, _ = self._clean_context(clean_id)
        contract = validate_source_table(self._conn, source_table)
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT to_jsonb(source) FROM {contract.quoted_table} AS source "
                f"WHERE source.id = %s::{contract.key_sql_type}",
                (source_row_key,),
            )
            row = cur.fetchone()
        return None if row is None else _decode_row(row[0])

    def _get_json_row(
        self,
        table: str,
        predicate: str,
        params: tuple[Any, ...],
    ) -> dict[str, Any] | None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT to_jsonb(entity) FROM {table} AS entity WHERE {predicate}",
                params,
            )
            row = cur.fetchone()
        return None if row is None else _decode_row(row[0])

    def _require_clean_target(self, target: ReplayTarget) -> str:
        if target.clean_id is None:
            raise ValueError(f"{target.kind} requires clean_id")
        self._verify_clean(target.clean_id, detect=False)
        return target.clean_id

    def _require_detect_target(self, target: ReplayTarget) -> str:
        if target.detect_id is None:
            raise ValueError(f"{target.kind} requires detect_id")
        self._verify_clean(target.detect_id, detect=True)
        return target.detect_id

    def _verify_clean(self, run_id: str, *, detect: bool) -> None:
        clean_id = self._clean_for_detect(run_id) if detect else run_id
        if clean_id in self._verified_clean_ids:
            return
        verify_clean_source_fingerprint(self._conn, clean_id)
        self._verified_clean_ids.add(clean_id)

    def _clean_for_detect(self, detect_id: str) -> str:
        cached = self._detect_clean_ids.get(detect_id)
        if cached is not None:
            return cached
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT detect.clean_id::text FROM detect_runs AS detect "
                "JOIN clean_runs AS clean ON clean.id = detect.clean_id "
                "WHERE detect.id = %s AND detect.status = 'completed' "
                "AND clean.status = 'completed'",
                (detect_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown completed Detect Run: {detect_id}")
        clean_id = str(row[0])
        self._detect_clean_ids[detect_id] = clean_id
        return clean_id

    def _clean_context(self, clean_id: str) -> tuple[str, dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT source_table, source_fingerprint FROM clean_runs "
                "WHERE id = %s AND status = 'completed'",
                (clean_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown completed Clean Run: {clean_id}")
        return str(row[0]), _decode_row(row[1])

    def _injected_brackets(
        self,
        row: dict[str, Any],
        clean_id: str,
    ) -> list[tuple[str, ReplayTarget]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, sequence_in_subtrack FROM kept_trajectory_points "
                "WHERE clean_id = %s AND subtrack_index = %s "
                "AND sequence_in_subtrack IN (%s, %s) ORDER BY sequence_in_subtrack",
                (
                    clean_id,
                    int(row["subtrack_index"]),
                    int(row["after_sequence_in_subtrack"]),
                    int(row["after_sequence_in_subtrack"]) + 1,
                ),
            )
            rows = list(cur)
        labels = {int(row["after_sequence_in_subtrack"]): "left_source"}
        labels[int(row["after_sequence_in_subtrack"]) + 1] = "right_source"
        return [
            (
                labels[int(sequence)],
                ReplayTarget("kept_trajectory_point", int(point_id), clean_id=clean_id),
            )
            for point_id, sequence in rows
        ]

    def _detect_children(
        self,
        table: str,
        foreign_key: str,
        parent_id: int | str,
        kind: str,
        detect_id: str,
        relation: str,
    ) -> list[tuple[str, ReplayTarget]]:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT id FROM {table} WHERE {foreign_key} = %s "
                "AND detect_id = %s ORDER BY id",
                (int(parent_id), detect_id),
            )
            rows = list(cur)
        return [
            (relation, ReplayTarget(kind, int(row[0]), detect_id=detect_id))
            for row in rows
        ]


def _decode_row(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("database returned an invalid replay row")


# ── In-memory algorithm replay helpers ─────────────────────────────


def _build_replay_index(
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, dict[tuple[str, str], TrajectoryPoint]]:
    index: dict[str, dict[tuple[str, str], TrajectoryPoint]] = {}
    for vehicle_id, points in tracks.items():
        index[vehicle_id] = {
            (point.point_ref.source_id, point.point_ref.source_row_key): point
            for point in points
            if point.point_ref.source_id and point.point_ref.source_row_key
        }
    return index


def _resolve_point(
    vehicle_id: str,
    point_ref: PointRef | None,
    index: dict[str, dict[tuple[str, str], TrajectoryPoint]],
    tracks: dict[str, list[TrajectoryPoint]],
) -> TrajectoryPoint | None:
    del tracks
    if point_ref is None:
        return None
    key = (point_ref.source_id, point_ref.source_row_key)
    if vehicle_id:
        return index.get(vehicle_id, {}).get(key)
    matches = [inner[key] for inner in index.values() if key in inner]
    return matches[0] if len(matches) == 1 else None


def replay_cluster(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    index = _build_replay_index(tracks)
    seen: set[tuple[str, str]] = set()
    result: list[TrajectoryPoint] = []

    def add(point: TrajectoryPoint) -> None:
        key = (point.point_ref.source_id, point.point_ref.source_row_key)
        if key not in seen:
            seen.add(key)
            result.append(point)

    for refs in cluster.source_point_refs.values():
        for ref in refs:
            point = _resolve_point("", ref, index, tracks)
            if point is not None:
                add(point)
    for crossing in cluster.crossing_points:
        point = _resolve_point(crossing.vehicle_id, crossing.point_ref, index, tracks)
        if point is not None:
            add(point)
    result.sort(key=lambda point: (point.vehicle_id, point.timestamp))
    return result


def replay_clusters(
    clusters: Sequence[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    return {
        cluster.cluster_ref or str(cluster.cluster_calculate_index): replay_cluster(
            cluster, tracks
        )
        for cluster in clusters
    }


def replay_crossing_points(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[tuple[CrossingPoint, TrajectoryPoint | None]]:
    index = _build_replay_index(tracks)
    return [
        (
            crossing,
            _resolve_point(crossing.vehicle_id, crossing.point_ref, index, tracks),
        )
        for crossing in cluster.crossing_points
    ]


def replay_inout_points(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[tuple[InOutIntersectionPoint, list[CrossingPoint]]]:
    del tracks
    crossings = {
        crossing.crossing_calculate_index: crossing
        for crossing in cluster.crossing_points
    }
    return [
        (
            point,
            [crossings[key] for key in point.crossing_refs if key in crossings],
        )
        for point in cluster.inout_points
    ]


def replay_cleaner(
    vehicle_id: str,
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    cleaner_result: CleanerResult | None = None,
) -> ReplayCleanResult:
    all_points = tracks.get(vehicle_id, [])
    if not all_points:
        return ReplayCleanResult(vehicle_id, [], [], [], [])
    original = sorted(all_points, key=lambda point: point.timestamp)
    index = _build_replay_index(tracks)
    excluded: list[tuple[TrajectoryPoint, str]] = []
    if cleaner_result is not None:
        original_refs = {point.point_ref for point in all_points}
        for item in cleaner_result.excluded:
            if item.point_ref not in original_refs:
                continue
            point = _resolve_point(vehicle_id, item.point_ref, index, tracks)
            if point is not None:
                excluded.append((point, item.reason))
    injected: list[tuple[TrajectoryPoint, TrajectoryPoint, TrajectoryPoint]] = []
    if cleaner_result is not None:
        for point in cleaner_result.active.iter_points():
            if point.vehicle_id != vehicle_id or not point.point_ref.generated_reason:
                continue
            left = _resolve_point(
                vehicle_id,
                PointRef(
                    point.point_ref.source_id,
                    point.point_ref.from_ref or "",
                    False,
                ),
                index,
                tracks,
            )
            right = _resolve_point(
                vehicle_id,
                PointRef(
                    point.point_ref.source_id,
                    point.point_ref.to_ref or "",
                    False,
                ),
                index,
                tracks,
            )
            if left is not None and right is not None:
                injected.append((point, left, right))
    active = (
        [
            point
            for point in cleaner_result.active.iter_points()
            if point.vehicle_id == vehicle_id
        ]
        if cleaner_result is not None
        else list(original)
    )
    return ReplayCleanResult(vehicle_id, original, excluded, injected, active)


def replay_cleaner_all(
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    cleaner_result: CleanerResult,
) -> dict[str, ReplayCleanResult]:
    return {
        vehicle_id: replay_cleaner(
            vehicle_id, tracks, cleaner_result=cleaner_result
        )
        for vehicle_id in tracks
    }


def replay_subtrack(
    subtrack: Sequence[TrajectoryPoint],
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    if not subtrack:
        return []
    index = _build_replay_index(tracks)
    result: list[TrajectoryPoint] = []
    seen: set[tuple[str, str]] = set()
    for point in subtrack:
        key = (point.point_ref.source_id, point.point_ref.source_row_key)
        original = index.get(point.vehicle_id, {}).get(key)
        if original is not None and key not in seen:
            seen.add(key)
            result.append(original)
    result.sort(key=lambda point: (point.vehicle_id, point.timestamp))
    return result


def replay_subtracks(
    subtracks: dict[str, list[TrajectoryPoint]],
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    return {
        key: replay_subtrack(points, tracks) for key, points in subtracks.items()
    }


def replay_points(
    points: Sequence[TrajectoryPoint],
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    index = _build_replay_index(tracks)
    result: list[TrajectoryPoint] = []
    seen: set[tuple[str, str]] = set()
    for point in points:
        key = (point.point_ref.source_id, point.point_ref.source_row_key)
        original = index.get(point.vehicle_id, {}).get(key)
        if original is not None and key not in seen:
            seen.add(key)
            result.append(original)
    result.sort(key=lambda point: (point.vehicle_id, point.timestamp))
    return result
