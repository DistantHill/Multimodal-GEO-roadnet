"""Evidence exports, keeping measured values traceable to persisted stages."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def build_report(root: str | Path, config_path: str | Path | None = None) -> Path:
    root = Path(root)
    def read(name: str, default: Any) -> Any:
        path = root / name
        return json.loads(path.read_text()) if path.exists() else default
    pilot = read('pilot.json', {})
    trials = read('trials.json', [])
    best = read('best.json', {})
    stages = [json.loads(path.read_text()) for path in sorted((root / 'results').glob('*/stage.json'))]
    fields = ['stage_id', 'threshold', 'fidelity', 'split', 'tp', 'fp', 'fn', 'precision', 'recall', 'f1', 'candidate_count', 'candidate_density_per_km2', 'fp_per_km2', 'negative_fp_per_km2', 'wall_seconds', 'cpu_seconds', 'artifact_dir']
    with (root / 'trials.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(stages)
    lines = ['# Intersection threshold experiment', '',
             'Optuna optimizes L2 micro F1. The final configuration is selected by L3 micro F1; independent holdout is evaluated only after that selection.', '',
             'Detection uses strict abs(turn_deg) > min_turn_deg in the manually defined region, without boundary expansion. Remaining Detect/Refine parameters remain fixed.', '',
             '## Baseline and pilot', '']
    def table(records: list[dict[str, Any]]) -> list[str]:
        result = ['| Trial | Threshold | Level | Split | TP | FP | FN | Precision | Recall | F1 | Wall seconds |', '|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|']
        for record in records:
            for level in ('L1', 'L2', 'L3', 'holdout'):
                if level not in record:
                    continue
                stage = record[level]
                values = [record['trial_id'], record['threshold'], level, stage.get('split')] + [stage.get(k) for k in ('tp', 'fp', 'fn', 'precision', 'recall', 'f1', 'wall_seconds')]
                result.append('| ' + ' | '.join('unavailable' if v is None else str(v) for v in values) + ' |')
        return result
    lines += table(pilot.get('records', []))
    pilot_records = pilot.get('records', [])
    if pilot_records:
        svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="820" height="340" viewBox="0 0 820 340"><rect width="820" height="340" fill="white"/>']
        for offset, level, metric in ((0, 'L1', 'recall'), (410, 'L2', 'f1')):
            svg.append(f'<text x="{offset+40}" y="25" font-family="sans-serif" font-size="16">{level} {metric} vs L3 F1</text>')
            svg.append(f'<path d="M {offset+40} 50 V 290 H {offset+370}" fill="none" stroke="black"/>')
            for record in pilot_records:
                x, y = offset + 40 + 330*float(record[level][metric]), 290 - 240*float(record['L3']['f1'])
                svg.append(f'<circle cx="{x}" cy="{y}" r="4" fill="#235ac5"><title>{record["trial_id"]}: threshold {record["threshold"]}</title></circle>')
            svg.append(f'<text x="{offset+40}" y="315" font-family="sans-serif" font-size="12">0                 proxy metric (0–1)                  1</text>')
        svg.append('</svg>')
        (root / 'pilot-scatter.svg').write_text(''.join(svg))
        lines += ['', '![Pilot parameter rankings](pilot-scatter.svg)', '']
    lines += ['', '## Calibration', '', '```json', json.dumps({k: v for k, v in pilot.items() if k != 'records'}, indent=2), '```', '',
              'Pilot correlations compare parameter rankings within each fidelity. Absolute L2/L3 F1 scores have different region composition. Bootstrap intervals describe the small pilot’s uncertainty.', '',
              '## L2 top ten', '']
    top = sorted([r for r in trials if r.get('L2', {}).get('f1') is not None], key=lambda r: -r['L2']['f1'])[:10]
    lines += table(top)
    lines += ['', '## L3 finalists and holdout', '']
    lines += table(best.get('candidates', []))
    if best.get('holdout'):
        lines += table([{'trial_id': best['winner']['trial_id'], 'threshold': best['winner']['threshold'], 'holdout': best['holdout']}])
    lines += ['', '## Scenario diagnostics', '', '| Stage ID | Level | Threshold | Per-type recall | Negative FP/km² |', '|---|---|---:|---|---:|']
    for stage in stages:
        per_type = stage.get('per_type_recall', stage.get('by_type_recall', {}))
        lines.append('| ' + ' | '.join(str(v) for v in (stage.get('stage_id'), stage.get('fidelity'), stage.get('threshold'), json.dumps(per_type, sort_keys=True), stage.get('negative_fp_per_km2', 'unavailable'))) + ' |')
    lines += ['', '## Measured cost and limitations', '']
    wall = sum(float(s.get('wall_seconds', s.get('runtime_s', 0))) for s in stages)
    cpu_values = [s.get('cpu_seconds') for s in stages]
    cpu = sum(cpu_values) if cpu_values and all(v is not None for v in cpu_values) else None
    pipeline_calls = sum(len(s.get('regions', [])) for s in stages)
    lines += [f'Persisted distinct stages: {len(stages)}. Measured wall seconds: {wall}. CPU seconds: {cpu if cpu is not None else "unavailable"}. Archived regional Pipeline evaluations: {pipeline_calls}.', '',
              'The all-L3 counterfactual cost has not been measured; no speedup is claimed. Failed/restarted work and shared preparation costs require separate measurement. Candidate caches and Refine reuse are reported in each stage artifact.', '',
              f'Completed L2 trials: {len([r for r in trials if r.get("L2")])}. Parameter importance is not estimated from an unvalidated small sample; this study varies one threshold.', '',
              'Bad cases require inspection of existing matching artifacts. No elevated crossing, continuous bend, or sparse segment error count is inferred without labeled evidence. Per-type recalls, negative-region FP density, run IDs and effective configuration hashes are preserved in stage JSON and regional matching artifacts.', '',
              'The historical F1 87.3% is not quantitatively compared without identical dataset, truth version and evaluator.', '', '## Reproduction', '']
    if config_path:
        lines += ['```sh', f'python -m track.v2.experiments.intersection_hpo reproduce --config {config_path}', '```', '']
    lines += ['Stage-level metrics and artifact locations are exported to trials.csv. Source/configuration identity is preserved in identity.json. Missing labels or incomplete reviews block evaluation rather than producing fabricated scores.', '']
    path = root / 'report.md'
    path.write_text('\n'.join(lines))
    return path
