"""Serial, restartable pilot and Optuna coordinator; no database SQL here."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import traceback
from typing import Any, Callable

from .promote import correlation_interval, passes_l1_guardrails, select_promotions


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str, allow_nan=False).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str, allow_nan=False) + '\n')
    temporary.replace(path)


def postgres_storage_url(settings: dict[str, Any]) -> str:
    """Construct the fixed roadnet_eval URL without logging credentials."""
    from sqlalchemy.engine import URL
    if settings.get('database') != 'roadnet_eval':
        raise ValueError('HPO storage database must be roadnet_eval')
    password = settings.get('password') or os.environ.get('POSTGRES_PASSWORD')
    if not password:
        raise ValueError('POSTGRES_PASSWORD is required for PostgreSQL study storage')
    return URL.create('postgresql+psycopg', username=settings['user'],
                      password=password, host=settings['host'], port=int(settings['port']),
                      database='roadnet_eval', query={'sslmode': settings.get('sslmode', 'prefer')}).render_as_string(hide_password=False)



class Controller:
    def __init__(self, config: Any, manifests: dict[str, Any], evaluator: Callable[..., dict[str, Any]]):
        self.config, self.manifests, self.evaluator = config, manifests, evaluator
        self.root = Path(config.output_root)
        self.search = dict(config.search)
        self.seed = int(config.seed)
        self.baseline = float(config.baseline)
        self.low, self.high = float(config.threshold_min), float(config.threshold_max)
        allowed = {'l1_recall_slack', 'l1_density_limit', 'min_l2_l3_spearman', 'trial_budget', 'pilot_count', 'promotion_top_k', 'recall_slots', 'exploration_slots'}
        if set(self.search) - allowed:
            raise ValueError('Unknown search options: ' + ', '.join(sorted(set(self.search) - allowed)))
        for field, default, minimum in [('trial_budget', 60, 1), ('promotion_top_k', 5, 1), ('recall_slots', 1, 0), ('exploration_slots', 1, 0)]:
            value = self.search.get(field, default)
            if type(value) is not int or value < minimum:
                raise ValueError(f'{field} must be an integer >= {minimum}')
        for name in ('l1_recall_slack', 'l1_density_limit', 'min_l2_l3_spearman'):
            if name not in self.search:
                raise ValueError(f'[search].{name} must be explicitly configured for pilot calibration')
        slack, density, correlation = (float(self.search[k]) for k in ('l1_recall_slack', 'l1_density_limit', 'min_l2_l3_spearman'))
        if not 0 <= slack <= 1 or not math.isfinite(density) or density <= 0 or not -1 <= correlation <= 1:
            raise ValueError('Invalid explicit pilot calibration policy')
        raw = asdict(config) if is_dataclass(config) else vars(config)
        self.identity = canonical_hash({'config': raw, 'manifests': manifests, 'controller_version': 1})
        self.root.mkdir(parents=True, exist_ok=True)
        identity_path = self.root / 'identity.json'
        if identity_path.exists() and json.loads(identity_path.read_text())['identity'] != self.identity:
            raise ValueError('Study identity changed; use a new output_root for changed config or manifests')
        if not identity_path.exists():
            write_json(identity_path, {'identity': self.identity, 'config': raw, 'manifest_hashes': {k: canonical_hash(v) for k, v in manifests.items()}})

    @contextmanager
    def locked(self):
        with (self.root / 'coordinator.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('Another coordinator owns this experiment') from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def stage(self, threshold: float, fidelity: str, split: str = 'tune') -> dict[str, Any]:
        key = canonical_hash({'identity': self.identity, 'threshold': threshold, 'fidelity': fidelity, 'split': split})
        directory = self.root / 'results' / key
        path = directory / 'stage.json'
        if path.exists():
            stored = json.loads(path.read_text())
            checksum = stored.pop('record_hash', None)
            if checksum != canonical_hash(stored) or stored.get('identity') != self.identity or stored.get('stage_id') != key:
                raise ValueError('Persisted stage content hash or identity mismatch')
            return stored
        directory.mkdir(parents=True, exist_ok=True)
        try:
            attempt = 0
            while (directory / f'attempt-{attempt:04d}').exists():
                attempt += 1
            result = self.evaluator(threshold, fidelity, split, directory / f'attempt-{attempt:04d}')
            result = {**result, **result.get('metrics', {}), 'threshold': threshold, 'fidelity': fidelity,
                      'split': split, 'identity': self.identity, 'stage_id': key}
            if fidelity == 'L1' and split != 'holdout':
                result.update(fp=None, precision=None, f1=None)
            if fidelity != 'L1' and result.get('f1') is None:
                raise ValueError('Complete reviewed labels and F1 are required for L2/L3')
            for metric in ('recall', 'precision', 'f1'):
                if result.get(metric) is not None and (not math.isfinite(float(result[metric])) or not 0 <= float(result[metric]) <= 1):
                    raise ValueError(f'Stage returned invalid {metric}')
            if not math.isfinite(float(result['candidate_density_per_km2'])) or float(result['candidate_density_per_km2']) < 0:
                raise ValueError('Stage returned invalid candidate density')
            write_json(path, {**result, 'record_hash': canonical_hash(result)})
            return result
        except Exception:
            write_json(directory / 'failure.json', {'status': 'FAILED', 'threshold': threshold,
                       'fidelity': fidelity, 'split': split, 'traceback': self.failure_traceback()})
            raise

    def failure_traceback(self) -> str:
        detail = traceback.format_exc()
        settings = getattr(getattr(self.evaluator, 'workspace', None), 'archive_settings', {})
        for secret in (os.environ.get('POSTGRES_PASSWORD'), settings.get('password')):
            if secret:
                from urllib.parse import quote, quote_plus
                for encoded in (secret, quote(secret, safe=''), quote_plus(secret)):
                    detail = detail.replace(encoded, '[REDACTED]')
        return detail

    def guard(self, result: dict[str, Any], baseline: dict[str, Any]) -> bool:
        return passes_l1_guardrails(result, baseline, recall_slack=float(self.search['l1_recall_slack']), density_limit=float(self.search['l1_density_limit']))

    def pilot(self) -> dict[str, Any]:
        count = int(self.search.get('pilot_count', 12))
        if not 10 <= count <= 15:
            raise ValueError('pilot_count must be between 10 and 15')
        rng = random.Random(self.seed)
        thresholds = [self.baseline, self.low, self.high]
        thresholds.extend(rng.uniform(self.low, self.high) for _ in range(count - 3))
        records = []
        for index, threshold in enumerate(thresholds):
            record = {'trial_id': f'pilot-{index:03d}', 'threshold': threshold,
                      **{level: self.stage(threshold, level) for level in ('L1', 'L2', 'L3')}}
            records.append(record)
            write_json(self.root / 'pilot.json', {'identity': self.identity, 'records': records, 'status': 'RUNNING'})
        baseline = records[0]['L1']
        diagnostics = {}
        for level, metric in (('L1', 'recall'), ('L2', 'f1')):
            diagnostics[level] = correlation_interval([float(r[level][metric]) for r in records], [float(r['L3']['f1']) for r in records], seed=self.seed)
        top_k = min(int(self.search.get('promotion_top_k', 5)), count)
        l3_top = sorted(records, key=lambda r: (-r['L3']['f1'], r['trial_id']))[:top_k]
        guarded = [r for r in records if self.guard(r['L1'], baseline)]
        selected = select_promotions(guarded, top_k=top_k, recall_slots=int(self.search.get('recall_slots', 1)), exploration_slots=int(self.search.get('exploration_slots', 1)), seed=self.seed)
        selected_ids = {r['trial_id'] for r in selected}
        l1_rejected_top = [r['trial_id'] for r in l3_top if not self.guard(r['L1'], baseline)]
        best_selected = max((r['L3']['f1'] for r in selected), default=None)
        rho = diagnostics['L2']['spearman']
        calibrated = (rho is not None and rho >= float(self.search['min_l2_l3_spearman']) and not l1_rejected_top)
        result = {'identity': self.identity, 'records': records, 'status': 'COMPLETE', 'calibrated': calibrated,
                  'correlations': diagnostics, 'promotion_hit_rate': sum(r['trial_id'] in selected_ids for r in l3_top) / len(l3_top),
                  'l1_rejected_l3_top': l1_rejected_top, 'promotion_regret': max(r['L3']['f1'] for r in records) - best_selected if best_selected is not None else None,
                  'policy': self.search, 'uncertainty': 'Small pilot; inspect bootstrap intervals and region composition before interpreting rankings.'}
        write_json(self.root / 'pilot.json', result)
        return result

    def tune(self, storage: Any = None) -> list[dict[str, Any]]:
        import optuna
        pilot = json.loads((self.root / 'pilot.json').read_text())
        if pilot.get('identity') != self.identity or not pilot.get('calibrated'):
            raise ValueError('Successful matching pilot calibration is required before tune')
        baseline = self.stage(self.baseline, 'L1')
        self.stage(self.baseline, 'L2')
        if storage is None:
            settings = self.evaluator.workspace.archive_settings
            try:
                storage = optuna.storages.RDBStorage(url=postgres_storage_url(settings))
            except Exception:
                raise RuntimeError('Cannot initialize PostgreSQL roadnet_eval study storage; verify archive connection settings') from None
        study = optuna.create_study(study_name='intersection_hpo_' + self.identity[:24], direction='maximize',
                                   storage=storage, load_if_exists=True, sampler=optuna.samplers.TPESampler(seed=self.seed),
                                   pruner=optuna.pruners.NopPruner())
        study.set_user_attr('experiment_identity', self.identity)
        study.set_user_attr('proxy_objective', 'L2_micro_f1')
        study.set_user_attr('manifest_hashes', {key: canonical_hash(value) for key, value in self.manifests.items()})
        records_path = self.root / 'trials.json'
        records = json.loads(records_path.read_text()) if records_path.exists() else []
        budget = int(self.search.get('trial_budget', 60))
        while len(study.trials) < budget or any(t.state == optuna.trial.TrialState.RUNNING for t in study.trials):
            running = [t for t in study.trials if t.state == optuna.trial.TrialState.RUNNING]
            if running:
                trial = optuna.trial.Trial(study, running[0]._trial_id)
            else:
                study.sampler = optuna.samplers.TPESampler(seed=self.seed + len(study.trials))
                trial = study.ask()
            threshold = trial.suggest_float('min_turn_deg', self.low, self.high)
            record = {'trial_id': trial.number, 'threshold': threshold, 'status': 'PROPOSED', 'identity': self.identity}
            trial.set_user_attr('experiment_identity', self.identity)
            trial.set_user_attr('parameter_hash', canonical_hash({'algorithm': 'turn_threshold', 'min_turn_deg': threshold, 'identity': self.identity}))
            trial.set_user_attr('effective_config', {'algorithm': 'turn_threshold', 'min_turn_deg': threshold, 'fixed_config_identity': self.identity})
            try:
                record['L1'] = self.stage(threshold, 'L1')
                trial.set_user_attr('L1_stage_id', record['L1']['stage_id'])
                trial.set_user_attr('L1_recall', record['L1']['recall'])
                if not self.guard(record['L1'], baseline):
                    record.update(status='L1_REJECTED', reject_reason='L1_GUARDRAIL')
                    trial.set_user_attr('record', record)
                    trial.set_user_attr('exit_reason', 'L1_GUARDRAIL')
                    study.tell(trial, state=optuna.trial.TrialState.PRUNED)
                else:
                    record['L2'] = self.stage(threshold, 'L2')
                    record['status'] = 'L2_DONE'
                    trial.set_user_attr('L2_stage_id', record['L2']['stage_id'])
                    trial.set_user_attr('L2_f1', record['L2']['f1'])
                    trial.set_user_attr('record', record)
                    study.tell(trial, float(record['L2']['f1']))
            except Exception:
                record.update(status='FAILED', traceback=self.failure_traceback())
                trial.set_user_attr('record', record)
                trial.set_user_attr('exit_reason', 'STAGE_FAILED')
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
            records = [r for r in records if r['trial_id'] != trial.number] + [record]
            write_json(records_path, records)
        # Reconcile a crash between database completion and local trial export.
        known = {r['trial_id'] for r in records}
        for trial in study.trials:
            if trial.number not in known:
                archived = trial.user_attrs.get('record')
                if archived is not None:
                    if archived.get('identity') != self.identity:
                        raise ValueError('Archived trial identity mismatch')
                    records.append(archived)
                    continue
                threshold = trial.params.get('min_turn_deg')
                record = {'trial_id': trial.number, 'threshold': threshold, 'identity': self.identity, 'status': trial.state.name}
                if threshold is not None:
                    for level in ('L1', 'L2'):
                        key = canonical_hash({'identity': self.identity, 'threshold': threshold, 'fidelity': level, 'split': 'tune'})
                        path = self.root / 'results' / key / 'stage.json'
                        if path.exists():
                            record[level] = json.loads(path.read_text())
                records.append(record)
        write_json(records_path, sorted(records, key=lambda r: r['trial_id']))
        return records

    def promote(self) -> dict[str, Any]:
        records = json.loads((self.root / 'trials.json').read_text())
        baseline = {'trial_id': 'baseline', 'threshold': self.baseline, 'L2': self.stage(self.baseline, 'L2')}
        # Use uniform string IDs for deterministic tie-breaking across baseline/trials.
        pool = [{**r, 'trial_id': str(r['trial_id'])} for r in records] + [baseline]
        promoted = select_promotions(pool, top_k=int(self.search.get('promotion_top_k', 5)), recall_slots=int(self.search.get('recall_slots', 1)), exploration_slots=int(self.search.get('exploration_slots', 1)), seed=self.seed)
        for record in promoted:
            record['L3'] = self.stage(float(record['threshold']), 'L3')
            record['status'] = 'L3_DONE'
        if not promoted:
            raise ValueError('No completed L2 candidates to promote')
        winner = min(promoted, key=lambda r: (-float(r['L3']['f1']), float(r['threshold'])))
        previous_path = self.root / 'best.json'
        if previous_path.exists():
            previous = json.loads(previous_path.read_text())
            if previous.get('holdout') and float(previous['winner']['threshold']) != float(winner['threshold']):
                raise ValueError('Holdout already opened for a different winner; final selection is frozen')
        # Selection is complete before the sole independent holdout evaluation.
        best = {'identity': self.identity, 'winner': winner, 'candidates': promoted}
        write_json(self.root / 'best.json', best)
        best['holdout'] = self.stage(float(winner['threshold']), 'L3', 'holdout')
        write_json(self.root / 'best.json', best)
        from .exports import write_best_config
        write_best_config(self.config, float(winner['threshold']))
        return best
