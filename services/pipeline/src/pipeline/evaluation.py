"""Independent, region-scoped metrics for the parameter workbench.

Intersection matching maximizes the number of one-to-one matches, then minimizes
their total centre distance. Centerline metrics measure length-weighted geometric
coverage and deliberately make no claim about routing/topological correctness.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import min_weight_full_bipartite_matching
from shapely import STRtree
from shapely.geometry import shape
from shapely.ops import unary_union

from .labels import LabelSet, Region, intersection_truths, owns_point


def _features(value: list[dict] | dict, allowed: set[str]) -> list[dict]:
    if isinstance(value, dict):
        if value.get("type") != "FeatureCollection":
            raise ValueError("predictions must be a FeatureCollection or a feature list")
        value = value.get("features")
    if not isinstance(value, list):
        raise ValueError("predictions must be a feature list")
    seen, result = set(), []
    for feature in value:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("predictions must contain GeoJSON Feature objects")
        identifier = feature.get("id", feature.get("properties", {}).get("id"))
        if identifier is None or str(identifier) in seen:
            raise ValueError("prediction IDs must be non-null and unique")
        seen.add(str(identifier))
        geometry = shape(feature["geometry"])
        if (geometry.geom_type not in allowed or geometry.is_empty or not geometry.is_valid
                or not all(math.isfinite(v) for v in geometry.bounds)):
            raise ValueError(f"prediction {identifier}: invalid {geometry.geom_type} geometry")
        result.append({**feature, "id": str(identifier)})
    return sorted(result, key=lambda row: row["id"])


def point_metrics(tp: int, fp: int, fn: int) -> dict[str, int | float]:
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / (tp + fn) if tp + fn else 0.0,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0}


def match_intersections(predictions: list[dict], truths: list[dict],
                        radius_m: float) -> dict[str, Any]:
    if not math.isfinite(radius_m) or radius_m <= 0:
        raise ValueError("matching_radius_m must be finite and positive")
    allowed = {"Point", "Polygon", "MultiPolygon"}
    predictions, truths = _features(predictions, allowed), _features(truths, allowed)
    predicted = [shape(row["geometry"]).centroid for row in predictions]
    actual = [shape(row["geometry"]).centroid for row in truths]
    n, m = len(predicted), len(actual)
    pairs: list[tuple[int, int, float]] = []
    if n and m:
        tree = STRtree(actual)
        rows, columns, weights = [], [], []
        distances = {}
        for i, point in enumerate(predicted):
            for j in sorted(int(index) for index in tree.query(point, predicate="dwithin", distance=radius_m)):
                distance = float(point.distance(actual[j]))
                rows.append(i)
                columns.append(j)
                # Positive weights are required by the sparse matching routine.
                weights.append(1.0 + distance)
                distances[i, j] = distance
        # Private dummy targets allow every prediction to be assigned. Their
        # cost exceeds the total possible real-edge cost, so cardinality wins.
        penalty = (min(n, m) + 1) * (radius_m + 1.0)
        for i in range(n):
            rows.append(i)
            columns.append(m + i)
            weights.append(penalty)
        graph = coo_matrix((weights, (rows, columns)), shape=(n, m + n)).tocsr()
        assigned_rows, assigned_columns = min_weight_full_bipartite_matching(graph)
        pairs = [(int(i), int(j), distances[int(i), int(j)])
                 for i, j in zip(assigned_rows, assigned_columns) if j < m]
    matched_predictions = {i for i, _, _ in pairs}
    matched_truths = {j for _, j, _ in pairs}
    matches = [{"prediction_id": predictions[i]["id"], "truth_id": truths[j]["id"],
                "classification": "TP", "distance_m": distance} for i, j, distance in pairs]
    matches += [{"prediction_id": row["id"], "truth_id": None, "classification": "FP"}
                for i, row in enumerate(predictions) if i not in matched_predictions]
    matches += [{"prediction_id": None, "truth_id": row["id"], "classification": "FN"}
                for j, row in enumerate(truths) if j not in matched_truths]
    by_type: dict[str, dict[str, int | float]] = {}
    for j, row in enumerate(truths):
        properties = row.get("properties", {})
        kind = str(properties.get("intersection_type", properties.get("type", "unclassified")))
        counts = by_type.setdefault(kind, {"tp": 0, "fn": 0})
        counts["tp" if j in matched_truths else "fn"] += 1
    for counts in by_type.values():
        counts["recall"] = counts["tp"] / (counts["tp"] + counts["fn"])
    return {**point_metrics(len(pairs), n - len(pairs), m - len(pairs)),
            "matches": matches, "per_type_recall": by_type,
            "mean_match_distance_m": float(np.mean([distance for _, _, distance in pairs])) if pairs else None}


def line_metrics(predicted_length_m: float, truth_length_m: float,
                 covered_prediction_m: float, covered_truth_m: float) -> dict[str, float | str]:
    precision = min(1.0, covered_prediction_m / predicted_length_m) if predicted_length_m else 0.0
    recall = min(1.0, covered_truth_m / truth_length_m) if truth_length_m else 0.0
    return {"metric_kind": "length_weighted_geometric_coverage",
            "predicted_length_m": predicted_length_m, "truth_length_m": truth_length_m,
            "covered_prediction_m": covered_prediction_m, "covered_truth_m": covered_truth_m,
            "precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def score_centerlines(predictions: list[dict], truths: list[dict], region: Region,
                      tolerance_m: float, all_regions: list[Region] | None = None) -> dict[str, float | str]:
    if not math.isfinite(tolerance_m) or tolerance_m <= 0:
        raise ValueError("line_tolerance_m must be finite and positive")
    allowed = {"LineString", "MultiLineString"}
    predictions, truths = _features(predictions, allowed), _features(truths, allowed)
    # The final physical-road network consumes combined fits. Optional directional
    # diagnostic exports must not improve/change its measured geometric quality.
    predictions = [row for row in predictions if row.get("properties", {}).get("variant", "combined") == "combined"]
    # Clip before buffering: only geometry within the reviewed region is scored.
    # Union removes duplicate representations of the same physical centerline.
    def clipped(rows):
        lines = []
        for row in rows:
            geometry = shape(row["geometry"]).intersection(region.geometry)
            # Shared-border line segments, like point labels, have one owner.
            for other in all_regions or []:
                if other.id < region.id and other.geometry.intersects(region.geometry):
                    geometry = geometry.difference(other.geometry.boundary)
            if geometry.length > 0:
                lines.append(geometry)
        return unary_union(lines)

    predicted = clipped(predictions)
    actual = clipped(truths)
    p_length, t_length = float(predicted.length), float(actual.length)
    p_covered = float(predicted.intersection(actual.buffer(tolerance_m)).length) if t_length else 0.0
    t_covered = float(actual.intersection(predicted.buffer(tolerance_m)).length) if p_length else 0.0
    return line_metrics(p_length, t_length, p_covered, t_covered)


def score_region(predictions: dict[str, Any], labels: LabelSet, region: Region,
                 settings: dict[str, Any]) -> dict[str, Any]:
    if not region.reviewed_complete:
        raise ValueError(f"region {region.id} is not fully reviewed; F1/false positives are unavailable")
    result: dict[str, Any] = {"region_id": region.id, "split": region.split,
                              "run_id": predictions.get("run_id"),
                              "resource_metrics": predictions.get("metrics", {})}
    if "intersections" in labels.metadata.get("layers", {}):
        actual = intersection_truths(labels.intersections, region, labels.regions)
        predicted = _features(predictions.get("intersections", []), {"Point", "Polygon", "MultiPolygon"})
        predicted = [row for row in predicted
                     if owns_point(region, shape(row["geometry"]).centroid, labels.regions)]
        result["intersections"] = match_intersections(
            predicted, actual, float(settings.get("matching_radius_m", 25.0)))
    if "centerlines" in labels.metadata.get("layers", {}):
        result["centerlines"] = score_centerlines(
            predictions.get("centerlines", []), labels.centerlines, region,
            float(settings.get("line_tolerance_m", 10.0)), labels.regions)
    return result


def aggregate_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"regions": len(scores)}
    point_scores = [score["intersections"] for score in scores if "intersections" in score]
    if point_scores:
        aggregate = point_metrics(*(sum(int(score[key]) for score in point_scores) for key in ("tp", "fp", "fn")))
        by_type: dict[str, dict[str, int | float]] = {}
        for score in point_scores:
            for kind, counts in score["per_type_recall"].items():
                total = by_type.setdefault(kind, {"tp": 0, "fn": 0})
                total["tp"] += counts["tp"]
                total["fn"] += counts["fn"]
        for counts in by_type.values():
            counts["recall"] = counts["tp"] / (counts["tp"] + counts["fn"])
        result["intersections"] = {**aggregate, "per_type_recall": by_type}
    lines = [score["centerlines"] for score in scores if "centerlines" in score]
    if lines:
        result["centerlines"] = line_metrics(*(sum(float(score[key]) for score in lines) for key in (
            "predicted_length_m", "truth_length_m", "covered_prediction_m", "covered_truth_m")))
    return result


def objective_value(metrics: dict[str, Any], objective: str) -> float:
    if objective == "intersection_f1":
        value = metrics["intersections"]["f1"]
    elif objective == "centerline_f1":
        value = metrics["centerlines"]["f1"]
    elif objective == "combined_f1":
        a, b = metrics["intersections"]["f1"], metrics["centerlines"]["f1"]
        value = 2 * a * b / (a + b) if a + b else 0.0
    else:
        raise ValueError("objective must be intersection_f1, centerline_f1, or combined_f1")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("objective must return a finite score in [0,1]")
    return float(value)
