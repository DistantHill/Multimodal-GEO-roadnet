"""Clip cursor planning is scoped; small metadata never joins the point stream."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from track.v2.intersections.query_results import fetch_resolved_clip_tracks
from track.v2.db.plan_full_scan_cursor import declare_full_scan_cursor
from .test_clip_fitting_streaming import Connection
from .test_clip import _CONTEXT


def test_default_full_scan_does_not_touch_nested_loop_setting():
    statements = []

    class Settings:
        def execute(self, query, params=()):
            statements.append((query, params))

        def fetchone(self):
            return ("0.37",)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Conn:
        def cursor(self):
            return Settings()

        def transaction(self):
            return nullcontext()

    class Declared:
        def execute(self, query, params):
            statements.append((query, params))

    declare_full_scan_cursor(Conn(), Declared(), "SELECT 1", ())
    assert all("enable_nestloop" not in query for query, _params in statements)


def test_discouraged_nested_loop_is_scoped_to_declare():
    state = {"fraction": "0.37", "nested_loop": "on"}

    class Settings:
        row = None

        def execute(self, query, params=()):
            if "current_setting('cursor_tuple_fraction')" in query:
                self.row = (state["fraction"], state["nested_loop"])
            elif query.startswith("SET LOCAL cursor_tuple_fraction"):
                state["fraction"] = "1"
            elif query.startswith("SET LOCAL enable_nestloop"):
                state["nested_loop"] = "off"
            elif "set_config('enable_nestloop'" in query:
                state["fraction"], state["nested_loop"] = params

        def fetchone(self):
            return self.row

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Conn:
        def cursor(self):
            return Settings()

        def transaction(self):
            return nullcontext()

    class Declared:
        def execute(self, _query, _params):
            assert state == {"fraction": "1", "nested_loop": "off"}

    declare_full_scan_cursor(
        Conn(), Declared(), "SELECT 1", (), discourage_nested_loop=True,
    )
    assert state == {"fraction": "0.37", "nested_loop": "on"}


@pytest.mark.parametrize("failure", [False, True])
def test_declare_uses_full_consumption_and_restores_callers_fraction(failure):
    conn = Connection([])

    class DeclareCursor:
        def execute(self, query, params):
            assert conn.fraction == "1"
            if failure:
                raise ValueError("DECLARE failed")

    if failure:
        with pytest.raises(ValueError, match="DECLARE failed"):
            declare_full_scan_cursor(conn, DeclareCursor(), "SELECT 1", ())
    else:
        declare_full_scan_cursor(conn, DeclareCursor(), "SELECT 1", ())
    assert conn.fraction == "0.37"


def test_intersection_lookup_preserves_missing_candidates_and_point_order():
    rows = [(1, 1, 0, "v", 1., 2., None, None, None),
            (2, 1, 1, None, 1., 2., 101, 10, 1),
            (3, 1, 2, None, 1., 2., 102, 99, 0),
            (4, 1, 3, "v", 1., 2., None, None, None)]
    conn = Connection(rows)
    with (
        patch("track.v2.intersections.query_results.fetch_detect_row", return_value=SimpleNamespace(status="completed", source_table="s", spatial_context=_CONTEXT)),
        patch("track.v2.intersections.query_results.validate_source_table", return_value=SimpleNamespace(quoted_table='"s"', key_sql_type="bigint")),
    ):
        tracks = list(fetch_resolved_clip_tracks(conn, "d", batch_size=2000))
    assert tracks[0].resolved_point_ids.tolist() == [1, 2, 3, 4]
    assert [int(identifier) if valid else None for identifier, valid in zip(tracks[0].intersection_ids, tracks[0].intersection_valid)] == [None, 10, None, None]
    assert [int(identifier) if valid else None for identifier, valid in zip(tracks[0].crossing_ids, tracks[0].crossing_valid)] == [None, 101, 102, None]
    assert conn.fraction == "0.37"
    assert conn.value.closed


def test_postgres_declare_restores_settings_on_success_error_and_early_close():
    import os
    import uuid
    import psycopg

    dsn = os.getenv("ROADNET_TEST_DSN")
    if not dsn:
        pytest.skip("ROADNET_TEST_DSN is not set")
    with psycopg.connect(dsn) as conn:
        conn.execute("SET LOCAL cursor_tuple_fraction = 0.37")
        with conn.cursor(name="clip_settings_" + uuid.uuid4().hex) as cur:
            declare_full_scan_cursor(conn, cur, "SELECT generate_series(1, 10)", ())
            assert conn.execute("SHOW cursor_tuple_fraction").fetchone()[0] == "0.37"
            assert cur.fetchone() == (1,)
        assert conn.execute("SHOW cursor_tuple_fraction").fetchone()[0] == "0.37"
        with conn.cursor(name="clip_failure_" + uuid.uuid4().hex) as cur:
            with pytest.raises(psycopg.errors.UndefinedTable):
                declare_full_scan_cursor(conn, cur, "SELECT * FROM nonexistent_clip_table_" + uuid.uuid4().hex, ())
        assert conn.execute("SHOW cursor_tuple_fraction").fetchone()[0] == "0.37"
        # An error rolled back only the declaration savepoint, not the caller's transaction.
        assert conn.execute("SELECT 123").fetchone() == (123,)
        conn.rollback()


@pytest.mark.parametrize('batch_size', [1, 2])
def test_replay_uses_persisted_xy_releases_prior_chunks_and_closes_early(batch_size):
    import weakref
    import numpy as np
    import track.v2.intersections.query_results as module
    rows = [(1,37,0,'same',120.,30.,None,None,None,123.25,-456.5),
            (2,37,1,'same',120.1,30.1,None,None,None,124.25,-455.5),
            (3,212,0,'same',121.,31.,None,None,None,9.,10.)]
    conn = Connection(rows)
    references = []
    original = np.fromiter
    def capture(*args, **kwargs):
        column = original(*args, **kwargs)
        references.append(weakref.ref(column))
        return column
    with patch.object(module,'fetch_detect_row',return_value=SimpleNamespace(status='completed',source_table='s',spatial_context=_CONTEXT)), \
         patch.object(module,'validate_source_table',return_value=SimpleNamespace(quoted_table='"s"',key_sql_type='bigint')), \
         patch.object(module.np,'fromiter',side_effect=capture):
        iterator = fetch_resolved_clip_tracks(conn,'d',batch_size=batch_size)
        track = next(iterator)
        np.testing.assert_array_equal(track.xy, [[123.25,-456.5],[124.25,-455.5]])
        assert track.subtrack_index == 37
        assert not track.xy.flags.writeable
        assert all(reference() is None for reference in references[:11])
        iterator.close()
    assert conn.value.closed
    assert conn.fraction == '0.37'
