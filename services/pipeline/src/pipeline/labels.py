"""Validated vector labels, independent snapshots, and a server-free SVG preview.

Geometry used by the scorer is always in the configured projected metre CRS.
GeoJSON exports are transformed to WGS84; GeoParquet retains the processing CRS.
Original vector attributes are retained in ``properties_json`` without relying on
the source file for later inspection. Source files are never modified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import html
import json
import math
from pathlib import Path
import tempfile
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pyogrio
from pyogrio.raw import read as read_vector
from pyproj import CRS, Transformer
from shapely import from_wkb, transform
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree


@dataclass(frozen=True)
class Region:
    id: str
    geometry: BaseGeometry
    split: str = "tune"
    reviewed_complete: bool = False
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.geometry.bounds


@dataclass
class LabelSet:
    crs: str
    intersections: list[dict[str, Any]]
    centerlines: list[dict[str, Any]]
    regions: list[Region]
    metadata: dict[str, Any]
    identity: str


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def vector_fingerprint(path: Path) -> dict[str, Any]:
    """Include SHP geometry, attributes, indexes, CRS, and encoding sidecars."""
    if not path.is_file():
        raise ValueError(f"label file does not exist: {path}")
    if path.suffix.lower() == ".shp":
        allowed = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx"}
        files = sorted(p for p in path.parent.iterdir()
                       if p.stem == path.stem and p.suffix.lower() in allowed)
        extensions = {p.suffix.lower() for p in files}
        if not {".shp", ".shx", ".dbf"} <= extensions:
            raise ValueError("Shapefile import requires .shp, .shx, and .dbf sidecars")
    else:
        files = [path]
        if path.suffix.lower() == ".gpkg" and Path(str(path) + "-wal").exists():
            raise ValueError("Close/checkpoint the GeoPackage writer before importing labels")
    return {"path": str(path), "files": [{"name": p.name, "size": p.stat().st_size,
                                           "sha256": _sha256(p)} for p in files]}


def list_layers(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f"label file does not exist: {path}")
    result = []
    for name, geometry_type in pyogrio.list_layers(path):
        info = pyogrio.read_info(path, layer=str(name))
        result.append({"layer": str(name), "geometry_type": geometry_type,
                       "crs": info["crs"], "count": int(info["features"]),
                       "fields": list(info["fields"]),
                       "fid_column": info.get("fid_column", "")})
    return result


def _scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_scalar(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _scalar(item) for key, item in value.items()}
    return str(value)


def metric_crs(value: str | int) -> CRS:
    crs = CRS.from_user_input(value)
    if not crs.is_projected or len(crs.axis_info) < 2 or any(
        not math.isclose(axis.unit_conversion_factor, 1.0) for axis in crs.axis_info[:2]
    ):
        raise ValueError("processing_crs must be a projected CRS whose XY units are metres")
    return crs


def _project(geometry: BaseGeometry, projection: Any) -> BaseGeometry:
    # ndarray interface works with Shapely 2.0 as well as later versions.
    return transform(geometry, lambda coordinates: np.column_stack(
        projection(coordinates[:, 0], coordinates[:, 1])))


def read_layer(settings: dict[str, Any], *, target_crs: str | int,
               base_dir: str | Path, kind: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read local GPKG, SHP or GeoJSON; explicit IDs and CRS are validated."""
    if kind not in {"intersections", "centerlines", "regions"}:
        raise ValueError(f"unsupported label kind: {kind}")
    if not isinstance(settings, dict) or not settings.get("path"):
        raise ValueError(f"labels.{kind}.path is required")
    path = Path(settings["path"]).expanduser()
    path = (Path(base_dir) / path).resolve() if not path.is_absolute() else path.resolve()
    if path.suffix.lower() not in {".gpkg", ".shp", ".geojson", ".json"}:
        raise ValueError("labels must use GPKG, SHP, or GeoJSON")
    fingerprint = vector_fingerprint(path)
    layer = settings.get("layer") or None
    if path.suffix.lower() == ".gpkg" and not layer:
        available = [item["layer"] for item in list_layers(path)]
        if len(available) != 1:
            raise ValueError(f"labels.{kind}.layer is required; available layers: {available}")
        layer = available[0]
    meta, fids, geometries, arrays = read_vector(path, layer=layer, return_fids=True,
                                                force_2d=True, datetime_as_string=True)
    if not meta.get("crs"):
        raise ValueError(f"labels.{kind} has no CRS; define it in the source file before import")
    target = metric_crs(target_crs)
    projection = Transformer.from_crs(meta["crs"], target, always_xy=True).transform
    fields = [str(name) for name in meta["fields"]]
    id_field = settings.get("id_field", "fid")
    if id_field not in fields and id_field not in {"fid", "__fid__"}:
        raise ValueError(f"labels.{kind} ID field {id_field!r} does not exist")
    permitted = {"intersections": {"Point", "Polygon", "MultiPolygon"},
                 "centerlines": {"LineString", "MultiLineString"},
                 "regions": {"Polygon", "MultiPolygon"}}[kind]
    features, seen = [], set()
    if geometries is None:
        raise ValueError(f"labels.{kind} layer has no geometry")
    for row, encoded in enumerate(geometries):
        properties = {name: _scalar(values[row]) for name, values in zip(fields, arrays)}
        identifier = properties.get(id_field) if id_field in fields else int(fids[row])
        if identifier is None or str(identifier) in seen:
            raise ValueError(f"labels.{kind} IDs must be non-null and unique")
        identifier = str(identifier)
        seen.add(identifier)
        if encoded is None:
            raise ValueError(f"labels.{kind} {identifier}: missing geometry")
        geometry = _project(from_wkb(encoded), projection)
        if (geometry.geom_type not in permitted or geometry.is_empty or not geometry.is_valid
                or not all(math.isfinite(v) for v in geometry.bounds)):
            raise ValueError(f"labels.{kind} {identifier}: invalid {geometry.geom_type} geometry")
        if kind == "centerlines" and geometry.length <= 0:
            raise ValueError(f"labels.centerlines {identifier}: zero-length geometry")
        features.append({"type": "Feature", "id": identifier, "geometry": mapping(geometry),
                         "properties": properties})
    features.sort(key=lambda item: item["id"])
    # Catch writes during import, including attribute-only SHP edits.
    if vector_fingerprint(path) != fingerprint:
        raise ValueError(f"label source changed while it was being imported: {path}")
    return features, {**fingerprint, "layer": layer, "id_field": id_field,
                      "source_crs": meta["crs"], "processing_crs": target.to_string(),
                      "count": len(features), "fields": fields,
                      "gdal_version": pyogrio.__gdal_version_string__}


