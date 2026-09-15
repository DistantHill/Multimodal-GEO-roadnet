"""Behavior/lifetime tests for the isolated disk member-chain prototype."""
from __future__ import annotations

import gc
import math
import struct
from dataclasses import replace
import weakref
from unittest.mock import patch

import pytest

from track.v2.cross_member_store import (
    CandidateStore, ClusterMemberSequence, ConcatSequence, PointRefSequence, SliceSequence,
)
from track.v2.models import CrossTrackCandidate, HotspotCluster, PointRef, RefinedPointRef


def candidate(index):
    return CrossTrackCandidate(
        PointRef("cross", str(index), False), PointRef("raw", f"a{index}", True),
        PointRef("other", f"b{index}", True), f"v{index}", float(index),
        116.+index*.00001,39.,float(index),float(index%3),90.,20.,
        RefinedPointRef("kept",index*2,index,0),
        RefinedPointRef("kept",index*2+1,index,1),index,
    )


def filled(tmp_path, size=6, incoming_refs=None):
    store=CandidateStore(directory=str(tmp_path))
    for index in range(size):
        item=candidate(index)
        store.append(item)
        if incoming_refs is not None:incoming_refs.append(weakref.ref(item))
    store.seal()
    return store


def test_candidate_output_preserves_ordinals_noise_and_repeatable_exports(tmp_path):
    with filled(tmp_path) as store:
        expected=[candidate(i) for i in range(6)]
        assert store==expected
        assert list(store)==list(store)==expected
        assert store[-1]==expected[-1]
        assert isinstance(store[1:4],SliceSequence)
        assert list(store[::-1])==expected[::-1]
        with pytest.raises(IndexError):store[6]
        with pytest.raises(RuntimeError,match='sealed'):store.append(candidate(6))
        with store.create_members([2,-1,0,2,0,-1]) as owner:
            groups=list(owner.iter_groups())
            assert [label for label,_ in groups]==[0,2]
            assert list(groups[0][1].iter_ordinals())==[2,4]
            assert list(groups[1][1].iter_ordinals())==[0,3]
            assert [e.event_calculate_index for e in groups[1][1]]==[0,1]
            assert [e.cross_track_calculate_index for e in groups[1][1]]==[0,3]
            assert len(store)==6  # Noise is retained for cross persistence.
            for label,view in groups:
                for i,(event,c) in enumerate(zip(view,view.iter_candidates())):
                    assert event==ClusterMemberSequence._event(c,i)
                    assert event.source_ids==('other','raw')
    assert not list(tmp_path.iterdir())


def test_pass_close_cannot_destroy_main_members_and_owner_survives_local_reference(tmp_path):
    with filled(tmp_path) as store:
        main=store.create_members([0,0,0,0,0,0])
        _,events=next(main.iter_groups())
        owner_ref=weakref.ref(main)
        del main
        gc.collect()
        assert owner_ref() is not None
        baseline=list(events)
        with store.create_members([0,-1,1,-1,2,-1]) as ablation:
            other=next(ablation.iter_groups())[1]
            assert len(list(other))==1
        with pytest.raises(RuntimeError,match='pass is closed'):list(other)
        assert list(events)==baseline
        owner_ref().close()
        with pytest.raises(RuntimeError,match='pass is closed'):list(events)


def test_flat_merge_ref_views_preserve_absorption_order_duplicates_and_as_dict(tmp_path):
    with filled(tmp_path) as store:
        owner=store.create_members([0,0,1,1,2,2])
        groups=[view for _,view in owner.iter_groups()]
        merged=ConcatSequence([groups[1],ConcatSequence([groups[0],groups[2]])])
        assert merged.parts==(groups[1],groups[0],groups[2])
        assert [e.cross_track_calculate_index for e in merged]==[2,3,0,1,4,5]
        assert [e.event_calculate_index for e in merged]==[0,1,0,1,0,1]
        refs=PointRefSequence(merged,'orthogonal_cross_track')
        doubled=ConcatSequence([refs,refs])
        assert [r.source_row_key for r in doubled]==['2','3','0','1','4','5']*2
        cluster=HotspotCluster(0,0.,0.,116.,39.,1.,events=merged,
            source_point_refs={'orthogonal_cross_track':doubled})
        eager=replace(cluster,events=tuple(merged),
            source_point_refs={'orthogonal_cross_track':tuple(doubled)})
        assert cluster.as_dict()==eager.as_dict()
        assert list(merged[1:5])==list(merged)[1:5]


