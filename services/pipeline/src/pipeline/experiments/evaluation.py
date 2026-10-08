"""Deterministic maximum-cardinality, minimum-cost intersection matching."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from pyproj import CRS, Transformer
from shapely import transform
from shapely.geometry import shape
from shapely.strtree import STRtree


@dataclass(frozen=True)
class EvaluationRule:
    mode: str
    distance_m: float | None = None
    iou_threshold: float | None = None

    @classmethod
    def parse(cls, raw: dict) -> "EvaluationRule":
        if not isinstance(raw, dict):
            raise ValueError("evaluation rules must be a table")
        try:
            rule = cls(**raw)
        except TypeError as exc:
            raise ValueError("invalid evaluation rule fields") from exc
        if rule.mode not in {"center_distance", "iou", "both"}:
            raise ValueError("evaluation.mode must be center_distance, iou or both")
        if rule.mode in {"center_distance", "both"} and (
            isinstance(rule.distance_m, bool) or not isinstance(rule.distance_m, (float, int))
            or not math.isfinite(rule.distance_m) or rule.distance_m <= 0
        ):
            raise ValueError("distance_m must be a finite positive number")
        if rule.mode in {"iou", "both"} and (
            isinstance(rule.iou_threshold, bool) or not isinstance(rule.iou_threshold, (float, int))
            or not math.isfinite(rule.iou_threshold)
            or not 0 < rule.iou_threshold <= 1
        ):
            raise ValueError("iou_threshold must be in (0, 1]")
        return rule


def validated_features(collection: dict | list, *, truth: bool = False) -> list[dict]:
    if isinstance(collection, dict):
        if collection.get("type") != "FeatureCollection" or not isinstance(collection.get("features"), list):
            raise ValueError("expected GeoJSON FeatureCollection with features array")
        features = collection["features"]
    elif isinstance(collection, list):
        features = collection
    else:
        raise ValueError("expected GeoJSON FeatureCollection or feature list")
    seen = set()
    result = []
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("expected GeoJSON Feature")
        if not isinstance(feature.get("properties", {}), dict) or not isinstance(feature.get("geometry"), dict):
            raise ValueError("feature requires geometry and object properties")
        identifier = feature.get("id", feature.get("properties", {}).get("id"))
        if identifier is None or str(identifier) in seen:
            raise ValueError("features require unique IDs")
        geometry = shape(feature["geometry"])
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError("empty or invalid feature geometry")
        if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("intersection geometry must be Polygon or MultiPolygon")
        west, south, east, north = geometry.bounds
        if not all(math.isfinite(v) for v in geometry.bounds) or not (
            -180 <= west <= east <= 180 and -90 <= south <= north <= 90
        ):
            raise ValueError("features must use WGS84 longitude/latitude")
        seen.add(str(identifier))
        result.append({**feature, "id": str(identifier)})
    return sorted(result, key=lambda item: item["id"])


def _matching(n: int, m: int, edges: list[tuple[int, int, float]]) -> list[tuple[int, int]]:
    # Successive shortest augmenting paths in the residual graph. Every augment
    # increases cardinality; its minimum cost optimizes the secondary objective.
    size, source, sink = n + m + 2, n + m, n + m + 1
    graph: list[list[list]] = [[] for _ in range(size)]
    def add(a, b, cost):
        forward = [b, len(graph[b]), 1, cost]
        reverse = [a, len(graph[a]), 0, -cost]
        graph[a].append(forward)
        graph[b].append(reverse)
        return forward
    for i in range(n):
        add(source, i, 0)
    for j in range(m):
        add(n + j, sink, 0)
    tracked = [(i, j, add(i, n + j, cost)) for i, j, cost in edges]
    while True:
        distance, previous = [float("inf")] * size, [None] * size
        distance[source] = 0
        for _ in range(size - 1):
            changed = False
            for a in range(size):
                if not math.isfinite(distance[a]):
                    continue
                for index, edge in enumerate(graph[a]):
                    b, _, capacity, cost = edge
                    if capacity and distance[a] + cost < distance[b] - 1e-12:
                        distance[b], previous[b] = distance[a] + cost, (a, index)
                        changed = True
            if not changed:
                break
        if previous[sink] is None:
            break
        b = sink
        while b != source:
            a, index = previous[b]
            edge = graph[a][index]
            edge[2] -= 1
            graph[b][edge[1]][2] += 1
            b = a
    return [(i, j) for i, j, edge in tracked if edge[2] == 0]


def evaluate(predictions: dict | list, truths: dict | list, rules: dict) -> dict:
    rule = EvaluationRule.parse(rules)
    predicted = validated_features(predictions)
    truth = validated_features(truths, truth=True)
    all_shapes = [shape(f["geometry"]) for f in predicted + truth]
    lon = sum(g.centroid.x for g in all_shapes) / len(all_shapes) if all_shapes else 0
    lat = sum(g.centroid.y for g in all_shapes) / len(all_shapes) if all_shapes else 0
    crs = CRS.from_proj4(f"+proj=laea +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m")
    projection = Transformer.from_crs(4326, crs, always_xy=True).transform
    p = [transform(shape(f["geometry"]), projection, interleaved=False) for f in predicted]
    t = [transform(shape(f["geometry"]), projection, interleaved=False) for f in truth]
    edges, measures = [], {}
    tree = STRtree([g.centroid for g in t] if rule.mode == "center_distance" else t)
    for i, a in enumerate(p):
        search = a.centroid.buffer(rule.distance_m) if rule.mode == "center_distance" else a
        for j in sorted(int(index) for index in tree.query(search)):
            b = t[j]
            distance = a.centroid.distance(b.centroid)
            union = a.union(b).area
            iou = a.intersection(b).area / union if union else 0.0
            if rule.mode in {"center_distance", "both"} and distance > rule.distance_m:
                continue
            if rule.mode in {"iou", "both"} and iou < rule.iou_threshold:
                continue
            edges.append((i, j, distance if rule.mode == "center_distance" else -iou))
            measures[i, j] = (distance, iou)
    matches = _matching(len(p), len(t), edges)
    used_p, used_t = {i for i, _ in matches}, {j for _, j in matches}
    tp, fp, fn = len(matches), len(p) - len(matches), len(t) - len(matches)
    details = [{"classification": "TP", "prediction_id": predicted[i]["id"],
                "truth_id": truth[j]["id"], "distance_m": measures[i, j][0],
                "iou": measures[i, j][1]} for i, j in matches]
    details.extend({"classification": "FP", "prediction_id": f["id"], "truth_id": None}
                   for i, f in enumerate(predicted) if i not in used_p)
    details.extend({"classification": "FN", "prediction_id": None, "truth_id": f["id"]}
                   for j, f in enumerate(truth) if j not in used_t)
    return {"metrics": {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None},
            "matches": details, "projection": crs.to_string(),
            "truth_version": hashlib.sha256(json.dumps(truth, sort_keys=True).encode()).hexdigest()}
