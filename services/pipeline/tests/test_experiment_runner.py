import json
import copy
import tomllib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from track.v2.cli.experiment import toml_text
from track.v2.db.pipeline_store import DetectPersistenceResult
from track.v2.experiments.config import load_workspace, refine_identity
from track.v2.experiments import runner


def workspace(tmp_path):
    root=Path(__file__).resolve().parents[1]
    payload=tomllib.loads((root/'config/pipeline-roadnet-test.toml').read_text())
    payload['paths']['data_root']=str(tmp_path)
    payload['paths']['parquet_directory']=str(tmp_path/'parquet')
    payload['parser']['field_specs_path']=str(root/'config/field_specs.toml')
    payload['logging']['directory']=str(tmp_path/'logs')
    payload['experiment']={'case':'case','schemes':['turn','joint']}
    payload['cases']={'case':{'source_table':'trajectory_points','time_start':'2023-03-01T00:00:00+08:00',
        'time_end':'2023-03-02T00:00:00+08:00','bbox':[[111,30],[112,31]]}}
    single = copy.deepcopy(payload['detect'])
    single['candidates'] = ['g_star']
    payload['schemes']={'turn':{'detect':single},'joint':{}}
    path=tmp_path/'experiment.toml';path.write_text(toml_text(payload))
    return load_workspace(path,password='testing-secret')


def setup_stages(monkeypatch, *, failed=False):
    @contextmanager
    def connection(config):
        yield object()
    class Store:
        refine_id=None
        detect_id=None
        def __init__(self, connection):pass
    monkeypatch.setattr(runner,'connect',connection)
    monkeypatch.setattr(runner,'TrackingStore',Store)
    monkeypatch.setattr(runner,'require_current_schema',lambda conn:None)
    monkeypatch.setattr('track.v2.db.source_table.validate_source_table',lambda *args:None)
    monkeypatch.setattr(runner,'init_logger',lambda *args:None)
    monkeypatch.setattr(runner,'shutdown_logger',lambda:None)
    monkeypatch.setattr(runner,'code_commit',lambda:'a'*40)
    counts={'refine':0,'detect':0}
    def refine(**kwargs):
        counts['refine']+=1
        return SimpleNamespace(ref=SimpleNamespace(refine_id='refine'),source_point_count=3)
    def detect(**kwargs):
        counts['detect']+=1
        if failed and counts['detect']==1:
            raise RuntimeError('first scheme failed')
        return SimpleNamespace(ref=SimpleNamespace(detect_id='detect-'+str(counts['detect'])),
            persisted=DetectPersistenceResult(3,0,0,0,0,0,0))
    monkeypatch.setattr(runner,'run_refine_stage',refine)
    monkeypatch.setattr(runner,'run_detect_stage',detect)
    monkeypatch.setattr(runner,'source_and_candidate_stats',lambda *args:{'source_point_count':3,'hotspot_event_count':0,'cross_track_candidate_count':0,'candidate_point_count':0})
    monkeypatch.setattr(runner,'candidate_instance_stats',lambda *args:[])
    monkeypatch.setattr(runner,'predictions_from_detect',lambda *args:{'type':'FeatureCollection','features':[]})
    def archive(workspace,directory):
        record=json.loads((directory/'manifest.json').read_text())
        record['archive_state']='archived'
        return record
    monkeypatch.setattr(runner,'archive_local',archive)
    return counts


def test_batch_continues_failure_and_shares_refine_and_restores_frozen_case(tmp_path,monkeypatch):
    ws=workspace(tmp_path)
    counts=setup_stages(monkeypatch,failed=True)
    records=runner.run_batch(ws)
    assert [record['status'] for record in records]==['failed','completed']
    assert counts=={'refine':1,'detect':2}
    assert records[1]['refine_id']=='refine'
    assert records[1]['stats']['source_point_count']==3
    assert records[1]['stats']['rss_scope']=='process_lifetime'
    assert records[0]['error']=='RuntimeError: first scheme failed'
    restore=tmp_path/'restored.toml'
    restore.write_text(toml_text(records[1]['draft_snapshot']))
    restored=load_workspace(restore,password='testing-secret')
    assert restored.case.geometry==ws.case.geometry
    assert restored.schemes==('joint',)
    assert restored.configs['joint'].detect==ws.configs['joint'].detect
    assert Path(restored.configs['joint'].parser.field_specs_path).is_file()


def test_refine_identity_differs_for_input_and_refine_not_detect(tmp_path):
    ws=workspace(tmp_path)
    turn=ws.configs['turn'];joint=ws.configs['joint']
    assert refine_identity(ws.case,turn)==refine_identity(ws.case,joint)
    changed=replace(ws.case,time_end=ws.case.time_end.replace(hour=12))
    assert refine_identity(changed,turn)!=refine_identity(ws.case,turn)


