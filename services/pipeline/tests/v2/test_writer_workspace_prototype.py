"""Isolated writer regressions: sorting, exact sources, ordering and ownership."""
from contextlib import nullcontext
from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from track.v2 import db_writer as writer
from track.v2.writer_workspace import WriterWorkspace
from track.v2.models import RefinedPointRef, HotspotCluster
from v2.test_db_writer import _build_resolved_rows, _crossing


class OnePass:
    def __init__(self, items):
        self.items = items
        self.iterations = 0
        self.closed = False

    def __iter__(self):
        self.iterations += 1
        assert self.iterations == 1
        try:
            yield from self.items
        finally:
            self.closed = True


def turn(index, ref, **kwargs):
    return NS(event_calculate_index=index, refined_point_ref=ref,
              entry_bearing=10., exit_bearing=90., turn_deg=-80., score=.75, **kwargs)


def cross(index, start, end):
    return NS(cross_track_calculate_index=index, from_segment_start=start,
              to_segment_start=end, x=float(index), y=-0., lon=113., lat=23.)


def member(index, kind='turn'):
    return NS(source=kind, event_calculate_index=index,
              cross_track_calculate_index=index if kind == 'orthogonal_cross_track' else None)


def empty_result():
    return NS(turn_events=[], cross_track_candidates=[], hotspot_events=[])


def test_external_merge_is_stable_bounded_repeatable_and_closes(tmp_path):
    rows = [(i % 9, i, -0. if i % 2 else float('inf')) for i in range(113)]
    with WriterWorkspace(directory=tmp_path, batch_size=3, merge_fan_in=2) as ws:
        for row in rows:
            ws.append('test', row)
        expected = sorted(rows, key=lambda row: row[0])
        actual = list(ws.sorted_records('test', lambda row: row[0]))
        assert actual == expected
        assert [math.copysign(1, row[2]) for row in actual] == [math.copysign(1, row[2]) for row in expected]
        iterator = ws.sorted_records('test', lambda row: row[0])
        assert next(iterator) == expected[0]
        iterator.close()
        assert list(ws.sorted_records('test', lambda row: row[0])) == expected
        assert ws.sort_peak_records <= 3
        assert ws.merge_peak_readers <= 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('batch_size', [1, 2, 3, 50])
def test_all_consumers_associate_exact_keys_and_restore_original_order(tmp_path, monkeypatch, batch_size):
    refs = [RefinedPointRef('kept', 7, 2, 0), RefinedPointRef('injected', 7, 2, 1),
            RefinedPointRef('kept', 7, 3, 0), RefinedPointRef('kept', 800, 2, 2)]
    turns = OnePass([turn(9, refs[0]), turn(1, refs[2]), turn(9, refs[3])])
    crosses = OnePass([cross(17, refs[0], refs[1]), cross(3, refs[2], refs[1]), cross(99, refs[0], refs[3])])
    hotspots = [NS(event_calculate_index=9, g_star_z=2.), NS(event_calculate_index=9, g_star_z=7.)]
    result = NS(turn_events=turns, cross_track_candidates=crosses, hotspot_events=hotspots)
    clusters = [NS(cluster_calculate_index=10, events=[member(9), member(17, 'orthogonal_cross_track'), member(9)]),
                NS(cluster_calculate_index=20, events=[member(3, 'orthogonal_cross_track')])]
    pending = [(clusters[1], _crossing(4, segment_start=refs[3], fraction=.5, is_entry=True))]
    candidate_ids = {10: 100, 20: 200}
    source_ids = [44, 5, 88, 22]
    output = {}
    batches = []
    progress = []
    monkeypatch.setattr(writer, '_copy_insert', lambda cur, table, columns, rows, **kw:
                        (output.setdefault(table, []).extend(rows), batches.append((len(rows), kw))))
    monkeypatch.setattr(writer, 'heartbeat_progress', lambda count, **kw: progress.append((count, kw['total'])))
    with WriterWorkspace(directory=tmp_path, batch_size=batch_size, merge_fan_in=2) as ws:
        ws.collect_sources(result, pending, (500,), candidate_ids)
        assert turns.closed and crosses.closed
        for ref, rid in zip(refs, source_ids):
            ws.add_mapping(ref, rid)
        ws.resolve_sources()
        ws.prepare_annotations(clusters, candidate_ids, hotspots)
        conn = NS(cursor=lambda: nullcontext(None))
        assert writer._insert_tracks_cross(conn, 'd', ws, batch_size=batch_size) == 3
        assert writer._insert_turn_events(conn, 'd', ws, batch_size=batch_size) == 3
        assert writer._insert_candidate_crossings(conn, 'd', ws, batch_size=batch_size) == 1
        assert output['tracks_cross'] == [
            ('d', 100, 5, 44, 17., -0., 113., 23.),
            ('d', 200, 5, 88, 3., -0., 113., 23.),
            ('d', None, 22, 44, 99., -0., 113., 23.),
        ]
        assert output['turn_events'] == [
            ('d', 100, 44, 10., 90., -80., .75, 7.),
            ('d', None, 88, 10., 90., -80., .75, None),
            ('d', 100, 22, 10., 90., -80., .75, 7.),
        ]
        p = pending[0][1]
        assert output['candidate_entry_exit_points'] == [(500, 'd', 200, 22, 1,
                                                         p.crossing_x, p.crossing_y, p.crossing_lon, p.crossing_lat)]
        assert all(count <= batch_size and kw == {'observe': False} for count, kw in batches)
        assert progress[-1] == (1, 1)
        assert (3, 3) in progress
        assert turns.iterations == crosses.iterations == 1


