"""Fitting stage — aggregate between-intersection clips into centerlines."""

from __future__ import annotations

import math
import sys
from collections.abc import Callable, Iterator

from ..clips.clip_input import ClipRelation, ClipFittingMetadata
from ..telemetry.heartbeat import observed_operation, heartbeat_progress, operation_heartbeat
from dataclasses import dataclass, replace

import numpy as np

from ..models import (
    Centerline,
    CenterlineFitBundle,
    ClipResult,
    FitIssue,
    FitReport,
    FitResult,
    FittingConfig,
    TrackClip,
)
from ..projection import CoordinateTransformer


_EPS = 1e-9
_BOUNDARY_CLIP_TYPES = {"boundary_start", "boundary_end", "full_track"}
_FIT_BLOCK_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _OrientedClip:
    value: TrackClip
    points_xy: np.ndarray
    was_reversed: bool


@observed_operation("Fitting：中心线拟合")
def fit_centerlines(
    clips: ClipResult,
    *,
    config: FittingConfig,
) -> FitResult:
    """Fit one centerline for every connected intersection pair.

    The lower intersection id defines a deterministic direction. Reverse clips
    are flipped, sampled at common normalized arc-length positions, combined
    with a coordinate-wise median, then resampled at ``point_spacing_m``.
    """
    return _fit_centerlines(clips, config=config, reverse=None)


def _fit_centerlines(
    clips: ClipResult, *, config: FittingConfig, reverse: bool | None,
) -> FitResult:
    return _fit_variants(clips, config=config, variants=(reverse,))[reverse]


def fit_centerline_variants(
    clips: ClipResult,
    *,
    config: FittingConfig,
) -> CenterlineFitBundle:
    """Fit the combined physical line and both observed travel directions."""
    with operation_heartbeat("Fitting：中心线拟合"):
        results = _fit_variants(clips, config=config, variants=(None, False, True))
    return CenterlineFitBundle(
        combined=results[None], a_to_b=_directed_result(results[False], False),
        b_to_a=_directed_result(results[True], True),
    )


def _fit_direction(
    clips: ClipResult, *, reverse: bool, config: FittingConfig,
) -> FitResult:
    return _directed_result(_fit_centerlines(clips, config=config, reverse=reverse), reverse)


def _directed_result(fitted: FitResult, reverse: bool) -> FitResult:
    centerlines = tuple(
        replace(
            line,
            from_intersection_id=line.to_intersection_id if reverse else line.from_intersection_id,
            to_intersection_id=line.from_intersection_id if reverse else line.to_intersection_id,
            points=line.points[::-1] if reverse else line.points,
            forward_clip_count=line.reverse_clip_count if reverse else line.forward_clip_count,
            reverse_clip_count=line.forward_clip_count if reverse else line.reverse_clip_count,
            variant="directed",
        )
        for line in fitted.centerlines
    )
    return replace(fitted, centerlines=centerlines)


@dataclass(frozen=True, slots=True)
class _ClipStatistics:
    clip_id: str
    length: float
    was_reversed: bool


@dataclass(frozen=True, slots=True)
class _CachedGeometry:
    points_xy: np.ndarray
    normalized: np.ndarray
    was_reversed: bool



