"""Refine stage: split trajectories, exclude stationary points and derive points."""

from __future__ import annotations

from collections import defaultdict
import math
import time
from collections.abc import Iterator
from dataclasses import replace

from .models import (
    RefineReport,
    RefineConfig,
    RefineIncrementalResult,
    DerivedPoint,
    GeneratedReason,
    RefinementSubtrack,
    RefineResult,
    ExcludedIndex,
    ExcludedPoint,
    FieldMapping,
    PointRef,
    ReadReport,
    SourceInfo,
    SpatialContext,
    TrajectoryPoint,
    TrajectorySourceHandle,
)
from .projection import CoordinateTransformer


def refine_vehicle(
    vehicle_points: list[TrajectoryPoint],
    *,
    spatial_context: SpatialContext,
    config: RefineConfig | None = None,
    _transformer: CoordinateTransformer | None = None,
) -> RefineIncrementalResult:
    """Refine one vehicle's trajectory points.

    ZH: 处理单辆车的轨迹点：按时间间隙切分子轨迹、去除静止段、注入正交候选点。
        返回该车辆的全部精炼结果，不累积全局状态。
    """
    cfg = config or RefineConfig()
    kept_points: list[TrajectoryPoint] = []
    excluded_points: list[ExcludedPoint] = []
    derived_points: list[DerivedPoint] = []
    derived_counts: dict[GeneratedReason, int] = defaultdict(int)
    refinement_subtracks: list[RefinementSubtrack] = []
    transformer = _transformer or CoordinateTransformer(spatial_context)

    subtracks = _split_by_gaps(vehicle_points, cfg.max_gap_s)
    rename = len(subtracks) > 1
    for index, subtrack in enumerate(subtracks, start=1):
        subtrack_kept, subtrack_excluded = _exclude_stationary(subtrack, cfg)
        kept_xy = transformer.forward_many(
            (point.lon, point.lat) for point in subtrack_kept
        )
        subtrack_kept = [
            _projected_point(
                point, x, y,
                f"{point.vehicle_id}_seg{index:02d}" if rename else point.vehicle_id,
            )
            for point, (x, y) in zip(subtrack_kept, kept_xy)
        ]
        kept_points.extend(subtrack_kept)
        excluded_points.extend(subtrack_excluded)
        injected_subtrack, count, injected_points = _inject_orthogonal(
            subtrack_kept,
            cfg,
            spatial_context=spatial_context,
            _transformer=transformer,
        )
        derived_points.extend(injected_points)
        derived_counts[GeneratedReason.HEADING_ORTHOGONAL] += count

        if subtrack_kept:
            refinement_subtracks.append(
                RefinementSubtrack(
                    vehicle_id=subtrack_kept[0].vehicle_id,
                    subtrack_index=index,
                    points=injected_subtrack,
                )
            )

    return RefineIncrementalResult(
        kept=kept_points,
        excluded=excluded_points,
        derived=derived_points,
        spatial_context=spatial_context,
        derived_counts=derived_counts,
        subtracks=refinement_subtracks,
    )


def _projected_point(
    point: TrajectoryPoint, x: float, y: float, vehicle_id: str,
) -> TrajectoryPoint:
    """Copy the parsed point once, reusing its immutable source reference."""
    if type(point) is not TrajectoryPoint:
        # Retain dataclass subclass fields and construction behavior.
        return replace(point, vehicle_id=vehicle_id, x=x, y=y)
    return TrajectoryPoint(
        point.point_ref, vehicle_id, point.timestamp, point.lon, point.lat,
        point.heading, point.speed, point.extra, x, y, point.refined_point_ref,
    )


