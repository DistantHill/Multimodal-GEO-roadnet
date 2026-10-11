"""Import polygon GeoPackages through GDAL, retaining curved source features."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pipeline.experiments.evaluation import validated_features

INTERSECTION_TYPES = ("十字路口", "T字路口", "Y字路口", "环岛", "匝道汇入/分流", "复杂交叉口", "其他")
UNCLASSIFIED = "未分类"
ARC_STEP_DEGREES = 1


def load_polygon_file(path: str | Path, *, layer: str | None = None, id_field: str = "fid") -> tuple[list[dict], dict[str, Any]]:
    """Read polygon GeoJSON or GPKG. GDAL linearizes curves at a fixed step.

    Geometry is exported in WGS84 as required by the existing evaluator. Source
    bytes remain unchanged and the conversion settings are provenance metadata.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("polygon file does not exist")
    version = None
    if path.suffix.lower() == ".gpkg":
        executable = shutil.which("ogr2ogr")
        if not executable:
            raise ValueError("GeoPackage import requires GDAL ogr2ogr (gdal-bin)")
        if not isinstance(layer, str) or not layer.strip() or layer.startswith("-"):
            raise ValueError("GeoPackage import requires an explicit layer name")
        version = subprocess.run([executable, "--version"], capture_output=True,
                                 text=True, check=True, timeout=60).stdout.strip()
        command = [executable, "--config", "OGR_ARC_STEPSIZE", str(ARC_STEP_DEGREES),
                   "-f", "GeoJSON", "/vsistdout/", str(path), layer,
                   "-nlt", "CONVERT_TO_LINEAR", "-preserve_fid", "-t_srs", "EPSG:4326"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError(f"GDAL polygon import failed: {result.stderr.strip()}")
        raw = json.loads(result.stdout)
    elif path.suffix.lower() in {".json", ".geojson"}:
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ValueError("polygon input must use .gpkg, .geojson or .json")
    if not isinstance(raw, dict) or not isinstance(raw.get("features"), list):
        raise ValueError("polygon input must contain a FeatureCollection")
    for feature in raw["features"]:
        props = feature.get("properties", {})
        if id_field != "fid":
            if id_field not in props or props[id_field] is None:
                raise ValueError(f"polygon input missing configured ID field {id_field}")
            feature["id"] = str(props[id_field])
        elif feature.get("id") is None and props.get("fid") is not None:
            feature["id"] = str(props["fid"])
    features = validated_features(raw, truth=True)
    columns = sorted({key for feature in features for key in feature.get("properties", {})})
    return features, {"path": str(path), "layer": layer, "id_field": id_field,
                      "crs": "EPSG:4326", "count": len(features), "columns": columns,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "gdal_version": version,
                      "curve_linearization": {"method": "CONVERT_TO_LINEAR", "arc_step_degrees": ARC_STEP_DEGREES} if version else None}


def load_labels(path: str | Path, *, layer: str = "labeled_intersections", id_field: str = "fid") -> tuple[list[dict], dict[str, Any]]:
    features, metadata = load_polygon_file(path, layer=layer, id_field=id_field)
    missing_types = 0
    for feature in features:
        props = feature.setdefault("properties", {})
        value = props.get("type")
        if value is None or value == "":
            props["type"] = UNCLASSIFIED
            missing_types += 1
        elif value == UNCLASSIFIED:
            missing_types += 1
        elif not isinstance(value, str):
            raise ValueError("intersection type must be a string or null")
    return features, {**metadata, "missing_type_count": missing_types,
                      "allowed_types": list(dict.fromkeys([*INTERSECTION_TYPES, UNCLASSIFIED,
                                                           *sorted({f["properties"]["type"] for f in features})])),
                      "warnings": ["Intersection types not yet annotated; type-specific conclusions unavailable"] if missing_types else []}