def _fit_variants(
    clips: ClipResult, *, config: FittingConfig, variants: tuple[bool | None, ...],
) -> dict[bool | None, FitResult]:
    _validate_config(config)
    transformer = CoordinateTransformer(clips.spatial_context)
    relation = clips.clips if isinstance(clips.clips, ClipRelation) else None
    # Sequence inputs already own their point arrays: retain only references.
    fallback: dict[tuple[int, int], list[tuple[int, TrackClip]]] = {}
    grouped: dict[tuple[int, int], set[bool]] = {}
    input_counts = dict.fromkeys(variants, 0)
    eligible_counts = dict.fromkeys(variants, 0)
    clip_issues: dict[bool | None, list[tuple[int, FitIssue]]] = {v: [] for v in variants}
    pair_issues: dict[bool | None, list[FitIssue]] = {v: [] for v in variants}
    lines: dict[bool | None, list[Centerline]] = {v: [] for v in variants}

    def metadata() -> Iterator[ClipFittingMetadata]:
        if relation is not None:
            yield from relation.iter_fitting_metadata()
        else:
            for ordinal, clip in enumerate(clips.clips):
                yield ClipFittingMetadata(
                    ordinal, clip.clip_id, clip.clip_type, clip.start_intersection_id,
                    clip.end_intersection_id, len(clip.xy),
                )

    with operation_heartbeat("Fitting：准备片段"):
        heartbeat_progress(0, total=len(clips.clips), unit="片段")
        for index, item in enumerate(metadata(), 1):
            try:
                start, end = item.start_intersection_id, item.end_intersection_id
                directed = (item.clip_type == "between_intersection" and start is not None
                            and end is not None and start != end)
                selected = [v for v in variants if v is None or (directed and (start > end) == v)]
                for v in selected:
                    input_counts[v] += 1
                if item.clip_type in _BOUNDARY_CLIP_TYPES:
                    continue
                issue = None
                if item.clip_type != "between_intersection":
                    issue = FitIssue(None, None, "unsupported_clip_type", item.clip_id)
                else:
                    for v in selected:
                        eligible_counts[v] += 1
                    if start is None or end is None:
                        issue = FitIssue(start, end, "missing_intersection_endpoint", item.clip_id)
                    elif start == end:
                        issue = FitIssue(start, end, "same_intersection_endpoint", item.clip_id)
                    else:
                        pair = (min(start, end), max(start, end))
                        grouped.setdefault(pair, set()).add(start > end)
                        if item.point_count == 0:
                            issue = FitIssue(pair[0], pair[1], "zero_length_geometry", item.clip_id)
                        elif relation is None:
                            fallback.setdefault(pair, []).append((item.ordinal, clips.clips[item.ordinal]))
                if issue is not None:
                    for v in selected:
                        clip_issues[v].append((item.ordinal, issue))
            finally:
                heartbeat_progress(index, total=len(clips.clips), unit="片段")
    for values in fallback.values():
        values.sort(key=lambda item: (item[1].clip_id, item[0]))

    def read_pair(pair: tuple[int, int], reverse: bool | None = None) -> Iterator[tuple[int, TrackClip]]:
        if relation is not None:
            yield from relation.iter_fitting_pair(pair, reverse=reverse)
        else:
            for ordinal, clip in fallback.get(pair, ()):
                if reverse is None or _matches_direction(clip, reverse):
                    yield ordinal, clip

    with operation_heartbeat("Fitting：按交叉口对拟合"):
        heartbeat_progress(0, total=len(grouped), unit="交叉口对")
        for index, pair in enumerate(sorted(grouped), 1):
            try:
                statistics, cache = _prepare_pair(read_pair(pair), transformer, variants, clip_issues)
                if 16 * len(statistics) > _FIT_BLOCK_BYTES:
                    # Preserve the existing at-least-one-column behavior without cache.
                    cache = None
                for variant in variants:
                    if variant is not None and variant not in grouped[pair]:
                        continue
                    selected_stats = [s for s in statistics if variant is None or s.was_reversed == variant]
                    if len(selected_stats) < config.min_clips_per_pair:
                        pair_issues[variant].append(FitIssue(*pair, "insufficient_clips"))
                        continue

                    def read_oriented(variant=variant) -> Iterator[_OrientedClip]:
                        for _, clip in read_pair(pair, variant):
                            oriented, issue = _orient_clip(clip, transformer)
                            if issue is None:
                                assert oriented is not None
                                yield oriented

                    line = _fit_pair_stream(
                        pair, read_oriented, len(selected_stats), config, transformer,
                        statistics=selected_stats, cache=cache, reverse=variant,
                    )
                    if line is None:
                        pair_issues[variant].append(FitIssue(*pair, "degenerate_centerline"))
                    else:
                        lines[variant].append(line)
            finally:
                cache = None
                heartbeat_progress(index, total=len(grouped), unit="交叉口对")
    results = {}
    for variant in variants:
        issues = tuple(issue for _, issue in sorted(clip_issues[variant], key=lambda item: item[0])) + tuple(pair_issues[variant])
        results[variant] = FitResult(
            centerlines=tuple(lines[variant]), issues=issues,
            report=FitReport(
                input_clip_count=input_counts[variant], eligible_clip_count=eligible_counts[variant],
                connected_pair_count=sum(variant is None or variant in directions for directions in grouped.values()),
                fitted_centerline_count=len(lines[variant]),
                skipped_clip_count=sum(issue.clip_id is not None for issue in issues),
                skipped_pair_count=len(pair_issues[variant]),
            ), spatial_context=clips.spatial_context, detect_id=clips.detect_id,
        )
    return results


