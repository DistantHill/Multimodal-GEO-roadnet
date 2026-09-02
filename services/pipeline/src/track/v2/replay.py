"""Replay module — trace back any pipeline output to original trajectory points.

Each function takes a pipeline product (cluster, subtrack, point list, etc.)
plus the original ``tracks`` dict and optional ``CleanerResult``, and returns
the corresponding original ``TrajectoryPoint`` list with metadata.

Public:
  replay_cluster            — HotspotCluster → original TrajectoryPoints
  replay_clusters           — batch of the above
  replay_crossing_points    — CrossingPoint → (crossing, original_point)[]
  replay_cleaner            — one vehicle's cleaner replay
  replay_cleaner_all        — all vehicles
  replay_subtrack           — Clip sub-track → original points
  replay_points             — generic point list → original points
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, overload

from .models import (
    CleanerResult,
    CenterlineInputReplay,
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
from .sources import open_text_auto, source_row_hash
from .db.queries import fetch_clean_points_for_subtracks, fetch_detect_row
from .projection import CoordinateTransformer


def replay_centerline_inputs(conn: object, centerline_id: int) -> CenterlineInputReplay:
    """Rebuild exact ordered Clip membership for one persisted centerline."""
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT c.fit_id, c.detect_id, c.variant, c.from_intersection_id, "
            "c.to_intersection_id, ST_AsText(c.geom), c.forward_clip_count, "
            "c.reverse_clip_count "
            "FROM centerlines AS c JOIN fit_runs AS f ON f.id = c.fit_id "
            "AND f.detect_id = c.detect_id "
            "WHERE c.id = %s AND f.status = 'completed'",
            (centerline_id,),
        )
        centerline_row = cur.fetchone()
    if centerline_row is None:
        raise ValueError(f"unknown completed Centerline: {centerline_id}")
    (fit_id, detect_id, variant, from_id, to_id, centerline_wkt,
     forward_count, reverse_count) = centerline_row
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT input_ordinal, clip_id, vehicle_id, subtrack_index, "
            "start_intersection_id, start_segment_index, start_segment_fraction, "
            "interior_start_index, interior_end_index_exclusive, end_intersection_id, "
            "end_segment_index, end_segment_fraction, "
            "reverse_to_centerline_direction, input_point_count "
            "FROM centerline_source_clips WHERE centerline_id = %s "
            "ORDER BY input_ordinal",
            (centerline_id,),
        )
        manifest_rows = list(cur)
    if len(manifest_rows) != int(forward_count) + int(reverse_count):
        raise ValueError(f"Centerline {centerline_id} source Clip manifest is incomplete")
    detect = fetch_detect_row(conn, str(detect_id))
    transformer = CoordinateTransformer(detect.spatial_context)
    requested_subtracks = {
        (str(row[2]), int(row[3]))
        for row in manifest_rows
    }
    grouped = fetch_clean_points_for_subtracks(
        conn,
        str(detect.parameters["target_table"]),
        detect.clean_id,
        requested_subtracks,
        spatial_context=detect.spatial_context,
    )
    clips: list[ReplayFitClip] = []
    for row in manifest_rows:
        (ordinal, clip_id, vehicle_id, subtrack_index, start_intersection_id,
         start_segment, start_fraction, interior_start, interior_end,
         end_intersection_id, end_segment, end_fraction, reverse_direction,
         input_point_count) = row
        track = grouped.get((str(vehicle_id), int(subtrack_index)))
        if track is None:
            raise ValueError(f"Centerline {centerline_id} references a missing subtrack")
        start = _replay_fit_endpoint(
            track,
            int(start_segment),
            float(start_fraction),
            transformer,
        )
        end = _replay_fit_endpoint(
            track,
            int(end_segment),
            float(end_fraction),
            transformer,
        )
        interior = tuple(track[int(interior_start):int(interior_end)])
        if len(interior) + 2 != int(input_point_count):
            raise ValueError(f"Centerline {centerline_id} Clip {clip_id} membership drifted")
        original_points = (start.point, *interior, end.point)
        oriented_points = (
            tuple(reversed(original_points)) if reverse_direction else original_points
        )
        clips.append(ReplayFitClip(
            clip_id=str(clip_id), input_ordinal=int(ordinal),
            original_from_intersection_id=int(start_intersection_id),
            original_to_intersection_id=int(end_intersection_id),
            reverse_to_centerline_direction=bool(reverse_direction),
            start=start, interior_points=interior, end=end, points=oriented_points,
        ))
    return CenterlineInputReplay(
        centerline_id=centerline_id, fit_id=str(fit_id), detect_id=str(detect_id),
        clean_id=detect.clean_id, variant=str(variant),
        from_intersection_id=int(from_id), to_intersection_id=int(to_id),
        centerline_wkt=str(centerline_wkt),
        value_semantics="current_kept_values_and_persisted_injected_values",
        clips=tuple(clips),
    )


def _replay_fit_endpoint(
    track: list[TrajectoryPoint],
    segment_index: int,
    fraction: float,
    transformer: CoordinateTransformer,
) -> ReplayFitEndpoint:
    if segment_index < 0 or segment_index + 1 >= len(track):
        raise ValueError("Centerline endpoint segment is outside its frozen subtrack")
    left, right = track[segment_index], track[segment_index + 1]
    (left_x, left_y), (right_x, right_y) = transformer.forward_many((
        (left.lon, left.lat),
        (right.lon, right.lat),
    ))
    x = left_x + fraction * (right_x - left_x)
    y = left_y + fraction * (right_y - left_y)
    lon, lat = transformer.inverse(x, y)
    point = TrajectoryPoint(
        point_ref=PointRef(
            source_id=left.point_ref.source_id,
            source_row_key=(f"{left.point_ref.source_row_key}->"
                            f"{right.point_ref.source_row_key}:clip_endpoint:{fraction:.17g}"),
            stable=False, from_ref=left.point_ref.source_row_key,
            to_ref=right.point_ref.source_row_key, generated_reason="clip_endpoint",
        ),
        vehicle_id=left.vehicle_id,
        timestamp=left.timestamp + fraction * (right.timestamp - left.timestamp),
        lon=lon,
        lat=lat,
        heading=_interpolate_heading(left.heading, right.heading, fraction),
        speed=left.speed + fraction * (right.speed - left.speed),
        x=x,
        y=y,
    )
    return ReplayFitEndpoint(
        point=point, bracket_refs=(left.point_ref, right.point_ref),
        segment_index=segment_index, segment_fraction=fraction,
    )


def _interpolate_heading(start: float, end: float, fraction: float) -> float:
    """Interpolate true-north headings over the shortest circular arc."""
    delta = (end - start + 180.0) % 360.0 - 180.0
    return (start + fraction * delta) % 360.0


@dataclass(frozen=True)
class ReplayTarget(TrackModel):
    """One persisted v2 entity to replay."""

    kind: str
    id: int
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
    id: int
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
    """Typed persistence seam used by :class:`ReplayService`."""

    def get_entity(self, target: ReplayTarget) -> dict[str, Any] | None: ...

    def parent_refs(
        self,
        target: ReplayTarget,
        row: dict[str, Any],
    ) -> list[tuple[str, ReplayTarget]]: ...


SourceRowResolver = Callable[[str, str], dict[str, Any]]


class ReplayService:
    """Trace any persisted v2 entity through typed relations to source rows."""

    def __init__(
        self,
        store: ReplayStore,
        *,
        source_row_resolver: SourceRowResolver,
    ) -> None:
        self._store = store
        self._source_row_resolver = source_row_resolver

    def replay(self, target: ReplayTarget) -> ReplayTrace:
        nodes: list[ReplayNode] = []
        edges: list[ReplayEdge] = []
        errors: list[ReplayError] = []
        visited: set[tuple[str, int, str | None, str | None]] = set()

        def fail(code: str, message: str, failed_target: ReplayTarget) -> None:
            errors.append(ReplayError(code, message, failed_target))

        def visit(current: ReplayTarget, depth: int) -> None:
            key = (current.kind, current.id, current.clean_id, current.detect_id)
            if key in visited:
                return
            visited.add(key)
            row = self._store.get_entity(current)
            if row is None:
                fail("target_not_found", "Persisted replay target was not found.", current)
                return

            raw_row: dict[str, Any] | None = None
            if current.kind in {"trajectory_point", "excluded_point"}:
                source_id = str(row.get("source_id") or "")
                source_row_key = str(row.get("source_row_key") or "")
                expected_hash = str(row.get("source_row_hash") or "")
                # Persisted raw provenance may omit the hash when hashing was skipped.
                if not source_id and not source_row_key:
                    pass  # No provenance metadata is available for verification.
                else:
                    try:
                        candidate = self._source_row_resolver(source_id, source_row_key)
                    except Exception as exc:
                        fail("source_row_unavailable", str(exc), current)
                    else:
                        if not expected_hash or source_row_hash(candidate) == expected_hash:
                            raw_row = candidate
                        else:
                            fail(
                                "source_row_hash_mismatch",
                                "The live source row differs from the imported row.",
                                current,
                            )

            nodes.append(ReplayNode(current.kind, current.id, depth, dict(row), raw_row))
            for relation, parent in self._store.parent_refs(current, row):
                edges.append(ReplayEdge(current, parent, relation))
                visit(parent, depth + 1)

        visit(target, 0)
        error_codes = {error.code for error in errors}
        if not errors:
            status = "ok"
        elif "source_row_hash_mismatch" in error_codes:
            status = "source_mismatch"
        elif "source_row_unavailable" in error_codes:
            status = "source_unavailable"
        elif "legacy_provenance_incomplete" in error_codes:
            status = "legacy_provenance_incomplete"
        elif not nodes:
            status = "not_found"
        else:
            status = "partial"
        return ReplayTrace(target, status, nodes, edges, errors)


_ENTITY_TABLES = {
    "trajectory_point": "trajectory_points",
    "excluded_point": "excluded_points",
    "kept_trajectory_point": "kept_trajectory_points",
    "injected_point": "injected_points",
    "tracks_cross": "tracks_cross",
    "turn_event": "turn_events",
    "candidate_intersection": "candidate_intersections",
    "candidate_entry_exit_point": "candidate_entry_exit_points",
    "intersection": "intersections",
    "entry_exit_point": "entry_exit_points",
}
CLEAN_REPLAY_KINDS = frozenset({
    "excluded_point", "kept_trajectory_point", "injected_point",
})
DETECT_REPLAY_KINDS = frozenset(_ENTITY_TABLES) - CLEAN_REPLAY_KINDS - {
    "trajectory_point",
}


class PostgresReplayStore:
    """Read the fixed v2 replay graph from PostgreSQL tables.

    The store intentionally follows the domain's concrete FK/source-reference
    graph instead of introducing a generic ``provenance_edges`` table.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._clean_context_cache: dict[str, tuple[str, str]] = {}
        self._detect_clean_cache: dict[str, str] = {}
        self._raw_context: dict[int, tuple[str, str, str]] = {}

    def get_entity(self, target: ReplayTarget) -> dict[str, Any] | None:
        table = _ENTITY_TABLES.get(target.kind)
        if table is None:
            raise ValueError(f"unsupported replay kind: {target.kind}")
        raw_context = self._raw_context.get(target.id)
        if target.kind == "trajectory_point" and raw_context is not None:
            table = raw_context[2]
        if not _is_safe_table_name(table):
            raise ValueError(f"unsupported target table: {table}")
        sql = f"SELECT to_jsonb(t) FROM {table} AS t WHERE id = %s"
        params: list[Any] = [target.id]
        if target.kind in CLEAN_REPLAY_KINDS and target.clean_id is not None:
            sql += " AND clean_id = %s"
            params.append(target.clean_id)
        elif target.kind in DETECT_REPLAY_KINDS and target.detect_id is not None:
            sql += " AND detect_id = %s"
            params.append(target.detect_id)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        value = row[0] if isinstance(row, (list, tuple)) else row
        result = json.loads(value) if isinstance(value, str) else dict(value)
        if target.kind == "trajectory_point" and raw_context is not None:
            result["source_id"] = raw_context[0]
            result["source_row_key"] = raw_context[1]
        return result

    def parent_refs(
        self,
        target: ReplayTarget,
        row: dict[str, Any],
    ) -> list[tuple[str, ReplayTarget]]:
        clean_id = target.clean_id
        detect_id = target.detect_id
        kind = target.kind
        parents: list[tuple[str, ReplayTarget]] = []

        def add(kind_: str, entity_id: Any, relation: str) -> None:
            if entity_id is not None:
                parents.append((
                    relation,
                    ReplayTarget(
                        kind_,
                        int(entity_id),
                        clean_id=clean_id if kind_ in CLEAN_REPLAY_KINDS else None,
                        detect_id=detect_id if kind_ in DETECT_REPLAY_KINDS else None,
                    ),
                ))

        if kind == "intersection":
            add("candidate_intersection", row.get("candidate_intersection_id"), "candidate")
            parents.extend(self._children(
                "SELECT id FROM entry_exit_points WHERE intersection_id = %s "
                "AND detect_id = %s ORDER BY id",
                target.id,
                "entry_exit_point",
                detect_id,
                "entry_exit_observation",
            ))
        elif kind == "candidate_intersection":
            # Database IDs define deterministic replay order.  If a caller
            # needs a calculation ordinal, it can enumerate this ordered set.
            parents.extend(self._children(
                "SELECT id FROM turn_events WHERE candidate_intersection_id = %s "
                "AND detect_id = %s ORDER BY id",
                target.id,
                "turn_event",
                detect_id,
                "member_turn_event",
            ))
            parents.extend(self._children(
                "SELECT id FROM tracks_cross WHERE candidate_intersection_id = %s "
                "AND detect_id = %s ORDER BY id",
                target.id,
                "tracks_cross",
                detect_id,
                "member_cross_event",
            ))
            parents.extend(self._children(
                "SELECT id FROM candidate_entry_exit_points "
                "WHERE candidate_intersection_id = %s AND detect_id = %s ORDER BY id",
                target.id,
                "candidate_entry_exit_point",
                detect_id,
                "crossing",
            ))
        elif kind == "entry_exit_point":
            add("intersection", row.get("intersection_id"), "intersection")
            add("candidate_entry_exit_point", row.get("entry_crossing_id"), "entry_crossing")
            add("candidate_entry_exit_point", row.get("exit_crossing_id"), "exit_crossing")
        elif kind == "turn_event":
            if row.get("from_source_id") and row.get("from_source_row_key"):
                self._add_segment_source_points(
                    parents,
                    target,
                    row.get("from_source_id"),
                    row.get("from_source_row_key"),
                    "input_from",
                )
                self._add_segment_source_points(
                    parents,
                    target,
                    row.get("to_source_id"),
                    row.get("to_source_row_key"),
                    "input_to",
                )
            else:
                # Records from before the typed source-pair migration can still
                # replay their single raw point when that legacy field exists.
                self._add_source_point(
                    parents,
                    target,
                    row.get("tp_source_id"),
                    row.get("tp_source_row_key"),
                    "input_point",
                )
        elif kind == "candidate_entry_exit_point":
            self._add_source_point(
                parents,
                target,
                row.get("from_source_id"),
                row.get("from_source_row_key"),
                "segment_start",
            )
            self._add_source_point(
                parents,
                target,
                row.get("to_source_id"),
                row.get("to_source_row_key"),
                "segment_end",
            )
        elif kind == "excluded_point":
            pass
        elif kind == "kept_trajectory_point":
            add("trajectory_point", row.get("trajectory_point_id"), "kept_raw_point")
            raw_target = self._raw_target(
                target,
                row.get("source_id"),
                row.get("source_row_key"),
                row.get("trajectory_point_id"),
            )
            parents.append(("kept_raw_point", raw_target))
        elif kind == "injected_point":
            # Injected points are terminal Clean-stage artifacts.  Replay keeps
            # their stored provenance fields as data, but does not expand them
            # into the two raw source points.
            pass
        elif kind == "tracks_cross":
            self._add_segment_source_points(
                parents,
                target,
                row.get("from_source_id"),
                row.get("from_source_row_key"),
                "from_segment",
            )
            self._add_segment_source_points(
                parents,
                target,
                row.get("to_source_id"),
                row.get("to_source_row_key"),
                "to_segment",
            )
        return parents

    def _children(
        self,
        sql: str,
        parent_id: int,
        kind: str,
        detect_id: str | None,
        relation: str,
    ) -> list[tuple[str, ReplayTarget]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, (parent_id, detect_id))
            rows = cur.fetchall()
        return [
            (relation, ReplayTarget(kind, int(row[0]), detect_id=detect_id))
            for row in rows
        ]

    def _add_source_point(
        self,
        parents: list[tuple[str, ReplayTarget]],
        owner: ReplayTarget,
        source_id: Any,
        source_row_key: Any,
        relation: str,
    ) -> None:
        if not source_id or not source_row_key:
            return
        clean_id, source_kind, _target_table = self._clean_context(owner)
        if str(source_id) in {"injected_points", "public.injected_points"}:
            parents.append((
                relation,
                ReplayTarget(
                    "injected_point",
                    int(source_row_key),
                    clean_id=clean_id,
                ),
            ))
            return
        if source_kind == "postgres_table":
            raw_id = int(source_row_key)
        else:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT trajectory_point_id FROM kept_trajectory_points "
                    "WHERE clean_id = %s AND source_id = %s AND source_row_key = %s",
                    (clean_id, str(source_id), str(source_row_key)),
                )
                row = cur.fetchone()
            if row is None:
                return
            raw_id = int(row[0])
        parents.append((relation, self._raw_target(
            owner, source_id, source_row_key, raw_id,
        )))
        # PG-only mode: source_row_key is the trajectory_points.id value
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM trajectory_points WHERE id = %s",
                (int(source_row_key),),
            )
            row = cur.fetchone()
        if row is not None:
            parents.append((relation, ReplayTarget("trajectory_point", int(row[0]))))

    def _add_segment_source_points(
        self,
        parents: list[tuple[str, ReplayTarget]],
        owner: ReplayTarget,
        source_id: Any,
        source_row_keys: Any,
        relation: str,
    ) -> None:
        if not source_id or not source_row_keys:
            return
        for ordinal, key in enumerate(str(source_row_keys).split(",")):
            self._add_source_point(
                parents, owner, source_id, key, f"{relation}_{ordinal}"
            )

    def _raw_target(
        self,
        owner: ReplayTarget,
        source_id: Any,
        source_row_key: Any,
        raw_id: Any,
    ) -> ReplayTarget:
        _clean_id, _source_kind, target_table = self._clean_context(owner)
        raw_id = int(raw_id)
        self._raw_context[raw_id] = (
            str(source_id), str(source_row_key), target_table,
        )
        return ReplayTarget("trajectory_point", raw_id)

    def _clean_context(self, owner: ReplayTarget) -> tuple[str, str, str]:
        if owner.clean_id is not None:
            clean_id = owner.clean_id
        elif owner.detect_id is not None:
            clean_id = self._detect_clean_cache.get(owner.detect_id)
            if clean_id is None:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT clean_id FROM detect_runs WHERE id = %s",
                        (owner.detect_id,),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise ValueError(f"unknown Detect Run: {owner.detect_id}")
                clean_id = str(row[0])
                self._detect_clean_cache[owner.detect_id] = clean_id
        else:
            raise ValueError("stage artifact requires a Clean or Detect Run id")
        cached = self._clean_context_cache.get(clean_id)
        if cached is None:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT source_kind, parameters->>'target_table' "
                    "FROM clean_runs WHERE id = %s",
                    (clean_id,),
                )
                row = cur.fetchone()
            if row is None:
                raise ValueError(f"unknown Clean Run: {clean_id}")
            cached = (str(row[0]), str(row[1]))
            self._clean_context_cache[clean_id] = cached
        return clean_id, cached[0], cached[1]


