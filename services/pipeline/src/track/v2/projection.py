"""Run-scoped coordinate transforms for metric pipeline calculations."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Iterable

from .models import ProjectionConfig, SpatialContext


COORDINATE_SEMANTICS_VERSION = 2


class ProjectionError(ValueError):
    """Raised when a Run's coordinate context is missing or unsafe."""


def create_spatial_context(
    config: ProjectionConfig,
    *,
    processing_srid: int | None = None,
) -> SpatialContext:
    """Validate a configured CRS pair and return its persisted Run context."""
    selected = config.processing_srid if processing_srid is None else processing_srid
    if selected is None:
        raise ProjectionError(
            "processing_srid is required for a new Clean Run; choose a projected "
            "metre-based CRS that is valid for the complete data region"
        )
    context = SpatialContext(
        source_srid=int(config.source_srid),
        processing_srid=int(selected),
        projection_config={
            "coordinate_semantics_version": COORDINATE_SEMANTICS_VERSION,
            "always_xy": True,
            "distance_semantics": "projected_euclidean_metre",
            "input_heading_semantics": "clockwise_degrees_from_true_north",
            "topology_bearing_semantics": "clockwise_degrees_from_processing_grid_north",
            "enforce_area_of_use": bool(config.enforce_area_of_use),
            "max_scale_error": float(config.max_scale_error),
        },
    )
    validate_spatial_context(context)
    return context


def spatial_context_from_values(
    source_srid: object,
    processing_srid: object,
    projection_config: object,
) -> SpatialContext:
    """Materialize and validate a context inherited from ``clean_runs``."""
    if processing_srid is None:
        raise ProjectionError(
            "Clean Run has no processing_srid; legacy Run coordinates cannot be "
            "silently treated as fixed-CRS metres"
        )
    if isinstance(projection_config, dict):
        raw_config = projection_config
    elif isinstance(projection_config, str):
        try:
            decoded = json.loads(projection_config)
        except json.JSONDecodeError as exc:
            raise ProjectionError("projection_config must be a JSON object") from exc
        if not isinstance(decoded, dict):
            raise ProjectionError("projection_config must be a JSON object")
        raw_config = decoded
    elif projection_config is None:
        raw_config = {}
    else:
        raise ProjectionError("projection_config must be a JSON object")
    context = SpatialContext(
        source_srid=int(source_srid),
        processing_srid=int(processing_srid),
        projection_config=dict(raw_config),
    )
    validate_spatial_context(context)
    return context


def validate_spatial_context(context: SpatialContext) -> None:
    """Reject geographic, non-metre, or mismatched source coordinate systems."""
    semantics_version = context.projection_config.get("coordinate_semantics_version")
    if semantics_version != COORDINATE_SEMANTICS_VERSION:
        raise ProjectionError(
            "SpatialContext requires coordinate_semantics_version=2; legacy or "
            "partially backfilled Run coordinates are not safe to consume"
        )
    CRS, _Proj, _Transformer = _pyproj_types()
    try:
        source = CRS.from_epsg(context.source_srid)
        processing = CRS.from_epsg(context.processing_srid)
    except Exception as exc:
        raise ProjectionError("source_srid and processing_srid must be valid EPSG codes") from exc
    if context.source_srid != 4326 or not source.is_geographic:
        raise ProjectionError(
            "source_srid must be EPSG:4326 while pipeline lon/lat and Geometry use 4326"
        )
    if not processing.is_projected:
        raise ProjectionError("processing_srid must identify a projected CRS")
    linear_axes = [axis for axis in processing.axis_info if axis.unit_name]
    if not linear_axes or any(
        axis.unit_name.lower() not in {"metre", "meter"}
        or not math.isclose(float(axis.unit_conversion_factor), 1.0, abs_tol=1e-12)
        for axis in linear_axes
    ):
        raise ProjectionError("processing_srid must use metres as its linear unit")
    horizontal_directions = {
        str(axis.direction).lower() for axis in processing.axis_info[:2]
    }
    if len(processing.axis_info) < 2 or horizontal_directions != {"east", "north"}:
        raise ProjectionError(
            "processing_srid horizontal axes must point east and north so "
            "always_xy guarantees +x east and +y north"
        )
    max_scale_error = _max_scale_error(context)
    if not math.isfinite(max_scale_error) or max_scale_error < 0.0:
        raise ProjectionError("projection_config.max_scale_error must be non-negative")