def _prepare_pair(
    clips: Iterator[tuple[int, TrackClip]], transformer: CoordinateTransformer,
    variants: tuple[bool | None, ...],
    issues: dict[bool | None, list[tuple[int, FitIssue]]],
) -> tuple[list[_ClipStatistics], list[_CachedGeometry] | None]:
    try:
        statistics = []
        cache: list[_CachedGeometry] | None = []
        retained_bytes = 0
        for ordinal, clip in clips:
            oriented, issue = _orient_clip(clip, transformer)
            if issue is not None:
                for variant in variants:
                    if variant is None or _matches_direction(clip, variant):
                        issues[variant].append((ordinal, issue))
                continue
            assert oriented is not None
            statistics.append(_ClipStatistics(
                clip.clip_id, _xy_polyline_length(oriented.points_xy), oriented.was_reversed,
            ))
            if cache is not None:
                try:
                    points, normalized = _prepare_geometry(oriented.points_xy)
                except ValueError:
                    # Preparation must not fail for a pair below the fitting threshold.
                    # If it is fitted, the original sampler will raise at the same point.
                    cache = None
                    continue
                geometry = _CachedGeometry(points, normalized, oriented.was_reversed)
                cache.append(geometry)
                retained_bytes += _geometry_entry_bytes(geometry)
                if sys.getsizeof(cache) + retained_bytes > _FIT_BLOCK_BYTES // 2:
                    cache = None
                del geometry, points, normalized
        return statistics, cache
    finally:
        # Tracebacks can outlive the operation; drop owned geometry references.
        cache = None
        oriented = clip = points = normalized = geometry = None
        close = getattr(clips, "close", None)
        if close is not None:
            close()


