from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

if TYPE_CHECKING:
    from ..config import MapConfig


BBox = tuple[float, float, float, float]


def validate_bbox(value: BBox) -> BBox:
    min_lon, min_lat, max_lon, max_lat = value
    if not all(math.isfinite(item) for item in value):
        raise ValueError("bbox coordinates must be finite")
    if not (-180.0 <= min_lon < max_lon <= 180.0):
        raise ValueError("bbox longitude must satisfy -180 <= min < max <= 180")
    if not (-90.0 <= min_lat < max_lat <= 90.0):
        raise ValueError("bbox latitude must satisfy -90 <= min < max <= 90")
    return value


def parse_bbox(value: str | None) -> BBox | None:
    if value is None:
        return None
    parts = value.split(",")
    if len(parts) != 4:
        raise ValueError("bbox must contain minLon,minLat,maxLon,maxLat")
    try:
        parsed = tuple(float(part.strip()) for part in parts)
    except ValueError as exc:
        raise ValueError("bbox coordinates must be numbers") from exc
    return validate_bbox(parsed)  # type: ignore[arg-type]


class ReplayRequest(BaseModel):
    intersection_id: int | None = Field(default=None, ge=1)
    intersection_ids: list[int] | None = Field(default=None, min_length=1)
    centerline_ids: list[int] | None = Field(default=None, min_length=1)
    tracks_cross_ids: list[int] | None = None
    turn_event_ids: list[int] | None = None
    injected_point_ids: list[int] | None = None
    database_name: str | None = Field(default=None, min_length=1, max_length=63)
    detect_id: UUID | None = None
    fit_id: UUID | None = None
    refine_id: UUID | None = None
    source_row_keys: list[str] | None = None
    resolved_point_ids: list[int] | None = None
    bbox: BBox | None = None
    max_vehicles: int | None = Field(default=None, ge=1)
    max_points: int | None = Field(default=None, ge=2)

    @field_validator("intersection_ids")
    @classmethod
    def validate_intersection_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(item <= 0 for item in value):
            raise ValueError("intersection_ids must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError("intersection_ids must contain distinct values")
        return value

    @field_validator("centerline_ids")
    @classmethod
    def validate_centerline_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(item <= 0 for item in value):
            raise ValueError("centerline_ids must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError("centerline_ids must contain distinct values")
        return value

    @field_validator("source_row_keys")
    @classmethod
    def validate_source_row_keys(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("source_row_keys must not be empty")
        normalized = []
        for item in value:
            key = item.strip()
            if re.fullmatch(r"[+-]?[0-9]+", key) is None:
                raise ValueError("source_row_keys must contain BIGINT integer strings")
            number = int(key)
            if not -(2**63) <= number < 2**63:
                raise ValueError("source_row_keys must be within the BIGINT range")
            normalized.append(str(number))
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_row_keys must be distinct")
        return normalized

    @field_validator("resolved_point_ids", "tracks_cross_ids", "turn_event_ids", "injected_point_ids")
    @classmethod
    def validate_point_and_event_ids(cls, value: list[int] | None, info: ValidationInfo) -> list[int] | None:
        if value is None:
            return None
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        if any(item <= 0 for item in value):
            raise ValueError(f"{info.field_name} must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError(f"{info.field_name} must be distinct")
        return value

    @field_validator("bbox")
    @classmethod
    def validate_selection_bbox(cls, value: BBox | None) -> BBox | None:
        return validate_bbox(value) if value is not None else None

    @model_validator(mode="after")
    def validate_selection(self) -> "ReplayRequest":
        if self.bbox is not None:
            raise ValueError(
                "Replay 不支持 bbox，请发送点 ID、交叉口 ID 或 centerline_ids"
            )
        selections = (
            self.intersection_id,
            self.intersection_ids,
            self.centerline_ids,
            self.tracks_cross_ids,
            self.turn_event_ids,
            self.injected_point_ids,
            self.source_row_keys,
            self.resolved_point_ids,
            self.bbox,
        )
        if sum(value is not None for value in selections) != 1:
            raise ValueError(
                "provide exactly one of source_row_keys, resolved_point_ids, intersection_id, intersection_ids, centerline_ids, tracks_cross_ids, turn_event_ids, or injected_point_ids"
            )
        if self.resolved_point_ids is not None and self.detect_id is None:
            raise ValueError("resolved_point_ids requires detect_id")
        if (self.tracks_cross_ids is not None or self.turn_event_ids is not None) and self.detect_id is None:
            raise ValueError("event Replay requires detect_id")
        if self.injected_point_ids is not None and self.detect_id is not None:
            raise ValueError("injected point Replay uses Refine provenance, not detect_id")
        if self.centerline_ids is not None and (self.fit_id is None or self.detect_id is None):
            raise ValueError("centerline_ids requires fit_id and detect_id")
        return self


class ClipRequest(BaseModel):
    database_name: str | None = Field(default=None, min_length=1, max_length=63)
    detect_id: UUID
    intersection_ids: list[int] = Field(min_length=2, max_length=2)
    max_vehicles: int | None = Field(default=None, ge=1)
    max_points: int | None = Field(default=None, ge=2)

    @field_validator("intersection_ids")
    @classmethod
    def validate_intersection_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("intersection_ids must contain positive integers")
        if len(set(value)) != 2:
            raise ValueError("intersection_ids must contain exactly two distinct values")
        return value


class FeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    crs: dict[str, Any]
    features: list[dict[str, Any]]
    metadata: dict[str, Any]


def resolve_replay_limits(request: ReplayRequest, config: "MapConfig") -> ReplayRequest:
    """Return a request with configured defaults after enforcing its resource budget."""
    for name, values in (
        ("intersection_ids", request.intersection_ids),
        ("centerline_ids", request.centerline_ids),
        ("source_row_keys", request.source_row_keys),
        ("resolved_point_ids", request.resolved_point_ids),
        ("tracks_cross_ids", request.tracks_cross_ids),
        ("turn_event_ids", request.turn_event_ids),
        ("injected_point_ids", request.injected_point_ids),
    ):
        if values is not None and len(values) > config.replay_selection_ids_max:
            raise ValueError(
                f"{name} accepts at most {config.replay_selection_ids_max} values"
            )
    max_vehicles = request.max_vehicles or config.replay_max_vehicles_default
    max_points = request.max_points or config.replay_max_points_default
    if max_vehicles > config.replay_max_vehicles_max:
        raise ValueError(
            f"max_vehicles must not exceed configured maximum {config.replay_max_vehicles_max}"
        )
    if max_points > config.replay_max_points_max:
        raise ValueError(
            f"max_points must not exceed configured maximum {config.replay_max_points_max}"
        )
    return request.model_copy(update={"max_vehicles": max_vehicles, "max_points": max_points})


def resolve_clip_limits(request: ClipRequest, config: "MapConfig") -> ClipRequest:
    """Return a request with configured defaults after enforcing its resource budget."""
    max_vehicles = request.max_vehicles or config.clip_max_vehicles_default
    max_points = request.max_points or config.clip_max_points_default
    if max_vehicles > config.clip_max_vehicles_max:
        raise ValueError(
            f"max_vehicles must not exceed configured maximum {config.clip_max_vehicles_max}"
        )
    if max_points > config.clip_max_points_max:
        raise ValueError(
            f"max_points must not exceed configured maximum {config.clip_max_points_max}"
        )
    return request.model_copy(update={"max_vehicles": max_vehicles, "max_points": max_points})
