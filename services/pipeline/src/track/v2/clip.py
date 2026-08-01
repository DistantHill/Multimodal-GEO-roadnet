"""Clip stage — extract trajectory fragments outside verified intersections."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from ._geometry import _distance_m, _to_xy
from .models import (
    ClipConfig,
    ClipEndpoint,
    ClipInput,
    ClipIntersection,
    ClipReport,
    ClipResult,
    ClipTrack,
    PointRef,
    TrackClip,
    TrajectoryPoint,
)


_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class _CrossingEvent:
    endpoint: ClipEndpoint

    @property
    def progress(self) -> float:
        return self.endpoint.segment_index + self.endpoint.segment_fraction


@dataclass(frozen=True, slots=True)
class _IndexedIntersection:
    value: ClipIntersection
    x: float
    y: float


class _IntersectionGrid:
    """Private uniform-grid index for candidate circle lookup per segment."""

    def __init__(self, intersections: tuple[ClipIntersection, ...]) -> None:
        self.origin_lon = intersections[0].lon
        self.origin_lat = intersections[0].lat
        self.cell_size = max(100.0, max(item.radius_m for item in intersections) * 2.0)
        self.cells: dict[tuple[int, int], list[_IndexedIntersection]] = {}
        cluster_ids: set[int] = set()
        for intersection in intersections:
            if intersection.cluster_id in cluster_ids:
                raise ValueError(f"duplicate ClipIntersection cluster_id: {intersection.cluster_id}")
            cluster_ids.add(intersection.cluster_id)
            x, y = _to_xy(
                intersection.lon,
                intersection.lat,
                self.origin_lon,
                self.origin_lat,
            )
            indexed = _IndexedIntersection(intersection, x, y)
            for cell in self._cells_for_bounds(
                x - intersection.radius_m,
                y - intersection.radius_m,
                x + intersection.radius_m,
                y + intersection.radius_m,
            ):
                self.cells.setdefault(cell, []).append(indexed)

    def point_xy(self, point: TrajectoryPoint) -> tuple[float, float]:
        return _to_xy(point.lon, point.lat, self.origin_lon, self.origin_lat)

    def candidates(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[_IndexedIntersection, ...]:
        found: dict[int, _IndexedIntersection] = {}
        for cell in self._cells_for_bounds(
            min(start[0], end[0]),
            min(start[1], end[1]),
            max(start[0], end[0]),
            max(start[1], end[1]),
        ):
            for indexed in self.cells.get(cell, ()):
                found[indexed.value.cluster_id] = indexed
        return tuple(found.values())

    def _cells_for_bounds(
        self,
        min_x: float,
        min_y: float,
        max_x: float,
        max_y: float,
    ):
        min_cell_x = math.floor(min_x / self.cell_size)
        max_cell_x = math.floor(max_x / self.cell_size)
        min_cell_y = math.floor(min_y / self.cell_size)
        max_cell_y = math.floor(max_y / self.cell_size)
        for cell_x in range(min_cell_x, max_cell_x + 1):
            for cell_y in range(min_cell_y, max_cell_y + 1):
                yield cell_x, cell_y


def extract_clips(
    source: ClipInput,
    *,
    config: ClipConfig | None = None,
) -> ClipResult:
    """Extract all clips through the storage-independent Clip interface."""
    cfg = config or ClipConfig()
    _validate_input(source)
    clips: list[TrackClip] = []
    skipped_degenerate = 0
    unmatched_entries = 0
    unmatched_exits = 0

    for track in source.tracks:
        if len(track.points) < 2:
            skipped_degenerate += 1
            continue
        if any(
            right.timestamp < left.timestamp
            for left, right in zip(track.points, track.points[1:])
        ):
            raise ValueError(f"ClipTrack {track.trip_id!r} is not ordered by timestamp")
        (
            track_clips,
            skipped,
            track_unmatched_entries,
            track_unmatched_exits,
        ) = _extract_track_clips(
            track,
            source.intersections,
            include_boundaries=cfg.include_boundary_clips,
            run_id=source.run_id,
        )
        clips.extend(track_clips)
        skipped_degenerate += skipped
        unmatched_entries += track_unmatched_entries
        unmatched_exits += track_unmatched_exits

    inter_count = sum(clip.clip_type == "inter_intersection" for clip in clips)
    boundary_count = len(clips) - inter_count
    return ClipResult(
        clips=tuple(clips),
        report=ClipReport(
            total_vehicles=len({track.vehicle_id for track in source.tracks}),
            total_clips=len(clips),
            inter_intersection_clips=inter_count,
            boundary_clips=boundary_count,
            total_points_in_clips=sum(clip.point_count for clip in clips),
            unmatched_entry_count=unmatched_entries,
            unmatched_exit_count=unmatched_exits,
            skipped_degenerate_count=skipped_degenerate,
        ),
        run_id=source.run_id,
    )


def _validate_input(source: ClipInput) -> None:
    cluster_ids: set[int] = set()
    for intersection in source.intersections:
        if intersection.cluster_id in cluster_ids:
            raise ValueError(
                f"duplicate ClipIntersection cluster_id: {intersection.cluster_id}"
            )
        cluster_ids.add(intersection.cluster_id)
        if not math.isfinite(intersection.radius_m) or intersection.radius_m <= 0.0:
            raise ValueError(
                f"ClipIntersection {intersection.cluster_id} requires a positive radius"
            )
    for track in source.tracks:
        if any(point.vehicle_id != track.vehicle_id for point in track.points):
            raise ValueError(
                f"ClipTrack {track.trip_id!r} contains a point from another vehicle"
            )


def extract_clips_for_run(
    conn: object,
    run_id: str,
    *,
    config: ClipConfig | None = None,
    allow_running: bool = False,
) -> ClipResult:
    """Extract clips from one persisted run.

    Historical replay accepts completed runs only. The pipeline runner may set
    ``allow_running`` while validating its just-written Intersect2 artifacts
    in the same transaction, before marking that run completed.
    """
    source = _load_clip_input_for_run(conn, run_id, allow_running=allow_running)
    return extract_clips(source, config=config)


def _load_clip_input_for_run(
    conn: object,
    run_id: str,
    *,
    allow_running: bool = False,
) -> ClipInput:
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT source_id, source_kind, parameters, status "
            "FROM pipeline_runs WHERE id = %s",
            (run_id,),
        )
        run_row = cur.fetchone()
    if run_row is None:
        raise ValueError(f"unknown pipeline run: {run_id}")
    source_id, _source_kind, raw_parameters, status = run_row
    accepted_statuses = {"completed", "running"} if allow_running else {"completed"}
    if status not in accepted_statuses:
        raise ValueError(f"pipeline run {run_id} is not completed")
    parameters = _decode_parameters(raw_parameters)
    table = _quoted_table_name(str(parameters.get("target_table", "trajectory_points")))
    skip_clean = bool(parameters.get("skip_clean", False))
    cleaner_max_gap_s = float(parameters.get("cleaner_max_gap_s", 120.0))

    intersections: list[ClipIntersection] = []
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            "SELECT i.cluster_id, ci.centroid_lon, ci.centroid_lat, i.clip_radius_m "
            "FROM intersections AS i "
            "JOIN candidate_intersections AS ci "
            "ON ci.id = i.candidate_intersection_id "
            "WHERE i.run_id = %s AND i.is_complete IS TRUE "
            "ORDER BY i.cluster_id",
            (run_id,),
        )
        for cluster_id, lon, lat, radius_m in cur:
            if radius_m is None:
                raise ValueError(f"pipeline run {run_id} predates Clip radius persistence")
            if not math.isfinite(float(radius_m)) or float(radius_m) <= 0.0:
                raise ValueError(f"pipeline run {run_id} has an invalid Clip radius")
            intersections.append(
                ClipIntersection(
                    cluster_id=int(cluster_id),
                    lon=float(lon),
                    lat=float(lat),
                    radius_m=float(radius_m),
                )
            )

    if skip_clean:
        point_sql = (
            "SELECT tp.vehicle_id, 0 AS subtrack_index, tp.id AS sequence_in_subtrack, "
            "%s::text AS point_source_id, tp.id::text AS source_row_key, "
            "tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, tp.extra "
            f"FROM {table} AS tp "
            "WHERE tp.is_active IS NOT FALSE "
            "ORDER BY tp.vehicle_id, tp.timestamp, tp.id"
        )
        point_params: tuple[object, ...] = (source_id,)
    else:
        point_sql = (
            "SELECT ktp.vehicle_id, ktp.subtrack_index, ktp.sequence_in_subtrack, "
            "ktp.source_id, ktp.source_row_key, "
            "tp.timestamp, tp.lon, tp.lat, tp.heading, tp.speed, tp.extra "
            "FROM kept_trajectory_points AS ktp "
            f"JOIN {table} AS tp ON tp.id = ktp.trajectory_point_id "
            "WHERE ktp.run_id = %s AND tp.is_active IS NOT FALSE "
            "ORDER BY ktp.vehicle_id, ktp.subtrack_index, "
            "ktp.sequence_in_subtrack, tp.id"
        )
        point_params = (run_id,)

    grouped: dict[tuple[str, int], list[TrajectoryPoint]] = {}
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(point_sql, point_params)
        for row in cur:
            vehicle_id = str(row[0])
            subtrack_index = int(row[1])
            if row[3] is None or row[4] is None:
                raise ValueError(
                    f"pipeline run {run_id} predates persisted source references"
                )
            grouped.setdefault((vehicle_id, subtrack_index), []).append(
                TrajectoryPoint(
                    point_ref=PointRef(str(row[3]), str(row[4]), stable=True),
                    vehicle_id=vehicle_id,
                    timestamp=float(row[5]),
                    lon=float(row[6]),
                    lat=float(row[7]),
                    heading=float(row[8]),
                    speed=float(row[9]),
                    extra=row[10] if isinstance(row[10], dict) else {},
                )
            )

    if skip_clean:
        split_grouped: dict[tuple[str, int], list[TrajectoryPoint]] = {}
        for (vehicle_id, _), points in grouped.items():
            subtrack_index = 1
            current: list[TrajectoryPoint] = []
            for point in points:
                if current and point.timestamp - current[-1].timestamp > cleaner_max_gap_s:
                    split_grouped[(vehicle_id, subtrack_index)] = current
                    subtrack_index += 1
                    current = []
                current.append(point)
            if current:
                split_grouped[(vehicle_id, subtrack_index)] = current
        grouped = split_grouped

    tracks = tuple(
        ClipTrack(
            vehicle_id=vehicle_id,
            trip_id=f"{vehicle_id}:subtrack-{subtrack_index:04d}",
            points=tuple(points),
        )
        for (vehicle_id, subtrack_index), points in grouped.items()
    )
    return ClipInput(
        intersections=tuple(intersections),
        tracks=tracks,
        run_id=run_id,
    )


def _decode_parameters(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("pipeline run parameters must be a JSON object")


def _quoted_table_name(value: str) -> str:
    parts = value.split(".")
    if not parts or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) is None for part in parts):
        raise ValueError(f"invalid trajectory table name: {value!r}")
    return ".".join(f'"{part}"' for part in parts)


def _extract_track_clips(
    track: ClipTrack,
    intersections: tuple[ClipIntersection, ...],
    *,
    include_boundaries: bool,
    run_id: str | None,
) -> tuple[list[TrackClip], int, int, int]:
    events = _crossing_events(track, intersections)
    clips: list[TrackClip] = []
    skipped = 0
    unmatched_entries = 0
    unmatched_exits = 0

    def append_clip(start: ClipEndpoint, end: ClipEndpoint, clip_type: str) -> None:
        nonlocal skipped
        if _progress(end) - _progress(start) <= _EPS:
            skipped += 1
            return
        # Integer point indices strictly inside the exact fractional bounds.
        # Slice directly so many clips do not repeatedly rescan the full trip.
        interior_start = math.floor(_progress(start) + _EPS) + 1
        interior_end = math.ceil(_progress(end) - _EPS)
        interior = track.points[interior_start:interior_end]
        geometry = [(start.lon, start.lat)]
        geometry.extend((point.lon, point.lat) for point in interior)
        geometry.append((end.lon, end.lat))
        ordinal = len(clips)
        clips.append(
            TrackClip(
                clip_id=f"{run_id or 'memory'}:{track.vehicle_id}:{track.trip_id}:{ordinal:04d}",
                vehicle_id=track.vehicle_id,
                trip_id=track.trip_id,
                clip_type=clip_type,
                start=start,
                end=end,
                points=interior,
                distance_m=_polyline_distance_m(geometry),
                point_count=len(interior) + 2,
            )
        )

    trip_start = _trip_endpoint(track, start=True)
    trip_end = _trip_endpoint(track, start=False)
    if not events:
        if include_boundaries and not _inside_any_intersection(
            track.points[0], intersections
        ):
            append_clip(trip_start, trip_end, "full_track")
        return clips, skipped, unmatched_entries, unmatched_exits

    if include_boundaries and events[0].endpoint.kind == "entry":
        append_clip(trip_start, events[0].endpoint, "boundary_start")

    pending_exit: ClipEndpoint | None = None
    for event_index, event in enumerate(events):
        endpoint = event.endpoint
        if endpoint.kind == "exit":
            if pending_exit is not None:
                unmatched_exits += 1
            pending_exit = endpoint
            continue
        if pending_exit is not None:
            if pending_exit.cluster_id != endpoint.cluster_id:
                append_clip(pending_exit, endpoint, "inter_intersection")
            pending_exit = None
        elif event_index != 0:
            unmatched_entries += 1

    if (
        include_boundaries
        and events[-1].endpoint.kind == "exit"
        and pending_exit is not None
    ):
        append_clip(pending_exit, trip_end, "boundary_end")
    return clips, skipped, unmatched_entries, unmatched_exits


def _crossing_events(
    track: ClipTrack,
    intersections: tuple[ClipIntersection, ...],
) -> list[_CrossingEvent]:
    if not intersections:
        return []
    grid = _IntersectionGrid(intersections)
    events: list[_CrossingEvent] = []
    for segment_index, (start, end) in enumerate(zip(track.points, track.points[1:])):
        start_xy = grid.point_xy(start)
        end_xy = grid.point_xy(end)
        for indexed in grid.candidates(start_xy, end_xy):
            intersection = indexed.value
            a = (start_xy[0] - indexed.x, start_xy[1] - indexed.y)
            b = (end_xy[0] - indexed.x, end_xy[1] - indexed.y)
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            for fraction in _circle_crossing_fractions(a, b, intersection.radius_m):
                rx = a[0] + fraction * dx
                ry = a[1] + fraction * dy
                radial_velocity = rx * dx + ry * dy
                if abs(radial_velocity) <= _EPS:
                    continue
                kind = "entry" if radial_velocity < 0.0 else "exit"
                endpoint = ClipEndpoint(
                    kind=kind,
                    lon=start.lon + fraction * (end.lon - start.lon),
                    lat=start.lat + fraction * (end.lat - start.lat),
                    timestamp=start.timestamp + fraction * (end.timestamp - start.timestamp),
                    cluster_id=intersection.cluster_id,
                    segment_index=segment_index,
                    segment_fraction=fraction,
                )
                events.append(_CrossingEvent(endpoint))

    events.sort(
        key=lambda event: (
            event.progress,
            event.endpoint.cluster_id if event.endpoint.cluster_id is not None else -1,
            event.endpoint.kind,
        )
    )
    deduplicated: list[_CrossingEvent] = []
    for event in events:
        if deduplicated:
            previous = deduplicated[-1]
            if (
                previous.endpoint.cluster_id == event.endpoint.cluster_id
                and abs(previous.progress - event.progress) <= _EPS
            ):
                if previous.endpoint.kind != event.endpoint.kind:
                    # An outside -> boundary -> outside reversal touches the
                    # circle at a shared vertex but never enters its interior.
                    deduplicated.pop()
                continue
        deduplicated.append(event)
    return deduplicated


def _inside_any_intersection(
    point: TrajectoryPoint,
    intersections: tuple[ClipIntersection, ...],
) -> bool:
    for intersection in intersections:
        x, y = _to_xy(point.lon, point.lat, intersection.lon, intersection.lat)
        if math.hypot(x, y) < intersection.radius_m - _EPS:
            return True
    return False


def _circle_crossing_fractions(
    start: tuple[float, float],
    end: tuple[float, float],
    radius_m: float,
) -> tuple[float, ...]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    qa = dx * dx + dy * dy
    if qa <= _EPS or radius_m <= 0.0:
        return ()
    qb = 2.0 * (start[0] * dx + start[1] * dy)
    qc = start[0] * start[0] + start[1] * start[1] - radius_m * radius_m
    discriminant = qb * qb - 4.0 * qa * qc
    if discriminant <= _EPS:
        return ()
    root = math.sqrt(discriminant)
    values = sorted(((-qb - root) / (2.0 * qa), (-qb + root) / (2.0 * qa)))
    return tuple(
        min(1.0, max(0.0, value))
        for value in values
        if -_EPS <= value <= 1.0 + _EPS
    )


def _trip_endpoint(track: ClipTrack, *, start: bool) -> ClipEndpoint:
    if start:
        point = track.points[0]
        return ClipEndpoint("trip_start", point.lon, point.lat, point.timestamp, None, 0, 0.0)
    point = track.points[-1]
    return ClipEndpoint(
        "trip_end",
        point.lon,
        point.lat,
        point.timestamp,
        None,
        len(track.points) - 2,
        1.0,
    )


def _progress(endpoint: ClipEndpoint) -> float:
    return endpoint.segment_index + endpoint.segment_fraction


def _polyline_distance_m(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    origin_lon, origin_lat = points[0]
    xy = [_to_xy(lon, lat, origin_lon, origin_lat) for lon, lat in points]
    return sum(_distance_m(left, right) for left, right in zip(xy, xy[1:]))
