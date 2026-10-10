"""Offline audit and serial experiment CLI; study storage connects only for tune."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..config import load_workspace
from .config import load_hpo_config
from .manifests import audit_data, build_manifests, write_manifests
from .report import build_report
from .tune import Controller, canonical_hash, write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Calibrated strict-region turn_threshold threshold search')
    parser.add_argument('--config', dest='global_config', type=Path)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('audit', 'build-manifests', 'pilot', 'tune', 'promote', 'report', 'reproduce'):
        sub = subparsers.add_parser(command)
        sub.add_argument('--config', type=Path)
    args = parser.parse_args(argv)
    config_path = args.config or args.global_config
    if config_path is None:
        parser.error('--config is required')
    try:
        config = load_hpo_config(config_path)
    except (ValueError, OSError, TypeError) as error:
        print(json.dumps({'ready': False, 'blockers': [str(error)]}, ensure_ascii=False))
        return 2
    if args.command == 'report':
        path = build_report(config.output_root, config.config_path)
        print(f' {path} ')
        return 0
    try:
        workspace = load_workspace(config.experiment_config, password=os.environ.get('POSTGRES_PASSWORD', ''), schemes=[config.scheme])
        audit = audit_data(config, workspace)
    except (ValueError, OSError, KeyError, TypeError) as error:
        detail = str(error)
        password = os.environ.get('POSTGRES_PASSWORD')
        if password:
            from urllib.parse import quote, quote_plus
            for encoded in (password, quote(password, safe=''), quote_plus(password)):
                detail = detail.replace(encoded, '[REDACTED]')
        audit = {'ready': False, 'blockers': [detail], 'region_count': 0}
        write_json(config.output_root / 'audit.json', audit)
        print(json.dumps(audit, indent=2))
        return 2
    if args.command == 'audit':
        write_json(config.output_root / 'audit.json', audit)
        print(json.dumps(audit, indent=2))
        return 0 if audit['ready'] else 2
    if not audit['ready']:
        print(json.dumps(audit, indent=2))
        return 2
    manifests = build_manifests(config, workspace)
    write_manifests(manifests, config.output_root)
    if args.command == 'build-manifests':
        print(json.dumps({k: v['manifest_hash'] for k, v in manifests.items()}, indent=2))
        return 0
    from .adapters import RunBatchEvaluator
    evaluator = RunBatchEvaluator(workspace, manifests, config.scheme)
    controller = Controller(config, manifests, evaluator)
    with controller.locked():
        if args.command == 'pilot':
            result = controller.pilot()
            build_report(config.output_root, config.config_path)
            return 0 if result['calibrated'] else 2
        if args.command == 'tune':
            controller.tune()
        elif args.command == 'promote':
            controller.promote()
        elif args.command == 'reproduce':
            best = json.loads((config.output_root / 'best.json').read_text())
            if best['identity'] != controller.identity:
                raise ValueError('Best configuration identity mismatch')
            threshold = float(best['winner']['threshold'])
            directory = config.output_root / 'reproduction' / canonical_hash({'threshold': threshold, 'identity': controller.identity})
            attempt = 0
            while (directory / f'attempt-{attempt:04d}').exists():
                attempt += 1
            actual = evaluator(threshold, 'L3', 'tune', directory / f'attempt-{attempt:04d}')
            expected = best['winner']['L3']
            actual_metrics = actual.get('metrics', actual)
            for key in ('tp', 'fp', 'fn', 'precision', 'recall', 'f1'):
                if actual_metrics.get(key) != expected.get(key):
                    raise ValueError(f'Reproduction mismatch for {key}')
            expected_regions = expected.get('regions', [])
            for observed, previous in zip(actual.get('regions', []), expected_regions, strict=True):
                if observed.get('candidate_count') != previous.get('candidate_count'):
                    raise ValueError('Reproduction mismatch in candidate count')
                for key in ('predictions', 'matches'):
                    if observed.get('hashes', {}).get(key) != previous.get('hashes', {}).get(key):
                        raise ValueError(f'Reproduction mismatch in canonical {key} artifact hash')
            write_json(directory / 'verification.json', {'matched': True, 'identity': controller.identity})
        build_report(config.output_root, config.config_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
