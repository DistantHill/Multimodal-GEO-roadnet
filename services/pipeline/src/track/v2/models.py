"""Stage column batches and low-frequency configuration/output containers."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
import json
import math
import re
import numpy as np
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Protocol


ColumnSelector = Callable[[list[str]], dict[str, str]]


# ── TrackModel base ────────────────────────────────────────────────


class TrackModel:
    """Boundary serialization for low-frequency output containers."""
    __slots__ = ()

    def as_dict(self) -> dict[str, Any]:
        return _to_output_value(self)


# ── JSON-safe conversion helper ─────────────────────────────────────


def _json_key(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _to_output_value(value: object) -> object:
    """Recursively convert common Python types to JSON-safe values."""
    if isinstance(value, ReplayRefineResult):
        original = value.original
        def source_point(i: int) -> dict[str, Any]:
            return {"point_ref": {"source_id": original.source_id,
                                  "source_row_key": str(original.source_row_key[i]),
                                  "stable": bool(original.source_stable[i]),
                                  "from_ref": None, "to_ref": None, "generated_reason": "",
                                  "source_row_hash": str(original.source_row_hash[i])},
                    "vehicle_id": original.vehicle_id, "timestamp": float(original.timestamp[i]),
                    "lon": float(original.lonlat[i, 0]), "lat": float(original.lonlat[i, 1]),
                    "heading": float(original.heading[i]), "speed": float(original.speed[i]),
                    "extra": {name: _to_output_value(column[i]) for name, column in original.extra.items()},
                    "x": None, "y": None, "refined_point_ref": None}
        keys = {str(key): i for i, key in enumerate(original.source_row_key)}
        active, injected = [], []
        for track in value.active:
            kept_keys = track.source_row_key[track.kind == "p"]
            for i in range(len(track)):
                derived = track.kind[i] == "i"
                anchor = int(track.after_sequence[i])
                left = str(kept_keys[anchor]) if derived and 0 <= anchor < len(kept_keys) else None
                right = str(kept_keys[anchor + 1]) if derived and 0 <= anchor + 1 < len(kept_keys) else None
                key = (f"{left}->{right}:orthogonal_heading" if derived else str(track.source_row_key[i]))
                point = {"point_ref": {"source_id": track.source_id, "source_row_key": key,
                                       "stable": not derived, "from_ref": left, "to_ref": right,
                                       "generated_reason": str(track.generated_reason[i]), "source_row_hash": ""},
                         "vehicle_id": track.vehicle_id, "timestamp": float(track.timestamp[i]),
                         "lon": float(track.lonlat[i, 0]), "lat": float(track.lonlat[i, 1]),
                         "heading": float(track.heading[i]), "speed": float(track.speed[i]),
                         "extra": {}, "x": float(track.xy[i, 0]), "y": float(track.xy[i, 1]),
                         "refined_point_ref": None}
                if not derived and key in keys:
                    source_output = source_point(keys[key])
                    point["point_ref"] = source_output["point_ref"]
                    point["extra"] = source_output["extra"]
                active.append(point)
                if derived and left in keys and right in keys:
                    injected.append([point, source_point(keys[left]), source_point(keys[right])])
        return {"vehicle_id": value.vehicle_id,
                "original": [source_point(i) for i in range(len(original))],
                "excluded": [[source_point(int(i)), str(reason)] for i, reason in
                             zip(value.excluded_indices, value.excluded_reasons)],
                "injected": injected, "active": active or [source_point(i) for i in range(len(original))]}
    if isinstance(value, ReplayFitClip):
        def endpoint(index: int, prefix: str) -> dict[str, Any]:
            return {"resolved_point_id": int(value.resolved_point_ids[index]),
                    "crossing_id": getattr(value, prefix + "_crossing_id"),
                    "intersection_id": getattr(value, prefix + "_intersection_id"),
                    "kind": getattr(value, prefix + "_kind"),
                    "lon": float(value.lonlat[index, 0]), "lat": float(value.lonlat[index, 1])}
        return {"input_ordinal": value.input_ordinal,
                "original_from_intersection_id": value.original_from_intersection_id,
                "original_to_intersection_id": value.original_to_intersection_id,
                "reverse_to_centerline_direction": value.reverse_to_centerline_direction,
                "start": endpoint(0, "start"), "end": endpoint(-1, "end"),
                "interior_points": [{"resolved_point_id": int(value.resolved_point_ids[i]),
                                     "lon": float(value.lonlat[i, 0]), "lat": float(value.lonlat[i, 1])}
                                    for i in range(1, len(value.lonlat) - 1)]}
    if isinstance(value, TrackClip):
        def endpoint(index: int, prefix: str) -> dict[str, Any]:
            return {"kind": getattr(value, prefix + "_kind"),
                    "intersection_id": getattr(value, prefix + "_intersection_id"),
                    "crossing_id": getattr(value, prefix + "_crossing_id"),
                    "lon": float(value.lonlat[index, 0]), "lat": float(value.lonlat[index, 1])}
        result = {name: _to_output_value(getattr(value, name)) for name in
                  ("clip_id", "vehicle_id", "trip_id", "clip_type", "distance_m", "point_count",
                   "subtrack_index", "clip_ordinal_in_track")}
        result.update(start=endpoint(0, "start"), end=endpoint(-1, "end"),
                      points=[{"resolved_point_id": int(value.resolved_point_ids[i]),
                               "lon": float(value.lonlat[i + 1, 0]), "lat": float(value.lonlat[i + 1, 1])}
                              for i in range(len(value.resolved_point_ids))])
        return result
    if value.__class__.__module__ in {
        "track.v2.candidates.turn_event_relation", "track.v2.candidates.cross_track_relation",
    }:
        from .candidates.turn_event_relation import TurnEventRelation
        from .candidates.cross_track_relation import CrossCandidateRelation
        from .candidates.detect_turn_events import empty_turn_batch
        from .candidates.detect_cross_track_pairs import empty_cross_batch
        if isinstance(value, (TurnEventRelation, CrossCandidateRelation)):
            empty = empty_turn_batch() if isinstance(value, TurnEventRelation) else empty_cross_batch()
            result = _to_output_value(empty)
            batches = value.iter_batches()
            try:
                for batch in batches:
                    output = _to_output_value(batch)
                    for name in result:
                        result[name].extend(output[name])
            finally:
                batches.close()
            return result
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
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
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


# ── Parser + Refine output models ──────────────────────────────


@dataclass(frozen=True)
class FieldSpec(TrackModel):
    """One canonical field definition from TOML config.

    PRD §3.3
    """

    canonical: str
    aliases: tuple[str, ...]
    required: bool
    type: str
    unit_scale: float
    default: Any
    extra: bool


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
    """Parser field specs plus data-quality thresholds."""

    specs: list[FieldSpec]
    sample_errors_max: int
    max_consecutive_errors: int
    max_error_ratio: float


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

    point_ref: str | None
    row_number: int | None
    level: str
    reason: str
    raw_values: dict[str, str]


@dataclass(frozen=True)
class ReadReport(TrackModel):
    """Parser statistics and error samples.

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
class RefineReport(TrackModel):
    """Refine statistics and excluded-point samples.

    PRD §3.4
    """

    active_count: int
    excluded_count: int
    injected_count: int
    excluded_reasons: dict[str, int]
    sample_excluded: list[tuple[str, str]]


