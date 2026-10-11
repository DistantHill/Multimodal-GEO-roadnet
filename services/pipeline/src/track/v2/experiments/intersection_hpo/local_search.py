"""Bounded L1-F1 TPE search with individually archived regional evaluations.

The evaluator returns only after its independent roadnet_eval transaction commits.
A durable RUNNING ledger entry consumes one call even if the process is interrupted.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Callable
from urllib.parse import quote, quote_plus

from .promote import correlation_interval


class NumericalTrialError(RuntimeError):
    """A proven parameter-local numerical failure; the next parameter may proceed."""


class LocalSearchStopped(RuntimeError):
    """Infrastructure, input or unknown failure stopped the current invocation."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')
    temporary.replace(path)


class LocalSearch:
    LIMITS = {'L1': 40, 'L2': 12, 'L3': 8}

    def __init__(self, root: Path, evaluator: Callable, storage: Any, identity: dict, seed: int = 42):
        self.root, self.evaluator, self.storage = Path(root), evaluator, storage
        self.seed = seed
        self.identity = {'inputs': identity, 'seed': seed, 'controller_version': 1,
                         'search': {'min_turn_deg': [30., 90.], 'baseline': 45., 'budget': self.LIMITS,
                                    'objective': 'L1_F1', 'initial_random': 9, 'tpe_trials': 30}}
        self.identity_hash = _hash(self.identity)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'ledger.json'
        self.state: dict[str, Any] = {}
        self.study = None
        self.secrets: list[str] = []
        if isinstance(storage, str):
            from sqlalchemy.engine import make_url
            url = make_url(storage)
            if url.get_backend_name() != 'postgresql' or url.database != 'roadnet_eval':
                raise ValueError('Local HPO requires PostgreSQL roadnet_eval storage')
            if url.password:
                self.secrets.append(url.password)

    def _safe_error(self, error: BaseException) -> str:
        detail = f'{type(error).__name__}: {error}'
        for secret in self.secrets:
            for encoded in (secret, quote(secret, safe=''), quote_plus(secret)):
                detail = detail.replace(encoded, '[REDACTED]')
        return detail

    def _save(self) -> None:
        _write(self.path, self.state)

    def _open(self) -> None:
        if self.path.exists():
            existing = json.loads(self.path.read_text())
            if existing.get('identity_hash') != self.identity_hash or existing.get('identity') != self.identity:
                raise ValueError('Local search identity changed; use a separate output root')
            self.state = existing
        else:
            self.state = {'identity': self.identity, 'identity_hash': self.identity_hash,
                          'status': 'RUNNING', 'calls': [], 'l1_trials': [], 'promotions': {}}
            self._save()
        import optuna
        self.study = optuna.create_study(study_name='intersection_local_' + self.identity_hash[:24],
                         direction='maximize', storage=self.storage, load_if_exists=True,
                         sampler=optuna.samplers.TPESampler(seed=self.seed, n_startup_trials=0),
                         pruner=optuna.pruners.NopPruner())
        self.study.set_user_attr('identity', self.identity)
        self.study.set_user_attr('objective', 'L1_F1')
        if self.state['l1_trials'] and len(self.study.trials) <= max(entry['trial_number'] for entry in self.state['l1_trials']):
            raise ValueError('Persisted ledger trial IDs are absent from the matching Optuna study')

    def _validate(self, result: dict) -> None:
        metrics = result.get('metrics', result)
        if not result.get('experiment_id'):
            raise ValueError('Evaluator must return an independently archived experiment_id')
        f1 = metrics.get('f1')
        if isinstance(f1, bool) or not isinstance(f1, (int, float)) or not math.isfinite(f1) or not 0 <= f1 <= 1:
            raise ValueError('Evaluator must return a finite local F1 within [0,1]')
        # Validate durable serialization before calling this evaluation successful.
        json.dumps(result, allow_nan=False)

    def _rank(self, level: str) -> list[dict]:
        return sorted([c for c in self.state['calls'] if c['level'] == level and c['status'] == 'SUCCESS'],
                      key=lambda c: (-c['result'].get('metrics', c['result'])['f1'], c['threshold'], c['call_id']))

    def _exports(self) -> None:
        for level in self.LIMITS:
            ranked = self._rank(level)
            _write(self.root / f'{level}-ranking.json', ranked)
            target = self.root / f'{level}-ranking.csv'
            temporary = target.with_suffix('.csv.tmp')
            with temporary.open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['rank', 'call_id', 'threshold', 'experiment_id', 'f1', 'tp', 'fp', 'fn', 'precision', 'recall'])
                writer.writeheader()
                for rank, call in enumerate(ranked, 1):
                    result = call['result']
                    metrics = result.get('metrics', result)
                    writer.writerow({'rank': rank, 'call_id': call['call_id'], 'threshold': call['threshold'],
                                     'experiment_id': result['experiment_id'], **{key: metrics.get(key) for key in ('f1', 'tp', 'fp', 'fn', 'precision', 'recall')}})
            temporary.replace(target)
        ranked = self._rank('L3')
        if ranked:
            _write(self.root / 'best.json', {'identity_hash': self.identity_hash, 'best': ranked[0],
                                           'selection_metric': 'L3_F1', 'independent_holdout': False})

    def _sync_trial(self, call: dict) -> None:
        if call.get('trial_number') is None or call['level'] != 'L1':
            return
        import optuna
        frozen = self.study.trials[call['trial_number']]
        if frozen.state != optuna.trial.TrialState.RUNNING:
            return
        trial = optuna.trial.Trial(self.study, frozen._trial_id)
        trial.set_user_attr('call_record', call)
        trial.set_user_attr('call_id', call['call_id'])
        if call['status'] == 'SUCCESS':
            self.study.tell(trial, call['result'].get('metrics', call['result'])['f1'])
        elif call['status'] != 'RUNNING':
            self.study.tell(trial, state=optuna.trial.TrialState.FAIL)

    def _recover(self) -> None:
        for call in self.state['calls']:
            if call['status'] in {'RUNNING', 'FAILED'}:
                lookup = getattr(self.evaluator, 'lookup', None)
                archived = lookup(call['call_id']) if lookup is not None else None
                if archived is not None:
                    self._validate(archived)
                    call.update(status='SUCCESS', result=archived, recovered=True)
                elif call['status'] == 'RUNNING':
                    call.update(status='INTERRUPTED', error='No independently archived result found; call remains charged')
                self._save()
                self._exports()
            self._sync_trial(call)
        for entry in self.state['l1_trials']:
            call = next((c for c in self.state['calls'] if c.get('trial_number') == entry['trial_number']), None)
            if call is None and entry['status'] == 'PROPOSED':
                call = self._call(entry['threshold'], 'L1', entry['trial_number'])
            if call is not None:
                entry.update(status=call['status'], call_id=call['call_id'])
                self._save()

    def _call(self, threshold: float, level: str, trial_number: int | None = None) -> dict:
        # Reuse successful results of this exact input/parameter/fidelity identity.
        for previous in self.state['calls']:
            if previous['status'] == 'SUCCESS' and previous['level'] == level and previous['threshold'] == threshold:
                if trial_number is not None:
                    self._sync_trial({**previous, 'trial_number': trial_number})
                return previous
        spent = sum(c['level'] == level for c in self.state['calls'])
        if spent >= self.LIMITS[level]:
            raise RuntimeError(f'{level} black-box budget exhausted')
        call = {'call_id': f'{self.identity_hash[:20]}-{level}-{spent:03d}', 'threshold': threshold,
                'level': level, 'status': 'RUNNING', 'trial_number': trial_number}
        self.state['calls'].append(call)
        self._save()  # Charge before invoking the expensive evaluator.
        try:
            result = self.evaluator(threshold, level, call['call_id'])
            self._validate(result)
        except NumericalTrialError as error:
            call.update(status='NUMERICAL_FAILED', error=self._safe_error(error))
            self._save()
            self._sync_trial(call)
            self._exports()
            return call
        except BaseException as error:
            call.update(status='RUNNING' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'FAILED', error=self._safe_error(error))
            self._save()
            # Leave the Optuna claim pending until archive lookup reconciles a
            # connection failure that may follow a committed result.
            self._exports()
            raise
        # A storage/export error here cannot roll back the independently archived result.
        call.update(status='SUCCESS', result=result)
        self._save()
        self._exports()
        self._sync_trial(call)
        return call

    def _l1(self) -> None:
        import optuna
        while len(self.state['l1_trials']) < 40:
            linked = {entry['trial_number'] for entry in self.state['l1_trials']}
            orphaned = [trial for trial in self.study.trials if trial.state == optuna.trial.TrialState.RUNNING and trial.number not in linked]
            if orphaned:
                trial = optuna.trial.Trial(self.study, orphaned[0]._trial_id)
            else:
                ordinal = len(self.state['l1_trials'])
                self.study.sampler = optuna.samplers.TPESampler(seed=self.seed + ordinal, n_startup_trials=0)
                if ordinal < 10 and not any(t.state == optuna.trial.TrialState.WAITING for t in self.study.trials):
                    rng = random.Random(self.seed)
                    initial = [45.] + [rng.uniform(30., 90.) for _ in range(9)]
                    self.study.enqueue_trial({'min_turn_deg': initial[ordinal]})
                trial = self.study.ask()
            threshold = trial.suggest_float('min_turn_deg', 30., 90.)
            entry = {'trial_number': trial.number, 'threshold': threshold, 'status': 'PROPOSED'}
            self.state['l1_trials'].append(entry)
            self._save()
            trial.set_user_attr('identity_hash', self.identity_hash)
            call = self._call(threshold, 'L1', trial.number)
            entry.update(status=call['status'], call_id=call['call_id'])
            self._save()

    def _promote(self, source: str, level: str) -> None:
        key = source + '_to_' + level
        if key not in self.state['promotions']:
            # Freeze promotion membership before any higher-fidelity score is observed.
            unique = {}
            for call in self._rank(source):
                unique.setdefault(call['threshold'], call)
            self.state['promotions'][key] = [{'threshold': c['threshold'], 'parent_call_id': c['call_id']}
                                           for c in list(unique.values())[:self.LIMITS[level]]]
            self._save()
        for candidate in self.state['promotions'][key]:
            attempted = [c for c in self.state['calls'] if c['level'] == level and c['threshold'] == candidate['threshold']]
            if attempted:  # Failure and interrupted calls are not retried without bound.
                continue
            call = self._call(candidate['threshold'], level)
            call['parent_call_id'] = candidate['parent_call_id']
            self._save()

    def _summary(self) -> dict:
        ranked = {level: self._rank(level) for level in self.LIMITS}
        correlations = {}
        for source, target in (('L1', 'L2'), ('L1', 'L3'), ('L2', 'L3')):
            source_scores = {c['threshold']: c['result'].get('metrics', c['result'])['f1'] for c in ranked[source]}
            shared = [c for c in ranked[target] if c['threshold'] in source_scores]
            correlations[source + '_' + target] = correlation_interval(
                [source_scores[c['threshold']] for c in shared],
                [c['result'].get('metrics', c['result'])['f1'] for c in shared], seed=self.seed)
        return {'status': self.state['status'], 'identity_hash': self.identity_hash, 'calls': self.state['calls'],
                'call_counts': {level: sum(c['level'] == level for c in self.state['calls']) for level in self.LIMITS},
                'rankings': ranked, 'best': ranked['L3'][0] if ranked['L3'] else None, 'correlations': correlations,
                'correlation_scope': 'Post-hoc correlations on promoted survivors; selection-biased, not a calibration gate.',
                'independent_holdout': False}

    def run(self) -> dict:
        with (self.root / 'coordinator.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LocalSearchStopped('Another local search coordinator owns this output root') from None
            try:
                self.state = {}
                self._open()
                self.state['status'] = 'RUNNING'
                self._save()
                self._recover()
                self._l1()
                self._promote('L1', 'L2')
                self._promote('L2', 'L3')
                self.state['status'] = 'COMPLETE'
                self._save()
                self._exports()
                summary = self._summary()
                _write(self.root / 'summary.json', summary)
                return summary
            except BaseException as error:
                detail = self._safe_error(error)
                if self.state:
                    self.state.update(status='STOPPED', stop_reason=detail)
                    self._save()
                    self._exports()
                raise LocalSearchStopped(detail) from None
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
