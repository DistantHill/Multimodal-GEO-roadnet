"""Cleanup protects consumers that have not persisted any points yet."""
import os
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from pipeline.experiments.archive import ArchiveStore
from track.v2.cli.experiment import cleanup_experiment
from v2.config_factory import pipeline_config
from v2.test_refine_parquet_postgres import refine_database, _seed

pytestmark=pytest.mark.skipif(not os.environ.get('ROADNET_TEST_DSN'),reason='ROADNET_TEST_DSN not set')


@pytest.fixture
def cleanup_case(refine_database,tmp_path,monkeypatch):
    conn,postgres,schema=refine_database
    refine_id,_=_seed(conn,schema)
    own_id=uuid4()
    conn.execute("INSERT INTO detect_runs(id,refine_id,status) VALUES (%s,%s,'running')",(own_id,refine_id))
    conn.execute("UPDATE detect_runs SET status='failed' WHERE id=%s",(own_id,))
    archive=ArchiveStore(conn);archive.initialize()
    config=replace(pipeline_config(),postgres=postgres)
    experiment_id=str(uuid4())
    record={'id':experiment_id,'batch_id':str(uuid4()),'case_name':'case','scheme_name':'scheme','status':'failed',
        'parameters':config.snapshot_parameters(),'refine_id':str(refine_id),'detect_id':str(own_id)}
    archive.save_experiment(record,[])
    conn.commit()
    scoped=conn.info.dsn
    monkeypatch.setattr('track.v2.config.connect',lambda config:psycopg.connect(scoped,password=postgres.password))
    @contextmanager
    def archived(settings):
        with psycopg.connect(scoped,password=postgres.password) as other:
            yield other
    monkeypatch.setattr('pipeline.experiments.archive.connect_archive',archived)
    workspace=SimpleNamespace(archive_settings={},schemes=['main'],configs={'main':config})
    return conn,archive,workspace,experiment_id,refine_id,own_id


def test_running_detect_without_points_keeps_shared_refine(cleanup_case):
    conn,archive,workspace,experiment_id,refine_id,own_id=cleanup_case
    other_id=uuid4()
    conn.execute("INSERT INTO detect_runs(id,refine_id,status) VALUES (%s,%s,'running')",(other_id,refine_id))
    conn.commit()
    cleanup_experiment(workspace,experiment_id)
    assert conn.execute('SELECT count(*) FROM kept_trajectory_points WHERE refine_id=%s',(refine_id,)).fetchone()[0]==2
    assert conn.execute('SELECT status FROM detect_runs WHERE id=%s',(other_id,)).fetchone()[0]=='running'


def test_any_downstream_fitting_prevents_experiment_cleanup(cleanup_case):
    conn,archive,workspace,experiment_id,refine_id,own_id=cleanup_case
    conn.execute("""INSERT INTO fit_runs(id,detect_id,clip_algorithm,clip_version,fitting_algorithm,fitting_version,status)
                    VALUES (%s,%s,'fixture','1','fixture','1','running')""",(uuid4(),own_id))
    conn.commit()
    with pytest.raises(ValueError,match='downstream Fitting'):
        cleanup_experiment(workspace,experiment_id)
    assert not archive.get_experiment(experiment_id)['cleaned']
    assert conn.execute('SELECT count(*) FROM kept_trajectory_points WHERE refine_id=%s',(refine_id,)).fetchone()[0]==2
