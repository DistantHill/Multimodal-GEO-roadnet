"""Injected stage fixtures test orchestration without inventing real experiment results."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from track.v2.experiments.intersection_hpo.promote import correlation_interval, select_promotions, spearman
from track.v2.experiments.intersection_hpo.tune import Controller, postgres_storage_url


def config(tmp_path: Path, **search):
    return SimpleNamespace(output_root=tmp_path, seed=42, baseline=30.0, threshold_min=0.0, threshold_max=60.0,
                           search={'pilot_count': 10, 'trial_budget': 4, 'l1_recall_slack': 0.5,
                                   'l1_density_limit': 100.0, 'min_l2_l3_spearman': 0.5, 'promotion_top_k': 2, **search})


class Evaluator:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, threshold, fidelity, split, artifact_dir):
        self.calls.append((threshold, fidelity, split))
        artifact_dir.mkdir(parents=True)
        if self.fail:
            self.fail = False
            raise RuntimeError('injected interruption')
        value = threshold / 120 + .4
        return {'metrics': {'tp': 1, 'fp': 0, 'fn': 1, 'precision': value, 'recall': value, 'f1': value},
                'candidate_density_per_km2': 1.0, 'candidate_count': 1, 'wall_seconds': .01}


def test_pilot_baseline_first_all_levels_and_deterministic_resume(tmp_path):
    evaluator = Evaluator()
    controller = Controller(config(tmp_path), {'L1': {'hash': 'fixed'}}, evaluator)
    first = controller.pilot()
    assert first['calibrated']
    assert evaluator.calls[:3] == [(30., level, 'tune') for level in ('L1', 'L2', 'L3')]
    assert len(evaluator.calls) == 30
    second = controller.pilot()
    assert first == second
    assert len(evaluator.calls) == 30
    assert first['records'][0]['L1']['f1'] is None
    assert first['correlations']['L2']['spearman'] == pytest.approx(1)


def test_changed_identity_and_tampered_stage_rejected(tmp_path):
    controller = Controller(config(tmp_path), {}, Evaluator())
    controller.stage(30., 'L1')
    with pytest.raises(ValueError, match='identity changed'):
        Controller(config(tmp_path), {'changed': True}, Evaluator())
    path = next((tmp_path / 'results').glob('*/stage.json'))
    path.write_text(path.read_text().replace('"recall": 0.65', '"recall": 0.1'))
    with pytest.raises(ValueError, match='hash'):
        controller.stage(30., 'L1')


def test_failed_stage_retry_new_immutable_attempt(tmp_path):
    evaluator = Evaluator()
    evaluator.fail = True
    controller = Controller(config(tmp_path), {}, evaluator)
    with pytest.raises(RuntimeError):
        controller.stage(30., 'L1')
    result = controller.stage(30., 'L1')
    assert result['recall'] == .65
    assert len(list((tmp_path / 'results').glob('*/attempt-*'))) == 2
    assert len(list((tmp_path / 'results').glob('*/failure.json'))) == 1


def test_promotion_ignores_l3_and_preserves_exploration():
    records = [{'trial_id': str(i), 'threshold': i, 'L2': {'f1': i / 10, 'recall': (10-i) / 10}, 'L3': {'f1': -i}} for i in range(6)]
    chosen = select_promotions(records, top_k=2, recall_slots=1, exploration_slots=1, seed=42)
    assert [r['threshold'] for r in chosen[:3]] == [5, 4, 0]
    assert len(chosen) == 4
    assert len({r['threshold'] for r in chosen}) == 4


def test_bootstrap_ties_and_constant_scores():
    assert spearman([1, 1, 2], [1, 1, 2]) == 1
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    assert correlation_interval([1, 2, 3], [3, 2, 1], seed=4) == correlation_interval([1, 2, 3], [3, 2, 1], seed=4)


def test_tune_requires_calibration_and_tpe_resume(tmp_path, monkeypatch):
    optuna = pytest.importorskip('optuna')
    evaluator = Evaluator()
    controller = Controller(config(tmp_path), {}, evaluator)
    controller.pilot()
    storage = optuna.storages.InMemoryStorage()  # Tests only; production enforces PostgreSQL.
    records = controller.tune(storage=storage)
    assert len(records) == 4
    calls = len(evaluator.calls)
    assert controller.tune(storage=storage) == records
    assert len(evaluator.calls) == calls
    study = optuna.load_study(study_name='intersection_hpo_' + controller.identity[:24], storage=storage)
    running = study.ask()
    running.suggest_float('min_turn_deg', 0., 60.)
    controller.tune(storage=storage)
    assert study.trials[-1].state == optuna.trial.TrialState.COMPLETE
    monkeypatch.setattr('track.v2.experiments.intersection_hpo.exports.write_best_config', lambda *args: None)
    controller.promote()
    assert [call[2] for call in evaluator.calls].count('holdout') == 1
    controller.promote()
    assert [call[2] for call in evaluator.calls].count('holdout') == 1


def test_storage_is_fixed_postgres_with_safe_url():
    sqlalchemy = pytest.importorskip('sqlalchemy')
    url = postgres_storage_url({'database': 'roadnet_eval', 'password': 'a@:/ b', 'host': 'archive', 'port': 5433, 'user': 'u', 'sslmode': 'require'})
    parsed = sqlalchemy.engine.make_url(url)
    assert parsed.password == 'a@:/ b'
    assert parsed.database == 'roadnet_eval'
    assert parsed.host == 'archive'
    with pytest.raises(ValueError):
        postgres_storage_url({'database': 'roadnet'})


def test_report_exports_only_recorded_measurements(tmp_path):
    from track.v2.experiments.intersection_hpo.report import build_report
    controller = Controller(config(tmp_path), {}, Evaluator())
    controller.pilot()
    path = build_report(tmp_path, '/external/hpo.toml')
    text = path.read_text()
    assert 'CPU seconds: unavailable' in text
    assert 'L2 micro F1' in text
    assert (tmp_path / 'pilot-scatter.svg').exists()
    assert (tmp_path / 'trials.csv').read_text().count('\n') == 31


def test_constant_pilot_does_not_authorize_tune(tmp_path):
    class ConstantEvaluator(Evaluator):
        def __call__(self, threshold, fidelity, split, artifact_dir):
            result = super().__call__(threshold, fidelity, split, artifact_dir)
            result['metrics'].update(f1=.5)
            return result
    controller = Controller(config(tmp_path), {}, ConstantEvaluator())
    assert controller.pilot()['calibrated'] is False
    pytest.importorskip('optuna')
    with pytest.raises(ValueError, match='calibration'):
        controller.tune(storage=object())


def test_failure_traceback_redacts_password(tmp_path, monkeypatch):
    monkeypatch.setenv('POSTGRES_PASSWORD', 'special@pass')
    class BadEvaluator(Evaluator):
        def __call__(self, *args):
            raise RuntimeError('special@pass special%40pass')
    controller = Controller(config(tmp_path), {}, BadEvaluator())
    with pytest.raises(RuntimeError):
        controller.stage(30., 'L1')
    persisted = next((tmp_path / 'results').glob('*/failure.json')).read_text()
    assert 'special@pass' not in persisted
    assert 'special%40pass' not in persisted


def test_actual_optuna_restart_recovers_database_record(tmp_path, monkeypatch):
    optuna = pytest.importorskip('optuna')
    evaluator = Evaluator()
    controller = Controller(config(tmp_path), {}, evaluator)
    controller.pilot()
    storage = optuna.storages.InMemoryStorage()
    records = controller.tune(storage=storage)
    study = optuna.load_study(study_name='intersection_hpo_' + controller.identity[:24], storage=storage)
    assert study.user_attrs['proxy_objective'] == 'L2_micro_f1'
    assert study.trials[0].user_attrs['L2_stage_id'] == records[0]['L2']['stage_id']
    assert study.trials[0].user_attrs['record'] == records[0]
    # Simulate interruption after PostgreSQL completion but before local export.
    (tmp_path / 'trials.json').unlink()
    restarted = Controller(config(tmp_path), {}, Evaluator())
    recovered = restarted.tune(storage=storage)
    assert recovered == records
    assert not restarted.evaluator.calls
    monkeypatch.setattr('track.v2.experiments.intersection_hpo.exports.write_best_config', lambda *args: None)
    best = restarted.promote()
    calls = len(restarted.evaluator.calls)
    assert restarted.promote() == best
    assert len(restarted.evaluator.calls) == calls


def test_cli_audit_workspace_error_becomes_redacted_blocker(tmp_path, monkeypatch, capsys):
    import json
    from track.v2.experiments.intersection_hpo import __main__ as cli
    cfg = SimpleNamespace(output_root=tmp_path, experiment_config=tmp_path / 'missing.toml', scheme='threshold')
    monkeypatch.setattr(cli, 'load_hpo_config', lambda _: cfg)
    monkeypatch.setenv('POSTGRES_PASSWORD', 'secret')
    def fail(*args, **kwargs):
        raise ValueError('invalid workspace secret')
    monkeypatch.setattr(cli, 'load_workspace', fail)
    assert cli.main(['audit', '--config', str(tmp_path / 'hpo.toml')]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output['ready'] is False
    assert output['blockers'] == ['invalid workspace [REDACTED]']
    assert json.loads((tmp_path / 'audit.json').read_text()) == output


def test_cli_invalid_hpo_config_reports_blocker(tmp_path, capsys):
    from track.v2.experiments.intersection_hpo.__main__ import main
    path = tmp_path / 'empty.toml'
    path.write_text('[hpo]\n')
    assert main(['audit', '--config', str(path)]) == 2
    import json
    result = json.loads(capsys.readouterr().out)
    assert not result['ready']
    assert 'missing' in result['blockers'][0]