class PostgresSourceRowResolver:
    """Resolve CSV paths or project PostgreSQL-table source ids at replay time."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._csv_rows: dict[Path, tuple[int, int, dict[str, dict[str, Any]]]] = {}

    def __call__(self, source_id: str, source_row_key: str) -> dict[str, Any]:
        path = Path(source_id)
        if path.is_file():
            return self._csv_row(path, source_row_key)
        if not _is_safe_table_name(source_id):
            raise ValueError(f"unsupported source_id: {source_id}")
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT to_jsonb(t) FROM {source_id} AS t WHERE id = %s", (source_row_key,))
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"source row not found: {source_id}:{source_row_key}")
        value = row[0] if isinstance(row, (list, tuple)) else row
        return json.loads(value) if isinstance(value, str) else dict(value)

    def _csv_row(self, path: Path, source_row_key: str) -> dict[str, Any]:
        row_number = int(source_row_key)
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        cached = self._csv_rows.get(path)
        if cached is None or cached[:2] != version:
            rows: dict[str, dict[str, Any]] = {}
            with open_text_auto(path, newline="") as source_file:
                reader = csv.DictReader(source_file)
                for current_number, row in enumerate(reader, start=2):
                    rows[str(current_number)] = {
                        str(key): "" if value is None else str(value)
                        for key, value in row.items()
                    }
            self._csv_rows[path] = (*version, rows)
        else:
            rows = cached[2]
        try:
            return rows[str(row_number)]
        except KeyError as exc:
            raise ValueError(f"source row not found: {path}:{source_row_key}") from exc


def _is_safe_table_name(value: str) -> bool:
    parts = value.split(".")
    return bool(parts) and all(part.replace("_", "a").isalnum() and not part[:1].isdigit() for part in parts)


# ── Index builder ────────────────────────────────────────────────────


def _build_replay_index(
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, dict[tuple[str, str], TrajectoryPoint]]:
    """Build a vehicle-local lookup keyed by the full source identity."""
    index: dict[str, dict[tuple[str, str], TrajectoryPoint]] = {}
    for vid, pts in tracks.items():
        inner: dict[tuple[str, str], TrajectoryPoint] = {}
        for p in pts:
            key = (p.point_ref.source_id, p.point_ref.source_row_key)
            if key[0] and key[1]:
                inner[key] = p
        index[vid] = inner
    return index


# ── Generic lookup helper ────────────────────────────────────────────


def _resolve_point(
    vehicle_id: str,
    point_ref: PointRef | None,
    index: dict[str, dict[tuple[str, str], TrajectoryPoint]],
    tracks: dict[str, list[TrajectoryPoint]],
) -> TrajectoryPoint | None:
    """Resolve a single PointRef back to the original TrajectoryPoint."""
    if point_ref is not None:
        key = (point_ref.source_id, point_ref.source_row_key)
        if vehicle_id:
            return index.get(vehicle_id, {}).get(key)
        matches = [inner[key] for inner in index.values() if key in inner]
        if len(matches) == 1:
            return matches[0]
        # Stable=False fallback: match by timestamp + lon + lat
        # (approximate — for injected points the ts is interpolated)
    # Fallback: return nothing
    return None


# ── Cluster replay ───────────────────────────────────────────────────


def replay_cluster(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    """All original TrajectoryPoints that contributed to this cluster.

    Uses ``cluster.source_point_refs`` and ``cluster.crossing_points``
    to find every point, then sorts by (vehicle_id, timestamp).
    """
    index = _build_replay_index(tracks)
    seen: set[tuple[str, str]] = set()
    result: list[TrajectoryPoint] = []

    def _add(pt: TrajectoryPoint) -> None:
        key = (pt.point_ref.source_id, pt.point_ref.source_row_key)
        if key not in seen:
            seen.add(key)
            result.append(pt)

    # From source_point_refs (turn events + cross-track candidates)
    for _source, refs in cluster.source_point_refs.items():
        for ref in refs:
            pt = _resolve_point("", ref, index, tracks)  # vehicle_id unknown from ref alone
            if pt is not None:
                _add(pt)

    # From crossing_points (segment-circle intersections)
    for cp in cluster.crossing_points:
        pt = _resolve_point(cp.vehicle_id, cp.point_ref, index, tracks)
        if pt is not None:
            _add(pt)

    # Sort by (vehicle_id, timestamp)
    result.sort(key=lambda p: (p.vehicle_id, p.timestamp))
    return result


def replay_clusters(
    clusters: Sequence[HotspotCluster],
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    """{cluster_ref: [TrajectoryPoint, ...]} for every cluster."""
    return {
        c.cluster_ref or str(c.cluster_calculate_index): replay_cluster(c, tracks)
        for c in clusters
    }


# ── Crossing-point replay ────────────────────────────────────────────


def replay_crossing_points(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[tuple[CrossingPoint, TrajectoryPoint | None]]:
    """Each CrossingPoint paired with its original TrajectoryPoint (or None)."""
    index = _build_replay_index(tracks)
    result: list[tuple[CrossingPoint, TrajectoryPoint | None]] = []
    for cp in cluster.crossing_points:
        pt = _resolve_point(cp.vehicle_id, cp.point_ref, index, tracks)
        result.append((cp, pt))
    return result


def replay_inout_points(
    cluster: HotspotCluster,
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[tuple[InOutIntersectionPoint, list[CrossingPoint]]]:
    """Each InOutIntersectionPoint paired with its aggregated CrossingPoints."""
    cp_by_id: dict[int, CrossingPoint] = {}
    for cp in cluster.crossing_points:
        cp_by_id[cp.crossing_id] = cp

    result: list[tuple[InOutIntersectionPoint, list[CrossingPoint]]] = []
    for iop in cluster.inout_points:
        cps = [cp_by_id[cid] for cid in iop.crossing_refs if cid in cp_by_id]
        result.append((iop, cps))
    return result


# ── Cleaner replay ───────────────────────────────────────────────────


def replay_cleaner(
    vehicle_id: str,
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    cleaner_result: CleanerResult | None = None,
) -> ReplayCleanResult:
    """Replay one vehicle through the cleaner stage.

    Returns original points, which were excluded (and why), which were
    injected (with parent refs), and the final active set.
    """
    all_pts = tracks.get(vehicle_id, [])
    if not all_pts:
        return ReplayCleanResult(
            vehicle_id=vehicle_id,
            original=[],
            excluded=[],
            injected=[],
            active=[],
        )

    original = sorted(all_pts, key=lambda p: p.timestamp)

    # Excluded points
    excluded: list[tuple[TrajectoryPoint, str]] = []
    if cleaner_result is not None:
        for item in cleaner_result.excluded:
            if item.point_ref not in {p.point_ref for p in all_pts}:
                continue
            pt = _resolve_point(vehicle_id, item.point_ref,
                                _build_replay_index(tracks), tracks)
            if pt is not None:
                excluded.append((pt, item.reason))

    # Injected points = those with generated_reason != "" in active
    injected: list[tuple[TrajectoryPoint, TrajectoryPoint, TrajectoryPoint]] = []
    if cleaner_result is not None:
        index = _build_replay_index(tracks)
        for pt in cleaner_result.active.iter_points():
            if pt.vehicle_id != vehicle_id:
                continue
            if pt.point_ref.generated_reason == "":
                continue
            from_pt = _resolve_point(vehicle_id,
                                     PointRef(pt.point_ref.source_id, pt.point_ref.from_ref or "", False),
                                     index, tracks)
            to_pt = _resolve_point(vehicle_id,
                                   PointRef(pt.point_ref.source_id, pt.point_ref.to_ref or "", False),
                                   index, tracks)
            if from_pt is not None and to_pt is not None:
                injected.append((pt, from_pt, to_pt))

    # Active points (from cleaner_result or all original)
    if cleaner_result is not None:
        active = [pt for pt in cleaner_result.active.iter_points()
                  if pt.vehicle_id == vehicle_id]
    else:
        active = list(original)

    return ReplayCleanResult(
        vehicle_id=vehicle_id,
        original=original,
        excluded=excluded,
        injected=injected,
        active=active,
    )


def replay_cleaner_all(
    tracks: dict[str, list[TrajectoryPoint]],
    *,
    cleaner_result: CleanerResult,
) -> dict[str, ReplayCleanResult]:
    """All vehicles' cleaner replay."""
    return {
        vid: replay_cleaner(vid, tracks, cleaner_result=cleaner_result)
        for vid in tracks
    }


