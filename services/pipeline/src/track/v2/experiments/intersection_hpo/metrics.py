"""Fidelity-aware polygon matching and micro aggregation."""
from __future__ import annotations

import json
from typing import Any
from shapely.geometry import shape
from pipeline.experiments.evaluation import evaluate, validated_features


def reject_overlapping_regions(regions: list[dict[str, Any]]) -> None:
    """Reject shared boundaries as well as area overlaps to avoid duplicate centers."""
    seen: set[str] = set()
    geometries = []
    for region in regions:
        if region['id'] in seen:
            raise ValueError('duplicate region IDs')
        seen.add(region['id'])
        geometry = shape(region['geometry'])
        if geometry.is_empty or not geometry.is_valid:
            raise ValueError('invalid region geometry')
        if any(geometry.intersects(other) for other in geometries):
            raise ValueError('region boundaries overlap; micro aggregation requires disjoint regions')
        geometries.append(geometry)


def score_region(predictions: dict, region: dict, rules: dict, fidelity: str) -> dict:
    if fidelity not in {'L1', 'L2', 'L3'}:
        raise ValueError('unknown fidelity')
    complete = region.get('reviewed_complete', region.get('fully_verified', False)) is True
    if fidelity != 'L1' and not complete:
        raise ValueError(f'{fidelity} requires fully verified region labels')
    boundary = shape(region['geometry'])
    truth = validated_features(region['truths'], truth=True)
    if any(not boundary.covers(shape(feature['geometry']).centroid) for feature in truth):
        raise ValueError('truth center lies outside manual evaluation boundary')
    predicted = validated_features(predictions)
    predicted = [feature for feature in predicted if boundary.covers(shape(feature['geometry']).centroid)]
    # The existing scorer sorts feature IDs. Persisted UUIDs change between runs,
    # so assign content-ordered IDs for deterministic tie resolution/projection.
    transient = {'id', 'detect_id', 'refine_id', 'experiment_id', 'batch_id'}
    def content_key(feature):
        return json.dumps({'geometry': shape(feature['geometry']).normalize().wkb_hex,
                           'properties': {k: v for k, v in feature.get('properties', {}).items() if k not in transient}},
                          sort_keys=True, separators=(',', ':'))
    ordered = sorted(predicted, key=content_key)
    originals = {f'{index:012d}': feature['id'] for index, feature in enumerate(ordered)}
    stable = [{**feature, 'id': f'{index:012d}'} for index, feature in enumerate(ordered)]
    result = evaluate(stable, truth, rules)
    for match in result['matches']:
        if match.get('prediction_id') is not None:
            match['prediction_id'] = originals[match['prediction_id']]
    # Partial labels establish misses and matches, never false positives.
    if fidelity == 'L1':
        result['metrics'].update(fp=None, precision=None, f1=None)
        result['matches'] = [match for match in result['matches'] if match['classification'] != 'FP']
    matched = {match['truth_id'] for match in result['matches'] if match['classification'] == 'TP'}
    by_type = {}
    for feature in truth:
        props = feature.get('properties', {})
        kind = str(props.get('intersection_type', props.get('type', 'unknown')))
        counts = by_type.setdefault(kind, {'tp': 0, 'fn': 0})
        counts['tp' if feature['id'] in matched else 'fn'] += 1
    for counts in by_type.values():
        counts['recall'] = counts['tp'] / (counts['tp'] + counts['fn'])
    result['metrics']['per_type_recall'] = by_type
    result['prediction_count'] = len(predicted)
    result['truth_count'] = len(truth)
    return result


def aggregate_scores(scores: list[dict], fidelity: str) -> dict:
    tp = sum(score['metrics']['tp'] for score in scores)
    fn = sum(score['metrics']['fn'] for score in scores)
    fp = None if fidelity == 'L1' else sum(score['metrics']['fp'] for score in scores)
    by_type = {}
    for score in scores:
        for kind, counts in score['metrics']['per_type_recall'].items():
            total = by_type.setdefault(kind, {'tp': 0, 'fn': 0})
            total['tp'] += counts['tp']
            total['fn'] += counts['fn']
    for counts in by_type.values():
        counts['recall'] = counts['tp'] / (counts['tp'] + counts['fn'])
    return {'tp': tp, 'fp': fp, 'fn': fn,
            'recall': tp / (tp + fn) if tp + fn else None,
            'precision': tp / (tp + fp) if fp is not None and tp + fp else None,
            'f1': 2 * tp / (2 * tp + fp + fn) if fp is not None and 2 * tp + fp + fn else None,
            'per_type_recall': by_type}
