"""Run-scoped coordinate transforms for metric pipeline calculations."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from .models import ProjectionConfig, SpatialContext


COORDINATE_SEMANTICS_VERSION = 2
QUERY_WINDOW_RULE_VERSION = 1
QUERY_MAX_SCALE_ERROR = 0.015


class ProjectionError(ValueError):
    """Raised when a Run's coordinate context is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class QueryWindowRequest:
    """Geographic centre plus grid-aligned metric dimensions."""

    center_lon: float
    center_lat: float
    width_m: float
    height_m: float


@dataclass
class SpatialWindow:
    """A processing-CRS rectangle shared by spatial readers and algorithms."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True, slots=True)
class QueryWindow:
    """A validated rectangle in one standard WGS84 / UTM query CRS.

    ``geographic_bounds`` describes the true inverse-projected footprint.
    ``prefilter_bounds`` is the independently outward-rounded Geometry GiST
    envelope and must never be used for zone or three-degree-grid decisions.
    """

    request: QueryWindowRequest
    query_srid: int
    x_min: float
    y_min: float
    x_max: float
    y_max: float
    geographic_bounds: tuple[float, float, float, float]
    prefilter_bounds: tuple[float, float, float, float]
    three_degree_cells: tuple[int, ...]
    rule_version: int = QUERY_WINDOW_RULE_VERSION


def build_query_window(request: QueryWindowRequest) -> QueryWindow:
    """Choose the easternmost entered standard UTM zone and build its window.

    Zone selection is bounded: the centre-zone candidate is evaluated, then a
    changed eastern-zone candidate is rebuilt once and must be self-consistent.
    """
    _validate_query_request(request)
    selected = _utm_srid(request.center_lon, request.center_lat)
    visited: set[int] = set()
    for _attempt in range(2):
        if selected in visited:
            raise ProjectionError("query UTM zone selection did not converge")
        visited.add(selected)
        window = _query_window_in_srid(request, selected, validate_final=False)
        required = _easternmost_utm_srid(
            window.geographic_bounds[0],
            window.geographic_bounds[2],
            request.center_lat,
        )
        if required == selected:
            return _query_window_in_srid(request, selected, validate_final=True)
        selected = required
    raise ProjectionError("query UTM zone selection has no self-consistent solution")


def query_window_from_processing_bounds(
    bounds: tuple[float, float, float, float],
    run_context: SpatialContext,
) -> QueryWindow:
    """Explicitly adapt an old processing-CRS rectangle for SQL querying.

    The original numeric width and height are retained, while its geographic
    centre is reprojected and the footprint is rebuilt in the selected query
    UTM grid. Consequently the geographic outline is intentionally different.
    """
    if not all(math.isfinite(value) for value in bounds):
        raise ProjectionError("processing bounds must be finite")
    x_min, y_min, x_max, y_max = bounds
    if x_min >= x_max or y_min >= y_max:
        raise ProjectionError("processing query bounds must have positive size")
    transformer = CoordinateTransformer(run_context)
    lon, lat = transformer.inverse((x_min + x_max) / 2.0, (y_min + y_max) / 2.0)
    return build_query_window(QueryWindowRequest(lon, lat, x_max - x_min, y_max - y_min))


def create_spatial_context(
    config: ProjectionConfig,
    *,
    processing_srid: int | None = None,
) -> SpatialContext:
    """Validate a configured CRS pair and return its persisted Run context."""
    selected = config.processing_srid if processing_srid is None else processing_srid
    if selected is None:
        raise ProjectionError(
            "processing_srid is required for a new Refine Run; choose a projected "
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
    """Materialize and validate a context inherited from ``refine_runs``."""
    if processing_srid is None:
        raise ProjectionError(
            "Refine Run has no processing_srid; legacy Run coordinates cannot be "
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
        densify_pts: int | None = None,
    ) -> tuple[float, float, float, float]:
        """Return a conservative bbox for a supported standard UTM rectangle.

        This no longer offers a sampling-based conservatism claim. Standard
        transverse Mercator is symmetric about its central meridian: over the
        supported, single-hemisphere local domain longitude is monotone with
        easting and latitude is monotone with northing on each side; a
        horizontal edge's only interior latitude extremum is at the central
        meridian. Thus the four corners plus both horizontal-edge crossings of
        false easting 500000 contain every boundary extremum. IEEE outward
        rounding then protects the SQL envelope from final-operation rounding.

        Other projected CRSs are explicitly rejected because no corresponding
        extremum proof is implemented. ``densify_pts`` remains only to produce
        a clear migration error for old callers.
        """
        if densify_pts is not None:
            raise ProjectionError("densify_pts is unsupported; conservative UTM bounds are analytic")
        return _utm_inverse_bounds(self.context.processing_srid, bounds)

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


def _validate_query_request(request: QueryWindowRequest) -> None:
    values = (
        request.center_lon, request.center_lat, request.width_m, request.height_m,
    )
    if not all(math.isfinite(value) for value in values):
        raise ProjectionError("query window values must be finite")
    _validate_lonlat_domain((
        request.center_lon, request.center_lat,
        request.center_lon, request.center_lat,
    ))
    if request.width_m <= 0.0 or request.height_m <= 0.0:
        raise ProjectionError("query window width and height must be positive")
    if not -80.0 <= request.center_lat <= 84.0:
        raise ProjectionError("standard UTM query windows require latitude in [-80, 84]")
    # The implementation deliberately excludes antimeridian and hemisphere
    # crossings; neither has a single safe SQL envelope/UTM hemisphere.
    if abs(request.center_lon) >= 179.0:
        raise ProjectionError("query windows near the antimeridian are unsupported")


def _utm_srid(lon: float, lat: float) -> int:
    zone = min(60, max(1, int(math.floor((lon + 180.0) / 6.0)) + 1))
    return (32600 if lat >= 0.0 else 32700) + zone


def _utm_zone(srid: int) -> tuple[int, bool]:
    if 32601 <= srid <= 32660:
        return srid - 32600, True
    if 32701 <= srid <= 32760:
        return srid - 32700, False
    raise ProjectionError(
        "conservative inverse bounds are implemented only for standard "
        "WGS84 / UTM EPSG:32601..32660 and EPSG:32701..32760"
    )


def _easternmost_utm_srid(west: float, east: float, latitude: float) -> int:
    if west <= -180.0 or east >= 180.0:
        raise ProjectionError("query window reaches the antimeridian")
    # ceil(...)-1 is stable at exact boundaries; nextafter before adding 180
    # can be rounded back onto the boundary at this magnitude.
    zone = min(60, max(1, int(math.ceil((east + 180.0) / 6.0))))
    return (32600 if latitude >= 0.0 else 32700) + zone


def _entered_three_degree_cells(
    west: float,
    east: float,
    *,
    enforce_limit: bool = True,
) -> tuple[int, ...]:
    first = math.floor((west + 1.5) / 3.0)
    last = math.ceil((east + 1.5) / 3.0) - 1
    cells = tuple(range(first, last + 1))
    if enforce_limit and (
        not cells
        or len(cells) > 2
        or (len(cells) == 2 and cells[1] != cells[0] + 1)
    ):
        raise ProjectionError("query window may enter at most two adjacent three-degree grid cells")
    return cells


def _query_window_in_srid(
    request: QueryWindowRequest,
    srid: int,
    *,
    validate_final: bool,
) -> QueryWindow:
    zone, north = _utm_zone(srid)
    if north != (request.center_lat >= 0.0):
        raise ProjectionError("query window cannot cross UTM hemispheres")
    context = SpatialContext(
        source_srid=4326,
        processing_srid=srid,
        projection_config={
            "coordinate_semantics_version": COORDINATE_SEMANTICS_VERSION,
            "enforce_area_of_use": False,
            "max_scale_error": QUERY_MAX_SCALE_ERROR,
        },
    )
    transformer = CoordinateTransformer(context)
    center_x, center_y = transformer.forward(request.center_lon, request.center_lat)
    half_width = request.width_m / 2.0
    half_height = request.height_m / 2.0
    bounds = (
        center_x - half_width, center_y - half_height,
        center_x + half_width, center_y + half_height,
    )
    geographic, prefilter = _utm_inverse_bounds_pair(srid, bounds)
    west, south, east, north_lat = geographic
    if west <= -180.0 or east >= 180.0:
        raise ProjectionError("query window reaches the antimeridian")
    if south < -80.0 or north_lat > 84.0:
        raise ProjectionError("query footprint falls outside the standard UTM latitude domain")
    if south < 0.0 < north_lat:
        raise ProjectionError("query window cannot cross UTM hemispheres")
    cells = _entered_three_degree_cells(
        west, east, enforce_limit=validate_final,
    )
    if validate_final:
        _validate_query_scale(srid, geographic)
    return QueryWindow(
        request=request,
        query_srid=srid,
        x_min=bounds[0], y_min=bounds[1], x_max=bounds[2], y_max=bounds[3],
        geographic_bounds=geographic,
        prefilter_bounds=prefilter,
        three_degree_cells=cells,
    )


def _utm_inverse_bounds_pair(
    srid: int,
    bounds: tuple[float, float, float, float],
) -> tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]:
    zone, utm_north = _utm_zone(srid)
    if not all(math.isfinite(value) for value in bounds):
        raise ProjectionError("processing bounds must be finite")
    x_min, y_min, x_max, y_max = bounds
    if x_min > x_max or y_min > y_max:
        raise ProjectionError("processing bounds must be ordered")
    # Bound the inverse-TM branch to the local, single-wrap UTM sheet on which
    # the monotonic/extremum argument applies. Standard zone use plus the query
    # policy's adjacent-grid reach is comfortably inside these limits.
    if x_min < -1_000_000.0 or x_max > 2_000_000.0 or y_min < 0.0 or y_max > 10_000_000.0:
        raise ProjectionError("UTM rectangle exceeds the supported local projected domain")
    _CRS, _Proj, Transformer = _pyproj_types()
    inverse = Transformer.from_crs(srid, 4326, always_xy=True)
    samples = [(x, y) for x in (x_min, x_max) for y in (y_min, y_max)]
    if x_min <= 500_000.0 <= x_max:
        samples.extend(((500_000.0, y_min), (500_000.0, y_max)))
    transformed = [inverse.transform(x, y) for x, y in samples]
    if any(not (math.isfinite(float(lon)) and math.isfinite(float(lat))) for lon, lat in transformed):
        raise ProjectionError("UTM bounds could not be transformed to EPSG:4326")
    lons = [float(lon) for lon, _lat in transformed]
    lats = [float(lat) for _lon, lat in transformed]
    central_meridian = zone * 6.0 - 183.0
    longitude_offsets = [
        (lon - central_meridian + 180.0) % 360.0 - 180.0 for lon in lons
    ]
    if any(abs(offset) > 15.0 for offset in longitude_offsets):
        raise ProjectionError("UTM rectangle exceeds the supported central-meridian domain")
    if max(lons) - min(lons) > 30.0:
        raise ProjectionError(
            "UTM rectangle crosses the antimeridian or exceeds the supported local domain"
        )
    geographic = (min(lons), min(lats), max(lons), max(lats))
    if geographic[1] < -80.0 or geographic[3] > 84.0:
        raise ProjectionError("UTM rectangle falls outside the supported latitude domain")
    if (utm_north and geographic[1] < 0.0) or (
        not utm_north and geographic[3] > 0.0
    ):
        raise ProjectionError("UTM rectangle crosses or uses the wrong hemisphere")
    # Two ulps cover min/max selection and the downstream text/binary parameter
    # conversion without using an arbitrary geographic epsilon.
    west, south, east, north = geographic
    for _ in range(2):
        west = math.nextafter(west, -math.inf)
        south = math.nextafter(south, -math.inf)
        east = math.nextafter(east, math.inf)
        north = math.nextafter(north, math.inf)
    return geographic, (west, south, east, north)


def _utm_inverse_bounds(
    srid: int,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return _utm_inverse_bounds_pair(srid, bounds)[1]


def _validate_query_scale(
    srid: int,
    bounds: tuple[float, float, float, float],
) -> None:
    _CRS, Proj, _Transformer = _pyproj_types()
    projection = Proj(f"EPSG:{srid}")
    west, south, east, north = bounds
    # For UTM, scale magnitude grows with absolute distance from the central
    # meridian and is largest toward the equator. These combinations therefore
    # include the extrema over the rectangular geographic enclosure.
    latitudes = {south, north}
    if south <= 0.0 <= north:
        latitudes.add(0.0)
    else:
        latitudes.add(south if abs(south) < abs(north) else north)
    for lon in (west, east):
        for lat in latitudes:
            factors = projection.get_factors(lon, lat)
            scales = (
                float(factors.meridional_scale), float(factors.parallel_scale),
                float(factors.tissot_semimajor), float(factors.tissot_semiminor),
            )
            if any(
                not math.isfinite(scale)
                or abs(scale - 1.0) > QUERY_MAX_SCALE_ERROR
                for scale in scales
            ):
                raise ProjectionError(
                    f"EPSG:{srid} query scale distortion exceeds "
                    f"max_scale_error={QUERY_MAX_SCALE_ERROR:g}"
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
