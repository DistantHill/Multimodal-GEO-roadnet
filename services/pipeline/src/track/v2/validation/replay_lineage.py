"""Replay completed persisted Pipeline results through their source lineage."""

from __future__ import annotations

import json
import numpy as np
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..telemetry.heartbeat import observed_operation, heartbeat_progress
from ..intersections.query_results import fetch_detect_row, fetch_resolved_clip_tracks
from ..db.source_table import validate_source_table, verify_refine_source_fingerprint
from ..models import (
    CenterlineInputReplay,
    ReplayFitClip,
    TrackModel,
)

def replay_centerline_inputs(
    conn: object, centerline_id: int, *, batch_size: int,
) -> CenterlineInputReplay:
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
            "JOIN refine_runs AS refine ON refine.id = detect.refine_id "
            "WHERE centerline.id = %s AND fit.status = 'completed' "
            "AND detect.status = 'completed' AND refine.status = 'completed'",
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
    verify_refine_source_fingerprint(conn, detect.refine_id)
    tracks = tuple(fetch_resolved_clip_tracks(conn, str(detect_id), batch_size=batch_size))
    crossing_index: dict[int, tuple[int, int]] = {}
    for track_index, track in enumerate(tracks):
        for point_index in np.flatnonzero(track.crossing_valid):
            crossing_index[int(track.crossing_ids[point_index])] = (track_index, int(point_index))

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
        start, end = start_location[1], end_location[1]
        for index, expected in ((start, 0), (end, 1)):
            if (not track.crossing_valid[index] or not track.intersection_valid[index]
                    or track.crossing_types[index] != expected):
                raise ValueError("Centerline manifest endpoint has invalid crossing type")
        clips.append(ReplayFitClip(
            input_ordinal=int(ordinal),
            original_from_intersection_id=int(track.intersection_ids[start]),
            original_to_intersection_id=int(track.intersection_ids[end]),
            reverse_to_centerline_direction=bool(reverse_direction),
            lonlat=track.lonlat[start:end+1], resolved_point_ids=track.resolved_point_ids[start:end+1],
            start_crossing_id=int(track.crossing_ids[start]), end_crossing_id=int(track.crossing_ids[end]),
            start_intersection_id=int(track.intersection_ids[start]), end_intersection_id=int(track.intersection_ids[end]),
            start_kind="exit", end_kind="entry",
        ))
    return CenterlineInputReplay(
        centerline_id=centerline_id,
        fit_id=str(fit_id),
        detect_id=str(detect_id),
        refine_id=detect.refine_id,
        variant=str(variant),
        from_intersection_id=int(from_id),
        to_intersection_id=int(to_id),
        centerline_wkt=str(centerline_wkt),
        value_semantics="persisted_crossings_and_verified_source_rows",
        clips=tuple(clips),
    )

