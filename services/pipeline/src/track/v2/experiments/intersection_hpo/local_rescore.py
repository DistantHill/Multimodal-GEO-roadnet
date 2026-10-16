"""Pure, separate re-scoring of saved calls; never reopen an optimization study.

Location-region truth circles identify locations, rather than physical faces.
Old metrics, ledger, promotions and archives remain untouched.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from pipeline.experiments.evaluation import evaluate

from .local_adapter import RULES, LABEL_SEMANTICS
from .local_search import _write


def rescore(source_root: str | Path, output_root: str | Path) -> dict:
    source, output = Path(source_root).resolve(), Path(output_root).resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Rescoring output must be a separate directory outside the original run tree')
    ledger_path = source / 'search' / 'ledger.json'
    ledger_bytes = ledger_path.read_bytes()
    ledger = json.loads(ledger_bytes)
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for call in ledger['calls']:
        if call['status'] != 'SUCCESS':
            continue
        call_id = call['call_id']
        if not isinstance(call_id, str) or Path(call_id).name != call_id or call_id in {'.', '..'}:
            raise ValueError('Invalid call ID in original ledger')
        directory = source / 'calls' / call_id
        predictions_bytes = (directory / 'predictions.geojson').read_bytes()
        truths_bytes = (directory / 'truth.geojson').read_bytes()
        scored = evaluate(json.loads(predictions_bytes), json.loads(truths_bytes), dict(RULES))
        row = {'call_id': call_id, 'level': call['level'], 'threshold': call['threshold'],
               'experiment_id': call['result']['experiment_id'],
               'original_evaluation_id': call['result'].get('evaluation_id'),
               'original_metrics': call['result']['metrics'], 'rules': dict(RULES),
               'label_semantics': LABEL_SEMANTICS,
               'predictions_sha256': hashlib.sha256(predictions_bytes).hexdigest(),
               'truths_sha256': hashlib.sha256(truths_bytes).hexdigest(), **scored}
        rows.append(row)
        _write(output / (call_id + '.json'), row)
    rankings = {level: sorted([r for r in rows if r['level'] == level],
                             key=lambda r: (-r['metrics']['f1'], r['threshold'], r['call_id']))
                for level in ('L1', 'L2', 'L3')}
    for level, ranked in rankings.items():
        _write(output / (level + '-ranking.json'), ranked)
        with (output / (level + '-ranking.csv')).open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['rank', 'call_id', 'threshold', 'original_f1', 'tp', 'fp', 'fn', 'precision', 'recall', 'f1'])
            writer.writeheader()
            for rank, row in enumerate(ranked, 1):
                writer.writerow({'rank': rank, 'call_id': row['call_id'], 'threshold': row['threshold'],
                                 'original_f1': row['original_metrics']['f1'], **row['metrics']})
    promotion = {}
    for source_level, destination, seats in (('L1', 'L2', 12), ('L2', 'L3', 8)):
        new_top = [row['threshold'] for row in rankings[source_level][:seats]]
        actually_evaluated = [row['threshold'] for row in rankings[destination]]
        common = set(new_top) & set(actually_evaluated)
        promotion[source_level + '_to_' + destination] = {
            'new_score_top_thresholds': new_top, 'originally_evaluated_thresholds': actually_evaluated,
            'overlap_count': len(common), 'top_count': len(new_top),
            'new_top_missing_higher_fidelity': [threshold for threshold in new_top if threshold not in common],
            'original_higher_fidelity_outside_new_top': [threshold for threshold in actually_evaluated if threshold not in common]}
    summary = {'rules': dict(RULES), 'label_semantics': LABEL_SEMANTICS,
               'source_root': str(source), 'source_ledger_sha256': hashlib.sha256(ledger_bytes).hexdigest(),
               'rescored_calls': len(rows), 'rankings': rankings,
               'best_available_l3': rankings['L3'][0] if rankings['L3'] else None,
               'promotion_bias': promotion,
               'limitations': [
                   'New scores do not alter the original TPE observations or original promotions.',
                   'The original sampler used the previous objective; these are retrospective scores, not a new TPE search.',
                   'L2/L3 candidates are selected survivors of the old scores. Missing higher-fidelity results cannot be inferred.',
                   'The best available L3 is conditional on the previously evaluated candidates; it is not an independently optimized or holdout result.']}
    _write(output / 'summary.json', summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Re-score existing artifacts without database writes or optimization')
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args(argv)
    summary = rescore(args.source_root, args.output_root)
    best = summary['best_available_l3']
    compact_best = {key: best[key] for key in ('call_id', 'threshold', 'metrics', 'experiment_id')} if best else None
    print(json.dumps({'rescored_calls': summary['rescored_calls'], 'rules': summary['rules'],
                      'best_available_l3': compact_best, 'promotion_bias': summary['promotion_bias']}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