def refine(
    source: TrajectorySourceHandle,
    *,
    spatial_context: SpatialContext,
    config: RefineConfig | None = None,
) -> RefineResult:
    """Refine all trajectories from a source handle.

    ZH: 对数据源中的全部车辆轨迹执行精炼。内部调用 ``refine_vehicle()`` 逐车处理，
        累积全局统计和输出。
    """
    cfg = config or RefineConfig()
    active: list[TrajectoryPoint] = []
    excluded: list[ExcludedPoint] = []
    kept_subtracks: list[list[TrajectoryPoint]] = []
    injected_count = 0

    t0 = time.perf_counter()
    last_t = t0
    group_count = 0
    print("  ── 精炼 / Refining ...")
    transformer = CoordinateTransformer(spatial_context)

    for vehicle_points in _group_by_vehicle(source.iter_points()):
        result = refine_vehicle(
            vehicle_points,
            config=cfg,
            spatial_context=spatial_context,
            _transformer=transformer,
        )
        for subtrack in result.subtracks:
            active.extend(subtrack.points)
        excluded.extend(result.excluded)
        kept_subtracks.extend([list(subtrack.points) for subtrack in result.subtracks])
        injected_count += sum(result.derived_counts.values())

        group_count += 1
        now = time.perf_counter()
        if group_count % 50 == 0 or (now - last_t) >= 3.0:
            print(f"    ... 已处理 / Processed {group_count} 车辆组/groups ({now - t0:.1f}s)")
            last_t = now

    print(f"  ── 精炼 / Refining: {len(active):,} 点/points ({time.perf_counter() - t0:.1f}s)")

    excluded_index = ExcludedIndex(excluded)
    report = RefineReport(
        active_count=len(active),
        excluded_count=len(excluded),
        injected_count=injected_count,
        excluded_reasons=excluded_index.reason_counts(),
        sample_excluded=[(item.point_ref, item.reason) for item in excluded[:cfg.excluded_sample_max]],
    )
    return RefineResult(
        active=ListTrajectorySourceHandle(
            active,
            source_info=source.source_info,
            field_mapping=source.field_mapping,
            spatial_context=spatial_context,
        ),
        excluded=excluded_index,
        report=report,
        spatial_context=spatial_context,
        kept_subtracks=kept_subtracks,
    )


class ListTrajectorySourceHandle:
    def __init__(
        self,
        points: list[TrajectoryPoint],
        *,
        source_info: SourceInfo,
        field_mapping: FieldMapping,
        spatial_context: SpatialContext | None = None,
    ) -> None:
        self._points = list(points)
        self._source_info = source_info
        self._field_mapping = field_mapping
        self._spatial_context = spatial_context
        self._closed = False
        self._report = ReadReport(
            source_info=source_info,
            total_rows=len(points),
            loaded_rows=len(points),
            skipped_rows=0,
            warn_count=0,
            error_count=0,
            fatal_count=0,
            sample_errors=[],
            sample_errors_max=0,
        )

    @property
    def source_info(self) -> SourceInfo:
        return self._source_info

    @property
    def field_mapping(self) -> FieldMapping:
        return self._field_mapping

    @property
    def spatial_context(self) -> SpatialContext | None:
        return self._spatial_context

    @property
    def report(self) -> ReadReport:
        return self._report

    @property
    def read_report(self) -> ReadReport:
        return self._report

    def get_report(self) -> ReadReport:
        return self._report

    def iter_points(self) -> Iterator[TrajectoryPoint]:
        if self._closed:
            raise RuntimeError("trajectory source handle is closed")
        yield from self._points

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> "ListTrajectorySourceHandle":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _group_by_vehicle(points: Iterator[TrajectoryPoint]) -> Iterator[list[TrajectoryPoint]]:
    current_vehicle: str | None = None
    current: list[TrajectoryPoint] = []
    for point in points:
        if current_vehicle is None:
            current_vehicle = point.vehicle_id
        if point.vehicle_id != current_vehicle:
            yield current
            current = []
            current_vehicle = point.vehicle_id
        current.append(point)
    if current:
        yield current


def _split_by_gaps(points: list[TrajectoryPoint], max_gap_s: float) -> list[list[TrajectoryPoint]]:
    if not points:
        return []
    subtracks: list[list[TrajectoryPoint]] = [[points[0]]]
    for previous, current in zip(points, points[1:]):
        if current.timestamp - previous.timestamp > max_gap_s:
            subtracks.append([])
        subtracks[-1].append(current)
    return subtracks


