"""Refine complete source vehicles using column arrays."""
from __future__ import annotations

import math
import numpy as np
from ..models import (SourceBatch, ExcludedBatch, RefinementSubtrack,
                      RefineIncrementalResult, RefineResult, RefineReport, RefineConfig, SpatialContext, GeneratedReason)
from ..projection import CoordinateTransformer


def refine_vehicle(vehicle_points: SourceBatch, *, spatial_context: SpatialContext,
                   config: RefineConfig,
                   _transformer: CoordinateTransformer | None = None) -> RefineIncrementalResult:
    source = vehicle_points
    transformer = _transformer or CoordinateTransformer(spatial_context)
    cuts = np.r_[0, np.flatnonzero(np.diff(source.timestamp) > config.max_gap_s) + 1, len(source)]
    subtracks = []
    removed = np.zeros(len(source), dtype=np.bool_)
    injected_count = 0
    for partition, (start, stop) in enumerate(zip(cuts[:-1], cuts[1:]), 1):
        indices = np.arange(start, stop)
        if len(indices) >= 3:
            cursor = 0
            while cursor < len(indices):
                if source.speed[indices[cursor]] >= config.min_speed_mps:
                    cursor += 1
                    continue
                first = cursor
                while cursor + 1 < len(indices) and source.speed[indices[cursor + 1]] < config.min_speed_mps:
                    cursor += 1
                if cursor > first and source.timestamp[indices[cursor]] - source.timestamp[indices[first]] > config.max_stationary_duration_s:
                    removed[indices[first + 1:cursor]] = True
                cursor += 1
        indices = indices[~removed[indices]]
        if not len(indices):
            continue
        kept_lonlat = source.lonlat[indices]
        kept_xy = transformer.forward_many(kept_lonlat)
        capacity = 2 * len(indices) - 1
        xy = np.empty((capacity, 2), dtype=np.float64)
        lonlat = np.empty((capacity, 2), dtype=np.float64)
        timestamps = np.empty(capacity, dtype=np.float64)
        headings = np.empty(capacity, dtype=np.float64)
        speeds = np.empty(capacity, dtype=np.float64)
        kinds = np.full(capacity, "p", dtype="U1")
        keys = np.zeros(capacity, dtype=source.source_row_key.dtype)
        key_valid = np.zeros(capacity, dtype=np.bool_)
        reasons = np.full(capacity, "", dtype="U32")
        anchors = np.full(capacity, -1, dtype=np.int64)
        size = 0
        for position, source_index in enumerate(indices):
            xy[size] = kept_xy[position]
            lonlat[size] = source.lonlat[source_index]
            timestamps[size] = source.timestamp[source_index]
            headings[size] = source.heading[source_index]
            speeds[size] = source.speed[source_index]
            keys[size] = source.source_row_key[source_index]
            key_valid[size] = True
            size += 1
            if position + 1 == len(indices):
                continue
            next_index = indices[position + 1]
            delta = angle_delta_deg(source.heading[source_index], source.heading[next_index])
            if not config.min_heading_delta_deg <= abs(delta) <= config.max_heading_delta_deg:
                continue
            ray_a = transformer.heading_vector(float(source.lonlat[source_index, 0]),
                                               float(source.lonlat[source_index, 1]),
                                               float((source.heading[source_index] + 180) % 360))
            ray_b = transformer.heading_vector(float(source.lonlat[next_index, 0]),
                                               float(source.lonlat[next_index, 1]),
                                               float((source.heading[next_index] + 180) % 360))
            hit = ray_intersection(kept_xy[position], ray_a, kept_xy[position + 1], ray_b)
            if hit is None:
                continue
            x, y, distance_a, distance_b = hit
            if min(distance_a, distance_b) < 0 or max(distance_a, distance_b) > config.max_extension_m:
                continue
            xy[size] = x, y
            lonlat[size] = transformer.inverse(x, y)
            timestamps[size] = (source.timestamp[source_index] + source.timestamp[next_index]) / 2
            headings[size] = (source.heading[source_index] + delta / 2) % 360
            speeds[size] = 0.0
            kinds[size] = "i"
            reasons[size] = str(GeneratedReason.HEADING_ORTHOGONAL)
            anchors[size] = position
            size += 1
            injected_count += 1
        subtracks.append(RefinementSubtrack(
            source.vehicle_id, partition, source.source_id, xy[:size], lonlat[:size],
            timestamps[:size], headings[:size], speeds[:size], kinds[:size], keys[:size],
            key_valid[:size], np.zeros(size, dtype=np.int64), np.zeros(size, dtype=np.bool_),
            reasons[:size], anchors[:size],
        ))
    excluded = ExcludedBatch(source.source_id, source.source_row_key[removed],
                             np.full(int(removed.sum()), "stationary"), source.lonlat[removed])
    return RefineIncrementalResult(excluded, spatial_context,
                                   {GeneratedReason.HEADING_ORTHOGONAL: injected_count}, subtracks)


def angle_delta_deg(start: float, end: float) -> float:
    return (end - start + 180.0) % 360.0 - 180.0


def heading_vector(heading_deg: float) -> tuple[float, float]:
    radians = math.radians(heading_deg)
    return math.sin(radians), math.cos(radians)


def ray_intersection(
    origin_a: tuple[float, float],
    direction_a: tuple[float, float],
    origin_b: tuple[float, float],
    direction_b: tuple[float, float],
) -> tuple[float, float, float, float] | None:
    m00 = direction_a[0]
    m01 = -direction_b[0]
    m10 = direction_a[1]
    m11 = -direction_b[1]
    rhs_x = origin_b[0] - origin_a[0]
    rhs_y = origin_b[1] - origin_a[1]
    det = m00 * m11 - m01 * m10
    if abs(det) < 1e-9:
        return None
    ta = (rhs_x * m11 - m01 * rhs_y) / det
    tb = (m00 * rhs_y - rhs_x * m10) / det
    x = origin_a[0] + ta * direction_a[0]
    y = origin_a[1] + ta * direction_a[1]
    return x, y, ta, tb


def refine(source, *, spatial_context: SpatialContext, config: RefineConfig) -> RefineResult:
    subtracks, exclusions = [], []
    injected = 0
    transformer = CoordinateTransformer(spatial_context)
    for batch in source.iter_vehicle_batches():
        result = refine_vehicle(batch, spatial_context=spatial_context, config=config,
                                _transformer=transformer)
        offset = len(subtracks)
        from dataclasses import replace
        subtracks.extend(replace(track, subtrack_index=offset + i + 1)
                         for i, track in enumerate(result.subtracks))
        exclusions.append(result.excluded)
        injected += sum(result.derived_counts.values())
    reasons = {}
    samples = []
    for excluded in exclusions:
        for reason, count in excluded.reason_counts().items():
            reasons[reason] = reasons.get(reason, 0) + count
        for i in range(min(len(excluded), max(0, config.excluded_sample_max - len(samples)))):
            samples.append((str(excluded.source_row_key[i]), str(excluded.reason[i])))
    return RefineResult(subtracks, exclusions,
                        RefineReport(sum(map(len, subtracks)), sum(map(len, exclusions)),
                                     injected, reasons, samples), spatial_context)
