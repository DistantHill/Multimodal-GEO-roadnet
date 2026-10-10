"""Explicit gates and reproducible promotion for the L2 proxy objective."""
from __future__ import annotations

import math
import random
from typing import Any


def passes_l1_guardrails(result: dict[str, Any], baseline: dict[str, Any], *, recall_slack: float, density_limit: float) -> bool:
    return (float(result['recall']) >= max(0.0, float(baseline['recall']) - recall_slack)
            and float(result['candidate_density_per_km2']) <= density_limit)


def select_promotions(records: list[dict[str, Any]], *, top_k: int, recall_slots: int, exploration_slots: int, seed: int) -> list[dict[str, Any]]:
    """Use only L2 records; neither holdout nor L3 affects selection."""
    eligible = [r for r in records if r.get('L2', {}).get('f1') is not None]
    ranked = sorted(eligible, key=lambda r: (-float(r['L2']['f1']), r['trial_id']))
    chosen: list[dict[str, Any]] = []
    seen: set[float] = set()

    def add(record: dict[str, Any], reason: str) -> bool:
        threshold = float(record['threshold'])
        if threshold in seen:
            return False
        seen.add(threshold)
        chosen.append({**record, 'promote_reason': reason})
        return True

    for record in ranked:
        if len(chosen) >= top_k:
            break
        add(record, 'L2_F1_TOP')
    count = 0
    for record in sorted(eligible, key=lambda r: (-float(r['L2']['recall']), r['trial_id'])):
        if count >= recall_slots:
            break
        count += add(record, 'L2_RECALL_DIVERSITY')
    remaining = [r for r in ranked if float(r['threshold']) not in seen]
    random.Random(seed).shuffle(remaining)
    for record in remaining[:exploration_slots]:
        add(record, 'SEEDED_EXPLORATION')
    return chosen


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        ordered = sorted(range(len(values)), key=values.__getitem__)
        result = [0.0] * len(values)
        start = 0
        while start < len(values):
            end = start + 1
            while end < len(values) and values[ordered[end]] == values[ordered[start]]:
                end += 1
            for index in ordered[start:end]:
                result[index] = (start + end - 1) / 2
            start = end
        return result

    a, b = ranks(x), ranks(y)
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    numerator = sum((u - ma) * (v - mb) for u, v in zip(a, b))
    denominator = math.sqrt(sum((u - ma)**2 for u in a) * sum((v - mb)**2 for v in b))
    return numerator / denominator if denominator else None


def correlation_interval(x: list[float], y: list[float], *, seed: int, samples: int = 1000) -> dict[str, Any]:
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        indices = [rng.randrange(len(x)) for _ in x]
        estimate = spearman([x[i] for i in indices], [y[i] for i in indices])
        if estimate is not None:
            estimates.append(estimate)
    estimates.sort()
    interval = ([estimates[int((len(estimates)-1)*p)] for p in (0.025, 0.975)] if estimates else None)
    return {'spearman': spearman(x, y), 'bootstrap_95_interval': interval,
            'sample_count': len(x), 'valid_resamples': len(estimates)}
