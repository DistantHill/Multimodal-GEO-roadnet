"""CLI assembly stays offline in tests; real search is not launched."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from track.v2.experiments.intersection_hpo import local_cli as cli


def config_file(tmp_path, extra=''):
    path = tmp_path / 'hpo.toml'
    path.write_text('[local_hpo]\nexperiment_config="experiment.toml"\nscheme="only"\nlabels_file="labels.geojson"\noutput_root="output"\nparent_refine_id="6c9acb0c-f356-49ce-81d0-2dd7dad51e68"\n' + extra)
    (tmp_path / 'experiment.toml').write_text('source')
    (tmp_path / 'labels.geojson').write_text('{}')
    return path


def test_config_paths_and_fixed_search_options(tmp_path):
    path = config_file(tmp_path)
    config = cli.load_local_config(path)
    assert config.labels_file == tmp_path / 'labels.geojson'
    assert config.processing_srid == 32649
    assert config.labels_layer == 'TP_intersection_fuping'
    path = config_file(tmp_path, 'trial_budget=100\n')
    with pytest.raises(ValueError, match='unknown'):
        cli.load_local_config(path)


def test_status_is_read_only_no_database_or_inputs(tmp_path, monkeypatch, capsys):
    path = config_file(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('Read-only status must not prepare or connect')
    monkeypatch.setattr(cli, 'prepare', forbidden)
    monkeypatch.setattr(cli, 'load_workspace', forbidden)
    assert cli.main(['--config', str(path), 'status']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'NOT_STARTED'
    assert not (tmp_path / 'output').exists()
    search = tmp_path / 'output' / 'search'
    search.mkdir(parents=True)
    (search / 'ledger.json').write_text(json.dumps({'status': 'STOPPED', 'calls': [{'level': 'L1', 'status': 'FAILED'}], 'stop_reason': 'OOM'}))
    assert cli.main(['status', '--config', str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['call_counts'] == {'L1': 1, 'L2': 0, 'L3': 0}


def workspace(tmp_path):
    timestamp = datetime(2023, 3, 1, tzinfo=timezone.utc)
    case = SimpleNamespace(time_start=timestamp, time_end=datetime(2023, 3, 6, tzinfo=timezone.utc), snapshot=lambda: {'source_table': 'public.trajectory_points', 'start': timestamp.isoformat()})
    config = SimpleNamespace(paths=SimpleNamespace(data_root=tmp_path), snapshot_parameters=lambda: {'fixed': 'params'})
    return SimpleNamespace(case=case, configs={'only': config}, archive_settings={'database': 'roadnet_eval'})


def test_prepare_freezes_identity_before_overwriting_inputs(tmp_path, monkeypatch):
    config = cli.load_local_config(config_file(tmp_path))
    selected = workspace(tmp_path)
    monkeypatch.setattr(cli, 'load_workspace', lambda *a, **k: selected)
    monkeypatch.setattr('track.v2.experiments.intersection_hpo.adapters.threshold_config', lambda *a: None)
    monkeypatch.setattr(cli, 'code_revision', lambda: 'fixed-code')
    calls = []
    def inputs(*args, **kwargs):
        calls.append(kwargs)
        return {'sha256': 'inputs', 'levels': {}}
    monkeypatch.setattr(cli, 'prepare_local_inputs', inputs)
    monkeypatch.setattr(cli, '_prepare_caches', lambda *args: {'cache_key': 'cache'})
    monkeypatch.setattr(cli, '_archive_preflight', lambda *args: None)
    _, _, _, identity = cli.prepare(config)
    assert calls[0]['seed'] == 42 and calls[0]['sizes'] == (20, 50)
    assert identity['inputs_sha256'] == 'inputs'
    assert identity['evaluation'] == {'mode': 'center_distance', 'distance_m': 30., 'region_policy': 'centroid_covers'}
    assert identity['label_semantics'] == 'location_region'
    cli.prepare(config)
    config.labels_file.write_text('changed')
    with pytest.raises(ValueError, match='identity changed'):
        cli.prepare(config)
    assert len(calls) == 2


def test_time_required_and_errors_redact_credentials(tmp_path, monkeypatch, capsys):
    path = config_file(tmp_path)
    monkeypatch.setenv('POSTGRES_PASSWORD', 'secret@pw')
    def failure(*args):
        raise ValueError('input secret@pw secret%40pw')
    monkeypatch.setattr(cli, 'prepare', failure)
    assert cli.main(['prepare', '--config', str(path)]) == 2
    output = capsys.readouterr().out
    assert 'secret' not in output and '[REDACTED]' in output


def test_run_wires_postgres_only_and_exports_selected_threshold(tmp_path, monkeypatch):
    config = cli.load_local_config(config_file(tmp_path))
    selected = workspace(tmp_path)
    selected.archive_settings.update(host='archive', port=5432, user='u', password='private')
    monkeypatch.setattr(cli, 'prepare', lambda *args: (selected, {'input': 'fixed'}, {'cache_key': 'fixed'}, {'identity': 'fixed'}))
    observed = {}
    class Evaluator:
        def __init__(self, *args):
            observed['evaluator'] = args
    class Search:
        def __init__(self, root, evaluator, storage, identity, seed):
            observed.update(root=root, storage=storage, identity=identity, seed=seed)
        def run(self):
            return {'best': {'threshold': 57.25}}
    monkeypatch.setattr('track.v2.experiments.intersection_hpo.local_adapter.LocalEvaluator', Evaluator)
    monkeypatch.setattr(cli, 'LocalSearch', Search)
    monkeypatch.setattr(cli, 'write_local_best', lambda cfg, workspace, inputs, threshold: observed.update(best=threshold))
    cli.run(config)
    assert observed['storage'].startswith('postgresql+psycopg:')
    assert observed['storage'].endswith('roadnet_eval?sslmode=prefer')
    assert observed['seed'] == 42
    assert observed['best'] == 57.25
    assert observed['evaluator'][2] == config.parent_refine_id


def test_best_export_uses_frozen_deduplicated_truth_and_resume_identity(tmp_path, monkeypatch):
    from dataclasses import make_dataclass
    import tomllib
    config = cli.load_local_config(config_file(tmp_path))
    selected = workspace(tmp_path)
    selected.case.name = 'sample'
    selected.root = tmp_path / 'experiments'
    selected.payload = {'experiment': {'case': 'sample', 'schemes': ['only']},
                        'schemes': {'only': {'detect': {'old': True}}},
                        'cases': {'sample': {'bbox': [[111, 30], [112, 31]], 'truth_file': 'obsolete.geojson'}},
                        'paths': {}, 'parser': {}, 'logging': {}}
    Paths = make_dataclass('Paths', ['data_root', 'parquet_directory', 'duckdb_postgres_extension_path'])
    effective = SimpleNamespace(detect=SimpleNamespace(as_dict=lambda: {'min_turn_deg': 57.25}),
             parser=SimpleNamespace(field_specs_path=tmp_path / 'fields.toml'),
             logging=SimpleNamespace(directory=tmp_path / 'logs'),
             paths=Paths(tmp_path, tmp_path / 'parquet', tmp_path / 'extension'))
    monkeypatch.setattr('track.v2.experiments.intersection_hpo.adapters.threshold_config', lambda *a: effective)
    monkeypatch.setattr(cli, 'code_revision', lambda: 'fixed-code')
    before = cli._config_identity(config, selected)
    truth = {'type': 'FeatureCollection', 'features': [{'id': 'unique'}]}
    ordinary, resume = cli.write_local_best(config, selected, {'levels': {'L3': {'truth': truth}}}, 57.25)
    payload = tomllib.loads(ordinary.read_text())
    assert json.loads(Path(payload['cases']['sample']['truth_file']).read_text()) == truth
    assert 'Original case region differs' in ordinary.read_text()
    assert payload['evaluation'] == {'mode': 'center_distance', 'distance_m': 30.}
    assert payload['schemes']['only']['detect']['min_turn_deg'] == 57.25
    assert cli._config_identity(cli.load_local_config(resume), selected) == before


def test_container_code_revision_retains_real_commit_prefix(monkeypatch):
    from track.v2.experiments.intersection_hpo import config as shared
    import subprocess
    commit = 'a' * 40
    monkeypatch.setenv('PIPELINE_GIT_COMMIT', commit)
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ['git'])
    monkeypatch.setattr(shared.subprocess, 'run', unavailable)
    assert shared.code_revision().startswith(commit + ':source-sha256:')


@pytest.mark.parametrize('missing', [False, True])
def test_archive_preflight_reads_metadata_without_initializing(monkeypatch, missing):
    from contextlib import contextmanager
    observed = {}
    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, statement):
            observed['statement'] = statement
        def fetchone(self):
            return ('eval_experiments', None if missing else 'eval_predictions', 'eval_evaluations', 'eval_truths', '3.5')
    class Connection:
        read_only = False
        def cursor(self):
            return Cursor()
    connection = Connection()
    @contextmanager
    def connect(settings):
        assert settings['database'] == 'roadnet_eval'
        yield connection
    monkeypatch.setattr('pipeline.experiments.archive.connect_archive', connect)
    selected = SimpleNamespace(archive_settings={'database': 'roadnet_eval'})
    if missing:
        with pytest.raises(ValueError, match='four existing'):
            cli._archive_preflight(selected)
    else:
        cli._archive_preflight(selected)
    assert connection.read_only is True
    assert observed['statement'] == cli.ARCHIVE_PREFLIGHT_SQL


def test_old_iou_objective_cannot_resume_into_location_objective(tmp_path, monkeypatch):
    config = cli.load_local_config(config_file(tmp_path))
    selected = workspace(tmp_path)
    monkeypatch.setattr(cli, 'load_workspace', lambda *a, **k: selected)
    monkeypatch.setattr('track.v2.experiments.intersection_hpo.adapters.threshold_config', lambda *a: None)
    monkeypatch.setattr(cli, 'code_revision', lambda: 'fixed-code')
    monkeypatch.setattr(cli, '_archive_preflight', lambda *a: None)
    monkeypatch.setattr(cli, '_prepare_caches', lambda *a: {'cache_key': 'cache'})
    calls = []
    def inputs(*args, **kwargs):
        calls.append(True)
        return {'sha256': 'inputs', 'levels': {}}
    monkeypatch.setattr(cli, 'prepare_local_inputs', inputs)
    cli.prepare(config)
    marker = config.output_root / 'preparation.json'
    old = json.loads(marker.read_text())
    old['config_identity']['evaluation'] = {'mode': 'both', 'distance_m': 30., 'iou_threshold': .70, 'region_policy': 'centroid_covers'}
    old['config_identity'].pop('label_semantics')
    marker.write_text(json.dumps(old))
    before = marker.read_bytes()
    with pytest.raises(ValueError, match='identity changed'):
        cli.prepare(config)
    assert len(calls) == 1
    assert marker.read_bytes() == before
