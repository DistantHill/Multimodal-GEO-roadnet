"""Point snapshots are included in initial writes, preserving source coordinates."""
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np

from track.v2.cli.init_db import DDL_STATEMENTS
from track.v2.db import write_refine_results as refine_writer
from track.v2.db.point_geometry import entry_exit_ewkt, point_ewkt
from track.v2.models import ExcludedBatch


def test_all_point_tables_have_geometry():
    ddl = dict(DDL_STATEMENTS)
    for table in ('trajectory_points', 'kept_trajectory_points', 'excluded_points',
                  'injected_points', 'resolved_subtrack_points',
                  'candidate_entry_exit_points', 'turn_events_geom', 'trun_hot_ponits'):
        assert 'geometry(Point, 4326)' in ddl[table], table
    assert 'geometry(MultiPoint, 4326)' in ddl['entry_exit_points']


def test_point_text_preserves_longitude_latitude_and_pair_order():
    assert point_ewkt((113.12345678901234, 30.25)) == 'SRID=4326;POINT(113.12345678901234 30.25)'
    assert entry_exit_ewkt((113., 30.), (114., 31.)) == 'SRID=4326;MULTIPOINT((113.0 30.0),(114.0 31.0))'


def test_excluded_points_write_coordinates_with_the_source_key(monkeypatch):
    calls = []
    monkeypatch.setattr(refine_writer, '_chunked_insert', lambda cur, table, columns, rows, **kw: calls.append((table, columns, rows)))
    excluded = ExcludedBatch('source', np.array([7], dtype=np.int64), np.array(['stationary']), np.array([[113., 30.]]))
    assert refine_writer.insert_excluded_points(SimpleNamespace(cursor=lambda: nullcontext(None)), excluded, refine_id='refine') == 1
    table, columns, rows = calls[0]
    assert table == 'excluded_points'
    assert dict(zip(columns, rows[0])) == {'refine_id': 'refine', 'source_row_key': 7,
                                         'reason': 'stationary', 'geom': 'SRID=4326;POINT(113.0 30.0)'}
