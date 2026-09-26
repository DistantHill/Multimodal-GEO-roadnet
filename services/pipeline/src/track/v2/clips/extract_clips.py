"""Clip resolved Detect sequences with one active-set state machine.

Detect owns every geometric circle crossing. Clip only consumes the persisted
p/i/c order and never solves segment/circle intersections again.
"""

from __future__ import annotations

import math
import numpy as np
from collections import Counter
from pathlib import Path

from .clip_input import ClipRelation
from ..telemetry.heartbeat import observed_operation, heartbeat_progress

from ..db.artifact_registry import find_detect_artifact
from ..intersections.query_results import fetch_detect_row
from ..intersections.results.read_parquet import DetectDatasetReader
from ..models import (
    ClipConfig,
    ClipInput,
    ClipIntersection,
    ClipReport,
    ClipResult,
    ClipTrack,
    RuntimeConfig,
    TrackClip,
)
from ..projection import CoordinateTransformer


_EPS = 1e-9


def extract_clips(
    source: ClipInput,
    *,
    config: ClipConfig,
) -> ClipResult:
    """Assemble road fragments from already-resolved p/i/c sequences."""
    cfg = config
    _validate_input(source)
    transformer = CoordinateTransformer(source.spatial_context)
    circles = _project_intersections(source.intersections, transformer)
    clips: list[TrackClip] = []
    skipped = 0

    for track in source.tracks:
        if len(track.xy) < 2:
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
    config: ClipConfig,
    verify_sha256: bool,
) -> ClipResult:
    """Load and clip one completed Detect Run."""
    run = fetch_detect_row(conn, detect_id)
    if run.status != "completed":
        raise ValueError(f"Detect Run {detect_id} is not completed")
    with conn.cursor() as cur:
        registered = find_detect_artifact(cur, detect_id)
    cfg = config
    transformer = CoordinateTransformer(run.spatial_context)
    relation = ClipRelation(runtime, data_root, batch_size=runtime.relation_batch_size)
    vehicles: set[str] = set()
    seen_subtracks: set[int] = set()
    counts: Counter[str] = Counter()
    skipped = point_count = 0
    try:
        with DetectDatasetReader(
            publication_root, registered.descriptor, registered.state, runtime,
            verify_sha256=verify_sha256,
        ) as reader:
            intersections = reader.intersections()
            circles = _project_intersections(intersections, transformer)
            _validate_input(ClipInput(intersections, (), run.spatial_context, detect_id))
            tracks = reader.iter_tracks(batch_size=runtime.parquet_read_batch_size)
            try:
                for track in tracks:
                    if track.subtrack_index in seen_subtracks:
                        raise ValueError(f"duplicate ClipTrack subtrack {track.subtrack_index}")
                    seen_subtracks.add(track.subtrack_index)
                    vehicles.add(track.vehicle_id)
                    _validate_input(ClipInput((), (track,), run.spatial_context, detect_id))
                    if len(track.xy) < 2:
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
        np.asarray([(intersection.lon, intersection.lat) for intersection in intersections], dtype=np.float64).reshape(-1, 2)
    )
    return {
        intersection.intersection_id: (x, y, intersection.radius_m)
        for intersection, (x, y) in zip(intersections, projected, strict=True)
    }


