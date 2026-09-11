from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


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
    database_name: str | None = Field(default=None, min_length=1, max_length=63)
    detect_id: UUID | None = None
    source_row_keys: list[str] | None = None
    resolved_point_ids: list[int] | None = None
    bbox: BBox | None = None
    max_vehicles: int = Field(default=25, ge=1, le=100)
    max_points: int = Field(default=10_000, ge=2, le=50_000)

    @field_validator("source_row_keys")
    @classmethod
    def validate_source_row_keys(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("source_row_keys must not be empty")
        if len(value) > 1_000:
            raise ValueError("source_row_keys accepts at most 1000 values")
        normalized = [str(item) for item in value]
        if any(not item for item in normalized):
            raise ValueError("source_row_keys must contain non-empty strings")
        if len(set(normalized)) != len(normalized):
            raise ValueError("source_row_keys must be distinct")
        return normalized

    @field_validator("resolved_point_ids")
    @classmethod
    def validate_resolved_point_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("resolved_point_ids must not be empty")
        if len(value) > 1_000:
            raise ValueError("resolved_point_ids accepts at most 1000 values")
        if any(item <= 0 for item in value):
            raise ValueError("resolved_point_ids must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError("resolved_point_ids must be distinct")
        return value

    @field_validator("bbox")
    @classmethod
    def validate_selection_bbox(cls, value: BBox | None) -> BBox | None:
        return validate_bbox(value) if value is not None else None

    @model_validator(mode="after")
    def validate_selection(self) -> "ReplayRequest":
        if self.bbox is not None:
            raise ValueError("Replay 不支持 bbox，请发送选中要素的 resolved_point_ids 或 intersection_id")
        selections = (
            self.intersection_id,
            self.source_row_keys,
            self.resolved_point_ids,
            self.bbox,
        )
        if sum(value is not None for value in selections) != 1:
            raise ValueError(
                "provide exactly one of source_row_keys, resolved_point_ids, or intersection_id"
            )
        if self.resolved_point_ids is not None and self.detect_id is None:
            raise ValueError("resolved_point_ids requires detect_id")
        return self


class ClipRequest(BaseModel):
    database_name: str | None = Field(default=None, min_length=1, max_length=63)
    detect_id: UUID
    intersection_ids: list[int] = Field(min_length=2, max_length=2)
    max_vehicles: int = Field(default=50, ge=1, le=200)
    max_points: int = Field(default=20_000, ge=2, le=100_000)

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
