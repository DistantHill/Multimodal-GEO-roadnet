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
    CrossingPoint,
    HotspotCluster,
    InOutIntersectionPoint,
    PointRef,
    ReplayCleanResult,
    TrackModel,
    TrajectoryPoint,
)
from .sources import open_text_auto, source_row_hash


@dataclass(frozen=True)
class ReplayTarget(TrackModel):
    """One persisted v2 entity to replay."""

    kind: str
    id: int
    run_id: str | None


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
        visited: set[tuple[str, int, str | None]] = set()

        def fail(code: str, message: str, failed_target: ReplayTarget) -> None:
            errors.append(ReplayError(code, message, failed_target))

        def visit(current: ReplayTarget, depth: int) -> None:
            key = (current.kind, current.id, current.run_id)
            if key in visited:
                return
            visited.add(key)
            row = self._store.get_entity(current)
            if row is None:
                fail("target_not_found", "Persisted replay target was not found.", current)
                return

            raw_row: dict[str, Any] | None = None
            if current.kind == "trajectory_point":
                source_id = str(row.get("source_id") or "")
                source_row_key = str(row.get("source_row_key") or "")
                expected_hash = str(row.get("source_row_hash") or "")
                if not expected_hash:
                    fail(
                        "legacy_provenance_incomplete",
                        "Trajectory point has no source-row fingerprint.",
                        current,
                    )
                else:
                    try:
                        candidate = self._source_row_resolver(source_id, source_row_key)
                    except Exception as exc:  # source adapters provide context in the error
                        fail("source_row_unavailable", str(exc), current)
                    else:
                        if source_row_hash(candidate) == expected_hash:
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
        if not errors and not any(
            node.kind == "trajectory_point" and node.raw_row is not None for node in nodes
        ):
            fail(
                "legacy_provenance_incomplete",
                "The persisted chain does not reach a hash-verified source row.",
                target,
            )
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
    "candidate_intersection_event": "candidate_intersection_events",
    "candidate_entry_exit_point": "candidate_entry_exit_points",
    "intersection": "intersections",
    "entry_exit_point": "entry_exit_points",
}
_RUN_SCOPED_KINDS = frozenset(_ENTITY_TABLES) - {"trajectory_point"}


