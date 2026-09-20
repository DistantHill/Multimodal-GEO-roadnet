"""Disk-free member sequence helper behavior."""
from __future__ import annotations

from dataclasses import replace

from track.v2.member_sequences import (
    ConcatSequence, PointRefSequence, ReadOnlySequence, SliceSequence,
    iter_member_identities, reference_source_ids, summarize_events,
)
from track.v2.models import PointRef, TurnEvent


def _event(index: int, *, source: str = "turn", point_source: str = "raw") -> TurnEvent:
    return TurnEvent(
        event_calculate_index=index, vehicle_id="v", timestamp=float(index),
        lon=116.0, lat=39.0, x=float(index), y=0.0, entry_bearing=0.0,
        exit_bearing=0.0, turn_deg=0.0, score=0.0, source=source,
        point_ref=PointRef(point_source, str(index), True),
        cross_track_calculate_index=None, source_ids=("event-source",),
    )


class LazySequence(ReadOnlySequence[TurnEvent]):
    def __init__(self, values):
        self.values = values
        self.closed = 0

    def __len__(self):
        return len(self.values)

    def _get(self, index):
        return self.values[index]

    def __iter__(self):
        try:
            yield from self.values
        finally:
            self.closed += 1


def test_tuple_list_lazy_reverse_slice_and_duplicate_semantics():
    values = [_event(0), _event(1), _event(2)]
    lazy = LazySequence(values)
    combined = ConcatSequence([(values[0],), lazy, (values[1],)])
    assert combined == [values[0], *values, values[1]]
    assert combined == tuple(combined)
    assert combined.count(values[1]) == 2
    assert isinstance(combined[::-1], SliceSequence)
    assert combined[::-1] == list(combined)[::-1]


def test_fallback_source_refs_and_plain_member_identities():
    events = [
        replace(_event(0, source=""), source_ids=("", "manual")),
        replace(_event(1), point_ref=PointRef("additional", "1", True)),
        replace(_event(2, source=""), point_ref=None),
    ]
    summary = summarize_events(events, fallback_source="fallback")
    assert summary.event_source_ids == frozenset(("event-source", "manual"))
    assert summary.ref_counts == {"fallback": 1, "turn": 1}
    refs = PointRefSequence(events, fallback_source="fallback")
    assert reference_source_ids(refs) == frozenset(("raw", "additional"))
    assert list(iter_member_identities(events)) == [
        ("", 0, None), ("turn", 1, None), ("", 2, None),
    ]


def test_iterators_close_when_consumers_stop_early():
    events = LazySequence([_event(0), _event(1)])
    refs = iter(PointRefSequence(events))
    next(refs)
    refs.close()
    assert events.closed == 1

    identities = iter_member_identities(events)
    next(identities)
    identities.close()
    assert events.closed == 2

    left = LazySequence([_event(0), _event(1)])
    right = LazySequence([_event(0), _event(9)])
    assert left != right
    assert left.closed == right.closed == 1
