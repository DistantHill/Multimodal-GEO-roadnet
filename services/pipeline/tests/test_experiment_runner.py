import json
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
    payload['schemes']={'turn':{'detect':{'use_cross_track_candidates':False}},'joint':{}}
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
