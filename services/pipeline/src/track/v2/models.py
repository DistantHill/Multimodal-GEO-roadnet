"""Unified base class for all v2 output models.

All output models inherit from TrackModel, which provides as_dict() and
format_print(). Parser 0 and Cleaner 1 models (TrajectoryPoint, PointRef,
FieldSpec, ReadReport, etc.) are defined here per the v2 PRD §3.
"""

from __future__ import annotations

from enum import StrEnum
import json
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Protocol, TextIO


ColumnSelector = Callable[[list[str]], dict[str, str]]


# ── TrackModel base ────────────────────────────────────────────────


class TrackModel:
    """Base class for all v2 output models.

    Every v2 output is a frozen dataclass inheriting from TrackModel.
    Subclasses get:
      - as_dict()       → recursive JSON-safe dict
      - format_print()  → pretty-print key: value pairs
    """

    def as_dict(self) -> dict[str, Any]:
        """Recursive, JSON-safe dictionary representation."""
        return _to_output_value(self)  # type: ignore[return-value]

    def format_print(
        self,
        *,
        title: str | None = None,
        file: TextIO | None = None,
    ) -> None:
        """Pretty-print as ``title:`` followed by ``  key: value`` lines."""
        out = file if file is not None else sys.stdout
        if title is not None:
            print(title, file=out)
        for key, value in self.as_dict().items():
            print(f"  {key}: {_format_scalar(value)}", file=out)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackModel:
        """Wrap a plain dict as a lightweight TrackModel for ad-hoc printing.

        Use when you have a one-off key-value mapping and want
        ``.format_print()`` without defining a full dataclass.
        """
        return _DictModel(data)


class _DictModel(TrackModel):
    """Internal: TrackModel wrapper around a plain dict."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def as_dict(self) -> dict[str, Any]:
        return _to_output_value(self._data)  # type: ignore[return-value]


# ── JSON-safe conversion helper ─────────────────────────────────────


def _json_key(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _to_output_value(value: object) -> object:
    """Recursively convert common Python types to JSON-safe values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_output_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {_json_key(k): _to_output_value(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)) and not isinstance(value, (bytes, bytearray, str)):
        return [_to_output_value(v) for v in value]
    # numpy array
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return _to_output_value(tolist())
    item = getattr(value, "item", None)
    if callable(item):
        return _to_output_value(item())
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return _to_output_value(as_dict())
    return str(value)