def _exclude_stationary(
    points: list[TrajectoryPoint],
    cfg: RefineConfig,
) -> tuple[list[TrajectoryPoint], list[ExcludedPoint]]:
    if len(points) < 3:
        return list(points), []

    excluded_refs: set[PointRef] = set()
    index = 0
    while index < len(points):
        if points[index].speed >= cfg.min_speed_mps:
            index += 1
            continue
        start = index
        while index + 1 < len(points) and points[index + 1].speed < cfg.min_speed_mps:
            index += 1
        stop = index
        if stop > start and points[stop].timestamp - points[start].timestamp > cfg.max_stationary_duration_s:
            for point in points[start + 1 : stop]:
                excluded_refs.add(point.point_ref)
        index += 1

    if not excluded_refs:
        return list(points), []

    kept: list[TrajectoryPoint] = []
    removed: list[ExcludedPoint] = []
    for point in points:
        if point.point_ref in excluded_refs:
            removed.append(ExcludedPoint(point.point_ref, "stationary", lon=point.lon, lat=point.lat))
        else:
            kept.append(point)
    return kept, removed


def _inject_orthogonal(
    points: list[TrajectoryPoint],
    cfg: RefineConfig,
    *,
    spatial_context: SpatialContext,
    _transformer: CoordinateTransformer | None = None,
) -> tuple[list[TrajectoryPoint], int, list[DerivedPoint]]:
    """return **injected subtrack** and **count** of injected points."""
    if len(points) < 2:
        return list(points), 0, []

    injected_subtrack: list[TrajectoryPoint] = []
    injected_count = 0
    injected_points: list[DerivedPoint] = []
    transformer = _transformer or CoordinateTransformer(spatial_context)

    for a, b in zip(points, points[1:]):
        injected_subtrack.append(a)
        injected_point = _orthogonal_candidate(
            a,
            b,
            cfg,
            spatial_context=spatial_context,
            _transformer=transformer,
        )
        if injected_point is not None:
            injected_subtrack.append(injected_point)
            injected_count += 1
            injected_points.append(injected_point)
    injected_subtrack.append(points[-1])
    return injected_subtrack, injected_count, injected_points


def _orthogonal_candidate(
    a: TrajectoryPoint,
    b: TrajectoryPoint,
    cfg: RefineConfig,
    *,
    spatial_context: SpatialContext,
    _transformer: CoordinateTransformer | None = None,
) -> DerivedPoint | None:
    if a.vehicle_id != b.vehicle_id:
        return None
    delta = angle_delta_deg(a.heading, b.heading)
    if not cfg.min_heading_delta_deg <= abs(delta) <= cfg.max_heading_delta_deg:
        return None

    transformer = _transformer or CoordinateTransformer(spatial_context)
    a_xy, b_xy = transformer.forward_many(((a.lon, a.lat), (b.lon, b.lat)))
    ray_a = transformer.heading_vector(
        a.lon,
        a.lat,
        (a.heading + 180.0) % 360.0,
    )
    ray_b = transformer.heading_vector(
        b.lon,
        b.lat,
        (b.heading + 180.0) % 360.0,
    )
    hit = ray_intersection(a_xy, ray_a, b_xy, ray_b)
    if hit is None:
        return None
    x, y, distance_a, distance_b = hit
    if distance_a < 0.0 or distance_b < 0.0:
        return None
    if distance_a > cfg.max_extension_m or distance_b > cfg.max_extension_m:
        return None

    lon, lat = transformer.inverse(x, y)
    return DerivedPoint(
        point_ref=PointRef(
            source_id=a.point_ref.source_id,
            source_row_key=f"{a.point_ref.source_row_key}->{b.point_ref.source_row_key}:orthogonal_heading",
            stable=False,
            from_ref=a.point_ref.source_row_key,
            to_ref=b.point_ref.source_row_key,
            generated_reason=GeneratedReason.HEADING_ORTHOGONAL,
        ),
        vehicle_id=a.vehicle_id,
        timestamp=(a.timestamp + b.timestamp) / 2.0,
        lon=lon,
        lat=lat,
        heading=(a.heading + delta / 2.0) % 360.0,
        speed=0.0,
        extra={},
        x=x,
        y=y,
    )


def _with_vehicle_id(point: TrajectoryPoint, vehicle_id: str) -> TrajectoryPoint:
    return replace(point, vehicle_id=vehicle_id)


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