def test_store_and_all_views_do_not_retain_decoded_members_or_point_refs(tmp_path):
    import track.v2.cross_member_store as storage
    observed=[]
    original=storage.pickle.loads
    def observe(payload):
        obj=original(payload)
        observed.extend((weakref.ref(obj),weakref.ref(obj.point_ref),
                         weakref.ref(obj.from_segment_start_point_ref)))
        return obj
    incoming=[]
    with filled(tmp_path,100,incoming) as store:
        assert all(ref() is None for ref in incoming)
        with patch.object(storage.pickle,'loads',side_effect=observe):
            owner=store.create_members([0]*100)
            events=next(owner.iter_groups())[1]
            merged=ConcatSequence([events,events])
            refs=PointRefSequence(merged)
            for event in merged:
                assert event.source=='orthogonal_cross_track'
            del event
            for ref in refs:
                assert ref.source_id=='cross'
            del ref
            gc.collect()
            assert observed
            assert all(ref() is None for ref in observed)
            assert len(merged)==200 and len(refs)==200
        owner.close()


def test_coordinate_reads_use_no_payload_decode_and_views_close_cursors(tmp_path):
    with filled(tmp_path) as store:
        owner=store.create_members([0]*6)
        events=next(owner.iter_groups())[1]
        with patch('track.v2.cross_member_store.pickle.loads',side_effect=AssertionError('payload decoded')):
            assert list(store.iter_coordinates())==[(float(i),float(i%3)) for i in range(6)]
            assert list(events.iter_coordinates())==list(store.iter_coordinates())
        iterator=iter(events)
        next(iterator)
        iterator.close()
        owner.close()
        assert store._db.execute('SELECT count(*) FROM members').fetchone()[0]==0


def test_failure_cleanup_and_store_close_invalidate_views(tmp_path):
    store=filled(tmp_path)
    with pytest.raises(ValueError,match='one DBSCAN label'):store.create_members([0])
    with pytest.raises(ValueError):store.create_members([0,0,'bad',0,0,0])
    assert store._db.execute('SELECT count(*) FROM members').fetchone()[0]==0
    owner=store.create_members([0]*6)
    view=next(owner.iter_groups())[1]
    iterator=iter(view);next(iterator)
    store.close()
    iterator.close()
    owner.close()
    with pytest.raises(RuntimeError,match='store is closed'):list(view)
    assert not list(tmp_path.iterdir())


def test_coordinates_preserve_signed_zero_nonfinite_values_and_double_bits(tmp_path):
    coordinates=[(-0.0,0.0),(math.inf,-math.inf),(math.nan,1e308)]
    with CandidateStore(directory=str(tmp_path)) as store:
        for i,(x,y) in enumerate(coordinates):
            store.append(replace(candidate(i),x=x,y=y))
        with store.create_members([0]*3) as owner:
            view=next(owner.iter_groups())[1]
            for actual,expected in zip(store.iter_coordinates(),coordinates):
                assert struct.pack('!dd',*actual)==struct.pack('!dd',*expected)
            for actual,expected in zip(view.iter_coordinates(),coordinates):
                assert struct.pack('!dd',*actual)==struct.pack('!dd',*expected)


