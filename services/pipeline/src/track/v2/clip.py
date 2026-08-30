"""Clip stage — extract trajectory fragments outside verified intersections."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ._geometry import _distance_m, _to_xy
from .db.queries import (
    fetch_clean_points,
    fetch_intersections,
    fetch_detect_row,
)
from .models import (
    ClipConfig,
    ClipEndpoint,
    ClipInput,
    ClipIntersection,
    ClipReport,
    ClipResult,
    ClipTrack,
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
        indexed_intersections: list[_IndexedIntersection] = []
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
            indexed_intersections.append(indexed)
            for cell in self._cells_for_bounds(
                x - intersection.radius_m,
                y - intersection.radius_m,
                x + intersection.radius_m,
                y + intersection.radius_m,
            ):
                self.cells.setdefault(cell, []).append(indexed)
        self.indexed_intersections = tuple(indexed_intersections)

    def point_xy(self, point: TrajectoryPoint) -> tuple[float, float]:
        return _to_xy(point.lon, point.lat, self.origin_lon, self.origin_lat)

    def candidates(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[_IndexedIntersection, ...]:
        min_x = min(start[0], end[0])
        min_y = min(start[1], end[1])
        max_x = max(start[0], end[0])
        max_y = max(start[1], end[1])
        min_cell_x = math.floor(min_x / self.cell_size)
        max_cell_x = math.floor(max_x / self.cell_size)
        min_cell_y = math.floor(min_y / self.cell_size)
        max_cell_y = math.floor(max_y / self.cell_size)
        cell_count = (
            (max_cell_x - min_cell_x + 1)
            * (max_cell_y - min_cell_y + 1)
        )
        if cell_count > len(self.indexed_intersections):
            return self.indexed_intersections

        found: dict[int, _IndexedIntersection] = {}
        for cell in self._cells_for_bounds(
            min_x,
            min_y,
            max_x,
            max_y,
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
    intersection_grid = (
        _IntersectionGrid(source.intersections) if source.intersections else None
    )

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
            intersection_grid=intersection_grid,
            include_boundaries=cfg.include_boundary_clips,
            detect_id=source.detect_id,
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
        detect_id=source.detect_id,
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


def extract_clips_for_detect(
    conn: object,
    detect_id: str,
    *,
    config: ClipConfig | None = None,
    allow_running: bool = False,
) -> ClipResult:
    """Extract clips from one persisted detect.

    Historical replay accepts completed detects only. The pipeline runner may set
    ``allow_running`` while validating its just-written Intersect2 artifacts
    in the same transaction, before marking that detect completed.
    """
    source = _load_clip_input_for_detect(conn, detect_id, allow_running=allow_running)
    return extract_clips(source, config=config)


def _load_clip_input_for_detect(
    conn: object,
    detect_id: str,
    *,
    allow_running: bool,
) -> ClipInput:
    run_row = fetch_detect_row(conn, detect_id)
    accepted_statuses = {"completed", "running"} if allow_running else {"completed"}
    if run_row.status not in accepted_statuses:
        raise ValueError(f"Detect Run {detect_id} is not completed")

    params = run_row.parameters
    table = str(params.get("target_table", "trajectory_points"))
    intersections = fetch_intersections(conn, detect_id)
    grouped = fetch_clean_points(conn, table, run_row.clean_id)

    return ClipInput(
        intersections=tuple(intersections),
        tracks=_build_clip_tracks(grouped),
        detect_id=detect_id,
    )


def _split_by_time_gaps(
    grouped: dict[tuple[str, int], list[TrajectoryPoint]],
    *,
    max_gap_s: float,
) -> dict[tuple[str, int], list[TrajectoryPoint]]:
    """Split raw-point groups into subtracks wherever the time gap exceeds *max_gap_s*."""
    result: dict[tuple[str, int], list[TrajectoryPoint]] = {}
    for (vehicle_id, _), points in grouped.items():
        subtrack_index = 1
        current: list[TrajectoryPoint] = []
        for point in points:
            if current and point.timestamp - current[-1].timestamp > max_gap_s:
                result[(vehicle_id, subtrack_index)] = current
                subtrack_index += 1
                current = []
            current.append(point)
        if current:
            result[(vehicle_id, subtrack_index)] = current
    return result


def _build_clip_tracks(
    grouped: dict[tuple[str, int], list[TrajectoryPoint]],
) -> tuple[ClipTrack, ...]:
    """Convert grouped points into ClipTrack tuples."""
    return tuple(
        ClipTrack(
            vehicle_id=vehicle_id,
            trip_id=f"{vehicle_id}:subtrack-{subtrack_index:04d}",
            points=tuple(points),
            subtrack_index=subtrack_index,
        )
        for (vehicle_id, subtrack_index), points in sorted(grouped.items())
    )


def _extract_track_clips(
    track: ClipTrack,
    intersections: tuple[ClipIntersection, ...],
    *,
    intersection_grid: _IntersectionGrid | None,
    include_boundaries: bool,
    detect_id: str | None,
) -> tuple[list[TrackClip], int, int, int]:
    events = _crossing_events(track, intersection_grid)
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
                clip_id=f"{detect_id or 'memory'}:{track.vehicle_id}:{track.trip_id}:{ordinal:04d}",
                vehicle_id=track.vehicle_id,
                trip_id=track.trip_id,
                clip_type=clip_type,
                start=start,
                end=end,
                points=interior,
                distance_m=_polyline_distance_m(geometry),
                point_count=len(interior) + 2,
                subtrack_index=track.subtrack_index,
                clip_ordinal_in_track=ordinal,
                interior_start_index=interior_start,
                interior_end_index_exclusive=interior_end,
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
    grid: _IntersectionGrid | None,
) -> list[_CrossingEvent]:
    if grid is None:
        return []
    events: list[_CrossingEvent] = []
    start = track.points[0]
    start_xy = grid.point_xy(start)
    for segment_index, end in enumerate(track.points[1:]):
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
        start = end
        start_xy = end_xy

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
