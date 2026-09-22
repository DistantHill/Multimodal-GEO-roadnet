"""Turn persistence consumes its event sequence once with bounded COPY batches."""
from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

from contextlib import nullcontext, closing
from types import SimpleNamespace
import os

import pytest

from track.v2 import db_writer as writer
from track.v2.models import RefinedPointRef, TurnEvent
from track.v2.models import RuntimeConfig
from track.v2.writer_relation import WriterRelation
from .writer_relation_helpers import prepared_turn_relation


def event(index):
    return SimpleNamespace(
        event_calculate_index=index,
        refined_point_ref=RefinedPointRef('kept', 100 + index, 1, index),
        entry_bearing=index * 1.0, exit_bearing=index + 90.0,
        turn_deg=90.0, score=index + .5,
    )


class OnePassEvents:
    def __init__(self, count, *, invalid_at=None):
        self.count = count
        self.invalid_at = invalid_at
        self.iterations = 0
        self.read = 0
        self.closed = False

    def __len__(self):
        return self.count

    def __iter__(self):
        self.iterations += 1
        assert self.iterations == 1, 'turn writer must not scan events twice'
        try:
            for index in range(self.count):
                self.read += 1
                value = event(index)
                if index == self.invalid_at:
                    value.refined_point_ref = None
                yield value
        finally:
            self.closed = True


def result(events):
    return SimpleNamespace(turn_events=events, hotspot_events=[
        SimpleNamespace(event_calculate_index=1, g_star_z=2.5),
    ])


def mapping(count):
    return {writer._refine_key(event(index).refined_point_ref): 200 + index for index in range(count)}


@pytest.mark.parametrize('batch_size', [1, 2, 3, 20])
def test_turn_rows_preserve_order_mapping_and_values_in_bounded_batches(monkeypatch, tmp_path, batch_size):
    events = OnePassEvents(5)
    copied, sizes, progress = [], [], []
    rows_read = [0]

    def copy(cur, table, columns, rows, *, observe):
        assert table == 'turn_events'
        assert observe is False
        assert len(rows) <= batch_size
        assert rows_read[0] == len(copied) + len(rows), 'COPY read ahead beyond its resolved row batch'
        copied.extend(rows)
        sizes.append(len(rows))

    monkeypatch.setattr(writer, '_copy_insert', copy)
    monkeypatch.setattr(writer, 'heartbeat_progress', lambda count, **kwargs: progress.append((count, kwargs['total'])))
    with prepared_turn_relation(result(events), mapping(5), {1: 42, 3: 43}, batch_size=batch_size, data_root=tmp_path) as ws:
        original = ws.iter_rows
        def observe_rows(*args):
            with closing(original(*args)) as rows:
                for row in rows:
                    rows_read[0] += 1
                    yield row
        monkeypatch.setattr(ws, 'iter_rows', observe_rows)
        count = writer._insert_turn_events(
            SimpleNamespace(cursor=lambda: nullcontext(None)), 'd', ws, batch_size=batch_size,
        )
    assert copied == [
        ('d', None, 200, 0., 90., 90., .5, None),
        ('d', 42, 201, 1., 91., 90., 1.5, 2.5),
        ('d', None, 202, 2., 92., 90., 2.5, None),
        ('d', 43, 203, 3., 93., 90., 3.5, None),
        ('d', None, 204, 4., 94., 90., 4.5, None),
    ]
    assert count == 5 and events.iterations == 1 and events.closed
    assert progress == [(sum(sizes[:i + 1]), 5) for i in range(len(sizes))]


@pytest.mark.parametrize('failure', ['copy', 'missing_ref', 'missing_mapping'])
def test_turn_failure_stops_reading_and_closes_event_iterator(monkeypatch, tmp_path, failure):
    events = OnePassEvents(6, invalid_at=2 if failure == 'missing_ref' else None)
    copies = []
    refs = mapping(6)
    if failure == 'missing_mapping':
        del refs[writer._refine_key(event(2).refined_point_ref)]

    def copy(cur, table, columns, rows, **kwargs):
        copies.extend(rows)
        if failure == 'copy':
            raise RuntimeError('COPY failed')

    monkeypatch.setattr(writer, '_copy_insert', copy)
    expected = {'copy': RuntimeError, 'missing_ref': ValueError, 'missing_mapping': KeyError}[failure]
    resolved_read = [0]
    resolved_closed = []
    with pytest.raises(expected):
        with prepared_turn_relation(result(events), refs, {}, batch_size=2, data_root=tmp_path) as ws:
            original = ws.iter_rows
            def observed(*args):
                try:
                    with closing(original(*args)) as rows:
                        for row in rows:
                            resolved_read[0] += 1
                            yield row
                finally:
                    resolved_closed.append(True)
            monkeypatch.setattr(ws, 'iter_rows', observed)
            writer._insert_turn_events(
                SimpleNamespace(cursor=lambda: nullcontext(None)), 'd', ws, batch_size=2,
            )
    # Source validation now precedes any turn COPY. COPY itself must still stop
    # its row consumer at the first failed batch and close that consumer.
    assert len(copies) == (2 if failure == 'copy' else 0)
    assert events.read == (3 if failure == 'missing_ref' else 6)
    if failure == 'copy':
        assert resolved_read == [2] and resolved_closed == [True]
    assert events.closed