def test_member_creation_decodes_once_and_provenance_uses_only_small_summaries(tmp_path):
    import track.v2.cross_member_store as storage
    from track.v2.cross_member_store import reference_source_ids, summarize_events
    with filled(tmp_path) as store:
        original=storage.pickle.loads
        with patch.object(storage.pickle,'loads',wraps=original) as loads:
            owner=store.create_members([0,-1,1,0,1,-1])
            assert loads.call_count==4  # Noise remains on disk without decoding.
            groups=[view for _,view in owner.iter_groups()]
            with patch.object(storage.pickle,'loads',side_effect=AssertionError('unexpected provenance decode')):
                events=ConcatSequence([groups[1],groups[0],groups[1]])
                summary=summarize_events(events)
                assert summary.event_source_ids==frozenset(('raw','other'))
                assert summary.ref_counts=={'orthogonal_cross_track':6}
                refs=PointRefSequence(events,'orthogonal_cross_track')
                assert len(refs)==6
                manual=(PointRef('manual','added',True),PointRef('manual','added',True))
                refs=ConcatSequence([refs,manual])
                assert len(refs)==8
                assert reference_source_ids(refs)==frozenset(('cross','manual'))
                assert summarize_events(events)==summary
            owner.close()


def test_manual_events_fallback_summary_preserves_ref_source_order_and_missing_refs():
    from track.v2.cross_member_store import reference_source_ids, summarize_events
    events=[ClusterMemberSequence._event(candidate(i),i) for i in range(4)]
    events=[replace(events[0],source='',source_ids=('manual-event',)),
            replace(events[1],source='turn'),
            replace(events[2],source='',point_ref=None),
            replace(events[3],source='turn',point_ref=PointRef('additional','x',True))]
    summary=summarize_events(events,fallback_source='fallback')
    assert list(summary.ref_counts)==['fallback','turn']
    assert summary.ref_counts=={'fallback':1,'turn':2}
    refs=PointRefSequence(events,'turn',fallback_source='fallback')
    assert len(refs)==2
    assert reference_source_ids(refs)==frozenset(('cross','additional'))
    assert [ref.source_row_key for ref in refs]==['1','x']


def test_narrow_membership_stream_avoids_payload_and_preserves_concat_duplicates(tmp_path):
    from track.v2.cross_member_store import iter_member_identities
    with filled(tmp_path) as store:
        owner=store.create_members([1,-1,0,1,0,-1])
        groups=[view for _,view in owner.iter_groups()]
        # Source-local event index differs from the global cross candidate index.
        manual=replace(ClusterMemberSequence._event(candidate(0),99),
                       source='turn',cross_track_calculate_index=None)
        events=ConcatSequence([groups[1],(manual,),ConcatSequence([groups[0],groups[1]])])
        queries=[]
        store._db.set_trace_callback(queries.append)
        with patch('track.v2.cross_member_store.pickle.loads',side_effect=AssertionError('membership decoded payload')):
            actual=list(iter_member_identities(events))
        store._db.set_trace_callback(None)
        assert actual==[
            ('orthogonal_cross_track',0,0),('orthogonal_cross_track',1,3),
            ('turn',99,None),
            ('orthogonal_cross_track',0,2),('orthogonal_cross_track',1,4),
            ('orthogonal_cross_track',0,0),('orthogonal_cross_track',1,3),
        ]
        assert queries
        assert all('FROM members' in q and 'payload' not in q and 'JOIN' not in q for q in queries)
        # No deduplication: the writer decides whether a repeated identity belongs
        # to the same final candidate or conflicts with another candidate.
        assert list(iter_member_identities(events))==actual


def test_narrow_membership_iterator_closes_on_early_stop_and_empty_input(tmp_path):
    from track.v2.cross_member_store import iter_member_identities
    assert list(iter_member_identities(()))==[]
    with filled(tmp_path) as store:
        owner=store.create_members([0]*6)
        view=next(owner.iter_groups())[1]
        iterator=iter_member_identities(ConcatSequence([view,view]))
        assert next(iterator)==('orthogonal_cross_track',0,0)
        iterator.close()
        owner.close()
        assert store._db.execute('SELECT count(*) FROM members').fetchone()[0]==0
        with pytest.raises(RuntimeError,match='pass is closed'):
            list(iter_member_identities(view))