@pytest.mark.parametrize('case', ['reverse_duplicate', 'degenerate'])
def test_cross_pairs_reject_before_first_copy_across_sort_runs(tmp_path, monkeypatch, case):
    refs = [RefinedPointRef('kept', i, 1, i) for i in range(5)]
    crosses = [cross(i, refs[0], refs[i+1]) for i in range(4)]
    crosses.append(cross(55, refs[1], refs[0]) if case == 'reverse_duplicate' else cross(55, refs[4], refs[4]))
    result = NS(turn_events=[], cross_track_candidates=crosses, hotspot_events=[])
    monkeypatch.setattr(writer, '_copy_insert', lambda *a, **kw: pytest.fail('COPY before pair validation'))
    with WriterWorkspace(directory=tmp_path, batch_size=2, merge_fan_in=2) as ws:
        ws.collect_sources(result, [], (), {})
        for ref in refs:
            ws.add_mapping(ref, ref.point_id + 100)
        ws.resolve_sources()
        ws.prepare_annotations([], {}, [])
        with pytest.raises(ValueError, match='duplicate/degenerate'):
            writer._insert_tracks_cross(NS(cursor=lambda: nullcontext(None)), 'd', ws, batch_size=2)


def test_membership_conflict_preserves_first_encounter_and_namespaces(tmp_path):
    # index 99 conflicts before index 1; sorting by index must not change error identity.
    groups = [NS(cluster_calculate_index=0, events=[member(99), member(1), member(5, 'orthogonal_cross_track')]),
              NS(cluster_calculate_index=1, events=[member(99), member(1), member(5)])]
    with WriterWorkspace(directory=tmp_path, batch_size=1, merge_fan_in=2) as ws:
        with pytest.raises(ValueError, match='Detect entity 99 belongs to multiple candidates'):
            ws.prepare_annotations(groups, {0: 10, 1: 20}, [])


@pytest.mark.parametrize('consumer', [0, 1, 2])
def test_missing_exact_source_key_is_not_replaced_by_sequence(tmp_path, consumer):
    ref = RefinedPointRef('kept', 100, 1, 0)
    with WriterWorkspace(directory=tmp_path, batch_size=1) as ws:
        ws.add_request(consumer, 0, 0, ref)
        ws.add_mapping(replace(ref, subtrack_index=2), 1)
        ws.add_mapping(replace(ref, point_kind='injected'), 2)
        with pytest.raises(KeyError) as exc:
            ws.resolve_sources()
        assert exc.value.args == (('kept', 100, 1),)


@pytest.mark.parametrize('batch_size', [1, 2, 3, 100])
def test_p_i_c_sequences_and_id_allocation_match_materialized_oracle(tmp_path, monkeypatch, batch_size):
    refs = [RefinedPointRef('kept', 100+i, 1 if i < 3 else 2, i if i < 3 else i-3) for i in range(5)]
    refs[1] = replace(refs[1], point_kind='injected')
    members = [writer._RefineMember(ref, 'v') for ref in refs]
    cluster = HotspotCluster(0, 0, 0, 0, 0, 1)
    pending = [(cluster, _crossing(i, segment_start=refs[start], fraction=fraction, is_entry=entry))
               for i, (start, fraction, entry) in enumerate([
                   (0, 0., True), (0, 1., False), (1, 0., True), (1, .5, True),
                   (1, .5, False), (1, 1.-5e-13, False), (3, .5, True)])]
    crossing_ids = {(0, i): 200+i for i in range(len(pending))}
    expected = _build_resolved_rows('d', members, tuple(range(1, 6)), pending,
                                    tuple(range(6, 6+len(pending))), crossing_ids)
    next_id = 1
    def reserve(cur, table, count):
        nonlocal next_id
        assert count <= batch_size
        values = tuple(range(next_id, next_id+count))
        next_id += count
        return values
    closed = []
    def batches(*args):
        try:
            for start in range(0, len(members), batch_size):
                yield members[start:start+batch_size]
        finally:
            closed.append(True)
    copied = []
    monkeypatch.setattr(writer, '_iter_refine_member_batches', batches)
    monkeypatch.setattr(writer, '_reserve_ids', reserve)
    monkeypatch.setattr(writer, '_copy_insert', lambda cur, table, cols, rows, **kw: copied.extend(rows))
    with WriterWorkspace(directory=tmp_path, batch_size=batch_size, merge_fan_in=2) as ws:
        result = empty_result()
        ws.collect_sources(result, pending, tuple(crossing_ids.values()), {0: 50})
        count = writer._persist_resolved_sequence(
            NS(cursor=lambda: nullcontext(None)), 'd', 'r', result, pending, pending,
            crossing_ids, batch_size=batch_size, workspace=ws)
        assert count == len(expected)
        assert sorted(copied, key=lambda row: (row[2], row[4])) == expected
        assert [row[0] for row in copied] == list(range(1, count+1))
    assert closed == [True]


