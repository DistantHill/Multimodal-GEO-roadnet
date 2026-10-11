"""Freeze nested, stratified label subsets and complete local evaluation inputs."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any

from pyproj import CRS, Transformer
from shapely import transform
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from .labels import load_labels

LOGGER = logging.getLogger(__name__)
MANIFEST_VERSION = "intersection-local-inputs-v1"
BUFFER_METRES = 200.0
AXES = ("type", "space", "structure", "composite")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _quotas(counts: Counter, size: int) -> dict[str, int]:
    """Capped largest remainder, reserving one per stratum when possible."""
    keys = sorted(counts)
    allocation = {key: int(size >= len(keys)) for key in keys}
    remaining = size - sum(allocation.values())
    capacity = {key: counts[key] - allocation[key] for key in keys}
    total = sum(capacity.values())
    if remaining and total:
        ideal = {key: remaining * capacity[key] / total for key in keys}
        for key in keys:
            allocation[key] += math.floor(ideal[key])
        left = size - sum(allocation.values())
        for key in sorted(keys, key=lambda key: (-(ideal[key] % 1), key))[:left]:
            allocation[key] += 1
    return allocation


def _strata(features: list[dict], projected: dict) -> tuple[dict, dict]:
    centers = {key: geometry.centroid for key, geometry in projected.items()}
    xs, ys = [p.x for p in centers.values()], [p.y for p in centers.values()]
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    areas = sorted(geometry.area for geometry in projected.values())
    cutoffs = [areas[(len(areas) - 1) // 3], areas[2 * (len(areas) - 1) // 3]]
    records = {}
    for feature in features:
        key = feature["id"]
        geometry, center = projected[key], centers[key]
        def bin_coordinate(value, low, high):
            return min(2, int(3 * (value - low) / (high - low))) if high > low else 0
        spatial = f"{bin_coordinate(center.x, bounds[0], bounds[2])}:{bin_coordinate(center.y, bounds[1], bounds[3])}"
        polygons = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
        holes = sum(len(p.interiors) for p in polygons)
        area_bin = sum(geometry.area > cutoff for cutoff in cutoffs)
        structure = f"area:{area_bin}|parts:{len(polygons)}|holes:{holes}"
        label_type = feature["properties"]["type"]
        records[key] = {"type": label_type, "space": spatial, "structure": structure,
                        "composite": _json([label_type, spatial, structure]),
                        "centroid_m": [center.x, center.y], "area_m2": geometry.area,
                        "part_count": len(polygons), "hole_count": holes}
    return records, {"space": {"method": "projected_centroid_3x3_equal_width_grid", "bounds_m": bounds},
                     "structure": {"method": "observed_area_tertiles_parts_holes", "area_cutoffs_m2": cutoffs},
                     "type": {"method": "source_type_null_unclassified_no_inference", "values": sorted({r["type"] for r in records.values()})}}


def _extend(selected: list[str], target: int, records: dict, seed: int) -> tuple[list[str], dict]:
    populations = {axis: Counter(row[axis] for row in records.values()) for axis in AXES}
    planned = {axis: _quotas(populations[axis], target) for axis in AXES}
    actual = {axis: Counter(records[key][axis] for key in selected) for axis in AXES}
    selected = list(selected)
    candidates = set(records) - set(selected)
    while len(selected) < target:
        def priority(key):
            row = records[key]
            # Cover marginal types, space and measured structures first. Sparse
            # cross strata share seats; they never trigger oversampling or loss
            # of the previous level's fixed members.
            uncovered = tuple(int(actual[axis][row[axis]] == 0 and planned[axis][row[axis]] > 0)
                              for axis in AXES[:3])
            deficit = sum(max(0, planned[axis][row[axis]] - actual[axis][row[axis]]) /
                          max(1, planned[axis][row[axis]]) for axis in AXES)
            tie = hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()
            return (*uncovered, deficit, tie)
        chosen = max(candidates, key=priority)
        selected.append(chosen)
        candidates.remove(chosen)
        for axis in AXES:
            actual[axis][records[chosen][axis]] += 1
    quotas = {axis: {key: {"population": populations[axis][key], "target": planned[axis][key],
                          "actual": actual[axis][key]} for key in sorted(populations[axis])} for axis in AXES}
    return selected, quotas


def prepare_local_inputs(labels_path: str | Path, output_root: str | Path, *,
                         layer: str = "TP_intersection_fuping", id_field: str = "fid",
                         seed: int = 42, sizes: tuple[int, int] = (20, 50),
                         processing_srid: int = 32649) -> dict:
    """Write one immutable-content manifest and GeoJSON inputs for each level.

    Exact topological equality removes duplicate geometry, irrespective of ring
    ordering; nearby or approximately equal geometries are retained. Truth uses
    complete original faces whose original WGS84 centroid is covered by the ROI.
    """
    if len(sizes) != 2 or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in sizes) or sizes[0] > sizes[1]:
        raise ValueError("sizes must contain two positive, nondecreasing integers")
    crs = CRS.from_epsg(processing_srid)
    if not crs.is_projected or any(axis.unit_conversion_factor != 1 for axis in crs.axis_info):
        raise ValueError("processing CRS must be projected with metre units")
    features, source = load_labels(labels_path, layer=layer, id_field=id_field)
    if not features:
        raise ValueError("label catalog is empty")
    unique, geometries, groups, source_id_map = [], {}, {}, {}
    # Bounds narrow equality candidates; equality remains exact topological,
    # never a rounded hash or equals_exact tolerance.
    by_bounds: dict[tuple, list[str]] = {}
    for feature in features:
        key, geometry = feature["id"], shape(feature["geometry"])
        representative = next((existing for existing in by_bounds.get(geometry.bounds, [])
                               if geometry.equals(geometries[existing])), None)
        if representative is None:
            unique.append(feature)
            geometries[key] = geometry
            groups[key] = [key]
            by_bounds.setdefault(geometry.bounds, []).append(key)
            representative = key
        else:
            groups[representative].append(key)
        source_id_map[key] = representative
    duplicates = [{"representative_id": key, "source_ids": ids} for key, ids in groups.items() if len(ids) > 1]
    for group in duplicates:
        LOGGER.warning("Exact duplicate intersection geometry: retained %s, source IDs %s",
                       group["representative_id"], group["source_ids"])
    forward = Transformer.from_crs(4326, crs, always_xy=True).transform
    backward = Transformer.from_crs(crs, 4326, always_xy=True).transform
    projected = {key: transform(geometry, forward, interleaved=False) for key, geometry in geometries.items()}
    records, definitions = _strata(unique, projected)
    levels, selected = {}, []
    for name, nominal in zip(("L1", "L2", "L3"), (*sizes, len(unique))):
        target = min(nominal, len(unique))
        selected, quotas = _extend(selected, target, records, seed)
        roi = unary_union([geometries[key] for key in selected])
        computation = transform(unary_union([projected[key].buffer(BUFFER_METRES) for key in selected]),
                                backward, interleaved=False)
        truth = [feature for feature in unique if roi.covers(geometries[feature["id"]].centroid)]
        # A highly concave original face can have its centroid outside itself.
        # Silently excluding a sampled target would invalidate its level.
        missing = set(selected) - {feature["id"] for feature in truth}
        if missing:
            raise ValueError(f"sampled label centroids outside evaluation ROI: {sorted(missing)}")
        levels[name] = {"label_ids": list(selected), "nominal_count": nominal, "selected_count": target,
                        "effective_count": len(truth), "truth_ids": [f["id"] for f in truth],
                        "additional_truth_ids": [f["id"] for f in truth if f["id"] not in selected],
                        "evaluation_region": mapping(roi), "computation_region": mapping(computation),
                        "truth": {"type": "FeatureCollection", "features": truth}, "quotas": quotas}
    manifest = {"version": MANIFEST_VERSION, "source": source, "source_count": len(features),
                "effective_label_count": len(unique), "duplicate_groups": duplicates, "source_id_map": source_id_map,
                "seed": seed, "processing_srid": processing_srid, "buffer_m": BUFFER_METRES,
                "stratification": {"definitions": definitions, "features": records,
                    "allocation_policy": "capped_largest_remainder_with_minimum_one_if_seats_allow",
                    "sparse_policy": "retain_nested_members; prioritize_uncovered_type_then_space_then_structure; maximize_normalized_marginal_and_composite_deficit; seeded_hash_tie_break; record_unmet_targets"},
                "levels": levels}
    manifest["sha256"] = _hash(manifest)
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    for name, level in levels.items():
        for field in ("evaluation_region", "computation_region", "truth"):
            path = root / f"{name}_{field}.geojson"
            path.write_text(_json(level[field]) + "\n", encoding="utf-8")
    (root / "local_inputs_manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
    return manifest