def _stringify_source_row_key(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep persisted BIGINT source identities aligned with API strings."""
    if row is not None and row.get("source_row_key") is not None:
        row["source_row_key"] = str(row["source_row_key"])
    return row

@dataclass(frozen=True)
class ReplayTarget(TrackModel):
    """One stable schema-v4 entity identity."""

    kind: str
    id: int | str
    refine_id: str | None = None
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

    @observed_operation("遍历来源追溯关系")
    def replay(self, target: ReplayTarget) -> ReplayTrace:
        nodes: list[ReplayNode] = []
        edges: list[ReplayEdge] = []
        errors: list[ReplayError] = []
        visited: set[tuple[str, str, str | None, str | None]] = set()

        def visit(current: ReplayTarget, depth: int) -> None:
            heartbeat_progress(len(nodes), unit="个节点")
            key = (
                current.kind,
                str(current.id),
                current.refine_id,
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

REFINE_REPLAY_KINDS = frozenset(
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
    """Read the concrete schema-v4 lineage graph from PostgreSQL."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._verified_refine_ids: set[str] = set()
        self._detect_refine_ids: dict[str, str] = {}

    def get_entity(self, target: ReplayTarget) -> dict[str, Any] | None:
        if target.kind == "centerline":
            return self._get_centerline(target)
        if target.kind in REFINE_REPLAY_KINDS:
            refine_id = self._require_refine_target(target)
            if target.kind == "source_point":
                return self._get_source_point(refine_id, str(target.id))
            if target.kind == "excluded_point":
                row = self._get_json_row(
                    "excluded_points",
                    "refine_id = %s AND source_row_key = %s::bigint",
                    (refine_id, str(target.id)),
                )
                return _stringify_source_row_key(row)
            table = _ENTITY_TABLES[target.kind]
            row = self._get_json_row(
                table,
                "id = %s AND refine_id = %s",
                (int(target.id), refine_id),
            )
            if target.kind == "kept_trajectory_point":
                return _stringify_source_row_key(row)
            return row
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
        refine_id = target.refine_id
        detect_id = target.detect_id
        parents: list[tuple[str, ReplayTarget]] = []

        def add(
            kind_: str,
            entity_id: Any,
            relation: str,
            *,
            refine: str | None = refine_id,
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
                        refine_id=refine if kind_ in REFINE_REPLAY_KINDS else None,
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
            parents.extend(self._injected_brackets(row, str(refine_id)))
        elif kind == "resolved_point":
            add(
                "kept_trajectory_point",
                row.get("kept_point_id"),
                "resolved_kept",
                refine=self._refine_for_detect(str(detect_id)),
            )
            add(
                "injected_point",
                row.get("injected_point_id"),
                "resolved_injected",
                refine=self._refine_for_detect(str(detect_id)),
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
                "JOIN refine_runs AS refine ON refine.id = detect.refine_id "
                "WHERE centerline.id = %s AND fit.status = 'completed' "
                "AND detect.status = 'completed' AND refine.status = 'completed'",
                (int(target.id),),
            )
            value = cur.fetchone()
        if value is None:
            return None
        result = _decode_row(value[0])
        self._verify_refine(str(result["detect_id"]), detect=True)
        return result

    def _get_source_point(self, refine_id: str, source_row_key: str) -> dict[str, Any] | None:
        source_table, _ = self._refine_context(refine_id)
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

    def _require_refine_target(self, target: ReplayTarget) -> str:
        if target.refine_id is None:
            raise ValueError(f"{target.kind} requires refine_id")
        self._verify_refine(target.refine_id, detect=False)
        return target.refine_id

    def _require_detect_target(self, target: ReplayTarget) -> str:
        if target.detect_id is None:
            raise ValueError(f"{target.kind} requires detect_id")
        self._verify_refine(target.detect_id, detect=True)
        return target.detect_id

    def _verify_refine(self, run_id: str, *, detect: bool) -> None:
        refine_id = self._refine_for_detect(run_id) if detect else run_id
        if refine_id in self._verified_refine_ids:
            return
        verify_refine_source_fingerprint(self._conn, refine_id)
        self._verified_refine_ids.add(refine_id)

    def _refine_for_detect(self, detect_id: str) -> str:
        cached = self._detect_refine_ids.get(detect_id)
        if cached is not None:
            return cached
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT detect.refine_id::text FROM detect_runs AS detect "
                "JOIN refine_runs AS refine ON refine.id = detect.refine_id "
                "WHERE detect.id = %s AND detect.status = 'completed' "
                "AND refine.status = 'completed'",
                (detect_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown completed Detect Run: {detect_id}")
        refine_id = str(row[0])
        self._detect_refine_ids[detect_id] = refine_id
        return refine_id

    def _refine_context(self, refine_id: str) -> tuple[str, dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT source_table, source_fingerprint FROM refine_runs "
                "WHERE id = %s AND status = 'completed'",
                (refine_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"unknown completed Refine Run: {refine_id}")
        return str(row[0]), _decode_row(row[1])

    def _injected_brackets(
        self,
        row: dict[str, Any],
        refine_id: str,
    ) -> list[tuple[str, ReplayTarget]]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, sequence_in_subtrack FROM kept_trajectory_points "
                "WHERE refine_id = %s AND subtrack_index = %s "
                "AND sequence_in_subtrack IN (%s, %s) ORDER BY sequence_in_subtrack",
                (
                    refine_id,
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
                ReplayTarget("kept_trajectory_point", int(point_id), refine_id=refine_id),
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