@pytest.mark.parametrize('failure', [RuntimeError('late copy'), KeyboardInterrupt()])
def test_public_writer_failure_closes_workspace_and_never_commits(tmp_path, monkeypatch, failure):
    monkeypatch.setenv('DATA_ROOT', str(tmp_path))
    refs = [RefinedPointRef('kept', i, 1, i) for i in range(6)]
    result = NS(turn_events=OnePass([turn(i, ref) for i, ref in enumerate(refs)]),
                cross_track_candidates=OnePass([]), hotspot_events=[], clusters=[], topologies=[], spatial_context=None)
    next_id = 1
    def reserve(cur, table, count):
        nonlocal next_id
        answer = tuple(range(next_id, next_id + count)); next_id += count
        return answer
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, *_args): pass
        def fetchone(self): return ('r', 'running')
    conn = NS(cursor=Cursor)  # No commit/rollback API: persistence must not own either.
    monkeypatch.setattr(writer, '_reserve_ids', reserve)
    monkeypatch.setattr(writer, '_iter_refine_member_batches', lambda *_a:
                        ( [writer._RefineMember(ref, 'v') for ref in refs[i:i+2]] for i in range(0, 6, 2)))
    monkeypatch.setattr(writer, '_insert_candidate_intersections', lambda *_a: None)
    turn_batches = []
    def copy(cur, table, columns, rows, **kw):
        if table == 'turn_events':
            turn_batches.append(tuple(rows))
            if len(turn_batches) == 2:
                raise failure
    monkeypatch.setattr(writer, '_copy_insert', copy)
    with pytest.raises(type(failure)):
        writer.persist_detect_result(conn, 'd', result, batch_size=2)
    assert len(turn_batches) == 2
    assert len(turn_batches[0]) == 2
    assert list(tmp_path.iterdir()) == []
    assert result.turn_events.closed and result.cross_track_candidates.closed


@pytest.mark.parametrize("consumer", ["turn", "cross", "crossing"])
def test_missing_source_reference_closes_active_consumer(tmp_path, consumer):
    ref = RefinedPointRef("kept", 1, 1, 0)
    turns = OnePass([turn(1, None)] if consumer == "turn" else [])
    crosses = OnePass([cross(1, ref, None)] if consumer == "cross" else [])
    pending = [(NS(cluster_calculate_index=1), NS(segment_start=None))] if consumer == "crossing" else []
    with WriterWorkspace(directory=tmp_path, batch_size=1) as ws:
        with pytest.raises(ValueError, match="stable"):
            ws.collect_sources(NS(turn_events=turns, cross_track_candidates=crosses),
                               pending, (10,) if pending else (), {1: 20})
    assert turns.closed
    if consumer != "turn":
        assert crosses.closed
    assert not list(tmp_path.iterdir())


def test_duplicate_source_keys_keep_last_mapping_and_empty_streams_work(tmp_path):
    ref = RefinedPointRef("kept", 2, 1, 0)
    with WriterWorkspace(directory=tmp_path, batch_size=1, merge_fan_in=2) as ws:
        ws.collect_sources(NS(turn_events=[turn(3, ref)], cross_track_candidates=[]), [], (), {})
        ws.add_mapping(ref, 11)
        ws.add_mapping(ref, 19)
        ws.resolve_sources()
        ws.prepare_annotations([], {}, [])
        assert list(ws.iter_rows(0, "d")) == [("d", None, 19, 10., 90., -80., .75, None)]
        assert list(ws.iter_rows(1, "d")) == []
        assert list(ws.iter_rows(2, "d")) == []
    assert not list(tmp_path.iterdir())
