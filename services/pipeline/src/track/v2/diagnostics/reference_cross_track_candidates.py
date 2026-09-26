"""Historical Method 3 vehicle-pair oracle used by equivalence benchmarks."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import numpy as np

from .._geometry import (
    _angle_delta_deg,
    _iter_bounded_segment_intersections,
    _to_lonlat,
    _to_xy,
)
from ..intersections.performance_timing import PerfTracker
from ..models import CrossTrackCandidate, TrajectoryPoint
from ..projection import CoordinateTransformer


def extract_cross_track_candidates_legacy(
    tracks: dict[str, list[TrajectoryPoint]],
    origin_lon: float,
    origin_lat: float,
    cfg: Any,
    live_status: Any = None,
    *,
    sub_detail: bool = False,
    perf: PerfTracker | None = None,
    telemetry: dict[str, int | float] | None = None,
    vehicle_aabb_prefilter: bool = True,
    coordinate_transformer: CoordinateTransformer | None = None,
) -> tuple[list[CrossTrackCandidate], int, int]:
    """Run the dense vehicle-pair reference path.

    ``coordinate_transformer=None`` retains the historical local-origin oracle
    solely for projection-difference evaluation. Supplying it makes both
    segment endpoints and result coordinates use the same Run CRS as the
    optimized implementation, which is the algorithm-equivalence oracle.
    """
    extraction_started = time.perf_counter() if telemetry is not None else 0.0
    segment_started = time.perf_counter() if telemetry is not None else 0.0
    track_segments: dict[
        str,
        list[tuple[float, float, float, float, float, float, int]],
    ] = {}
    for vehicle_id, track in tracks.items():
        if len(track) < 2:
            continue
        segments: list[tuple[float, float, float, float, float, float, int]] = []
        for segment_index in range(len(track) - 1):
            start = track[segment_index]
            end = track[segment_index + 1]
            if abs(_angle_delta_deg(start.heading, end.heading)) > cfg.cross_track_straightness_deg:
                continue
            if coordinate_transformer is None:
                x1, y1 = _to_xy(start.lon, start.lat, origin_lon, origin_lat)
                x2, y2 = _to_xy(end.lon, end.lat, origin_lon, origin_lat)
            else:
                if start.x is None or start.y is None or end.x is None or end.y is None:
                    raise ValueError("same-CRS reference requires projected point x/y")
                x1, y1 = float(start.x), float(start.y)
                x2, y2 = float(end.x), float(end.y)
            segments.append((
                x1, y1, x2, y2, start.heading, start.timestamp, segment_index,
            ))
        if segments:
            track_segments[vehicle_id] = segments

    if telemetry is not None:
        telemetry["segment_build_seconds"] = telemetry.get(
            "segment_build_seconds", 0.0,
        ) + (time.perf_counter() - segment_started)
        telemetry["segments"] = telemetry.get("segments", 0) + sum(
            len(segments) for segments in track_segments.values()
        )
    vehicle_ids = sorted(track_segments)
    if len(vehicle_ids) < 2:
        return [], 0, 0

    array_started = time.perf_counter() if telemetry is not None else 0.0
    segment_arrays: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, list[float]]] = {}
    for vehicle_id in vehicle_ids:
        segments = track_segments[vehicle_id]
        segment_arrays[vehicle_id] = (
            np.array([[segment[0], segment[1]] for segment in segments], dtype=float),
            np.array([[segment[2], segment[3]] for segment in segments], dtype=float),
            np.array([segment[4] for segment in segments], dtype=float),
            [segment[5] for segment in segments],
        )
    vehicle_aabbs = {
        vehicle_id: (
            float(np.minimum(arrays[0][:, 0], arrays[1][:, 0]).min()),
            float(np.maximum(arrays[0][:, 0], arrays[1][:, 0]).max()),
            float(np.minimum(arrays[0][:, 1], arrays[1][:, 1]).min()),
            float(np.maximum(arrays[0][:, 1], arrays[1][:, 1]).max()),
        )
        for vehicle_id, arrays in segment_arrays.items()
    }
    if telemetry is not None:
        telemetry["array_build_seconds"] = telemetry.get(
            "array_build_seconds", 0.0,
        ) + (time.perf_counter() - array_started)

    candidates: list[CrossTrackCandidate] = []
    ordered_candidates: list[tuple[tuple[int, ...], CrossTrackCandidate]] = []
    actual_vehicle_ids = sorted({
        track[0].vehicle_id for track in tracks.values() if track
    })
    vehicle_rank = {vehicle_id: index for index, vehicle_id in enumerate(actual_vehicle_ids)}
    total_pairs = len(vehicle_ids) * (len(vehicle_ids) - 1) // 2
    pair_count = 0
    started = time.perf_counter()
    last_progress = started
    vehicle_indexes = range(len(vehicle_ids))
    outer_iter = perf.timed_iter(
        vehicle_indexes,
        name="cross_track_vehicle_i",
        total=len(vehicle_ids),
    ) if perf else vehicle_indexes
    for index_a in outer_iter:
        vehicle_a = vehicle_ids[index_a]
        segments_a = track_segments[vehicle_a]
        a1, a2, headings_a, timestamps_a = segment_arrays[vehicle_a]
        for index_b in range(index_a + 1, len(vehicle_ids)):
            vehicle_b = vehicle_ids[index_b]
            actual_a = tracks[vehicle_a][0].vehicle_id
            actual_b = tracks[vehicle_b][0].vehicle_id
            if coordinate_transformer is not None and actual_a == actual_b:
                continue
            segments_b = track_segments[vehicle_b]
            b1, b2, headings_b, timestamps_b = segment_arrays[vehicle_b]
            if telemetry is not None:
                telemetry["vehicle_pairs"] = telemetry.get("vehicle_pairs", 0) + 1
                telemetry["segment_pair_space"] = telemetry.get(
                    "segment_pair_space", 0,
                ) + len(segments_a) * len(segments_b)

            a_box = vehicle_aabbs[vehicle_a]
            b_box = vehicle_aabbs[vehicle_b]
            overlaps = not vehicle_aabb_prefilter or (
                a_box[0] <= b_box[1] and b_box[0] <= a_box[1]
                and a_box[2] <= b_box[3] and b_box[2] <= a_box[3]
            )
            if telemetry is not None:
                telemetry["vehicle_aabb_pairs"] = telemetry.get("vehicle_aabb_pairs", 0) + 1
                telemetry["vehicle_aabb_rejected"] = telemetry.get(
                    "vehicle_aabb_rejected", 0,
                ) + int(not overlaps)
                if overlaps:
                    telemetry["vehicle_kernel_pairs"] = telemetry.get(
                        "vehicle_kernel_pairs", 0,
                    ) + 1

            intersections = _iter_bounded_segment_intersections(
                a1, a2, headings_a, b1, b2, headings_b,
                cfg.cross_track_min_angle_deg,
                cfg.cross_track_max_angle_deg,
                telemetry=telemetry,
            ) if overlaps else ()
            for a_offset, xs, ys, flat_indices in intersections:
                payload_started = time.perf_counter() if telemetry is not None else 0.0
                for x, y, flat_index in zip(xs, ys, flat_indices):
                    segment_index_a = a_offset + flat_index // len(segments_b)
                    segment_index_b = flat_index % len(segments_b)
                    start_a = tracks[vehicle_a][segments_a[segment_index_a][6]]
                    end_a = tracks[vehicle_a][segments_a[segment_index_a][6] + 1]
                    start_b = tracks[vehicle_b][segments_b[segment_index_b][6]]
                    end_b = tracks[vehicle_b][segments_b[segment_index_b][6] + 1]
                    if coordinate_transformer is None:
                        lon, lat = _to_lonlat(float(x), float(y), origin_lon, origin_lat)
                    else:
                        lon, lat = coordinate_transformer.inverse(float(x), float(y))
                    candidate = CrossTrackCandidate(
                        point_ref=start_a.point_ref,
                        from_segment_start_point_ref=start_a.point_ref,
                        to_segment_start_point_ref=start_b.point_ref,
                        vehicle_id=(
                            f"{actual_a}|{actual_b}"
                            if coordinate_transformer is not None
                            else f"{vehicle_a}|{vehicle_b}"
                        ),
                        timestamp=(timestamps_a[segment_index_a] + timestamps_b[segment_index_b]) / 2.0,
                        lon=lon,
                        lat=lat,
                        x=float(x),
                        y=float(y),
                        heading=segments_a[segment_index_a][4],
                        speed=start_a.speed,
                        from_segment_start=start_a.refined_point_ref,
                        to_segment_start=start_b.refined_point_ref,
                        cross_track_calculate_index=len(candidates),
                    )
                    if coordinate_transformer is None:
                        candidates.append(candidate)
                    else:
                        ordered_candidates.append(((
                            vehicle_rank[actual_a], vehicle_rank[actual_b],
                            segment_index_a, segment_index_b, index_a, index_b,
                        ), candidate))
                if telemetry is not None:
                    telemetry["payload_seconds"] = telemetry.get("payload_seconds", 0.0) + (
                        time.perf_counter() - payload_started
                    )
                    telemetry["exact_hits"] = telemetry.get("exact_hits", 0) + len(flat_indices)

            pair_count += 1
            now = time.perf_counter()
            if pair_count % 100 == 0 or now - last_progress >= 3.0:
                _emit_progress(
                    live_status,
                    pair_count,
                    total_pairs,
                    len(candidates),
                    now - started,
                    sub_detail=sub_detail,
                )
                last_progress = now

    if coordinate_transformer is not None:
        ordered_candidates.sort(key=lambda item: item[0])
        candidates = [
            replace(candidate, cross_track_calculate_index=index)
            for index, (_key, candidate) in enumerate(ordered_candidates)
        ]
    _emit_progress(
        live_status,
        pair_count,
        total_pairs,
        len(candidates),
        time.perf_counter() - started,
        sub_detail=sub_detail,
        final=True,
    )
    if telemetry is not None:
        telemetry["cross_track_seconds"] = telemetry.get(
            "cross_track_seconds", 0.0,
        ) + (time.perf_counter() - extraction_started)
        telemetry["output_candidates"] = telemetry.get("output_candidates", 0) + len(candidates)
    return candidates, pair_count, total_pairs


def extract_cross_track_candidates_reference(
    tracks: dict[str, list[TrajectoryPoint]],
    transformer: CoordinateTransformer,
    cfg: Any,
    live_status: Any = None,
    **kwargs: Any,
) -> tuple[list[CrossTrackCandidate], int, int]:
    """Dense same-processing-CRS reference for the optimized algorithm gate."""
    return extract_cross_track_candidates_legacy(
        tracks,
        0.0,
        0.0,
        cfg,
        live_status,
        coordinate_transformer=transformer,
        **kwargs,
    )


def _emit_progress(
    live_status: Any,
    pair_count: int,
    total_pairs: int,
    candidate_count: int,
    elapsed: float,
    *,
    sub_detail: bool = False,
    final: bool = False,
) -> str:
    detail = (
        f"跨车求交/Cross-track:\t{pair_count:,}/{total_pairs:,}\t车辆对/pairs,\t"
        f"{candidate_count:,}\t候选/candidates"
    )
    line = detail if final else f"计算中：{detail}"
    if live_status is not None:
        if sub_detail:
            live_status.update(sub_detail=line, elapsed=elapsed)
        else:
            live_status.update(
                progress=(pair_count / total_pairs) if total_pairs else 0.0,
                detail=line,
                elapsed=elapsed,
            )
    else:
        print(f"    ... {line} ({elapsed:.1f}s)")
    return detail