# ── Sub-track replay (Clip layer) ────────────────────────────────────


def replay_subtrack(
    subtrack: Sequence[TrajectoryPoint],
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    """Take a Clip-produced sub-track and return the original points.

    Matches by vehicle_id + point_ref.source_row_key.
    """
    if not subtrack:
        return []
    index = _build_replay_index(tracks)
    result: list[TrajectoryPoint] = []
    seen: set[tuple[str, str]] = set()
    for pt in subtrack:
        inner = index.get(pt.vehicle_id, {})
        key = (pt.point_ref.source_id, pt.point_ref.source_row_key)
        if key[1] and key in inner and key not in seen:
            seen.add(key)
            result.append(inner[key])
    result.sort(key=lambda p: (p.vehicle_id, p.timestamp))
    return result


def replay_subtracks(
    subtracks: dict[str, list[TrajectoryPoint]],
    tracks: dict[str, list[TrajectoryPoint]],
) -> dict[str, list[TrajectoryPoint]]:
    """Batch sub-track replay."""
    return {k: replay_subtrack(v, tracks) for k, v in subtracks.items()}


# ── Generic point replay ─────────────────────────────────────────────


def replay_points(
    points: Sequence[TrajectoryPoint],
    tracks: dict[str, list[TrajectoryPoint]],
) -> list[TrajectoryPoint]:
    """Generic: any TrajectoryPoint list → deduped original points."""
    index = _build_replay_index(tracks)
    result: list[TrajectoryPoint] = []
    seen: set[tuple[str, str]] = set()
    for pt in points:
        inner = index.get(pt.vehicle_id, {})
        key = (pt.point_ref.source_id, pt.point_ref.source_row_key)
        if key[1] and key in inner and key not in seen:
            seen.add(key)
            result.append(inner[key])
    result.sort(key=lambda p: (p.vehicle_id, p.timestamp))
    return result