def _extract_track_clips(
    track: ClipTrack,
    circles: dict[int, tuple[float, float, float]],
    transformer: CoordinateTransformer,
    *, include_boundaries: bool, detect_id: str | None,
) -> tuple[list[TrackClip], int]:
    x, y = track.xy[0]
    active = {identifier for identifier, (cx, cy, radius) in circles.items()
              if math.hypot(x-cx, y-cy) < radius-_EPS}
    event_indexes = np.flatnonzero(track.crossing_valid).tolist()
    clips: list[TrackClip] = []
    skipped = 0
    open_index: int | None = 0 if not active and include_boundaries else None
    open_after_index = 1

    def append_clip(start: int, end: int, lo: int, hi: int, clip_type: str) -> None:
        nonlocal skipped
        # A simultaneous crossing batch may choose an endpoint outside the
        # consecutive interior range; gather only those two boundary members.
        if start == lo-1 and end == hi:
            xy, lonlat = track.xy[start:end+1], track.lonlat[start:end+1]
        else:
            xy = np.concatenate((track.xy[start:start+1], track.xy[lo:hi], track.xy[end:end+1]))
            lonlat = np.concatenate((track.lonlat[start:start+1], track.lonlat[lo:hi], track.lonlat[end:end+1]))
        distance = float(np.linalg.norm(np.diff(xy, axis=0), axis=1).sum())
        if distance <= _EPS:
            skipped += 1
            return
        ordinal = len(clips)
        valid_start, valid_end = track.crossing_valid[start], track.crossing_valid[end]
        clips.append(TrackClip(
            clip_id=f"{detect_id or 'memory'}:{track.vehicle_id}:{track.subtrack_index}:{ordinal:04d}",
            vehicle_id=track.vehicle_id, trip_id=track.trip_id, clip_type=clip_type,
            xy=xy, lonlat=lonlat, resolved_point_ids=track.resolved_point_ids[lo:hi],
            start_index=start, end_index=end, interior_start=lo, interior_end=hi,
            start_kind=("entry" if track.crossing_types[start] == 1 else "exit") if valid_start else "boundary",
            end_kind=("entry" if track.crossing_types[end] == 1 else "exit") if valid_end else "boundary",
            start_intersection_id=int(track.intersection_ids[start]) if valid_start else None,
            end_intersection_id=int(track.intersection_ids[end]) if valid_end else None,
            start_crossing_id=int(track.crossing_ids[start]) if valid_start else None,
            end_crossing_id=int(track.crossing_ids[end]) if valid_end else None,
            distance_m=distance, point_count=len(xy), subtrack_index=track.subtrack_index,
            clip_ordinal_in_track=ordinal,
        ))

    cursor = 0
    while cursor < len(event_indexes):
        batch_start = cursor
        first = event_indexes[cursor]
        cursor += 1
        while cursor < len(event_indexes):
            candidate = event_indexes[cursor]
            if not np.all(np.abs(track.lonlat[first]-track.lonlat[candidate]) <= 1e-12):
                break
            if candidate != event_indexes[cursor-1]+1:
                break
            cursor += 1
        batch = event_indexes[batch_start:cursor]
        before_empty = not active
        key = lambda index: (int(track.intersection_ids[index]) or -1, int(track.crossing_ids[index]) or -1)
        exits = sorted((i for i in batch if track.crossing_types[i] == 0), key=key)
        entries = sorted((i for i in batch if track.crossing_types[i] == 1), key=key)
        if len(exits)+len(entries) != len(batch):
            raise ValueError(f"subtrack {track.subtrack_index} has invalid crossing type")
        for index in exits:
            identifier = int(track.intersection_ids[index])
            if identifier not in active:
                raise ValueError(f"subtrack {track.subtrack_index} exits inactive intersection {identifier}")
            active.remove(identifier)
        for index in entries:
            identifier = int(track.intersection_ids[index])
            if identifier in active:
                raise ValueError(f"subtrack {track.subtrack_index} re-enters active intersection {identifier}")
            active.add(identifier)
        after_empty = not active
        if before_empty and not after_empty:
            if open_index is not None:
                clip_type = "between_intersection" if track.crossing_valid[open_index] else "boundary_end"
                append_clip(open_index, entries[0], open_after_index, batch[0], clip_type)
            open_index = None
        elif not before_empty and after_empty:
            open_index = exits[-1]
            open_after_index = batch[-1]+1
        elif before_empty and after_empty:
            raise ValueError(f"subtrack {track.subtrack_index} has a no-op crossing batch")
    if not event_indexes:
        if not active and include_boundaries:
            append_clip(0, len(track.xy)-1, 1, len(track.xy)-1, "full_track")
        return clips, skipped
    if not active and open_index is not None and include_boundaries:
        append_clip(open_index, len(track.xy)-1, open_after_index, len(track.xy)-1, "boundary_start")
    return clips, skipped


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
        if len(track.resolved_point_ids) != len(np.unique(track.resolved_point_ids)):
            raise ValueError(f"subtrack {track.subtrack_index} repeats a resolved point")
        if not np.isfinite(track.xy).all() or not np.isfinite(track.lonlat).all():
            raise ValueError(f"subtrack {track.subtrack_index} has invalid coordinates")
        if np.any(track.crossing_valid != track.intersection_valid) or np.any(track.crossing_valid & ~np.isin(track.crossing_types, (0, 1))):
            raise ValueError("resolved crossing metadata must be all-or-none")