def _prepare_geometry(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    try:
        points, cumulative = _deduplicate_xy(points)
        return points, cumulative / cumulative[-1]
    finally:
        points = cumulative = None


def _geometry_cache_bytes(cache: list[_CachedGeometry]) -> int:
    # Both arrays own their allocation. Include list capacity and slot records,
    # as well as ndarray headers and the actual retained array buffers.
    return sys.getsizeof(cache) + sum(_geometry_entry_bytes(item) for item in cache)


def _geometry_entry_bytes(item: _CachedGeometry) -> int:
    return sys.getsizeof(item) + sys.getsizeof(item.points_xy) + sys.getsizeof(item.normalized)


def _matches_direction(clip: TrackClip, reverse: bool) -> bool:
    start_id, end_id = clip.start_intersection_id, clip.end_intersection_id
    return (
        clip.clip_type == "between_intersection" and start_id is not None
        and end_id is not None and start_id != end_id and ((start_id > end_id) == reverse)
    )


def _validate_config(config: FittingConfig) -> None:
    if not math.isfinite(config.point_spacing_m) or config.point_spacing_m <= 0.0:
        raise ValueError("FittingConfig.point_spacing_m must be positive")
    if not math.isfinite(config.fit_sample_spacing_m) or config.fit_sample_spacing_m <= 0.0:
        raise ValueError("FittingConfig.fit_sample_spacing_m must be positive")
    if config.min_clips_per_pair < 1:
        raise ValueError("FittingConfig.min_clips_per_pair must be at least 1")


def _orient_clip(
    clip: TrackClip,
    transformer: CoordinateTransformer,
) -> tuple[_OrientedClip | None, FitIssue | None]:
    start_id = clip.start_intersection_id
    end_id = clip.end_intersection_id
    if start_id is None or end_id is None:
        return None, FitIssue(start_id, end_id, "missing_intersection_endpoint", clip.clip_id)
    if start_id == end_id:
        return None, FitIssue(start_id, end_id, "same_intersection_endpoint", clip.clip_id)

    if not np.isfinite(clip.xy).all() or not np.isfinite(clip.lonlat).all():
        return None, FitIssue(start_id, end_id, "invalid_geometry", clip.clip_id)
    was_reversed = start_id > end_id
    points_xy = clip.xy[::-1] if was_reversed else clip.xy
    if _xy_polyline_length(points_xy) <= _EPS:
        return None, FitIssue(min(start_id, end_id), max(start_id, end_id), "zero_length_geometry", clip.clip_id)
    return _OrientedClip(clip, points_xy, was_reversed), None


def _fit_pair_stream(
    pair: tuple[int, int],
    read_clips: Callable[[], Iterator[_OrientedClip]],
    count: int,
    config: FittingConfig,
    transformer: CoordinateTransformer,
    *, statistics: list[_ClipStatistics] | None = None,
    cache: list[_CachedGeometry] | None = None, reverse: bool | None = None,
) -> Centerline | None:
    try:
        if count < 1:
            raise ValueError("clip count must be positive")
        if statistics is None:
            statistics = []
            for actual, clip in enumerate(read_clips(), 1):
                if actual > count:
                    raise ValueError("clip reader count changed")
                statistics.append(_ClipStatistics(
                    clip.value.clip_id, _xy_polyline_length(clip.points_xy), clip.was_reversed,
                ))
        if len(statistics) != count:
            raise ValueError("clip reader count changed")
        lengths = np.asarray([clip.length for clip in statistics], dtype=np.float64)
        source_ids = [clip.clip_id for clip in statistics]
        forward = sum(not clip.was_reversed for clip in statistics)
        median_length = float(np.median(lengths))
        if median_length <= _EPS:
            return None
        sample_count = max(2, int(math.ceil(median_length / config.fit_sample_spacing_m)) + 1)
        progress = np.linspace(0.0, 1.0, sample_count)
        fitted_xy = np.empty((sample_count, 2), dtype=np.float64)
        if 16 * count > _FIT_BLOCK_BYTES:
            cache = None
        cache_bytes = _geometry_cache_bytes(cache) if cache is not None else 0
        block = max(1, (_FIT_BLOCK_BYTES - cache_bytes) // max(16 * count, 1))
        for start in range(0, sample_count, block):
            end = min(start + block, sample_count)
            sampled = np.empty((count, end - start, 2), dtype=np.float64)
            actual = 0
            if cache is not None:
                for clip in cache:
                    if reverse is not None and clip.was_reversed != reverse:
                        continue
                    actual += 1
                    if actual > count:
                        raise ValueError("clip reader count changed")
                    sampled[actual - 1] = _interpolate_geometry(
                        clip.points_xy, clip.normalized, progress[start:end],
                    )
            else:
                for actual, clip in enumerate(read_clips(), 1):
                    if actual > count:
                        raise ValueError("clip reader count changed")
                    sampled[actual - 1] = _resample_at_progress(
                        np.asarray(clip.points_xy), progress[start:end],
                    )
            if actual != count:
                raise ValueError("clip reader count changed")
            # This block is no longer needed after aggregation. Partition it in
            # place instead of allocating a second full sampling block.
            fitted_xy[start:end] = np.median(sampled, axis=0, overwrite_input=True)
        if _xy_polyline_length(fitted_xy) <= _EPS:
            return None
        spaced_xy = _resample_at_spacing(fitted_xy, config.point_spacing_m)
        if len(spaced_xy) < 2:
            return None
        return Centerline(
            from_intersection_id=pair[0], to_intersection_id=pair[1],
            points=transformer.inverse_many(spaced_xy),
            length_m=_xy_polyline_length(spaced_xy), source_clip_ids=tuple(source_ids),
            forward_clip_count=forward, reverse_clip_count=count-forward,
        )
    finally:
        cache = None
        clip = None


def _resample_at_progress(points: np.ndarray, progress: np.ndarray) -> np.ndarray:
    try:
        points, normalized = _prepare_geometry(points)
        return _interpolate_geometry(points, normalized, progress)
    finally:
        points = normalized = None


def _interpolate_geometry(points: np.ndarray, normalized: np.ndarray, progress: np.ndarray) -> np.ndarray:
    try:
        return np.column_stack(
            (
                np.interp(progress, normalized, points[:, 0]),
                np.interp(progress, normalized, points[:, 1]),
            )
        )
    finally:
        points = normalized = None


def _resample_at_spacing(points: np.ndarray, spacing_m: float) -> np.ndarray:
    points, cumulative = _deduplicate_xy(points)
    total = cumulative[-1]
    target_array = np.arange(0.0, total, spacing_m)
    if not len(target_array) or total - target_array[-1] > _EPS:
        target_array = np.append(target_array, total)
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