def _reviewed(value: Any) -> bool:
    # SHP has no consistent Boolean representation across writers.
    if value is True or value == 1 or isinstance(value, str) and value.lower() in {"true", "1", "yes"}:
        return True
    if value is False or value is None or value == 0 or isinstance(value, str) and value.lower() in {"false", "0", "no", ""}:
        return False
    raise ValueError("reviewed_complete must be Boolean or 0/1")


def validate_labels_settings(settings: dict[str, Any]) -> None:
    if not isinstance(settings, dict):
        raise ValueError("labels must be a table")
    allowed = {"intersections", "centerlines", "regions", "reviewed_bbox", "reviewed_complete"}
    if set(settings) - allowed:
        raise ValueError(f"unknown labels keys: {sorted(set(settings) - allowed)}")
    if "reviewed_complete" in settings and type(settings["reviewed_complete"]) is not bool:
        raise ValueError("labels.reviewed_complete must be Boolean")
    for kind in ("intersections", "centerlines", "regions"):
        if kind not in settings:
            continue
        table = settings[kind]
        if not isinstance(table, dict):
            raise ValueError(f"labels.{kind} must be a table")
        fields = {"path", "layer", "id_field"}
        if kind == "regions":
            fields |= {"split_field", "reviewed_field", "reviewed_complete"}
        if set(table) - fields:
            raise ValueError(f"unknown labels.{kind} keys: {sorted(set(table) - fields)}")
        for key, value in table.items():
            if key == "reviewed_complete":
                if type(value) is not bool:
                    raise ValueError(f"labels.{kind}.reviewed_complete must be Boolean")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"labels.{kind}.{key} must be a nonempty string")
    if settings.get("regions", {}).get("path") and "reviewed_bbox" in settings:
        raise ValueError("use labels.regions or labels.reviewed_bbox, not both")