class PostgresReplayStore:
    """Read the fixed v2 replay graph from PostgreSQL tables.

    The store intentionally follows the domain's concrete FK/source-reference
    graph instead of introducing a generic ``provenance_edges`` table.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def get_entity(self, target: ReplayTarget) -> dict[str, Any] | None:
        table = _ENTITY_TABLES.get(target.kind)
        if table is None:
            raise ValueError(f"unsupported replay kind: {target.kind}")
        sql = f"SELECT to_jsonb(t) FROM {table} AS t WHERE id = %s"
        params: list[Any] = [target.id]
        if target.kind in _RUN_SCOPED_KINDS and target.run_id is not None:
            sql += " AND run_id = %s"
            params.append(target.run_id)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        value = row[0] if isinstance(row, (list, tuple)) else row
        if isinstance(value, str):
            return json.loads(value)
        return dict(value)

    def parent_refs(
        self,
        target: ReplayTarget,
        row: dict[str, Any],
    ) -> list[tuple[str, ReplayTarget]]:
        run_id = target.run_id
        kind = target.kind
        parents: list[tuple[str, ReplayTarget]] = []

        def add(kind_: str, entity_id: Any, relation: str, *, source: bool = False) -> None:
            if entity_id is not None:
                parents.append((relation, ReplayTarget(kind_, int(entity_id), None if source else run_id)))

        if kind == "intersection":
            add("candidate_intersection", row.get("candidate_intersection_id"), "candidate")
            parents.extend(self._children(
                "SELECT id FROM entry_exit_points WHERE intersection_id = %s",
                target.id,
                "entry_exit_point",
                run_id,
                "entry_exit_observation",
            ))
        elif kind == "candidate_intersection":
            parents.extend(self._children(
                "SELECT id FROM candidate_intersection_events "
                "WHERE candidate_intersection_id = %s",
                target.id,
                "candidate_intersection_event",
                run_id,
                "member_event",
            ))
            parents.extend(self._children(
                "SELECT id FROM candidate_entry_exit_points "
                "WHERE candidate_intersection_id = %s",
                target.id,
                "candidate_entry_exit_point",
                run_id,
                "crossing",
            ))
        elif kind == "candidate_intersection_event":
            if row.get("turn_event_id") is not None:
                add("turn_event", row.get("turn_event_id"), "turn_event")
            else:
                self._add_segment_source_points(
                    parents,
                    row.get("from_source_id"),
                    row.get("from_source_row_key"),
                    "input_from",
                )
                self._add_segment_source_points(
                    parents,
                    row.get("to_source_id"),
                    row.get("to_source_row_key"),
                    "input_to",
                )
        elif kind == "entry_exit_point":
            add("intersection", row.get("intersection_id"), "intersection")
            add("candidate_entry_exit_point", row.get("entry_crossing_id"), "entry_crossing")
            add("candidate_entry_exit_point", row.get("exit_crossing_id"), "exit_crossing")
        elif kind == "turn_event":
            if row.get("from_source_id") and row.get("from_source_row_key"):
                self._add_segment_source_points(
                    parents,
                    row.get("from_source_id"),
                    row.get("from_source_row_key"),
                    "input_from",
                )
                self._add_segment_source_points(
                    parents,
                    row.get("to_source_id"),
                    row.get("to_source_row_key"),
                    "input_to",
                )
            else:
                # Records from before the typed source-pair migration can still
                # replay their single raw point when that legacy field exists.
                self._add_source_point(
                    parents,
                    row.get("tp_source_id"),
                    row.get("tp_source_row_key"),
                    "input_point",
                )
        elif kind == "candidate_entry_exit_point":
            self._add_source_point(
                parents,
                row.get("from_source_id"),
                row.get("from_source_row_key"),
                "segment_start",
            )
            self._add_source_point(
                parents,
                row.get("to_source_id"),
                row.get("to_source_row_key"),
                "segment_end",
            )
        elif kind == "excluded_point":
            self._add_source_point(
                parents, row.get("source_id"), row.get("source_row_key"), "excluded_raw_point"
            )
        elif kind == "kept_trajectory_point":
            add("trajectory_point", row.get("trajectory_point_id"), "kept_raw_point", source=True)
        elif kind == "injected_point":
            self._add_source_point(
                parents,
                row.get("from_source_id"),
                row.get("from_source_row_key"),
                "from_raw_point",
            )
            self._add_source_point(
                parents,
                row.get("to_source_id"),
                row.get("to_source_row_key"),
                "to_raw_point",
            )
        elif kind == "tracks_cross":
            self._add_segment_source_points(
                parents,
                row.get("from_source_id"),
                row.get("from_source_row_key"),
                "from_segment",
            )
            self._add_segment_source_points(
                parents,
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
        run_id: str | None,
        relation: str,
    ) -> list[tuple[str, ReplayTarget]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, (parent_id,))
            rows = cur.fetchall()
        return [
            (relation, ReplayTarget(kind, int(row[0]), run_id))
            for row in rows
        ]

    def _add_source_point(
        self,
        parents: list[tuple[str, ReplayTarget]],
        source_id: Any,
        source_row_key: Any,
        relation: str,
    ) -> None:
        if not source_id or not source_row_key:
            return
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM trajectory_points "
                "WHERE source_id = %s AND source_row_key = %s",
                (str(source_id), str(source_row_key)),
            )
            row = cur.fetchone()
        if row is not None:
            parents.append((relation, ReplayTarget("trajectory_point", int(row[0]), None)))

    def _add_segment_source_points(
        self,
        parents: list[tuple[str, ReplayTarget]],
        source_id: Any,
        source_row_keys: Any,
        relation: str,
    ) -> None:
        if not source_id or not source_row_keys:
            return
        for ordinal, key in enumerate(str(source_row_keys).split(",")):
            self._add_source_point(parents, source_id, key, f"{relation}_{ordinal}")


class PostgresSourceRowResolver:
    """Resolve CSV paths or project PostgreSQL-table source ids at replay time."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        # Cache each CSV once per replay command. A trace can fan out to many
        # raw points from the same source file, so rescanning per leaf would
        # repeatedly read the full file.
        self._csv_rows: dict[
            Path,
            tuple[int, int, dict[str, dict[str, Any]]],
        ] = {}

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
        try:
            row_number = int(source_row_key)
        except ValueError as exc:
            raise ValueError(f"CSV source row key is not a line number: {source_row_key}") from exc
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        cached = self._csv_rows.get(path)
        if cached is None or cached[:2] != version:
            rows = {}
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
        if str(row_number) in rows:
            return rows[str(row_number)]
        raise ValueError(f"source row not found: {path}:{source_row_key}")


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

    # From source_point_refs (turn events + orthogonal candidates)
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
    return {c.cluster_ref or str(c.cluster_id): replay_cluster(c, tracks) for c in clusters}


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