def _format_scalar(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


# ── Parser 0 + Cleaner 1 output models ──────────────────────────────


@dataclass(frozen=True, slots=True)
class PointRef(TrackModel):
    """Immutable reference to one point in the source data.

    PRD §3.2
    """

    source_id: str
    source_row_key: str
    stable: bool
    from_ref: str | None = None
    to_ref: str | None = None
    generated_reason: str = ""
    source_row_hash: str = ""


@dataclass(frozen=True, slots=True)
class TrajectoryPoint(TrackModel):
    """Normalized trajectory point emitted by Parser 0 iter_points().

    PRD §3.1
    """

    point_ref: PointRef
    vehicle_id: str
    timestamp: float
    lon: float
    lat: float
    heading: float
    speed: float
    extra: dict[str, Any] = field(default_factory=dict)
    x: float | None = None
    y: float | None = None


@dataclass(frozen=True, slots=True)
class DerivedPoint(TrajectoryPoint):
    """派生点：orthogonal_heading / inter_segment_orthogonal
    """

    point_ref: PointRef
    vehicle_id: str
    timestamp: float
    lon: float
    lat: float
    heading: float
    speed: float
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class FieldSpec(TrackModel):
    """One canonical field definition from TOML config.

    PRD §3.3
    """

    canonical: str
    aliases: tuple[str, ...] = ()
    required: bool = True
    type: str = "float"
    unit_scale: float = 1.0
    default: Any = None
    extra: bool = False


@dataclass(frozen=True)
class FieldMapping(TrackModel):
    """Resolved field mapping for one data source.

    PRD §3.3
    """

    specs: list[FieldSpec]
    canonical_to_source: dict[str, str]

    @cached_property
    def specs_by_canonical(self) -> dict[str, FieldSpec]:
        """ZH: 按 canonical 名索引的 spec 缓存——parse_row 热路径每行都需要，
            预构建避免每行重建 dict。
        EN: Canonical-name → FieldSpec cache for the parse_row hot path."""
        return {spec.canonical: spec for spec in self.specs}


@dataclass(frozen=True)
class FieldConfig(TrackModel):
    """Parser 0 field specs plus data-quality thresholds."""

    specs: list[FieldSpec]
    sample_errors_max: int = 50
    max_consecutive_errors: int = 50
    max_error_ratio: float = 1.0


@dataclass(frozen=True)
class SourceInfo(TrackModel):
    """Metadata about the opened data source.

    PRD §3.4
    """

    kind: str
    path_or_dsn: str
    encoding: str | None
    column_count: int
    columns: list[str]


@dataclass(frozen=True)
class ParseIssue(TrackModel):
    """One data-quality incident recorded during parsing.

    PRD §3.4
    """

    point_ref: PointRef | None
    row_number: int | None
    level: str
    reason: str
    raw_values: dict[str, str]


@dataclass(frozen=True)
class ReadReport(TrackModel):
    """Parser 0 statistics and error samples.

    PRD §3.4
    """

    source_info: SourceInfo | None  # None means parse failed before reading rows
    total_rows: int
    loaded_rows: int
    skipped_rows: int
    warn_count: int
    error_count: int
    fatal_count: int
    sample_errors: list[ParseIssue]
    sample_errors_max: int


@dataclass(frozen=True)
class CleanReport(TrackModel):
    """Cleaner 1 statistics and excluded-point samples.

    PRD §3.4
    """

    active_count: int
    excluded_count: int
    injected_count: int
    excluded_reasons: dict[str, int]
    sample_excluded: list[tuple[PointRef, str]]


@dataclass(frozen=True)
class ExcludedPoint(TrackModel):
    """One point removed by Cleaner 1."""

    point_ref: PointRef
    reason: str
    lon: float = 0.0
    lat: float = 0.0


@dataclass(frozen=True)
class ExcludedIndex(TrackModel):
    """Traceable index of points excluded from downstream algorithms."""

    items: list[ExcludedPoint] = field(default_factory=list)

    def by_reason(self, reason: str) -> list[ExcludedPoint]:
        return [item for item in self.items if item.reason == reason]

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[ExcludedPoint]:
        return iter(self.items)


@dataclass(frozen=True)
class CleanerConfig(TrackModel):
    """Cleaner 1 configuration."""

    max_extension_m: float = 80.0
    min_heading_delta_deg: float = 80.0
    max_heading_delta_deg: float = 100.0
    max_gap_s: float = 120.0
    min_speed_mps: float = 0.3
    max_stationary_duration_s: float = 300.0
    excluded_sample_max: int = 50


@dataclass(frozen=True, slots=True)
class ProjectionConfig(TrackModel):
    """Run-level source and processing CRS policy.

    ``processing_srid`` deliberately has no default. A projected CRS is a
    property of the data region, not a safe repository-wide constant.
    """

    source_srid: int = 4326
    processing_srid: int | None = None
    enforce_area_of_use: bool = True
    max_scale_error: float = 0.01


@dataclass(frozen=True, slots=True)
class SpatialContext(TrackModel):
    """Concrete coordinate context inherited by every stage of one Clean Run."""

    source_srid: int
    processing_srid: int
    projection_config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeConfig(TrackModel):
    """Cross-cutting runtime IO / DB / progress parameters.

    ZH: 跨阶段的运行时参数（DB 写入批大小、CSV 编码探测、进度节流等），
        不从任何单一阶段 dataclass 派生，统一由 pipeline TOML 配置驱动。
    EN: Cross-stage runtime tuning (DB insert batch, CSV encoding probes,
        progress throttling). Owned by the pipeline TOML, not any one stage.
    """

    db_insert_batch: int = 5000
    import_batch_size: int = 10_000
    csv_max_field_size: int = 10 * 1024 * 1024
    encoding_sample_size: int = 64 * 1024
    encoding_confidence: float = 0.5
    default_text_encoding: str = "utf-8-sig"
    fallback_encodings: tuple[str, ...] = ("gb18030", "gbk", "cp936", "big5")
    mark_flush_every_n: int = 2000
    excluded_flush_threshold: int = 2000
    cleaner_flush_point_threshold: int = 100_000
    progress_interval: int = 500
    progress_time_threshold_s: float = 2.0


class TrajectorySourceHandle(Protocol):
    @property
    def source_info(self) -> SourceInfo: ...

    @property
    def report(self) -> ReadReport: ...

    @property
    def read_report(self) -> ReadReport: ...

    @property
    def field_mapping(self) -> FieldMapping: ...

    def iter_points(self) -> Iterator[TrajectoryPoint]: ...

    def close(self) -> None: ...

    def __enter__(self) -> "TrajectorySourceHandle": ...

    def __exit__(self, *args: object) -> None: ...

class GeneratedReason(StrEnum):
    HEADING_ORTHOGONAL = "orthogonal_heading"
    INTER_SEGMENT_ORTHOGONAL = "inter_segment_orthogonal"

    
@dataclass(frozen=True)
class CleanerResult(TrackModel):
    active: TrajectorySourceHandle
    excluded: ExcludedIndex
    report: CleanReport
    spatial_context: SpatialContext
    kept_subtracks: list[list[TrajectoryPoint]] = field(default_factory=list)


@dataclass(frozen=True)
class RefinementSubtrack(TrackModel):
    """One Cleaner subtrack with kept and derived points in deterministic order."""

    vehicle_id: str
    subtrack_index: int
    points: list[TrajectoryPoint | DerivedPoint] = field(default_factory=list)

@dataclass(frozen=True)
class CleanerIncrementalResult(TrackModel):
    """Cleaner 1 result for one incremental run. """
    kept: list[TrajectoryPoint]
    excluded: list[ExcludedPoint]
    derived: list[DerivedPoint]
    spatial_context: SpatialContext
    derived_counts: dict[GeneratedReason, int] = field(default_factory=dict)
    subtracks: list[RefinementSubtrack] = field(default_factory=list) 


@dataclass(frozen=True)
class CsvSource(TrackModel):
    path: str | Path
    encoding: str | None = None


@dataclass(frozen=True)
class PostgresSource(TrackModel):
    config: Any
    query: str
    params: tuple[Any, ...] = ()
    connection_factory: Callable[[Any], Any] | None = None
    source_id: str | None = None
    primary_key_field: str = "point_id"


# ── Intersect2 output models ──────────────────────────────────────────
# PRD v2 Intersect2 §3, §7


@dataclass(frozen=True)
class TurnEvent(TrackModel):
    """Turn event detected at one trajectory point via bearing change.

    v2 differs from v1: no ``scenario`` field (not present on v2 TrajectoryPoint),
    and no mutable ``g_star`` (z-scores stored in a separate dict).
    """

    event_id: int
    vehicle_id: str
    timestamp: float
    lon: float
    lat: float
    x: float  # meter coordinate
    y: float  # meter coordinate
    in_bearing: float  # mean bearing before turn
    out_bearing: float  # mean bearing after turn
    turn_deg: float  # signed turn angle in [-180, 180)
    score: float  # = abs(turn_deg) for ranking
    source: str = "turn"
    point_ref: PointRef | None = None
    source_ids: tuple[str, ...] = ()
    from_source_id: str | None = None
    from_source_row_key: str | None = None
    to_source_id: str | None = None
    to_source_row_key: str | None = None


@dataclass(frozen=True)
class OrthogonalCandidate(TrackModel):
    """Intersection candidate from orthogonal heading injection.

    Cleaner 1 injects synthetic points where two consecutive headings are
    ~90° apart (``generated_reason="orthogonal_heading"``). Intersect2
    extracts these as high-confidence intersection candidates.
    """

    point_ref: PointRef  # derived PointRef with from_ref / to_ref
    vehicle_id: str
    timestamp: float
    lon: float
    lat: float
    x: float
    y: float


@dataclass(frozen=True)
class CrossTrackCandidate(TrackModel):
    """One orthogonal intersection between segments from two vehicles.

    ``from_*`` records the canonical vehicle A segment and ``to_*`` records
    vehicle B.  Each row key contains the segment's start/end source keys.
    """

    point_ref: PointRef
    vehicle_id: str
    timestamp: float
    lon: float
    lat: float
    x: float
    y: float
    heading: float
    speed: float
    from_source_id: str
    from_source_row_key: str
    to_source_id: str
    to_source_row_key: str


@dataclass(frozen=True)
class HotspotCluster(TrackModel):
    """A DBSCAN cluster of candidate points (turn events or orthogonal candidates).

    Uses ``tuple`` for events to preserve frozen-dataclass immutability.
    """

    cluster_id: int
    centroid_x: float
    centroid_y: float
    centroid_lon: float
    centroid_lat: float
    radius_m: float
    cluster_ref: str = ""        # stable ref → "cluster_{source}_{id}"
    events: tuple[TurnEvent, ...] = ()
    source: str = "turn"
    algorithm_sources: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_counts: dict[str, int] = field(default_factory=dict)
    source_ratios: dict[str, float] = field(default_factory=dict)
    source_point_refs: dict[str, tuple[PointRef, ...]] = field(default_factory=dict)
    critical_sources: tuple[str, ...] = ()
    # ── topology intermediate products ──
    crossing_points: tuple[()] = ()   # tuple[CrossingPoint, ...]; forward-ref via ()
    inout_points: tuple[()] = ()      # tuple[InOutIntersectionPoint, ...]

    @property
    def sources(self) -> tuple[str, ...]:
        """Backward-compatible alias for algorithm source labels.

        ``candidate_intersections.sources`` is now reserved for physical
        ``source_id`` values.  Algorithm provenance remains available through
        this compatibility property plus ``source_counts`` and
        ``critical_sources``.
        """
        return self.algorithm_sources


@dataclass(frozen=True)
class EntryExitObservation(TrackModel):
    """One vehicle's entry/exit through an intersection circle."""

    cluster_id: int
    vehicle_id: str
    entry_branch_angle: float
    exit_branch_angle: float
    inbound_bearing: float
    outbound_bearing: float
    turn_deg: float
    entry_crossing_id: int | None = None
    exit_crossing_id: int | None = None


@dataclass(frozen=True)
class CrossingPoint(TrackModel):
    """A segment-circle intersection point, preserved for replay.

    Produced by ``_extract_entry_exit_observations_batch`` via
    ``_segment_circle_crossings_batch``.
    """

    crossing_id: int                # globally unique
    cluster_ref: str                # FK → HotspotCluster.cluster_ref
    vehicle_id: str
    point_ref: PointRef             # segment-start TrajectoryPoint
    segment_index: int              # index in (flattened) vehicle track
    crossing_x: float               # processing-CRS metres
    crossing_y: float               # processing-CRS metres
    crossing_lon: float
    crossing_lat: float
    is_entry: bool                  # True=entering circle, False=leaving
    end_point_ref: PointRef | None = None
    produced_by: str = "segment_circle_crossings_batch"


@dataclass(frozen=True)
class InOutIntersectionPoint(TrackModel):
    """Clustered entry/exit direction for one intersection.

    Replaces the bare ``EntryExitObservation`` — retains references back
    to the ``CrossingPoint`` list so replay can recover the original
    trajectory segments.
    """

    point_id: int
    cluster_ref: str                # FK → HotspotCluster.cluster_ref
    branch_angle_deg: float
    is_entry: bool
    inbound_bearing: float
    outbound_bearing: float
    turn_deg: float
    crossing_refs: tuple[int, ...]  # → CrossingPoint.crossing_id
    vehicle_id: str
    produced_by: str = "_cluster_angles_greedy"


@dataclass(frozen=True)
class IntersectionTopology(TrackModel):
    """Extracted road topology for one intersection cluster."""

    cluster_id: int
    branch_centers_deg: list[float]
    transition_counts: dict[tuple[int, int], int]
    observation_count: int
    is_complete: bool
    reason: str
    clip_radius_m: float = 0.0


@dataclass(frozen=True)
class IntersectReport(TrackModel):
    """Intersect2 aggregated statistics."""

    track_count: int
    turn_event_count: int
    hotspot_event_count: int
    orthogonal_candidate_count: int
    cross_track_candidate_count: int
    cluster_count: int
    cluster_count_turn: int
    cluster_count_ortho: int
    cluster_count_cross_track: int
    complete_intersection_count: int
    complete_intersection_source_counts: dict[str, int] = field(default_factory=dict)
    complete_intersection_source_ratios: dict[str, float] = field(default_factory=dict)
    complete_intersection_critical_source_counts: dict[str, int] = field(default_factory=dict)
    complete_intersection_critical_source_ratios: dict[str, float] = field(default_factory=dict)


# ── Clip output models ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClipIntersection(TrackModel):
    """One verified intersection circle used for trajectory clipping."""

    cluster_id: int
    lon: float
    lat: float
    radius_m: float


@dataclass(frozen=True, slots=True)
class ClipTrack(TrackModel):
    """One time-ordered trip presented to the Clip module."""

    vehicle_id: str
    trip_id: str
    points: tuple[TrajectoryPoint, ...]
    subtrack_index: int = 1


@dataclass(frozen=True, slots=True)
class ClipInput(TrackModel):
    """Complete, storage-independent input for one Clip extraction."""

    intersections: tuple[ClipIntersection, ...]
    tracks: tuple[ClipTrack, ...]
    spatial_context: SpatialContext
    detect_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClipEndpoint(TrackModel):
    """An exact trip or interpolated intersection endpoint."""

    kind: str
    lon: float
    lat: float
    timestamp: float
    cluster_id: int | None
    segment_index: int
    segment_fraction: float


@dataclass(frozen=True, slots=True)
class TrackClip(TrackModel):
    """One trajectory fragment outside verified intersection circles.

    ``points`` contains only source points strictly inside the exact endpoint
    bounds. ``point_count`` includes both explicit endpoints.
    """

    clip_id: str
    vehicle_id: str
    trip_id: str
    clip_type: str
    start: ClipEndpoint
    end: ClipEndpoint
    points: tuple[TrajectoryPoint, ...]
    distance_m: float
    point_count: int
    subtrack_index: int = 1
    clip_ordinal_in_track: int = 0
    interior_start_index: int = 0
    interior_end_index_exclusive: int = 0


@dataclass(frozen=True, slots=True)
class ClipReport(TrackModel):
    """Aggregate counts; ``total_points_in_clips`` includes endpoints."""

    total_vehicles: int
    total_clips: int
    inter_intersection_clips: int
    boundary_clips: int
    total_points_in_clips: int
    unmatched_entry_count: int = 0
    unmatched_exit_count: int = 0
    skipped_degenerate_count: int = 0


@dataclass(frozen=True, slots=True)
class ClipResult(TrackModel):
    clips: tuple[TrackClip, ...]
    report: ClipReport
    spatial_context: SpatialContext
    detect_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClipConfig(TrackModel):
    include_boundary_clips: bool = True


# ── Fitting output models ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FittingConfig(TrackModel):
    """Configuration for fitting one centerline per connected pair."""

    point_spacing_m: float = 50.0
    fit_sample_spacing_m: float = 10.0
    min_clips_per_pair: int = 1


CenterlineVariant = Literal["combined", "directed"]


@dataclass(frozen=True, slots=True)
class Centerline(TrackModel):
    """One physical road centerline in canonical cluster-id direction."""

    from_cluster_id: int
    to_cluster_id: int
    points: tuple[tuple[float, float], ...]
    length_m: float
    source_clip_ids: tuple[str, ...]
    forward_clip_count: int
    reverse_clip_count: int
    variant: CenterlineVariant = "combined"

    @property
    def source_clip_count(self) -> int:
        return self.forward_clip_count + self.reverse_clip_count


@dataclass(frozen=True, slots=True)
class FitIssue(TrackModel):
    """A rejected clip or connected pair, retained for auditability."""

    from_cluster_id: int | None
    to_cluster_id: int | None
    reason: str
    clip_id: str | None = None


@dataclass(frozen=True, slots=True)
class FitReport(TrackModel):
    input_clip_count: int
    eligible_clip_count: int
    connected_pair_count: int
    fitted_centerline_count: int
    skipped_clip_count: int
    skipped_pair_count: int


@dataclass(frozen=True, slots=True)
class FitResult(TrackModel):
    centerlines: tuple[Centerline, ...]
    issues: tuple[FitIssue, ...]
    report: FitReport
    spatial_context: SpatialContext
    detect_id: str | None = None


@dataclass(frozen=True, slots=True)
class CenterlineFitBundle(TrackModel):
    """Fixed combined and observed-direction fitting results."""

    combined: FitResult
    a_to_b: FitResult
    b_to_a: FitResult


# ── Network output models ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NetworkConfig(TrackModel):
    include_isolated_nodes: bool = True


@dataclass(frozen=True, slots=True)
class NetworkNode(TrackModel):
    cluster_id: int
    lon: float
    lat: float
    radius_m: float
    degree: int


@dataclass(frozen=True, slots=True)
class NetworkEdge(TrackModel):
    edge_id: str
    from_cluster_id: int
    to_cluster_id: int
    points: tuple[tuple[float, float], ...]
    length_m: float
    source_clip_count: int
    forward_clip_count: int
    reverse_clip_count: int


@dataclass(frozen=True, slots=True)
class NetworkIssue(TrackModel):
    from_cluster_id: int
    to_cluster_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class NetworkReport(TrackModel):
    input_centerline_count: int
    node_count: int
    edge_count: int
    isolated_node_count: int
    skipped_centerline_count: int


@dataclass(frozen=True, slots=True)
class NetworkResult(TrackModel):
    nodes: tuple[NetworkNode, ...]
    edges: tuple[NetworkEdge, ...]
    issues: tuple[NetworkIssue, ...]
    report: NetworkReport
    detect_id: str | None = None


# ── Centerline Replay output models ────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplayFitEndpoint(TrackModel):
    point: TrajectoryPoint
    bracket_refs: tuple[PointRef, PointRef]
    segment_index: int
    segment_fraction: float


@dataclass(frozen=True, slots=True)
class ReplayFitClip(TrackModel):
    clip_id: str
    input_ordinal: int
    original_from_cluster_id: int
    original_to_cluster_id: int
    reverse_to_centerline_direction: bool
    start: ReplayFitEndpoint
    interior_points: tuple[TrajectoryPoint, ...]
    end: ReplayFitEndpoint
    points: tuple[TrajectoryPoint, ...]


@dataclass(frozen=True, slots=True)
class CenterlineInputReplay(TrackModel):
    centerline_id: int
    fit_id: str
    detect_id: str
    clean_id: str
    variant: str
    from_cluster_id: int
    to_cluster_id: int
    centerline_wkt: str
    value_semantics: str
    clips: tuple[ReplayFitClip, ...]


# ── Replay output models ────────────────────────────────────────────


@dataclass(frozen=True)
class ReplayCleanResult(TrackModel):
    """Replay output for one vehicle's cleaner stage."""

    vehicle_id: str
    original: list[TrajectoryPoint]     # 原始点（时间序）
    excluded: list[tuple[TrajectoryPoint, str]]   # 被排除 + 原因
    injected: list[tuple[TrajectoryPoint, TrajectoryPoint, TrajectoryPoint]]
    #              ↑ injected_pt,      from_original_pt,  to_original_pt
    active: list[TrajectoryPoint]       # 清洗后
