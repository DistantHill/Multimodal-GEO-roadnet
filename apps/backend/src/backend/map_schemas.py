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
    detect_id: UUID | None = None
    point_ids: list[int] | None = None
    bbox: BBox | None = None
    max_vehicles: int = Field(default=25, ge=1, le=100)
    max_points: int = Field(default=10_000, ge=2, le=50_000)

    @field_validator("point_ids")
    @classmethod
    def validate_point_ids(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("point_ids must not be empty")
        if len(value) > 1_000:
            raise ValueError("point_ids accepts at most 1000 values")
        if any(item <= 0 for item in value):
            raise ValueError("point_ids must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError("point_ids must be distinct")
        return value

    @field_validator("bbox")
    @classmethod
    def validate_selection_bbox(cls, value: BBox | None) -> BBox | None:
        return validate_bbox(value) if value is not None else None

    @model_validator(mode="after")
    def validate_selection(self) -> "ReplayRequest":
        if (self.point_ids is None) == (self.bbox is None):
            raise ValueError("provide exactly one of point_ids or bbox")
        return self


class ClipRequest(BaseModel):
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