def test_unavailable_commit_stops_before_running_batch(tmp_path,monkeypatch):
    ws=workspace(tmp_path)
    monkeypatch.setattr(runner,'code_commit',lambda:None)
    with pytest.raises(ValueError,match='commit'):
        runner.run_batch(ws)
    assert not (ws.root/'batches').exists()


def test_cli_returns_failure_for_evaluation_error(monkeypatch,tmp_path,capsys):
    import sys
    from track.v2.cli import experiment
    monkeypatch.setattr(sys,'argv',['experiment','run'])
    monkeypatch.setattr(experiment,'load_workspace',lambda *args,**kwargs:object())
    monkeypatch.setattr(experiment,'run_batch',lambda *args,**kwargs:[{'status':'completed','archive_state':'archived','evaluation_error':'invalid truth'}])
    assert experiment.main()==1


@pytest.mark.parametrize('all_time', [False, True])
def test_explicit_week_refine_accepts_case_slice_or_empty_time(tmp_path, monkeypatch, all_time):
    from datetime import timedelta
    from track.v2.projection import create_spatial_context
    ws = workspace(tmp_path)
    counts = setup_stages(monkeypatch)
    history = SimpleNamespace(id='week',status='completed',source_table=ws.case.source_table,
        spatial_context=create_spatial_context(ws.configs['turn'].projection),
        parameters={'source_selection': {
            'time_start': (ws.case.time_start-timedelta(days=1)).isoformat(),
            'time_end': (ws.case.time_end+timedelta(days=5)).isoformat()}})
    if all_time:
        ws = replace(ws,case=replace(ws.case,time_start=None,time_end=None))
    base_store = runner.TrackingStore
    class Store(base_store):
        def get_refine_run(self, refine_id):
            assert refine_id == 'week'
            return history
    monkeypatch.setattr(runner,'PostgresPipelineStore',Store)
    monkeypatch.setattr(runner,'TrackingStore',Store)
    expected = (ws.case.time_start,ws.case.time_end)
    detect = runner.run_detect_stage
    def checked_detect(**kwargs):
        ref = kwargs['refine']
        assert (ref.time_start,ref.time_end) == expected
        return detect(**kwargs)
    monkeypatch.setattr(runner,'run_detect_stage',checked_detect)
    selections = []
    def stats(*args, **kwargs):
        selections.append(kwargs)
        return {'source_point_count':3}
    monkeypatch.setattr(runner,'source_and_candidate_stats',stats)
    records = runner.run_batch(ws,explicit_refine_id='week')
    assert [r['status'] for r in records] == ['completed','completed']
    assert counts == {'refine':0,'detect':2}
    assert selections == ([{},{}] if all_time else [dict(time_start=expected[0],time_end=expected[1])]*2)
    if all_time:
        assert records[0]['draft_snapshot']['cases']['case']['time_start'] == ''
        restored = tmp_path/'restored-empty.toml'
        restored.write_text(toml_text(records[0]['draft_snapshot']))
        assert load_workspace(restored,password='testing-secret').case.time_start is None


def test_independent_instances_share_refine_and_archive_each_identity(tmp_path, monkeypatch):
    from dataclasses import replace
    from track.v2.config.detect_config import CandidateAlgorithmConfig
    ws = workspace(tmp_path)
    base = ws.configs['turn']
    params = base.detect.candidate['g_star'].params['default']
    definition = CandidateAlgorithmConfig(('low',), {'low': params, 'high': replace(params, min_turn_deg=60.)})
    configured = replace(base, detect=replace(base.detect, candidates=('g_star',), candidate={'g_star': definition}))
    ws = replace(ws, schemes=('turn',), configs={'turn': configured})
    counts = setup_stages(monkeypatch)
    selected = []
    detect = runner.run_detect_stage
    def checked(**kwargs):
        selected.append(kwargs['detect_config'].candidate['g_star'].output_instances)
        return detect(**kwargs)
    monkeypatch.setattr(runner, 'run_detect_stage', checked)
    records = runner.run_batch(ws, independent_instances=True)
    assert counts == {'refine': 1, 'detect': 2}
    assert selected == [('low',), ('high',)]
    assert [record['instance_evaluation']['instance_id'] for record in records] == ['low', 'high']
    assert records[1]['instance_evaluation']['params']['min_turn_deg'] == 60.
    assert records[1]['draft_snapshot']['detect']['candidate']['g_star']['output_instances'] == ['high']
    assert configured.detect.candidate['g_star'].output_instances == ('low',)
