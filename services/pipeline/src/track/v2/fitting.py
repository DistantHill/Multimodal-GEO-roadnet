"""Fitting stage — aggregate between-intersection clips into centerlines."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator

from .clip_relation import ClipRelation
from .heartbeat import observed_operation, heartbeat_progress, operation_heartbeat
from dataclasses import dataclass, replace

import numpy as np

from .models import (
    Centerline,
    CenterlineFitBundle,
    ClipResult,
    FitIssue,
    FitReport,
    FitResult,
    FittingConfig,
    TrackClip,
)
from .projection import CoordinateTransformer


_EPS = 1e-9
_BOUNDARY_CLIP_TYPES = {"boundary_start", "boundary_end", "full_track"}
_FIT_BLOCK_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _OrientedClip:
    value: TrackClip
    points_xy: tuple[tuple[float, float], ...]
    was_reversed: bool


@observed_operation("Fitting：中心线拟合")
def fit_centerlines(
    clips: ClipResult,
    *,
    config: FittingConfig | None = None,
) -> FitResult:
    """Fit one centerline for every connected intersection pair.

    The lower intersection id defines a deterministic direction. Reverse clips
    are flipped, sampled at common normalized arc-length positions, combined
    with a coordinate-wise median, then resampled at ``point_spacing_m``.
    """
    return _fit_centerlines(clips, config=config, reverse=None)


def _fit_centerlines(
    clips: ClipResult, *, config: FittingConfig | None, reverse: bool | None,
) -> FitResult:
    cfg = config or FittingConfig()
    _validate_config(cfg)
    transformer = CoordinateTransformer(clips.spatial_context)
    grouped: dict[tuple[int, int], int] = {}
    fallback: dict[tuple[int, int], list[_OrientedClip]] = {}
    issues: list[FitIssue] = []
    eligible_count = 0
    input_count = 0
    prepare_name = (
        "Fitting：准备片段" if reverse is None
        else f"Fitting：筛选{'反向' if reverse else '正向'}片段"
    )
    with operation_heartbeat(prepare_name):
        heartbeat_progress(0, total=len(clips.clips), unit="片段")
        for clip_index, clip in enumerate(clips.clips, 1):
            try:
                if reverse is not None and not _matches_direction(clip, reverse):
                    continue
                input_count += 1
                if clip.clip_type in _BOUNDARY_CLIP_TYPES:
                    continue
                if clip.clip_type != "between_intersection":
                    issues.append(FitIssue(None, None, "unsupported_clip_type", clip.clip_id))
                    continue
                eligible_count += 1
                start_id, end_id = clip.start.intersection_id, clip.end.intersection_id
                pair = None
                if start_id is not None and end_id is not None and start_id != end_id:
                    pair = (min(start_id, end_id), max(start_id, end_id))
                    grouped.setdefault(pair, 0)
                oriented, issue = _orient_clip(clip, transformer)
                if issue is not None:
                    issues.append(issue)
                    continue
                assert oriented is not None and pair is not None
                grouped[pair] += 1
                if not isinstance(clips.clips, ClipRelation):
                    fallback.setdefault(pair, []).append(oriented)
            finally:
                heartbeat_progress(clip_index, total=len(clips.clips), unit="片段")
    for values in fallback.values():
        values.sort(key=lambda value: value.value.clip_id)

    def read_pair(pair: tuple[int, int]) -> Iterator[_OrientedClip]:
        if not isinstance(clips.clips, ClipRelation):
            return iter(fallback.get(pair, ()))
        def generate() -> Iterator[_OrientedClip]:
            for clip in clips.clips.iter_pair(pair, reverse=reverse):
                oriented, issue = _orient_clip(clip, transformer)
                if issue is None:
                    assert oriented is not None
                    yield oriented
        return generate()

    centerlines: list[Centerline] = []
    skipped_pairs = 0
    with operation_heartbeat("Fitting：按交叉口对拟合"):
        heartbeat_progress(0, total=len(grouped), unit="交叉口对")
        for pair_index, pair in enumerate(sorted(grouped), 1):
            try:
                if grouped[pair] < cfg.min_clips_per_pair:
                    skipped_pairs += 1
                    issues.append(FitIssue(pair[0], pair[1], "insufficient_clips"))
                    continue
                centerline = _fit_pair_stream(
                    pair, lambda pair=pair: read_pair(pair), grouped[pair], cfg, transformer,
                )
                if centerline is None:
                    skipped_pairs += 1
                    issues.append(FitIssue(pair[0], pair[1], "degenerate_centerline"))
                else:
                    centerlines.append(centerline)
            finally:
                heartbeat_progress(pair_index, total=len(grouped), unit="交叉口对")
    return FitResult(
        centerlines=tuple(centerlines), issues=tuple(issues),
        report=FitReport(
            input_clip_count=input_count, eligible_clip_count=eligible_count,
            connected_pair_count=len(grouped), fitted_centerline_count=len(centerlines),
            skipped_clip_count=sum(issue.clip_id is not None for issue in issues),
            skipped_pair_count=skipped_pairs,
        ), spatial_context=clips.spatial_context, detect_id=clips.detect_id,
    )


def fit_centerline_variants(
    clips: ClipResult,
    *,
    config: FittingConfig | None = None,
) -> CenterlineFitBundle:
    """Fit the combined physical line and both observed travel directions."""
    combined = fit_centerlines(clips, config=config)
    a_to_b = _fit_direction(clips, reverse=False, config=config)
    b_to_a = _fit_direction(clips, reverse=True, config=config)
    return CenterlineFitBundle(combined=combined, a_to_b=a_to_b, b_to_a=b_to_a)


def _fit_direction(
    clips: ClipResult,
    *,
    reverse: bool,
    config: FittingConfig | None,
) -> FitResult:
    fitted = _fit_centerlines(clips, config=config, reverse=reverse)
    centerlines = tuple(
        replace(
            centerline,
            from_intersection_id=(
                centerline.to_intersection_id
                if reverse
                else centerline.from_intersection_id
            ),
            to_intersection_id=(
                centerline.from_intersection_id
                if reverse
                else centerline.to_intersection_id
            ),
            points=(
                tuple(reversed(centerline.points)) if reverse else centerline.points
            ),
            # ``fit_centerlines`` reports counts against its canonical
            # low-id → high-id direction.  Once the directed reverse variant
            # flips that direction, forward and reverse evidence swap roles.
            forward_clip_count=(
                centerline.reverse_clip_count
                if reverse
                else centerline.forward_clip_count
            ),
            reverse_clip_count=(
                centerline.forward_clip_count
                if reverse
                else centerline.reverse_clip_count
            ),
            variant="directed",
        )
        for centerline in fitted.centerlines
    )
    return replace(fitted, centerlines=centerlines)


def _matches_direction(clip: TrackClip, reverse: bool) -> bool:
    start_id, end_id = clip.start.intersection_id, clip.end.intersection_id
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
    start_id = clip.start.intersection_id
    end_id = clip.end.intersection_id
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
    points_xy = tuple(transformer.forward_many(points))
    if _xy_polyline_length(np.asarray(points_xy)) <= _EPS:
        return None, FitIssue(min(start_id, end_id), max(start_id, end_id), "zero_length_geometry", clip.clip_id)
    return _OrientedClip(replace(clip, points=()), points_xy, was_reversed), None


def _fit_pair_stream(
    pair: tuple[int, int],
    read_clips: Callable[[], Iterator[_OrientedClip]],
    count: int,
    config: FittingConfig,
    transformer: CoordinateTransformer,
) -> Centerline | None:
    if count < 1:
        raise ValueError("clip count must be positive")
    lengths = np.empty(count, dtype=np.float64)
    source_ids: list[str] = []
    forward = 0
    actual = 0
    for actual, clip in enumerate(read_clips(), 1):
        if actual > count:
            raise ValueError("clip reader count changed")
        lengths[actual - 1] = _xy_polyline_length(np.asarray(clip.points_xy))
        source_ids.append(clip.value.clip_id)
        forward += not clip.was_reversed
    if actual != count:
        raise ValueError("clip reader count changed")
    median_length = float(np.median(lengths))
    if median_length <= _EPS:
        return None
    sample_count = max(2, int(math.ceil(median_length / config.fit_sample_spacing_m)) + 1)
    progress = np.linspace(0.0, 1.0, sample_count)
    fitted_xy = np.empty((sample_count, 2), dtype=np.float64)
    block = max(1, _FIT_BLOCK_BYTES // max(16 * count, 1))
    for start in range(0, sample_count, block):
        end = min(start + block, sample_count)
        sampled = np.empty((count, end - start, 2), dtype=np.float64)
        actual = 0
        for actual, clip in enumerate(read_clips(), 1):
            if actual > count:
                raise ValueError("clip reader count changed")
            sampled[actual - 1] = _resample_at_progress(
                np.asarray(clip.points_xy), progress[start:end],
            )
        if actual != count:
            raise ValueError("clip reader count changed")
        fitted_xy[start:end] = np.median(sampled, axis=0)
    if _xy_polyline_length(fitted_xy) <= _EPS:
        return None
    spaced_xy = _resample_at_spacing(fitted_xy, config.point_spacing_m)
    if len(spaced_xy) < 2:
        return None
    return Centerline(
        from_intersection_id=pair[0], to_intersection_id=pair[1],
        points=tuple(transformer.inverse(float(x), float(y)) for x, y in spaced_xy),
        length_m=_xy_polyline_length(spaced_xy), source_clip_ids=tuple(source_ids),
        forward_clip_count=forward, reverse_clip_count=count-forward,
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
