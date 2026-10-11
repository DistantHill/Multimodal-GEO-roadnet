"""Real Optuna with injected archived results; no production search or SQLite."""
import json
from pathlib import Path
import random

import optuna
import pytest

from track.v2.experiments.intersection_hpo.local_search import LocalSearch, LocalSearchStopped, NumericalTrialError


class Evaluator:
    def __init__(self, root: Path):
        self.root = root
        self.calls = []
        self.archived = {}
        self.error_at = None
        self.error = None
        self.archive_then_crash = False

    def __call__(self, threshold, level, call_id):
        self.calls.append((threshold, level, call_id))
        # Ledger is committed and charged before the evaluator starts.
        ledger = json.loads((self.root / 'ledger.json').read_text())
        assert ledger['calls'][-1]['call_id'] == call_id
        assert ledger['calls'][-1]['status'] == 'RUNNING'
        if self.error_at == len(self.calls):
            raise self.error
        f1 = threshold / 100 if level != 'L3' else 1 - threshold / 100
        result = {'metrics': {'f1': f1, 'recall': f1, 'precision': f1, 'tp': 1, 'fp': 0, 'fn': 0}, 'experiment_id': call_id, 'wall_seconds': .001}
        self.archived[call_id] = result
        if self.archive_then_crash:
            self.archive_then_crash = False
            # Simulate SIGKILL: independently archived transaction exists, but
            # local completion has never been persisted.
            raise KeyboardInterrupt('injected crash')
        return result

    def lookup(self, call_id):
        return self.archived.get(call_id)


def search(tmp_path, evaluator=None, storage=None, identity=None):
    return LocalSearch(tmp_path, evaluator or Evaluator(tmp_path), storage or optuna.storages.InMemoryStorage(), identity or {'refine': 'fixed', 'labels': 'hash'}, seed=42)


def test_real_tpe_fixed_baseline_random_and_40_12_8(tmp_path):
    evaluator = Evaluator(tmp_path)
    controller = search(tmp_path, evaluator)
    summary = controller.run()
    assert summary['call_counts'] == {'L1': 40, 'L2': 12, 'L3': 8}
    rng = random.Random(42)
    assert [c[0] for c in evaluator.calls[:10]] == [45.] + [rng.uniform(30., 90.) for _ in range(9)]
    assert all(30 <= c[0] <= 90 for c in evaluator.calls)
    assert [c[0] for c in evaluator.calls[40:52]] == [r['threshold'] for r in summary['rankings']['L1'][:12]]
    assert [c[0] for c in evaluator.calls[52:]] == [r['threshold'] for r in summary['rankings']['L2'][:8]]
    assert summary['best']['threshold'] == min(c[0] for c in evaluator.calls[52:])
    assert summary['independent_holdout'] is False
    assert all(t.value == pytest.approx(t.params['min_turn_deg'] / 100) for t in controller.study.trials)
    for level, count in [('L1', 40), ('L2', 12), ('L3', 8)]:
        assert len(json.loads((tmp_path / f'{level}-ranking.json').read_text())) == count
        assert len((tmp_path / f'{level}-ranking.csv').read_text().splitlines()) == count + 1
    calls = len(evaluator.calls)
    assert controller.run() == summary
    assert len(evaluator.calls) == calls


def test_numerical_failure_charged_success_exported_immediately(tmp_path):
    evaluator = Evaluator(tmp_path)
    evaluator.error_at, evaluator.error = 2, NumericalTrialError('parameter-local convergence')
    controller = search(tmp_path, evaluator)
    summary = controller.run()
    assert summary['call_counts']['L1'] == 40
    assert len(summary['rankings']['L1']) == 39
    assert summary['calls'][1]['status'] == 'NUMERICAL_FAILED'
    assert controller.study.trials[1].state == optuna.trial.TrialState.FAIL
    assert json.loads((tmp_path / 'L1-ranking.json').read_text())[0]['status'] == 'SUCCESS'


@pytest.mark.parametrize('error', [MemoryError('OOM'), OSError('storage'), ValueError('bad input'), RuntimeError('unknown')])
def test_infrastructure_failure_stops_preserves_success_and_budget(tmp_path, error):
    evaluator = Evaluator(tmp_path)
    evaluator.error_at, evaluator.error = 2, error
    controller = search(tmp_path, evaluator)
    with pytest.raises(LocalSearchStopped):
        controller.run()
    ledger = json.loads((tmp_path / 'ledger.json').read_text())
    assert len(evaluator.calls) == 2
    assert ledger['status'] == 'STOPPED'
    assert [call['status'] for call in ledger['calls']] == ['SUCCESS', 'FAILED']
    assert len(json.loads((tmp_path / 'L1-ranking.json').read_text())) == 1
    evaluator.error_at = None
    summary = search(tmp_path, evaluator, controller.storage).run()
    assert summary['call_counts'] == {'L1': 40, 'L2': 12, 'L3': 8}
    assert len(evaluator.calls) == 60