def load_labels(settings: dict[str, Any], target_crs: str | int,
                base_dir: str | Path = ".") -> LabelSet:
    validate_labels_settings(settings)
    target = metric_crs(target_crs)
    features: dict[str, list[dict[str, Any]]] = {"intersections": [], "centerlines": []}
    metadata: dict[str, Any] = {"format_version": 1, "processing_crs": target.to_string(),
                              "layers": {}, "warnings": []}
    for kind in features:
        if settings.get(kind, {}).get("path"):
            features[kind], metadata["layers"][kind] = read_layer(
                settings[kind], target_crs=target, base_dir=base_dir, kind=kind)
    if not metadata["layers"]:
        raise ValueError("At least one labels.intersections or labels.centerlines source is required")
    regions = []
    region_settings = settings.get("regions", {})
    if region_settings.get("path"):
        rows, metadata["layers"]["regions"] = read_layer(
            region_settings, target_crs=target, base_dir=base_dir, kind="regions")
        split_field = region_settings.get("split_field", "split")
        reviewed_field = region_settings.get("reviewed_field", "reviewed")
        for row in rows:
            props = row["properties"]
            split = str(props.get(split_field, "tune"))
            if split not in {"tune", "holdout"}:
                raise ValueError(f"region {row['id']}: split must be tune or holdout")
            declared = props.get(reviewed_field, region_settings.get("reviewed_complete", False))
            regions.append(Region(row["id"], shape(row["geometry"]), split,
                                  _reviewed(declared), props))
    elif settings.get("reviewed_bbox") is not None:
        bounds = settings["reviewed_bbox"]
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) for v in bounds)
                or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
            raise ValueError("labels.reviewed_bbox must be [xmin,ymin,xmax,ymax] in processing CRS")
        regions = [Region("reviewed-bbox", box(*bounds), "tune",
                          _reviewed(settings.get("reviewed_complete", False)), {})]
        metadata["reviewed_bbox"] = list(bounds)
        metadata["reviewed_complete"] = regions[0].reviewed_complete
    else:
        metadata["warnings"].append("No reviewed evaluation region configured; preview only, tuning unavailable")
    regions.sort(key=lambda item: item.id)
    # Shared boundaries are accepted; feature ownership below is deterministic.
    if regions:
        tree = STRtree([region.geometry for region in regions])
        for index, region in enumerate(regions):
            for other in tree.query(region.geometry, predicate="intersects"):
                if int(other) > index and region.geometry.intersection(regions[int(other)].geometry).area > 0:
                    raise ValueError(f"evaluation regions overlap: {region.id}, {regions[int(other)].id}")
        owned = {row["id"] for region in regions for row in intersection_truths(
            features["intersections"], region, regions)}
        ignored = len(features["intersections"]) - len(owned)
        if ignored:
            metadata["warnings"].append(f"{ignored} intersection labels lie outside evaluation regions")
    if not any(region.split == "holdout" for region in regions):
        metadata["warnings"].append("No holdout region configured; independent generalization score unavailable")
    identity = canonical_hash({"settings": settings, "metadata": metadata,
                               "crs": target.to_wkt(), "format_version": 1})
    return LabelSet(target.to_string(), features["intersections"], features["centerlines"],
                    regions, metadata, identity)


def owns_point(region: Region, point: BaseGeometry, all_regions: list[Region]) -> bool:
    if not region.geometry.covers(point):
        return False
    # Lowest ID owns shared boundaries, preventing duplicate micro counts.
    return not any(other.id < region.id and other.geometry.covers(point) for other in all_regions)


def intersection_truths(features: list[dict[str, Any]], region: Region,
                        all_regions: list[Region]) -> list[dict[str, Any]]:
    return [feature for feature in features
            if owns_point(region, shape(feature["geometry"]).centroid, all_regions)]


def _geoparquet(path: Path, features: list[dict[str, Any]], crs: str) -> None:
    rows = [{"id": str(row["id"]), "geometry": shape(row["geometry"]).wkb,
             "properties_json": json.dumps(row.get("properties", {}), ensure_ascii=False,
                                           sort_keys=True, allow_nan=False)} for row in features]
    schema = pa.schema([("id", pa.string()), ("geometry", pa.binary()),
                        ("properties_json", pa.string())])
    geometries = sorted({shape(row["geometry"]).geom_type for row in features})
    geo = {"version": "1.1.0", "primary_column": "geometry", "columns": {
        "geometry": {"encoding": "WKB", "geometry_types": geometries,
                     "crs": CRS.from_user_input(crs).to_json_dict()}}}
    schema = schema.with_metadata({b"geo": json.dumps(geo).encode()})
    temporary = path.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), temporary, compression="zstd")
    temporary.replace(path)


