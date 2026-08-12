"""Fitting stage — aggregate inter-intersection clips into centerlines."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ._geometry import _to_lonlat, _to_xy
from .models import (
    Centerline,
    ClipResult,
    FitIssue,
    FitReport,
    FitResult,
    FittingConfig,
    TrackClip,
)


_EPS = 1e-9
_BOUNDARY_CLIP_TYPES = {"boundary_start", "boundary_end", "full_track"}


@dataclass(frozen=True, slots=True)
class _OrientedClip:
    value: TrackClip
    points_lonlat: tuple[tuple[float, float], ...]
    was_reversed: bool


def fit_centerlines(
    clips: ClipResult,
    *,
    config: FittingConfig | None = None,
) -> FitResult:
    """Fit one centerline for every connected intersection pair.

    The lower cluster id defines a deterministic direction. Reverse clips are
    flipped, sampled at common normalized arc-length positions, combined with
    a coordinate-wise median, then resampled at ``point_spacing_m``.
    """
    cfg = config or FittingConfig()
    _validate_config(cfg)
    grouped: dict[tuple[int, int], list[_OrientedClip]] = {}
    issues: list[FitIssue] = []
    eligible_count = 0

    for clip in clips.clips:
        if clip.clip_type in _BOUNDARY_CLIP_TYPES:
            continue
        if clip.clip_type != "inter_intersection":
            issues.append(FitIssue(None, None, "unsupported_clip_type", clip.clip_id))
            continue
        eligible_count += 1
        start_id = clip.start.cluster_id
        end_id = clip.end.cluster_id
        pair = None
        if start_id is not None and end_id is not None and start_id != end_id:
            pair = (min(start_id, end_id), max(start_id, end_id))
            grouped.setdefault(pair, [])
        oriented, issue = _orient_clip(clip)
        if issue is not None:
            issues.append(issue)
            continue
        assert oriented is not None
        assert pair is not None
        grouped.setdefault(pair, []).append(oriented)

    centerlines: list[Centerline] = []
    skipped_pairs = 0
    for pair in sorted(grouped):
        pair_clips = sorted(grouped[pair], key=lambda item: item.value.clip_id)
        if len(pair_clips) < cfg.min_clips_per_pair:
            skipped_pairs += 1
            issues.append(FitIssue(pair[0], pair[1], "insufficient_clips"))
            continue
        centerline = _fit_pair(pair, pair_clips, cfg)
        if centerline is None:
            skipped_pairs += 1
            issues.append(FitIssue(pair[0], pair[1], "degenerate_centerline"))
            continue
        centerlines.append(centerline)

    return FitResult(
        centerlines=tuple(centerlines),
        issues=tuple(issues),
        report=FitReport(
            input_clip_count=len(clips.clips),
            eligible_clip_count=eligible_count,
            connected_pair_count=len(grouped),
            fitted_centerline_count=len(centerlines),
            skipped_clip_count=sum(issue.clip_id is not None for issue in issues),
            skipped_pair_count=skipped_pairs,
        ),
        run_id=clips.run_id,
    )


def _validate_config(config: FittingConfig) -> None:
    if not math.isfinite(config.point_spacing_m) or config.point_spacing_m <= 0.0:
        raise ValueError("FittingConfig.point_spacing_m must be positive")
    if not math.isfinite(config.fit_sample_spacing_m) or config.fit_sample_spacing_m <= 0.0:
        raise ValueError("FittingConfig.fit_sample_spacing_m must be positive")
    if config.min_clips_per_pair < 1:
        raise ValueError("FittingConfig.min_clips_per_pair must be at least 1")


def _orient_clip(clip: TrackClip) -> tuple[_OrientedClip | None, FitIssue | None]:
    start_id = clip.start.cluster_id
    end_id = clip.end.cluster_id
    if start_id is None or end_id is None:
        return None, FitIssue(start_id, end_id, "missing_intersection_endpoint", clip.clip_id)
    if start_id == end_id:
        return None, FitIssue(start_id, end_id, "same_intersection_endpoint", clip.clip_id)

    points = [(clip.start.lon, clip.start.lat)]
    points.extend((point.lon, point.lat) for point in clip.points)
    points.append((clip.end.lon, clip.end.lat))
    if any(not (math.isfinite(lon) and math.isfinite(lat)) for lon, lat in points):
        return None, FitIssue(start_id, end_id, "invalid_geometry", clip.clip_id)

    was_reversed = start_id > end_id
    if was_reversed:
        points.reverse()
    if _lonlat_polyline_length_m(points) <= _EPS:
        return None, FitIssue(min(start_id, end_id), max(start_id, end_id), "zero_length_geometry", clip.clip_id)
    return _OrientedClip(clip, tuple(points), was_reversed), None


def _fit_pair(
    pair: tuple[int, int],
    clips: list[_OrientedClip],
    config: FittingConfig,
) -> Centerline | None:
    all_points = [point for clip in clips for point in clip.points_lonlat]
    origin_lon = float(np.median([point[0] for point in all_points]))
    origin_lat = float(np.median([point[1] for point in all_points]))
    tracks_xy = [
        np.asarray([_to_xy(lon, lat, origin_lon, origin_lat) for lon, lat in clip.points_lonlat])
        for clip in clips
    ]
    lengths = [_xy_polyline_length(track) for track in tracks_xy]
    median_length = float(np.median(lengths))
    if median_length <= _EPS:
        return None

    sample_count = max(2, int(math.ceil(median_length / config.fit_sample_spacing_m)) + 1)
    progress = np.linspace(0.0, 1.0, sample_count)
    sampled = np.stack([_resample_at_progress(track, progress) for track in tracks_xy])
    fitted_xy = np.median(sampled, axis=0)
    if _xy_polyline_length(fitted_xy) <= _EPS:
        return None
    spaced_xy = _resample_at_spacing(fitted_xy, config.point_spacing_m)
    if len(spaced_xy) < 2:
        return None
    points = tuple(
        _to_lonlat(float(x), float(y), origin_lon, origin_lat)
        for x, y in spaced_xy
    )
    return Centerline(
        from_cluster_id=pair[0],
        to_cluster_id=pair[1],
        points=points,
        length_m=_xy_polyline_length(spaced_xy),
        source_clip_ids=tuple(clip.value.clip_id for clip in clips),
        forward_clip_count=sum(not clip.was_reversed for clip in clips),
        reverse_clip_count=sum(clip.was_reversed for clip in clips),
    )


def _resample_at_progress(points: np.ndarray, progress: np.ndarray) -> np.ndarray:
    points, cumulative = _deduplicate_xy(points)
    normalized = cumulative / cumulative[-1]
    return np.column_stack(
        (
            np.interp(progress, normalized, points[:, 0]),
            np.interp(progress, normalized, points[:, 1]),
        )
    )


def _resample_at_spacing(points: np.ndarray, spacing_m: float) -> np.ndarray:
    points, cumulative = _deduplicate_xy(points)
    total = cumulative[-1]
    targets = list(np.arange(0.0, total, spacing_m))
    if not targets or total - targets[-1] > _EPS:
        targets.append(total)
    target_array = np.asarray(targets, dtype=float)
    return np.column_stack(
        (
            np.interp(target_array, cumulative, points[:, 0]),
            np.interp(target_array, cumulative, points[:, 1]),
        )
    )


def _deduplicate_xy(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    segment_lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    keep = np.concatenate(([True], segment_lengths > _EPS))
    unique = points[keep]
    if len(unique) < 2:
        raise ValueError("polyline requires at least two distinct points")
    cumulative = np.concatenate(
        ([0.0], np.cumsum(np.linalg.norm(np.diff(unique, axis=0), axis=1)))
    )
    return unique, cumulative


def _xy_polyline_length(points: np.ndarray) -> float:
    if len(points) < 2:
        return 0.0
    return float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())


def _lonlat_polyline_length_m(points: list[tuple[float, float]]) -> float:
    origin_lon, origin_lat = points[0]
    xy = np.asarray([_to_xy(lon, lat, origin_lon, origin_lat) for lon, lat in points])
    return _xy_polyline_length(xy)