@dataclass(frozen=True)
class RefineConfig(TrackModel):
    """Refine configuration."""

    max_extension_m: float
    min_heading_delta_deg: float
    max_heading_delta_deg: float
    max_gap_s: float
    min_speed_mps: float
    max_stationary_duration_s: float
    excluded_sample_max: int


@dataclass(frozen=True, slots=True)
class ProjectionConfig(TrackModel):
    """Run-level source and processing CRS policy.

    ``processing_srid`` deliberately has no default. A projected CRS is a
    property of the data region, not a safe repository-wide constant.
    """

    source_srid: int
    processing_srid: int
    enforce_area_of_use: bool
    max_scale_error: float


@dataclass(frozen=True, slots=True)
class SpatialContext(TrackModel):
    """Concrete coordinate context inherited by every stage of one Refine Run."""

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

    db_insert_batch: int
    db_stream_batch_size: int
    relation_batch_size: int
    parquet_read_batch_size: int
    validation_batch_size: int
    import_batch_size: int
    csv_max_field_size: int
    encoding_sample_size: int
    encoding_confidence: float
    default_text_encoding: str
    fallback_encodings: tuple[str, ...]
    mark_flush_every_n: int
    excluded_flush_threshold: int
    refine_flush_point_threshold: int
    progress_interval: int
    progress_time_threshold_s: float
    duckdb_memory_limit: str
    duckdb_event_memory_limit: str
    duckdb_grid_memory_limit: str
    duckdb_threads: int
    duckdb_max_temp_directory_size: str
    polars_threads: int
    parquet_target_file_size_bytes: int
    parquet_row_group_size_rows: int
    artifact_gc_grace_days: int

    def __post_init__(self) -> None:
        for name, lower, upper in (
            ("duckdb_threads", 1, 4),
            ("polars_threads", 1, None),
            ("parquet_target_file_size_bytes", 0, None),
            ("parquet_row_group_size_rows", 0, None),
            ("artifact_gc_grace_days", 1, None),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < lower or (upper is not None and value > upper):
                maximum = f" and <= {upper}" if upper is not None else ""
                raise ValueError(f"{name} must be >= {lower}{maximum}")

        for name in ("duckdb_memory_limit", "duckdb_event_memory_limit", "duckdb_grid_memory_limit", "duckdb_max_temp_directory_size"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a capacity string")
            match = re.fullmatch(
                r"\s*([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
                r"(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*",
                value,
                flags=re.IGNORECASE,
            )
            if match is None or not math.isfinite(float(match.group(1))):
                raise ValueError(f"{name} must be a non-negative finite capacity")


class TrajectorySourceHandle(Protocol):
    @property
    def source_info(self) -> SourceInfo: ...

    @property
    def report(self) -> ReadReport: ...

    @property
    def read_report(self) -> ReadReport: ...

    @property
    def field_mapping(self) -> FieldMapping: ...

    def iter_vehicle_batches(self) -> Iterator[SourceBatch]: ...

    def close(self) -> None: ...

    def __enter__(self) -> "TrajectorySourceHandle": ...

    def __exit__(self, *args: object) -> None: ...

class GeneratedReason(StrEnum):
    HEADING_ORTHOGONAL = "heading_orthogonal"
    INTER_SEGMENT_ORTHOGONAL = "inter_segment_orthogonal"


@dataclass(frozen=True, slots=True)
class RefineResult(TrackModel):
    subtracks: list[RefinementSubtrack]
    excluded: list[ExcludedBatch]
    report: RefineReport
    spatial_context: SpatialContext


def require_array_columns(instance: object) -> None:
    for model_field in fields(instance):
        if model_field.type in (np.ndarray, "np.ndarray"):
            value = getattr(instance, model_field.name)
            if not isinstance(value, np.ndarray) or value.ndim == 0:
                raise ValueError(f"{model_field.name} must be an ndarray column")


def validate_column_lengths(instance: object, size: int) -> None:
    require_array_columns(instance)
    for model_field in fields(instance):
        value = getattr(instance, model_field.name)
        if isinstance(value, np.ndarray) and (value.ndim == 0 or len(value) != size):
            raise ValueError(f"{model_field.name} has inconsistent length")
        if isinstance(value, np.ndarray) and model_field.name not in {"xy", "lonlat", "points"} and value.ndim != 1:
            raise ValueError(f"{model_field.name} must be one-dimensional")
        if model_field.name.endswith("_valid") and isinstance(value, np.ndarray) and value.dtype != np.bool_:
            raise ValueError(f"{model_field.name} must be a boolean mask")
        integer_identity = (model_field.name in {"calculation_index", "member_order", "resolved_point_ids", "crossing_ids", "intersection_ids", "after_sequence"}
                            or model_field.name.endswith(("subtrack_index", "point_index", "refine_point_id", "source_row_key", "crossing_calculate_index")))
        if integer_identity and isinstance(value, np.ndarray) and value.dtype != np.int64:
            raise ValueError(f"{model_field.name} must be int64")


def readonly_columns(instance: object) -> None:
    """Publish array columns without copying views or enforcing contiguity."""
    for model_field in fields(instance):
        value = getattr(instance, model_field.name)
        if isinstance(value, np.ndarray):
            value.flags.writeable = False
        elif isinstance(value, dict):
            for column in value.values():
                if isinstance(column, np.ndarray):
                    column.flags.writeable = False


@dataclass(frozen=True, slots=True)
class SourceBatch(TrackModel):
    """One complete source vehicle, with independent identity columns."""
    source_id: str
    vehicle_id: str
    lonlat: np.ndarray
    timestamp: np.ndarray
    heading: np.ndarray
    speed: np.ndarray
    source_row_key: np.ndarray
    source_stable: np.ndarray
    source_row_hash: np.ndarray
    extra: dict[str, np.ndarray] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_array_columns(self)
        if self.lonlat.shape != (len(self.timestamp), 2) or self.lonlat.dtype != np.float64:
            raise ValueError("lonlat must be float64 (N,2)")
        validate_column_lengths(self, len(self.timestamp))
        if self.source_row_key.dtype != np.int64 or self.source_stable.dtype != np.bool_:
            raise ValueError("source keys must be int64 and stable flags boolean")
        if any(not isinstance(column, np.ndarray) or column.ndim != 1 or len(column) != len(self.timestamp) for column in self.extra.values()):
            raise ValueError("extra columns must be one-dimensional with consistent lengths")
        readonly_columns(self)

    def __len__(self) -> int:
        return len(self.timestamp)


@dataclass(frozen=True, slots=True)
class ExcludedBatch(TrackModel):
    source_id: str
    source_row_key: np.ndarray
    reason: np.ndarray
    lonlat: np.ndarray

    def __post_init__(self) -> None:
        require_array_columns(self)
        validate_column_lengths(self, len(self.source_row_key))
        if self.source_row_key.dtype != np.int64:
            raise ValueError("source keys must be int64")
        if self.lonlat.shape != (len(self.source_row_key), 2) or self.lonlat.dtype != np.float64:
            raise ValueError("lonlat must be float64 (N,2)")
        readonly_columns(self)

    def __len__(self) -> int:
        return len(self.source_row_key)

    def reason_counts(self) -> dict[str, int]:
        values, counts = np.unique(self.reason, return_counts=True)
        return {str(value): int(count) for value, count in zip(values, counts)}


@dataclass(frozen=True, slots=True)
class RefinementSubtrack(TrackModel):
    """Complete ordered p/i columns; segment i joins rows i and i+1."""
    vehicle_id: str
    subtrack_index: int
    source_id: str
    xy: np.ndarray
    lonlat: np.ndarray
    timestamp: np.ndarray
    heading: np.ndarray
    speed: np.ndarray
    kind: np.ndarray
    source_row_key: np.ndarray
    source_row_key_valid: np.ndarray
    refine_point_id: np.ndarray
    refine_point_id_valid: np.ndarray
    generated_reason: np.ndarray
    after_sequence: np.ndarray

    def __post_init__(self) -> None:
        require_array_columns(self)
        size = len(self.timestamp)
        for name in ("xy", "lonlat"):
            value = getattr(self, name)
            if value.shape != (size, 2) or value.dtype != np.float64:
                raise ValueError(f"{name} must be float64 (N,2)")
        validate_column_lengths(self, len(self.timestamp))
        for name in ("source_row_key", "refine_point_id", "after_sequence"):
            if getattr(self, name).dtype != np.int64:
                raise ValueError(f"{name} must be int64")
        readonly_columns(self)

    def __len__(self) -> int:
        return len(self.timestamp)


@dataclass(frozen=True, slots=True)
class RefineIncrementalResult(TrackModel):
    excluded: ExcludedBatch
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


# ── Detect output models ──────────────────────────────────────────
# PRD v2 Detect §3, §7


@dataclass(frozen=True)
class HotspotCluster(TrackModel):
    """A DBSCAN cluster of turn or cross-track candidate events.

    ``cluster_calculate_index`` is a transient Detect-stage association key and
    is never persisted. Member columns refer to event batches or repeatable
    read-only relations whose owning Detect result controls their lifetime.
    """

    cluster_calculate_index: int
    centroid_x: float
    centroid_y: float
    centroid_lon: float
    centroid_lat: float
    radius_m: float
    cluster_ref: str = ""        # stable ref → "cluster_{source}_{id}"
    members: MemberBatch | None = None
    source: str = "turn"
    algorithm_sources: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    source_counts: dict[str, int] = field(default_factory=dict)
    source_ratios: dict[str, float] = field(default_factory=dict)
    critical_sources: tuple[str, ...] = ()
    # ── topology intermediate products ──
    crossing_points: CrossingBatch | None = None


@dataclass(frozen=True)
class IntersectionTopology(TrackModel):
    """Extracted topology linked by a transient cluster calculation key."""

    cluster_calculate_index: int
    branch_centers_deg: list[float]
    transition_counts: dict[tuple[int, int], int]
    observation_count: int
    is_complete: bool
    reason: str
    clip_radius_m: float = 0.0


@dataclass(frozen=True)
class IntersectReport(TrackModel):
    """Detect aggregated statistics."""

    track_count: int
    turn_event_count: int
    hotspot_event_count: int
    cross_track_candidate_count: int
    cluster_count: int
    cluster_count_turn: int
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

    intersection_id: int
    lon: float
    lat: float
    radius_m: float


@dataclass(frozen=True, slots=True)
class ClipTrack(TrackModel):
    vehicle_id: str
    trip_id: str
    xy: np.ndarray
    lonlat: np.ndarray
    resolved_point_ids: np.ndarray
    crossing_ids: np.ndarray
    intersection_ids: np.ndarray
    crossing_types: np.ndarray
    crossing_valid: np.ndarray
    intersection_valid: np.ndarray
    subtrack_index: int = 1


    def __post_init__(self) -> None:
        require_array_columns(self)
        validate_column_lengths(self, len(self.lonlat))
        if self.xy.shape != self.lonlat.shape or self.lonlat.shape != (len(self.lonlat), 2):
            raise ValueError("clip geometry must have shape (N,2)")
        if self.xy.dtype != np.float64 or self.lonlat.dtype != np.float64:
            raise ValueError("clip geometry must be float64")
        readonly_columns(self)

@dataclass(frozen=True, slots=True)
class ClipInput(TrackModel):
    """Complete, storage-independent input for one Clip extraction."""

    intersections: tuple[ClipIntersection, ...]
    tracks: tuple[ClipTrack, ...]
    spatial_context: SpatialContext
    detect_id: str | None = None


@dataclass(frozen=True, slots=True)
class TrackClip(TrackModel):
    clip_id: str
    vehicle_id: str
    trip_id: str
    clip_type: str
    xy: np.ndarray
    lonlat: np.ndarray
    resolved_point_ids: np.ndarray
    start_index: int
    end_index: int
    interior_start: int
    interior_end: int
    start_kind: str
    end_kind: str
    start_intersection_id: int | None
    end_intersection_id: int | None
    start_crossing_id: int | None
    end_crossing_id: int | None
    distance_m: float
    point_count: int
    subtrack_index: int = 1
    clip_ordinal_in_track: int = 0


    def __post_init__(self) -> None:
        require_array_columns(self)
        if self.xy.dtype != np.float64 or self.xy.shape != (len(self.xy), 2) or self.lonlat.dtype != np.float64 or self.lonlat.shape != self.xy.shape:
            raise ValueError("clip geometry must be float64 (N,2)")
        if self.resolved_point_ids.ndim != 1 or self.resolved_point_ids.dtype != np.int64:
            raise ValueError("interior resolved point IDs must be int64 (N,)")
        readonly_columns(self)

@dataclass(frozen=True, slots=True)
class ClipReport(TrackModel):
    """Aggregate counts; ``total_points_in_clips`` includes endpoints."""

    total_vehicles: int
    total_subtracks: int
    total_clips: int
    between_intersection_clips: int
    boundary_start_clips: int
    boundary_end_clips: int
    full_track_clips: int
    total_points_in_clips: int
    skipped_degenerate_count: int = 0


@dataclass(frozen=True, slots=True)
class ClipResult(TrackModel):
    clips: Sequence[TrackClip]
    report: ClipReport
    spatial_context: SpatialContext
    detect_id: str | None = None
    intersections: tuple[ClipIntersection, ...] = ()


@dataclass(frozen=True, slots=True)
class ClipConfig(TrackModel):
    include_boundary_clips: bool


# ── Fitting output models ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FittingConfig(TrackModel):
    """Configuration for fitting one centerline per connected pair."""

    point_spacing_m: float
    fit_sample_spacing_m: float
    min_clips_per_pair: int


CenterlineVariant = Literal["combined", "directed"]


@dataclass(frozen=True, slots=True)
class Centerline(TrackModel):
    """One physical road centerline in canonical intersection-ID direction."""

    from_intersection_id: int
    to_intersection_id: int
    points: tuple[tuple[float, float], ...]
    length_m: float
    source_clip_ids: tuple[str, ...]
    forward_clip_count: int
    reverse_clip_count: int
    variant: CenterlineVariant = "combined"

    @property
    def source_clip_count(self) -> int:
        return self.forward_clip_count + self.reverse_clip_count


    def __post_init__(self) -> None:
        if not isinstance(self.points, np.ndarray) or self.points.dtype != np.float64 or self.points.ndim != 2 or self.points.shape[1] != 2:
            raise ValueError("points must be a float64 (N,2) array")
        readonly_columns(self)


@dataclass(frozen=True, slots=True)
class FitIssue(TrackModel):
    """A rejected clip or connected pair, retained for auditability."""

    from_intersection_id: int | None
    to_intersection_id: int | None
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
    include_isolated_nodes: bool


@dataclass(frozen=True, slots=True)
class NetworkNode(TrackModel):
    intersection_id: int
    lon: float
    lat: float
    radius_m: float
    degree: int


@dataclass(frozen=True, slots=True)
class NetworkEdge(TrackModel):
    edge_id: str
    from_intersection_id: int
    to_intersection_id: int
    points: tuple[tuple[float, float], ...]
    length_m: float
    source_clip_count: int
    forward_clip_count: int
    reverse_clip_count: int


    def __post_init__(self) -> None:
        if not isinstance(self.points, np.ndarray) or self.points.dtype != np.float64 or self.points.ndim != 2 or self.points.shape[1] != 2:
            raise ValueError("points must be a float64 (N,2) array")
        readonly_columns(self)


@dataclass(frozen=True, slots=True)
class NetworkIssue(TrackModel):
    from_intersection_id: int
    to_intersection_id: int
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
class ReplayFitClip(TrackModel):
    input_ordinal: int
    original_from_intersection_id: int
    original_to_intersection_id: int
    reverse_to_centerline_direction: bool
    lonlat: np.ndarray
    resolved_point_ids: np.ndarray
    start_crossing_id: int
    end_crossing_id: int
    start_intersection_id: int
    end_intersection_id: int
    start_kind: str
    end_kind: str

    def __post_init__(self) -> None:
        require_array_columns(self)
        validate_column_lengths(self, len(self.lonlat))
        if self.lonlat.dtype != np.float64 or self.lonlat.shape != (len(self.lonlat), 2):
            raise ValueError("replay geometry must be float64 (N,2)")
        readonly_columns(self)

@dataclass(frozen=True, slots=True)
class CenterlineInputReplay(TrackModel):
    centerline_id: int
    fit_id: str
    detect_id: str
    refine_id: str
    variant: str
    from_intersection_id: int
    to_intersection_id: int
    centerline_wkt: str
    value_semantics: str
    clips: tuple[ReplayFitClip, ...]


# ── Replay output models ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplayRefineResult(TrackModel):
    vehicle_id: str
    original: SourceBatch
    excluded_indices: np.ndarray
    excluded_reasons: np.ndarray
    active: list[RefinementSubtrack]

    def __post_init__(self) -> None:
        require_array_columns(self)
        validate_column_lengths(self, len(self.excluded_indices))
        if self.excluded_indices.dtype != np.int64:
            raise ValueError("excluded indices must be int64")
        readonly_columns(self)


@dataclass(frozen=True, slots=True)
class TurnBatch(TrackModel):
    calculation_index: np.ndarray
    vehicle_id: np.ndarray
    subtrack_index: np.ndarray
    point_index: np.ndarray
    xy: np.ndarray
    lonlat: np.ndarray
    timestamp: np.ndarray
    entry_bearing: np.ndarray
    exit_bearing: np.ndarray
    turn_deg: np.ndarray
    score: np.ndarray
    source_id: np.ndarray
    source_row_key: np.ndarray
    source_row_key_valid: np.ndarray
    point_kind: np.ndarray
    refine_point_id: np.ndarray
    refine_point_id_valid: np.ndarray
    generated_reason: np.ndarray
    g_star_z: np.ndarray
    g_star_z_valid: np.ndarray

    def __post_init__(self) -> None:
        require_array_columns(self)
        size = len(self.vehicle_id) if hasattr(self, "vehicle_id") else len(self.calculation_index)
        for model_field in fields(self):
            column = getattr(self, model_field.name)
            if isinstance(column, np.ndarray) and len(column) != size:
                raise ValueError(f"{model_field.name} has inconsistent length")
        validate_column_lengths(self, size)
        for name in ("xy", "lonlat"):
            if hasattr(self, name):
                geometry = getattr(self, name)
                if geometry.shape != (size, 2) or geometry.dtype != np.float64:
                    raise ValueError(f"{name} must be float64 (N,2)")
        readonly_columns(self)

    def __len__(self) -> int:
        return len(self.vehicle_id) if hasattr(self, "vehicle_id") else len(self.calculation_index)

    def take(self, indices: np.ndarray | slice):
        return replace(self, **{model_field.name: getattr(self, model_field.name)[indices]
                                for model_field in fields(self)
                                if isinstance(getattr(self, model_field.name), np.ndarray)})


@dataclass(frozen=True, slots=True)
class CrossCandidateBatch(TrackModel):
    calculation_index: np.ndarray
    xy: np.ndarray
    lonlat: np.ndarray
    timestamp: np.ndarray
    heading: np.ndarray
    speed: np.ndarray
    from_vehicle_id: np.ndarray
    to_vehicle_id: np.ndarray
    from_subtrack_index: np.ndarray
    to_subtrack_index: np.ndarray
    from_point_index: np.ndarray
    to_point_index: np.ndarray
    from_point_kind: np.ndarray
    to_point_kind: np.ndarray
    from_refine_point_id: np.ndarray
    to_refine_point_id: np.ndarray
    from_refine_point_id_valid: np.ndarray
    to_refine_point_id_valid: np.ndarray
    from_source_id: np.ndarray
    to_source_id: np.ndarray
    from_source_row_key: np.ndarray
    to_source_row_key: np.ndarray
    from_source_row_key_valid: np.ndarray
    to_source_row_key_valid: np.ndarray
    from_generated_reason: np.ndarray
    to_generated_reason: np.ndarray

    def __post_init__(self) -> None:
        require_array_columns(self)
        size = len(self.vehicle_id) if hasattr(self, "vehicle_id") else len(self.calculation_index)
        for model_field in fields(self):
            column = getattr(self, model_field.name)
            if isinstance(column, np.ndarray) and len(column) != size:
                raise ValueError(f"{model_field.name} has inconsistent length")
        validate_column_lengths(self, size)
        for name in ("xy", "lonlat"):
            if hasattr(self, name):
                geometry = getattr(self, name)
                if geometry.shape != (size, 2) or geometry.dtype != np.float64:
                    raise ValueError(f"{name} must be float64 (N,2)")
        readonly_columns(self)

    def __len__(self) -> int:
        return len(self.vehicle_id) if hasattr(self, "vehicle_id") else len(self.calculation_index)

    def take(self, indices: np.ndarray | slice):
        return replace(self, **{model_field.name: getattr(self, model_field.name)[indices]
                                for model_field in fields(self)
                                if isinstance(getattr(self, model_field.name), np.ndarray)})


@dataclass(frozen=True, slots=True)
class CrossingBatch(TrackModel):
    cluster_ref: str
    calculation_index: np.ndarray
    vehicle_id: np.ndarray
    subtrack_index: np.ndarray
    point_index: np.ndarray
    point_kind: np.ndarray
    refine_point_id: np.ndarray
    refine_point_id_valid: np.ndarray
    source_id: np.ndarray
    source_row_key: np.ndarray
    source_row_key_valid: np.ndarray
    generated_reason: np.ndarray
    xy: np.ndarray
    lonlat: np.ndarray
    is_entry: np.ndarray
    branch_angle_deg: np.ndarray
    segment_fraction: np.ndarray

    def __post_init__(self) -> None:
        require_array_columns(self)
        size = len(self.vehicle_id) if hasattr(self, "vehicle_id") else len(self.calculation_index)
        for model_field in fields(self):
            column = getattr(self, model_field.name)
            if isinstance(column, np.ndarray) and len(column) != size:
                raise ValueError(f"{model_field.name} has inconsistent length")
        validate_column_lengths(self, size)
        for name in ("xy", "lonlat"):
            if hasattr(self, name):
                geometry = getattr(self, name)
                if geometry.shape != (size, 2) or geometry.dtype != np.float64:
                    raise ValueError(f"{name} must be float64 (N,2)")
        readonly_columns(self)

    def __len__(self) -> int:
        return len(self.vehicle_id) if hasattr(self, "vehicle_id") else len(self.calculation_index)

    def take(self, indices: np.ndarray | slice):
        return replace(self, **{model_field.name: getattr(self, model_field.name)[indices]
                                for model_field in fields(self)
                                if isinstance(getattr(self, model_field.name), np.ndarray)})


@dataclass(frozen=True, slots=True)
class ObservationBatch(TrackModel):
    cluster_calculate_index: int
    vehicle_id: np.ndarray
    subtrack_index: np.ndarray
    entry_branch_angle: np.ndarray
    exit_branch_angle: np.ndarray
    entry_bearing: np.ndarray
    exit_bearing: np.ndarray
    turn_deg: np.ndarray
    entry_crossing_calculate_index: np.ndarray
    entry_crossing_calculate_index_valid: np.ndarray
    exit_crossing_calculate_index: np.ndarray
    exit_crossing_calculate_index_valid: np.ndarray

    def __post_init__(self) -> None:
        require_array_columns(self)
        size = len(self.vehicle_id) if hasattr(self, "vehicle_id") else len(self.calculation_index)
        for model_field in fields(self):
            column = getattr(self, model_field.name)
            if isinstance(column, np.ndarray) and len(column) != size:
                raise ValueError(f"{model_field.name} has inconsistent length")
        validate_column_lengths(self, size)
        for name in ("xy", "lonlat"):
            if hasattr(self, name):
                geometry = getattr(self, name)
                if geometry.shape != (size, 2) or geometry.dtype != np.float64:
                    raise ValueError(f"{name} must be float64 (N,2)")
        readonly_columns(self)

    def __len__(self) -> int:
        return len(self.vehicle_id) if hasattr(self, "vehicle_id") else len(self.calculation_index)

    def take(self, indices: np.ndarray | slice):
        return replace(self, **{model_field.name: getattr(self, model_field.name)[indices]
                                for model_field in fields(self)
                                if isinstance(getattr(self, model_field.name), np.ndarray)})


@dataclass(frozen=True, slots=True)
class MemberBatch(TrackModel):
    kind: np.ndarray
    calculation_index: np.ndarray
    member_order: np.ndarray
    source_id: np.ndarray
    source_id_valid: np.ndarray

    def __post_init__(self) -> None:
        require_array_columns(self)
        validate_column_lengths(self, len(self.kind))
        readonly_columns(self)

    def __len__(self) -> int:
        return len(self.kind)

    def take(self, indices: np.ndarray | slice):
        return replace(self, **{f.name: getattr(self, f.name)[indices] for f in fields(self)})