def test_empty_turn_sequence_does_not_start_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(writer, '_copy_insert', lambda *args, **kwargs: pytest.fail('empty COPY'))
    events = OnePassEvents(0)
    with prepared_turn_relation(result(events), {}, {}, batch_size=2, data_root=tmp_path) as ws:
        assert writer._insert_turn_events(
            SimpleNamespace(cursor=lambda: nullcontext(None)), 'd', ws, batch_size=2,
        ) == 0
    assert events.closed


@pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
def test_postgres_turn_batches_keep_id_order_and_rollback_prior_batches(tmp_path):
    import psycopg

    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        try:
            conn.execute('''CREATE TEMP TABLE turn_events (
                id bigserial PRIMARY KEY, detect_id text, candidate_intersection_id bigint,
                resolved_point_id bigint, entry_bearing double precision, exit_bearing double precision,
                turn_deg double precision, score double precision, g_star_z double precision
            ) ON COMMIT DROP''')
            with prepared_turn_relation(result(OnePassEvents(5)), mapping(5), {1: 42}, batch_size=2, data_root=tmp_path) as ws:
                count = writer._insert_turn_events(conn, 'd', ws, batch_size=2)
            assert count == 5
            assert conn.execute('SELECT id,resolved_point_id FROM turn_events ORDER BY id').fetchall() == [
                (1,200),(2,201),(3,202),(4,203),(5,204),
            ]
            with pytest.raises(ValueError):
                with conn.transaction():
                    with prepared_turn_relation(result(OnePassEvents(5)), mapping(5), {}, batch_size=2, data_root=tmp_path) as ws:
                        original = ws.iter_rows
                        def fail_late(*args):
                            with closing(original(*args)) as rows:
                                for ordinal, row in enumerate(rows):
                                    if ordinal == 3:
                                        # One successful COPY batch already exists in this savepoint.
                                        assert conn.execute("SELECT count(*) FROM turn_events WHERE detect_id='failed'").fetchone() == (2,)
                                        raise ValueError('late row read failure')
                                    yield row
                        ws.iter_rows = fail_late
                        writer._insert_turn_events(conn, 'failed', ws, batch_size=2)
            assert conn.execute("SELECT count(*) FROM turn_events WHERE detect_id='failed'").fetchone() == (0,)
            assert conn.execute('SELECT count(*) FROM turn_events').fetchone() == (5,)
            conn.rollback()
            assert conn.execute("SELECT to_regclass('pg_temp.turn_events')").fetchone() == (None,)
        finally:
            conn.rollback()


def test_turn_relation_survives_sparse_mapping_scan_and_batched_write(monkeypatch, tmp_path):
    from track.v2.turn_event_relation import TurnEventRelation

    next_id = [200]
    copied = []

    def members(*args):
        for start in range(0, 5, 2):
            yield [writer._RefineMember(event(i).refined_point_ref, 'v') for i in range(start, min(start + 2, 5))]

    def reserve(cur, table, count):
        values = tuple(range(next_id[0], next_id[0] + count))
        next_id[0] += count
        return values

    def copy(cur, table, columns, rows, **kwargs):
        if table == 'turn_events':
            copied.extend(rows)

    monkeypatch.setattr(writer, '_iter_refine_member_batches', members)
    monkeypatch.setattr(writer, '_reserve_ids', reserve)
    monkeypatch.setattr(writer, '_copy_insert', copy)
    conn = SimpleNamespace(cursor=lambda: nullcontext(None))
    with TurnEventRelation(runtime_config(), tmp_path) as turns:
        for i in [1, 3]:
            value = event(i)
            turns.append(TurnEvent(
                event_calculate_index=len(turns), vehicle_id="v", timestamp=float(i),
                lon=120.0, lat=30.0, x=float(i), y=float(i), entry_bearing=value.entry_bearing,
                exit_bearing=value.exit_bearing, turn_deg=value.turn_deg, score=value.turn_deg,
                refined_point_ref=value.refined_point_ref,
            ))
        turns.seal()
        output = result(turns)
        output.cross_track_candidates = []
        with WriterRelation(runtime_config(), tmp_path, "d", batch_size=2) as ws:
            ws.collect_sources(output, [], (), {})
            count = writer._persist_resolved_sequence(conn, 'd', 'r', output, [], [], {}, batch_size=2, workspace=ws)
            assert count == 5
            assert list(ws.iter_resolved_sources(0)) == [(0, 0, 201), (1, 0, 203)]
            ws.prepare_annotations([], {}, output.hotspot_events)
            assert writer._insert_turn_events(conn, 'd', ws, batch_size=1) == 2
            assert [row[2] for row in copied] == [201, 203]
        # The writer closes only its iterator; ownership stays with the result.
        assert [value.event_calculate_index for value in turns] == [0, 1]
