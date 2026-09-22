"""Full-scan planning changes only DECLARE, preserving caller transactions."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

from track.v2.db.cursors import declare_full_scan_cursor


@pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
def test_fresh_connection_keeps_cursor_alive_and_transaction_uncommitted():
    import psycopg
    from psycopg.pq import TransactionStatus

    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        assert conn.info.transaction_status == TransactionStatus.IDLE
        try:
            with conn.cursor(name='fresh_scan_' + uuid4().hex) as cur:
                declare_full_scan_cursor(conn, cur, 'SELECT generate_series(1,3)', ())
                assert conn.info.transaction_status == TransactionStatus.INTRANS
                assert cur.fetchall() == [(1,), (2,), (3,)]
        finally:
            conn.rollback()


@pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
@pytest.mark.parametrize('initial', ['on', 'off'])
def test_discouraged_nested_loop_restores_callers_setting(initial):
    import psycopg

    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        try:
            conn.execute(f'SET LOCAL enable_nestloop={initial}')
            with conn.cursor(name='nested_loop_scan_' + uuid4().hex) as cur:
                declare_full_scan_cursor(
                    conn, cur, 'SELECT generate_series(1,3)', (),
                    discourage_nested_loop=True,
                )
                assert conn.execute('SHOW enable_nestloop').fetchone() == (initial,)
                assert cur.fetchall() == [(1,), (2,), (3,)]
        finally:
            conn.rollback()


@pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
@pytest.mark.parametrize('initial', ['on', 'off'])
def test_failed_declaration_preserves_prior_writes_and_timeout(initial):
    import psycopg

    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        try:
            conn.execute('CREATE TEMP TABLE full_scan_prior_work(value int) ON COMMIT DROP')
            conn.execute('INSERT INTO full_scan_prior_work VALUES(42)')
            conn.execute(
                "SET LOCAL cursor_tuple_fraction=0.37; "
                f"SET LOCAL enable_nestloop={initial}; SET LOCAL statement_timeout='2s'"
            )
            with conn.cursor(name='failed_scan_' + uuid4().hex) as cur:
                with pytest.raises(psycopg.errors.UndefinedTable):
                    declare_full_scan_cursor(
                        conn, cur, 'SELECT * FROM missing_scan_' + uuid4().hex, (),
                        discourage_nested_loop=True,
                    )
            assert conn.execute('SELECT value FROM full_scan_prior_work').fetchone() == (42,)
            assert conn.execute('SHOW cursor_tuple_fraction').fetchone() == ('0.37',)
            assert conn.execute('SHOW enable_nestloop').fetchone() == (initial,)
            assert conn.execute('SHOW statement_timeout').fetchone() == ('2s',)
        finally:
            conn.rollback()


@pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
@pytest.mark.parametrize('discourage_nested_loop', [False, True])
def test_two_full_scan_cursors_coexist_and_restore_setting(discourage_nested_loop):
    import psycopg

    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        try:
            conn.execute("SET LOCAL cursor_tuple_fraction=0.37; SET LOCAL enable_nestloop=on")
            with conn.cursor(name='full_scan_left_' + uuid4().hex) as left, \
                 conn.cursor(name='full_scan_right_' + uuid4().hex) as right:
                declare_full_scan_cursor(
                    conn, left, 'SELECT generate_series(1,3)', (),
                    discourage_nested_loop=discourage_nested_loop,
                )
                declare_full_scan_cursor(
                    conn, right, 'SELECT generate_series(4,6)', (),
                    discourage_nested_loop=discourage_nested_loop,
                )
                assert conn.execute('SHOW cursor_tuple_fraction').fetchone() == ('0.37',)
                assert conn.execute('SHOW enable_nestloop').fetchone() == ('on',)
                assert left.fetchone() == (1,)
                assert right.fetchone() == (4,)
                assert left.fetchall() == [(2,), (3,)]
                assert right.fetchall() == [(5,), (6,)]
        finally:
            conn.rollback()
