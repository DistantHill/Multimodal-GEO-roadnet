"""Bounded persistence preserves the resolved sequence and source identities."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
import os

import pytest

from track.v2 import db_writer as writer
from track.v2.models import RefinedPointRef, HotspotCluster
from .writer_relation_helpers import run_resolved_sequence
from v2.test_db_writer import _build_resolved_rows, _crossing


@pytest.mark.parametrize('batch_size', [1, 2, 3, 100])
def test_streamed_rows_match_materialized_sequence_and_sparse_mapping(monkeypatch, tmp_path, batch_size):
    refs = [RefinedPointRef('kept', 100 + i, 1 if i < 3 else 2, i if i < 3 else i - 3)
            for i in range(5)]
    refs[1] = replace(refs[1], point_kind='injected')
    members = [writer._RefineMember(ref, 'v') for ref in refs]
    cluster = HotspotCluster(0, 0, 0, 0, 0, 1)
    crossings = [(cluster, _crossing(i, segment_start=refs[start], fraction=fraction, is_entry=entry))
                 for i, (start, fraction, entry) in enumerate([
                     (0, 0.0, True), (0, 1.0, False), (1, 0.0, True),
                     (1, 0.5, True), (1, 0.5, False), (1, 1.0 - 5e-13, False),
                     (3, 0.5, True),
                 ])]
    crossing_ids = {(0, i): 200 + i for i in range(len(crossings))}
    expected = _build_resolved_rows(
        'detect', members, tuple(range(1, 6)), crossings,
        tuple(range(6, 6 + len(crossings))), crossing_ids,
    )
    state = {'next': 1, 'closed': False}
    copied = []

    def batches(*args):
        try:
            for start in range(0, len(members), batch_size):
                yield members[start:start + batch_size]
        finally:
            state['closed'] = True

    def reserve(_cur, _table, count):
        assert count <= batch_size
        ids = tuple(range(state['next'], state['next'] + count))
        state['next'] += count
        return ids

    def copy(_cur, _table, _columns, rows, **kwargs):
        assert len(rows) <= batch_size
        copied.extend(rows)

    monkeypatch.setattr(writer, '_iter_refine_member_batches', batches)
    monkeypatch.setattr(writer, '_reserve_ids', reserve)
    monkeypatch.setattr(writer, '_copy_insert', copy)
    result = SimpleNamespace(turn_events=[], cross_track_candidates=[])
    count, mapping = run_resolved_sequence(
        SimpleNamespace(cursor=lambda: nullcontext(None)), 'detect', 'refine',
        result, crossings, crossings, crossing_ids, batch_size=batch_size, data_root=tmp_path,
    )
    assert sorted(copied, key=lambda row: (row[2], row[4])) == expected
    assert count == len(expected)
    assert mapping == {writer._refine_key(refs[i]): i + 1 for i in (0, 1, 3)}
    assert state['closed']


def test_streaming_closes_reader_after_copy_failure(monkeypatch, tmp_path):
    closed = []

    def batches(*args):
        try:
            yield [writer._RefineMember(RefinedPointRef('kept', 1, 1, 0), 'v')]
            raise AssertionError('read ahead after failed COPY')
        finally:
            closed.append(True)

    def fail(*args, **kwargs):
        raise RuntimeError('copy failed')

    monkeypatch.setattr(writer, '_iter_refine_member_batches', batches)
    monkeypatch.setattr(writer, '_reserve_ids', lambda *args: (1,))
    monkeypatch.setattr(writer, '_copy_insert', fail)
    with pytest.raises(RuntimeError, match='copy failed'):
        run_resolved_sequence(
            SimpleNamespace(cursor=lambda: nullcontext(None)), 'detect', 'refine',
            SimpleNamespace(turn_events=[], cross_track_candidates=[]), [], [], {},
            batch_size=1, data_root=tmp_path,
        )
    assert closed == [True]


def test_sparse_mapping_retains_unclustered_events_and_cross_segments(monkeypatch, tmp_path):
    refs = [RefinedPointRef('kept', i, 1, i) for i in range(5)]
    monkeypatch.setattr(writer, '_iter_refine_member_batches',
                        lambda *args: (batch for batch in [[writer._RefineMember(r, 'v') for r in refs]]))
    monkeypatch.setattr(writer, '_reserve_ids', lambda *args: tuple(range(10, 15)))
    monkeypatch.setattr(writer, '_copy_insert', lambda *args, **kwargs: None)
    result = SimpleNamespace(
        turn_events=[SimpleNamespace(refined_point_ref=refs[0])],
        cross_track_candidates=[SimpleNamespace(from_segment_start=refs[2], to_segment_start=refs[3])],
    )
    count, mapping = run_resolved_sequence(
        SimpleNamespace(cursor=lambda: nullcontext(None)), 'd', 'r', result,
        [], [], {}, batch_size=5, data_root=tmp_path,
    )
    assert count == 5
    assert mapping == {writer._refine_key(refs[i]): i + 10 for i in (0, 2, 3)}


@pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
def test_named_cursor_and_batched_copy_share_atomic_transaction(tmp_path):
    import psycopg
    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        try:
            # Temporary tables shadow runtime tables and disappear on rollback.
            conn.execute('''CREATE TEMP TABLE kept_trajectory_points (
                id bigint, refine_id text, vehicle_id text, subtrack_index int,
                sequence_in_subtrack int)''')
            conn.execute('''CREATE TEMP TABLE injected_points (
                id bigint, refine_id text, vehicle_id text, subtrack_index int,
                after_sequence_in_subtrack int)''')
            conn.execute('''CREATE TEMP TABLE resolved_subtrack_points (
                id bigserial PRIMARY KEY, detect_id text, subtrack_index int,
                refined_sequence_in_subtrack int, sequence_in_subtrack int,
                kept_point_id bigint, injected_point_id bigint, crossing_point_id bigint)''')
            conn.execute("INSERT INTO kept_trajectory_points VALUES (1,'r','v',1,0),(2,'r','v',1,1),(3,'r','w',2,0)")
            conn.execute("INSERT INTO injected_points VALUES (1,'r','v',1,0)")
            result = SimpleNamespace(turn_events=[], cross_track_candidates=[])
            count, mapping = run_resolved_sequence(
                conn, 'd', 'r', result, [], [], {}, batch_size=1, data_root=tmp_path,
            )
            assert count == 4 and mapping == {}
            assert conn.execute('''SELECT id,subtrack_index,refined_sequence_in_subtrack,
                    sequence_in_subtrack,kept_point_id,injected_point_id
                    FROM resolved_subtrack_points ORDER BY id''').fetchall() == [
                (1, 1, 0, 0, 1, None), (2, 1, 1, 1, None, 1),
                (3, 1, 2, 2, 2, None), (4, 2, 0, 0, 3, None),
            ]
            assert conn.execute("SELECT count(*) FROM pg_cursors WHERE name LIKE 'resolved_members_%'").fetchone() == (0,)
            # The caller owns rollback: even successfully copied batches vanish.
            conn.rollback()
            assert conn.execute("SELECT to_regclass('pg_temp.resolved_subtrack_points')").fetchone() == (None,)
        finally:
            conn.rollback()


@pytest.mark.parametrize('missing_ref', [False, True])
def test_empty_refine_and_missing_required_source(monkeypatch, tmp_path, missing_ref):
    monkeypatch.setattr(writer, '_iter_refine_member_batches', lambda *args: (x for x in []))
    events = [SimpleNamespace(refined_point_ref=RefinedPointRef('kept', 1, 1, 0))] if missing_ref else []
    result = SimpleNamespace(turn_events=events, cross_track_candidates=[])
    args = (SimpleNamespace(cursor=lambda: nullcontext(None)), 'd', 'r', result, [], [], {})
    if missing_ref:
        with pytest.raises(KeyError):
            run_resolved_sequence(*args, batch_size=1, data_root=tmp_path)
    else:
        assert run_resolved_sequence(*args, batch_size=1, data_root=tmp_path) == (0, {})


def test_refine_vehicle_check_crosses_fetch_batch_boundary():
    class Cursor:
        def __init__(self):
            self.calls = 0
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def execute(self, *args):
            pass

        def fetchmany(self, size):
            assert size == 1
            self.calls += 1
            return [('kept', self.calls, 'v1' if self.calls == 1 else 'v2', 1, self.calls - 1)]

    cursor = Cursor()
    conn = SimpleNamespace(cursor=lambda **kwargs: cursor)
    batches = writer._iter_refine_member_batches(conn, 'r', 1)
    assert next(batches)[0].identity.refined_sequence_in_subtrack == 0
    with pytest.raises(ValueError, match='multiple vehicles'):
        next(batches)
    assert cursor.closed


def test_short_copy_batches_keep_cumulative_heartbeat(monkeypatch, tmp_path):
    from threading import Event
    from track.v2.heartbeat import operation_heartbeat

    ticks = {1: Event(), 2: Event()}
    messages = []
    clock = [100.0]
    copies = [0]

    def emit(message):
        messages.append(message)
        for completed, tick in ticks.items():
            elapsed = (completed + 1) * 10
            if (f'，{completed} 行' in message and f'已用 {elapsed:.1f} 秒' in message
                    and '写入 Detect 解析序列' in message):
                tick.set()

    class Copy:
        def write_row(self, row):
            copies[0] += 1
            clock[0] += 10
            if copies[0] > 1:
                assert ticks[copies[0] - 1].wait(2), messages

    class Cursor:
        def copy(self, sql):
            return nullcontext(Copy())

    def batches(*args):
        for i in range(3):
            yield [writer._RefineMember(RefinedPointRef('kept', i, 1, i), 'v')]

    monkeypatch.setattr(writer, '_iter_refine_member_batches', batches)
    monkeypatch.setattr(writer, '_reserve_ids', lambda *args: (copies[0] + 1,))
    monkeypatch.setattr('track.v2.heartbeat.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('track.v2.heartbeat.log_info', lambda message: None)
    with operation_heartbeat('外层写入', emit=emit, interval=.01):
        count, _ = run_resolved_sequence(
            SimpleNamespace(cursor=lambda: nullcontext(Cursor())), 'd', 'r',
            SimpleNamespace(turn_events=[], cross_track_candidates=[]), [], [], {},
            batch_size=1, data_root=tmp_path,
        )
    assert count == 3
    assert ticks[1].is_set() and ticks[2].is_set()
    assert not any('/1 行' in message or '写入 resolved_subtrack_points' in message for message in messages)


def test_standalone_copy_still_has_its_own_heartbeat(monkeypatch):
    from threading import Event
    from track.v2.heartbeat import operation_heartbeat

    tick = Event()

    def emit(message):
        if '写入 one_copy' in message:
            tick.set()

    class Copy:
        def write_row(self, row):
            assert tick.wait(2), 'standalone COPY lost its heartbeat'

    cursor = SimpleNamespace(copy=lambda sql: nullcontext(Copy()))
    monkeypatch.setattr('track.v2.heartbeat.log_info', lambda message: None)
    with operation_heartbeat('外层', emit=emit, interval=.01):
        writer._copy_insert(cursor, 'one_copy', ['id'], [(1,)])
    assert tick.is_set()
