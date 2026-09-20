"""Clip resolved Detect sequences with one active-set state machine.

Detect owns every geometric circle crossing. Clip only consumes the persisted
p/i/c order and never solves segment/circle intersections again.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

from .clip_relation import ClipRelation
from .heartbeat import observed_operation, heartbeat_progress

from .db.artifact_registry import find_detect_artifact
from .db.queries import fetch_detect_row
from .detect_parquet_reader import DetectDatasetReader
from .models import (
    ClipConfig,
    ClipEndpoint,
    ClipInput,
    ClipIntersection,
    ClipPoint,
    ClipReport,
    ClipResult,
    ClipTrack,
    ResolvedTrackPoint,
    RuntimeConfig,
    TrackClip,
)
from .projection import CoordinateTransformer


_EPS = 1e-9


def extract_clips(
    source: ClipInput,
    *,
    config: ClipConfig | None = None,
) -> ClipResult:
    """Assemble road fragments from already-resolved p/i/c sequences."""
    cfg = config or ClipConfig()
    _validate_input(source)
    transformer = CoordinateTransformer(source.spatial_context)
    circles = _project_intersections(source.intersections, transformer)
    clips: list[TrackClip] = []
    skipped = 0

    for track in source.tracks:
        if len(track.points) < 2:
            skipped += 1
            continue
        track_clips, track_skipped = _extract_track_clips(
            track,
            circles,
            transformer,
            include_boundaries=cfg.include_boundary_clips,
            detect_id=source.detect_id,
        )
        clips.extend(track_clips)
        skipped += track_skipped

    return ClipResult(
        clips=tuple(clips),
        report=ClipReport(
            total_vehicles=len({track.vehicle_id for track in source.tracks}),
            total_subtracks=len(source.tracks),
            total_clips=len(clips),
            between_intersection_clips=sum(
                clip.clip_type == "between_intersection" for clip in clips
            ),
            boundary_start_clips=sum(
                clip.clip_type == "boundary_start" for clip in clips
            ),
            boundary_end_clips=sum(
                clip.clip_type == "boundary_end" for clip in clips
            ),
            full_track_clips=sum(clip.clip_type == "full_track" for clip in clips),
            total_points_in_clips=sum(clip.point_count for clip in clips),
            skipped_degenerate_count=skipped,
        ),
        spatial_context=source.spatial_context,
        detect_id=source.detect_id,
        intersections=source.intersections,
    )


@observed_operation("Clip：流式构建片段")
def extract_clips_for_detect(
    conn: object,
    detect_id: str,
    *,
    publication_root: Path | str,
    data_root: Path | str,
    runtime: RuntimeConfig,
    config: ClipConfig | None = None,
) -> ClipResult:
    """Load and clip one completed Detect Run."""
    run = fetch_detect_row(conn, detect_id)
    if run.status != "completed":
        raise ValueError(f"Detect Run {detect_id} is not completed")
    with conn.cursor() as cur:
        registered = find_detect_artifact(cur, detect_id)
    cfg = config or ClipConfig()
    transformer = CoordinateTransformer(run.spatial_context)
    relation = ClipRelation(runtime, data_root)
    vehicles: set[str] = set()
    seen_subtracks: set[int] = set()
    counts: Counter[str] = Counter()
    skipped = point_count = 0
    try:
        with DetectDatasetReader(
            publication_root, registered.descriptor, registered.state, runtime,
        ) as reader:
            intersections = reader.intersections()
            circles = _project_intersections(intersections, transformer)
            _validate_input(ClipInput(intersections, (), run.spatial_context, detect_id))
            tracks = reader.iter_tracks()
            try:
                for track in tracks:
                    if track.subtrack_index in seen_subtracks:
                        raise ValueError(f"duplicate ClipTrack subtrack {track.subtrack_index}")
                    seen_subtracks.add(track.subtrack_index)
                    vehicles.add(track.vehicle_id)
                    _validate_input(ClipInput((), (track,), run.spatial_context, detect_id))
                    if len(track.points) < 2:
                        skipped += 1
                        continue
                    track_clips, track_skipped = _extract_track_clips(
                        track, circles, transformer,
                        include_boundaries=cfg.include_boundary_clips, detect_id=detect_id,
                    )
                    skipped += track_skipped
                    for clip in track_clips:
                        relation.append(clip)
                        counts[clip.clip_type] += 1
                        point_count += clip.point_count
                    heartbeat_progress(len(seen_subtracks))
            finally:
                close = getattr(tracks, "close", None)
                if close is not None:
                    close()
        relation.seal()
        return ClipResult(
            clips=relation,
            report=ClipReport(
                total_vehicles=len(vehicles), total_subtracks=len(seen_subtracks),
                total_clips=len(relation),
                between_intersection_clips=counts["between_intersection"],
                boundary_start_clips=counts["boundary_start"],
                boundary_end_clips=counts["boundary_end"],
                full_track_clips=counts["full_track"],
                total_points_in_clips=point_count, skipped_degenerate_count=skipped,
            ),
            spatial_context=run.spatial_context, detect_id=detect_id,
            intersections=intersections,
        )
    except BaseException:
        relation.close()
        raise


def _project_intersections(
    intersections: tuple[ClipIntersection, ...],
    transformer: CoordinateTransformer,
) -> dict[int, tuple[float, float, float]]:
    projected = transformer.forward_many(
        (intersection.lon, intersection.lat) for intersection in intersections
    )
    return {
        intersection.intersection_id: (x, y, intersection.radius_m)
        for intersection, (x, y) in zip(intersections, projected, strict=True)
    }


def _initial_active_set(
    point: ResolvedTrackPoint,
    circles: dict[int, tuple[float, float, float]],
    transformer: CoordinateTransformer,
) -> set[int]:
    x, y = transformer.forward(point.lon, point.lat)
    return {
        intersection_id
        for intersection_id, (center_x, center_y, radius) in circles.items()
        if math.hypot(x - center_x, y - center_y) < radius - _EPS
    }


def _extract_track_clips(
    track: ClipTrack,
    circles: dict[int, tuple[float, float, float]],
    transformer: CoordinateTransformer,
    *,
    include_boundaries: bool,
    detect_id: str | None,
) -> tuple[list[TrackClip], int]:
    points = track.points
    active = _initial_active_set(points[0], circles, transformer)
    event_indexes = [
        index for index, point in enumerate(points) if point.crossing_id is not None
    ]
    clips: list[TrackClip] = []
    skipped = 0

    open_endpoint: ClipEndpoint | None = None
    open_after_index = 1
    if not active and include_boundaries:
        open_endpoint = _boundary_endpoint(points[0])

    def append_clip(
        start: ClipEndpoint,
        end: ClipEndpoint,
        interior_start: int,
        interior_end: int,
        clip_type: str,
    ) -> None:
        nonlocal skipped
        interior = tuple(
            ClipPoint(
                resolved_point_id=point.resolved_point_id,
                lon=point.lon,
                lat=point.lat,
            )
            for point in points[interior_start:interior_end]
        )
        geometry = [(start.lon, start.lat)]
        geometry.extend((point.lon, point.lat) for point in interior)
        geometry.append((end.lon, end.lat))
        distance = _polyline_distance_m(geometry, transformer)
        if distance <= _EPS:
            skipped += 1
            return
        ordinal = len(clips)
        clips.append(TrackClip(
            clip_id=(
                f"{detect_id or 'memory'}:{track.vehicle_id}:"
                f"{track.subtrack_index}:{ordinal:04d}"
            ),
            vehicle_id=track.vehicle_id,
            trip_id=track.trip_id,
            clip_type=clip_type,
            start=start,
            end=end,
            points=interior,
            distance_m=distance,
            point_count=len(interior) + 2,
            subtrack_index=track.subtrack_index,
            clip_ordinal_in_track=ordinal,
        ))

    cursor = 0
    while cursor < len(event_indexes):
        batch_start_pos = cursor
        first_index = event_indexes[cursor]
        cursor += 1
        while cursor < len(event_indexes):
            candidate_index = event_indexes[cursor]
            if not _same_position(points[first_index], points[candidate_index]):
                break
            # Only consecutive crossing rows form a simultaneous event batch.
            if candidate_index != event_indexes[cursor - 1] + 1:
                break
            cursor += 1
        batch_indexes = event_indexes[batch_start_pos:cursor]
        batch = [points[index] for index in batch_indexes]
        before_empty = not active

        exits = sorted(
            (point for point in batch if point.crossing_type == 0),
            key=lambda point: (point.intersection_id or -1, point.crossing_id or -1),
        )
        entries = sorted(
            (point for point in batch if point.crossing_type == 1),
            key=lambda point: (point.intersection_id or -1, point.crossing_id or -1),
        )
        if len(exits) + len(entries) != len(batch):
            raise ValueError(f"subtrack {track.subtrack_index} has invalid crossing type")
        for point in exits:
            assert point.intersection_id is not None
            if point.intersection_id not in active:
                raise ValueError(
                    f"subtrack {track.subtrack_index} exits inactive intersection "
                    f"{point.intersection_id}"
                )
            active.remove(point.intersection_id)
        for point in entries:
            assert point.intersection_id is not None
            if point.intersection_id in active:
                raise ValueError(
                    f"subtrack {track.subtrack_index} re-enters active intersection "
                    f"{point.intersection_id}"
                )
            active.add(point.intersection_id)

        after_empty = not active
        if before_empty and not after_empty:
            if open_endpoint is not None:
                endpoint = _crossing_endpoint(entries[0])
                clip_type = (
                    "boundary_end"
                    if open_endpoint.crossing_id is None
                    else "between_intersection"
                )
                append_clip(
                    open_endpoint,
                    endpoint,
                    open_after_index,
                    batch_indexes[0],
                    clip_type,
                )
            open_endpoint = None
        elif not before_empty and after_empty:
            open_endpoint = _crossing_endpoint(exits[-1])
            open_after_index = batch_indexes[-1] + 1
        # If both states are nonempty, this batch lies in overlapping circles
        # and cannot delimit a road fragment. Empty→empty would mean Detect
        # persisted a non-state-changing root, which is an invalid sequence.
        elif before_empty and after_empty:
            raise ValueError(
                f"subtrack {track.subtrack_index} has a no-op crossing batch"
            )

    if not event_indexes:
        if not active and include_boundaries:
            append_clip(
                _boundary_endpoint(points[0]),
                _boundary_endpoint(points[-1]),
                1,
                len(points) - 1,
                "full_track",
            )
        # Entirely inside one or more circles intentionally produces no Clip.
        return clips, skipped

    if not active and open_endpoint is not None and include_boundaries:
        append_clip(
            open_endpoint,
            _boundary_endpoint(points[-1]),
            open_after_index,
            len(points) - 1,
            "boundary_start",
        )
    return clips, skipped


def _crossing_endpoint(point: ResolvedTrackPoint) -> ClipEndpoint:
    if (
        point.crossing_id is None
        or point.intersection_id is None
        or point.crossing_type not in (0, 1)
    ):
        raise ValueError("resolved crossing metadata is incomplete")
    return ClipEndpoint(
        kind="entry" if point.crossing_type == 1 else "exit",
        lon=point.lon,
        lat=point.lat,
        intersection_id=point.intersection_id,
        crossing_id=point.crossing_id,
    )


def _boundary_endpoint(point: ResolvedTrackPoint) -> ClipEndpoint:
    return ClipEndpoint(
        kind="boundary",
        lon=point.lon,
        lat=point.lat,
        intersection_id=None,
        crossing_id=None,
    )


def _same_position(left: ResolvedTrackPoint, right: ResolvedTrackPoint) -> bool:
    return abs(left.lon - right.lon) <= 1e-12 and abs(left.lat - right.lat) <= 1e-12


def _polyline_distance_m(
    points: list[tuple[float, float]],
    transformer: CoordinateTransformer,
) -> float:
    xy = transformer.forward_many(points)
    return sum(
        math.hypot(right[0] - left[0], right[1] - left[1])
        for left, right in zip(xy, xy[1:])
    )


def _validate_input(source: ClipInput) -> None:
    intersection_ids: set[int] = set()
    for intersection in source.intersections:
        if intersection.intersection_id in intersection_ids:
            raise ValueError(
                f"duplicate ClipIntersection ID {intersection.intersection_id}"
            )
        intersection_ids.add(intersection.intersection_id)
        if not math.isfinite(intersection.radius_m) or intersection.radius_m <= 0:
            raise ValueError("ClipIntersection radius must be positive")
    seen_subtracks: set[int] = set()
    for track in source.tracks:
        if track.subtrack_index in seen_subtracks:
            raise ValueError(f"duplicate ClipTrack subtrack {track.subtrack_index}")
        seen_subtracks.add(track.subtrack_index)
        resolved_ids = [point.resolved_point_id for point in track.points]
        if len(resolved_ids) != len(set(resolved_ids)):
            raise ValueError(f"subtrack {track.subtrack_index} repeats a resolved point")
        for point in track.points:
            if not (math.isfinite(point.lon) and math.isfinite(point.lat)):
                raise ValueError(f"subtrack {track.subtrack_index} has invalid coordinates")
            is_crossing = point.crossing_id is not None
            if is_crossing != (
                point.intersection_id is not None and point.crossing_type in (0, 1)
            ):
                raise ValueError("resolved crossing metadata must be all-or-none")
