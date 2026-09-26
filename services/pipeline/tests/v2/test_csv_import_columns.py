"""CSV import writes isolated PostgreSQL tables from source columns."""
import os
from unittest.mock import patch
import numpy as np
import pytest
from track.v2.models import SourceBatch
from track.v2.db.insert_trajectory_points import insert_trajectory_points
from track.v2.trajectories.read_source import detect_encoding


def batch(size=3):
    return SourceBatch('csv', '车辆甲', np.tile([120., 30.], (size, 1)),
                       np.arange(size, dtype=np.float64), np.full(size, 90.),
                       np.full(size, 2.), np.arange(size, dtype=np.int64),
                       np.ones(size, dtype=bool), np.full(size, ''),
                       {'label': np.full(size, '中文🚗'), 'number': np.arange(size)})


def test_empty_import_does_not_open_database_cursor():
    class Connection:
        def cursor(self):
            raise AssertionError('empty import must not write')
    assert insert_trajectory_points(Connection(), batch(0), batch_size=2) == 0


def test_non_utf_encoding_guess_uses_runtime_default(tmp_path):
    path = tmp_path / '中文.csv'
    path.write_bytes('vehicle_id,lon,lat\n车辆甲,120,30\n'.encode('gbk'))
    with patch('track.v2.trajectories.read_source._guess_with_chardet', return_value=('GB2312', .99)):
        assert detect_encoding(path, hint_encoding=None, sample_size=4096, default_encoding='utf-8-sig',
                               min_confidence=.5, fallback=()) == 'gb18030'


@pytest.mark.skipif(not os.environ.get('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
def test_import_bounded_copy_preserves_unicode_extra_and_values():
    import psycopg
    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as connection:
        connection.execute('''CREATE TEMP TABLE csv_import_points (
            vehicle_id text, timestamp double precision, lon double precision,
            lat double precision, heading double precision, speed double precision,
            extra jsonb) ON COMMIT DROP''')
        assert insert_trajectory_points(connection, batch(), table='pg_temp.csv_import_points', batch_size=2) == 3
        rows = connection.execute('SELECT vehicle_id,timestamp,lon,lat,heading,speed,extra FROM pg_temp.csv_import_points ORDER BY timestamp').fetchall()
        assert rows == [('车辆甲', float(i), 120., 30., 90., 2., {'label':'中文🚗', 'number':i}) for i in range(3)]
        connection.rollback()


@pytest.mark.skipif(not os.environ.get('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
def test_actual_csv_import_cli_writes_multiple_vehicles_and_closes_handle(tmp_path, capsys):
    import psycopg
    from contextlib import nullcontext
    from track.v2.cli import import_trajectories
    from track.v2.trajectories.read_source import get_database
    from .config_factory import pipeline_config
    csv = tmp_path / 'source.csv'
    csv.write_text('vehicle_id,timestamp,lon,lat,heading,speed,road_id\nA甲,0,120,30,90,36,道路甲\nA甲,1,120.1,30.1,90,36,道路乙\nB乙,2,121,31,0,0,道路丙\n', encoding='utf-8')
    config = pipeline_config()
    handles = []
    def database(*args, **kwargs):
        handle = get_database(*args, **kwargs)
        original_close = handle.close
        handle.close = lambda: (handles.append('closed'), original_close())[1]
        return handle
    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as connection:
        connection.execute('''CREATE TEMP TABLE csv_cli_points (
            vehicle_id text, timestamp double precision, lon double precision,
            lat double precision, heading double precision, speed double precision, extra jsonb)''')
        with patch.object(import_trajectories, 'load_startup_config', return_value=config), \
             patch.object(import_trajectories, 'pg_connect', return_value=nullcontext(connection)), \
             patch.object(import_trajectories, 'get_database', side_effect=database), \
             patch.object(import_trajectories, 'init_logger'), patch.object(import_trajectories, 'shutdown_logger'), \
             patch('sys.argv', ['import-trajectories','--csv',str(csv),'--table','pg_temp.csv_cli_points']):
            assert import_trajectories.main() == 0
        assert connection.execute('SELECT vehicle_id,count(*) FROM pg_temp.csv_cli_points GROUP BY vehicle_id ORDER BY vehicle_id').fetchall() == [('A甲',2),('B乙',1)]
        assert connection.execute('SELECT extra FROM pg_temp.csv_cli_points WHERE vehicle_id=%s ORDER BY timestamp',('A甲',)).fetchall() == [({'road_id':'道路甲'},),({'road_id':'道路乙'},)]
    assert handles == ['closed']
    assert '3 rows' in capsys.readouterr().out


def test_numeric_exclusion_lookup_preserves_source_order_and_last_reason():
    from dataclasses import replace
    from track.v2.models import ExcludedBatch, RefineResult, RefineReport, SpatialContext
    from track.v2.validation.resolve_point_references import replay_refine
    source = replace(batch(), source_row_key=np.array([0,-1,7],dtype=np.int64))
    excluded = [ExcludedBatch('csv',np.array([-1,0],dtype=np.int64),np.array(['first','zero']),np.tile([120.,30.],(2,1))),
                ExcludedBatch('csv',np.array([-1],dtype=np.int64),np.array(['last']),np.array([[120.,30.]]))]
    result = RefineResult([], excluded, RefineReport(0,3,0,{},[]), SpatialContext(4326,32651,{}))
    replay = replay_refine('车辆甲', {'车辆甲':source}, refine_result=result)
    assert replay.excluded_indices.tolist() == [0,1]
    assert replay.excluded_reasons.tolist() == ['zero','last']