def prepare_labels(labels: LabelSet, output_dir: str | Path) -> dict[str, Any]:
    """Persist independently readable label snapshots and a static SVG preview."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    regions = [{"type": "Feature", "id": region.id, "geometry": mapping(region.geometry),
                "properties": {**region.properties, "split": region.split,
                               "reviewed_complete": region.reviewed_complete}} for region in labels.regions]
    to_wgs84 = Transformer.from_crs(labels.crs, 4326, always_xy=True).transform
    outputs = {}
    for name, features in {"intersections": labels.intersections, "centerlines": labels.centerlines,
                           "regions": regions}.items():
        _geoparquet(root / f"{name}.parquet", features, labels.crs)
        geographic = [{**row, "geometry": mapping(_project(shape(row["geometry"]), to_wgs84))}
                      for row in features]
        write_json(root / f"{name}.geojson", {"type": "FeatureCollection", "features": geographic})
        outputs[name] = {"count": len(features), "parquet": str(root / f"{name}.parquet"),
                         "geojson": str(root / f"{name}.geojson")}
    preview = root / "labels-preview.svg"
    preview.write_text(render_preview(labels), encoding="utf-8")
    manifest = {"identity": labels.identity, **labels.metadata, "outputs": outputs,
                "preview": str(preview)}
    write_json(root / "labels.json", manifest)
    return manifest


def render_preview(labels: LabelSet) -> str:
    """Small, dependency-free view; full resolution data is in the snapshots."""
    groups = [("regions", [region.geometry for region in labels.regions]),
              ("centerlines", [shape(row["geometry"]) for row in labels.centerlines]),
              ("intersections", [shape(row["geometry"]) for row in labels.intersections])]
    geometries = [geometry for _, items in groups for geometry in items]
    if geometries:
        x0 = min(geometry.bounds[0] for geometry in geometries)
        y0 = min(geometry.bounds[1] for geometry in geometries)
        x1 = max(geometry.bounds[2] for geometry in geometries)
        y1 = max(geometry.bounds[3] for geometry in geometries)
    else:
        x0 = y0 = 0.0
        x1 = y1 = 1.0
    scale = min(1040.0 / max(x1 - x0, 1.0), 650.0 / max(y1 - y0, 1.0))

    def xy(x: float, y: float) -> str:
        return f"{60 + (x - x0) * scale:.3f},{760 - (y - y0) * scale:.3f}"

    def draw(geometry: BaseGeometry, kind: str) -> str:
        if geometry.geom_type.startswith("Multi") or geometry.geom_type == "GeometryCollection":
            return "".join(draw(part, kind) for part in geometry.geoms)
        if geometry.geom_type == "Point":
            x, y = xy(geometry.x, geometry.y).split(",")
            return f'<circle cx="{x}" cy="{y}" r="4"/>'
        if geometry.geom_type == "Polygon":
            rings = [geometry.exterior, *geometry.interiors]
            paths = ["M " + " L ".join(xy(x, y) for x, y, *_ in ring.coords) + " Z" for ring in rings]
            return f'<path d="{" ".join(paths)}" fill-rule="evenodd"/>'
        return f'<polyline points="{" ".join(xy(x, y) for x, y, *_ in geometry.coords)}"/>'

    shapes = "".join(f'<g class="{kind}">' + "".join(draw(geometry, kind) for geometry in items) + "</g>"
                     for kind, items in groups)
    crs = html.escape(labels.crs)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="1160" height="840" viewBox="0 0 1160 840">'
            '<style>text{font-family:system-ui,sans-serif;fill:#173247}.regions{fill:#e4edf3;fill-opacity:.5;'
            'stroke:#678495;stroke-width:1.5;stroke-dasharray:6 4}.centerlines{fill:none;stroke:#187cbe;'
            'stroke-width:2.5}.intersections{fill:#ee7c3d;fill-opacity:.7;stroke:#b8511b;stroke-width:1.2}</style>'
            '<rect width="1160" height="840" fill="#fbfcfe"/>'
            '<text x="40" y="40" font-size="24">Roadnet label preview</text>'
            f'<text x="40" y="68" font-size="14">{crs} · intersections {len(labels.intersections)} · '
            f'centerlines {len(labels.centerlines)} · regions {len(labels.regions)}</text>'
            + shapes + '<text x="40" y="813" font-size="13">Orange: intersection labels · blue: '
            'centerlines · dashed: evaluation regions. Distances use the configured projected CRS.</text></svg>')