class CoordinateTransformer:
    """Forward/inverse transformer bound to one immutable Run context."""

    def __init__(self, context: SpatialContext) -> None:
        validate_spatial_context(context)
        self.context = context
        CRS, Proj, _Transformer = _pyproj_types()
        self._processing = CRS.from_epsg(context.processing_srid)
        self._projection = Proj(self._processing)
        self._forward, self._inverse = _transformers(
            context.source_srid, context.processing_srid
        )

    def forward(self, lon: float, lat: float) -> tuple[float, float]:
        _validate_lonlat_domain((lon, lat, lon, lat))
        x, y = self._forward.transform(lon, lat)
        if not (math.isfinite(float(x)) and math.isfinite(float(y))):
            raise ProjectionError("coordinate could not be transformed into processing_srid")
        return float(x), float(y)

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        lon, lat = self._inverse.transform(x, y)
        if not (math.isfinite(float(lon)) and math.isfinite(float(lat))):
            raise ProjectionError("coordinate could not be transformed into source_srid")
        return float(lon), float(lat)

    def inverse_bounds(
        self,
        bounds: tuple[float, float, float, float],
        *,
        densify_pts: int = 64,
    ) -> tuple[float, float, float, float]:
        """Return a conservative 4326 bbox for one processing-CRS rectangle.

        Densifying the four edges before transforming preserves the existing
        4326 GiST prefilter even when projected grid edges are curved in
        longitude/latitude. The processing-CRS predicate remains the exact
        authority for window membership.
        """
        x_min, y_min, x_max, y_max = bounds
        if not all(math.isfinite(value) for value in bounds):
            raise ProjectionError("processing bounds must be finite")
        if x_min > x_max or y_min > y_max:
            raise ProjectionError("processing bounds must be ordered")
        if densify_pts < 2:
            raise ProjectionError("densify_pts must be at least 2")
        west, south, east, north = self._inverse.transform_bounds(
            x_min,
            y_min,
            x_max,
            y_max,
            densify_pts=densify_pts,
        )
        source_bounds = tuple(float(value) for value in (west, south, east, north))
        if not all(math.isfinite(value) for value in source_bounds):
            raise ProjectionError("processing bounds could not be transformed to source_srid")
        # A single ST_MakeEnvelope cannot represent an antimeridian split. A
        # world-width prefilter is conservative; the projected predicate still
        # supplies exact membership.
        if west > east:
            west, east = -180.0, 180.0
        west = max(-180.0, math.nextafter(float(west), -math.inf))
        south = max(-90.0, math.nextafter(float(south), -math.inf))
        east = min(180.0, math.nextafter(float(east), math.inf))
        north = min(90.0, math.nextafter(float(north), math.inf))
        return west, south, east, north

    def heading_vector(
        self,
        lon: float,
        lat: float,
        heading_deg: float,
    ) -> tuple[float, float]:
        """Map a true-north compass heading to a unit vector in processing CRS."""
        _validate_lonlat_domain((lon, lat, lon, lat))
        next_lon, next_lat, _back_azimuth = _wgs84_geod().fwd(
            lon,
            lat,
            heading_deg,
            1.0,
        )
        x0, y0 = self._forward.transform(lon, lat)
        x1, y1 = self._forward.transform(next_lon, next_lat)
        dx = float(x1) - float(x0)
        dy = float(y1) - float(y0)
        length = math.hypot(dx, dy)
        if not math.isfinite(length) or length <= 0.0:
            raise ProjectionError("heading could not be mapped into processing_srid")
        return dx / length, dy / length

    def forward_many(
        self,
        points: Iterable[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        values = list(points)
        if not values:
            return []
        lons = [value[0] for value in values]
        lats = [value[1] for value in values]
        self.validate_bounds((min(lons), min(lats), max(lons), max(lats)))
        xs, ys = self._forward.transform(lons, lats)
        projected = [(float(x), float(y)) for x, y in zip(xs, ys)]
        if any(not (math.isfinite(x) and math.isfinite(y)) for x, y in projected):
            raise ProjectionError("coordinates could not be transformed into processing_srid")
        return projected

    def validate_bounds(self, bounds: tuple[float, float, float, float]) -> None:
        """Validate area-of-use and scale distortion at bounds corners and centre."""
        lon_min, lat_min, lon_max, lat_max = bounds
        _validate_lonlat_domain(bounds)
        area = self._processing.area_of_use
        if _enforce_area_of_use(self.context) and area is not None:
            if (
                not _longitude_bounds_within_area(
                    lon_min,
                    lon_max,
                    west=float(area.west),
                    east=float(area.east),
                )
                or lat_min < float(area.south)
                or lat_max > float(area.north)
            ):
                raise ProjectionError(
                    f"data bounds fall outside EPSG:{self.context.processing_srid} "
                    f"area of use ({area.west}, {area.south}, {area.east}, {area.north})"
                )

        centre_lon = (lon_min + lon_max) / 2.0
        centre_lat = (lat_min + lat_max) / 2.0
        samples = {
            (lon_min, lat_min), (lon_min, lat_max),
            (lon_max, lat_min), (lon_max, lat_max),
            (centre_lon, centre_lat),
        }
        allowed = _max_scale_error(self.context)
        for lon, lat in samples:
            try:
                factors = self._projection.get_factors(lon, lat)
                scales = (
                    float(factors.meridional_scale),
                    float(factors.parallel_scale),
                    float(factors.tissot_semimajor),
                    float(factors.tissot_semiminor),
                )
            except Exception as exc:
                raise ProjectionError("could not evaluate processing CRS scale distortion") from exc
            if any(not math.isfinite(scale) or abs(scale - 1.0) > allowed for scale in scales):
                raise ProjectionError(
                    f"EPSG:{self.context.processing_srid} scale distortion exceeds "
                    f"configured max_scale_error={allowed:g} at ({lon}, {lat})"
                )


def _enforce_area_of_use(context: SpatialContext) -> bool:
    return bool(context.projection_config.get("enforce_area_of_use", True))


def _max_scale_error(context: SpatialContext) -> float:
    return float(context.projection_config.get("max_scale_error", 0.01))


def _longitude_bounds_within_area(
    lon_min: float,
    lon_max: float,
    *,
    west: float,
    east: float,
) -> bool:
    """Return whether an ordered longitude interval fits an area-of-use bbox.

    EPSG represents an area that crosses the antimeridian with ``west > east``.
    A conventional ordered input interval must then fit wholly on either side
    of the split; treating ``west`` and ``east`` as ordinary minimum/maximum
    values would reject every valid point in that CRS.
    """
    if west <= east:
        return west <= lon_min and lon_max <= east
    return (
        west <= lon_min and lon_max <= 180.0
    ) or (
        -180.0 <= lon_min and lon_max <= east
    )


def _validate_lonlat_domain(bounds: tuple[float, float, float, float]) -> None:
    lon_min, lat_min, lon_max, lat_max = bounds
    if not all(math.isfinite(value) for value in bounds):
        raise ProjectionError("longitude/latitude bounds must be finite")
    if lon_min > lon_max or lat_min > lat_max:
        raise ProjectionError("longitude/latitude bounds must be ordered")
    if lon_min < -180.0 or lon_max > 180.0 or lat_min < -90.0 or lat_max > 90.0:
        raise ProjectionError("longitude/latitude bounds are outside EPSG:4326")


@lru_cache(maxsize=16)
def _transformers(source_srid: int, processing_srid: int):
    _CRS, _Proj, Transformer = _pyproj_types()
    return (
        Transformer.from_crs(source_srid, processing_srid, always_xy=True),
        Transformer.from_crs(processing_srid, source_srid, always_xy=True),
    )


@lru_cache(maxsize=1)
def _pyproj_types():
    try:
        from pyproj import CRS, Proj, Transformer
    except ImportError as exc:  # pragma: no cover - packaging failure guard
        raise RuntimeError("pyproj is required for fixed-CRS pipeline calculations") from exc
    return CRS, Proj, Transformer


@lru_cache(maxsize=1)
def _wgs84_geod():
    try:
        from pyproj import Geod
    except ImportError as exc:  # pragma: no cover - packaging failure guard
        raise RuntimeError("pyproj is required for fixed-CRS pipeline calculations") from exc
    return Geod(ellps="WGS84")