def test_reconcile_archive_after_hard_interruption_no_duplicate_call(tmp_path):
    evaluator = Evaluator(tmp_path)
    controller = search(tmp_path, evaluator)
    evaluator.archive_then_crash = True
    with pytest.raises(LocalSearchStopped):
        controller.run()
    # Our catch records the interrupt; emulate uncatchable process termination
    # that leaves only the original charged RUNNING record on disk.
    ledger = json.loads((tmp_path / 'ledger.json').read_text())
    ledger['calls'][0]['status'] = 'RUNNING'
    (tmp_path / 'ledger.json').write_text(json.dumps(ledger))
    # Optuna would still be RUNNING on SIGKILL rather than catchable KeyboardInterrupt.
    restarted = search(tmp_path, evaluator, controller.storage)
    summary = restarted.run()
    assert summary['calls'][0]['status'] == 'SUCCESS'
    assert summary['calls'][0]['recovered'] is True
    assert sum(c[2] == summary['calls'][0]['call_id'] for c in evaluator.calls) == 1
    assert len(evaluator.calls) == 60


def test_interrupted_without_archive_stays_charged_and_not_retried(tmp_path):
    evaluator = Evaluator(tmp_path)
    controller = search(tmp_path, evaluator)
    evaluator.error_at, evaluator.error = 1, KeyboardInterrupt('interrupt')
    with pytest.raises(LocalSearchStopped):
        controller.run()
    ledger = json.loads((tmp_path / 'ledger.json').read_text())
    ledger['calls'][0]['status'] = 'RUNNING'
    (tmp_path / 'ledger.json').write_text(json.dumps(ledger))
    evaluator.error_at = None
    summary = search(tmp_path, evaluator, controller.storage).run()
    assert summary['calls'][0]['status'] == 'INTERRUPTED'
    assert summary['call_counts']['L1'] == 40
    assert len(summary['rankings']['L1']) == 39
    assert len({c[2] for c in evaluator.calls}) == 60


def test_changed_identity_does_not_touch_previous_ledger(tmp_path):
    evaluator = Evaluator(tmp_path)
    evaluator.error_at, evaluator.error = 1, MemoryError('stop')
    with pytest.raises(LocalSearchStopped):
        search(tmp_path, evaluator).run()
    before = (tmp_path / 'ledger.json').read_bytes()
    with pytest.raises(LocalSearchStopped, match='identity changed'):
        search(tmp_path, identity={'refine': 'changed'}).run()
    assert (tmp_path / 'ledger.json').read_bytes() == before


def test_sqlite_storage_rejected_and_postgres_password_redacted(tmp_path):
    with pytest.raises(ValueError, match='PostgreSQL'):
        LocalSearch(tmp_path, Evaluator(tmp_path), 'sqlite:///forbidden.db', {})
    controller = LocalSearch(tmp_path, Evaluator(tmp_path), 'postgresql+psycopg://u:secret%40word@host/roadnet_eval', {})
    assert controller._safe_error(RuntimeError('secret@word secret%40word')) == 'RuntimeError: [REDACTED] [REDACTED]'


def test_fewer_successes_promote_only_available_and_ties_are_stable(tmp_path):
    class SparseEvaluator(Evaluator):
        def __call__(self, threshold, level, call_id):
            if level == 'L1' and len(self.calls) >= 3:
                self.calls.append((threshold, level, call_id))
                raise NumericalTrialError('fixture local nonconvergence')
            result = super().__call__(threshold, level, call_id)
            result['metrics']['f1'] = .5
            return result
    evaluator = SparseEvaluator(tmp_path)
    summary = search(tmp_path, evaluator).run()
    assert summary['call_counts'] == {'L1': 40, 'L2': 3, 'L3': 3}
    assert len(evaluator.calls) == 46
    assert [r['threshold'] for r in summary['rankings']['L1']] == sorted(c[0] for c in evaluator.calls[:3])
    assert summary['best']['threshold'] == min(c[0] for c in evaluator.calls[:3])
    assert summary['correlations']['L1_L3']['spearman'] is None


def test_archive_success_survives_optuna_commit_failure(tmp_path, monkeypatch):
    evaluator = Evaluator(tmp_path)
    controller = search(tmp_path, evaluator)
    def storage_failure(call):
        raise OSError('Optuna commit unavailable after result archive')
    monkeypatch.setattr(controller, '_sync_trial', storage_failure)
    with pytest.raises(LocalSearchStopped, match='Optuna commit'):
        controller.run()
    ledger = json.loads((tmp_path / 'ledger.json').read_text())
    assert ledger['calls'][0]['status'] == 'SUCCESS'
    assert len(json.loads((tmp_path / 'L1-ranking.json').read_text())) == 1
    resumed = search(tmp_path, evaluator, controller.storage)
    summary = resumed.run()
    assert len(evaluator.calls) == 60
    assert summary['call_counts'] == {'L1': 40, 'L2': 12, 'L3': 8}
    assert resumed.study.trials[0].value == .45


def test_committed_archive_after_connection_failure_is_recovered(tmp_path):
    class CommitThenFail(Evaluator):
        def __call__(self, *args):
            result = super().__call__(*args)
            if len(self.calls) == 1:
                raise OSError('connection exit after archive commit')
            return result
    evaluator = CommitThenFail(tmp_path)
    controller = search(tmp_path, evaluator)
    with pytest.raises(LocalSearchStopped):
        controller.run()
    assert json.loads((tmp_path / 'ledger.json').read_text())['calls'][0]['status'] == 'FAILED'
    assert controller.study.trials[0].state == optuna.trial.TrialState.RUNNING
    resumed = search(tmp_path, evaluator, controller.storage)
    summary = resumed.run()
    assert summary['calls'][0]['status'] == 'SUCCESS'
    assert summary['calls'][0]['recovered'] is True
    assert len(evaluator.calls) == 60
    assert resumed.study.trials[0].value == .45
