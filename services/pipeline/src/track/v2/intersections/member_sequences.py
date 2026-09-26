"""Disk-free helpers for immutable, repeatable member sequences."""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from itertools import islice, zip_longest
from typing import Generic, TypeVar

from ..models import PointRef, TurnEvent

T = TypeVar("T")


def _close_iterator(iterator: object) -> None:
    close = getattr(iterator, "close", None)
    if close is not None:
        close()


@dataclass(frozen=True)
class EventSummary:
    event_source_ids: frozenset[str]
    ref_counts: dict[str, int]
    ref_source_ids: dict[str, frozenset[str]]


def summarize_events(events: Sequence[TurnEvent], fallback_source: str = "") -> EventSummary:
    summary = getattr(events, "provenance_summary", None)
    if summary is not None:
        return summary(fallback_source=fallback_source)
    event_ids: set[str] = set()
    ref_counts: dict[str, int] = {}
    ref_ids: dict[str, set[str]] = {}
    iterator = iter(events)
    try:
        for event in iterator:
            event_ids.update(source_id for source_id in event.source_ids if source_id)
            if event.point_ref is not None:
                source = event.source or fallback_source
                ref_counts[source] = ref_counts.get(source, 0) + 1
                ids = ref_ids.setdefault(source, set())
                if event.point_ref.source_id:
                    ids.add(event.point_ref.source_id)
    finally:
        _close_iterator(iterator)
    return EventSummary(frozenset(event_ids), ref_counts,
                        {source: frozenset(ids) for source, ids in ref_ids.items()})


def reference_source_ids(refs: Sequence[PointRef]) -> frozenset[str]:
    summary = getattr(refs, "reference_source_ids", None)
    if summary is not None:
        return summary()
    iterator = iter(refs)
    try:
        return frozenset(ref.source_id for ref in iterator if ref.source_id)
    finally:
        _close_iterator(iterator)


def iter_member_identities(
    events: Sequence[TurnEvent],
) -> Iterator[tuple[str, int, int | None]]:
    """Yield stable membership keys in encounter order, without payload if supported."""
    narrow = getattr(events, "iter_member_identities", None)
    if narrow is not None:
        iterator = iter(narrow())
        try:
            yield from iterator
        finally:
            _close_iterator(iterator)
        return
    iterator = iter(events)
    try:
        for event in iterator:
            yield event.source, event.event_calculate_index, event.cross_track_calculate_index
    finally:
        _close_iterator(iterator)


class ReadOnlySequence(Sequence[T], Generic[T]):
    def __getitem__(self, index):
        if isinstance(index, slice):
            return SliceSequence(self, range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return self._get(index)

    def _get(self, index: int) -> T:
        raise NotImplementedError

    def __eq__(self, other):
        if not isinstance(other, Sequence) or isinstance(other, (str, bytes)):
            return NotImplemented
        if len(self) != len(other):
            return False
        left, right = iter(self), iter(other)
        marker = object()
        try:
            return all(a == b for a, b in zip_longest(left, right, fillvalue=marker))
        finally:
            _close_iterator(left)
            _close_iterator(right)


class SliceSequence(ReadOnlySequence[T]):
    def __init__(self, source: Sequence[T], indices: range) -> None:
        self.source = source
        self.indices = indices

    def __len__(self) -> int:
        return len(self.indices)

    def _get(self, index: int) -> T:
        return self.source[self.indices[index]]

    def __iter__(self) -> Iterator[T]:
        if self.indices.step < 0:
            for index in self.indices:
                yield self.source[index]
            return
        iterator = iter(self.source)
        try:
            yield from islice(iterator, self.indices.start, self.indices.stop, self.indices.step)
        finally:
            _close_iterator(iterator)


class ConcatSequence(ReadOnlySequence[T]):
    """Flat concatenation preserves merge order without copying member objects."""
    def __init__(self, parts: Iterable[Sequence[T]]) -> None:
        flattened: list[Sequence[T]] = []
        for part in parts:
            if isinstance(part, ConcatSequence):
                flattened.extend(part.parts)
            elif len(part):
                flattened.append(part)
        self.parts = tuple(flattened)
        total = 0
        ends = []
        for part in self.parts:
            total += len(part)
            ends.append(total)
        self._ends = tuple(ends)
        self._count = total
        self._event_summaries: dict[str, EventSummary] = {}
        self._reference_ids: frozenset[str] | None = None

    def provenance_summary(self, fallback_source: str = "") -> EventSummary:
        if fallback_source not in self._event_summaries:
            event_ids: set[str] = set()
            counts: dict[str, int] = {}
            refs: dict[str, set[str]] = {}
            for part in self.parts:
                summary = summarize_events(part, fallback_source)
                event_ids.update(summary.event_source_ids)
                for source, count in summary.ref_counts.items():
                    counts[source] = counts.get(source, 0) + count
                for source, ids in summary.ref_source_ids.items():
                    refs.setdefault(source, set()).update(ids)
            self._event_summaries[fallback_source] = EventSummary(
                frozenset(event_ids), counts,
                {source: frozenset(ids) for source, ids in refs.items()},
            )
        return self._event_summaries[fallback_source]

    def iter_member_identities(self) -> Iterator[tuple[str, int, int | None]]:
        for part in self.parts:
            iterator = iter_member_identities(part)
            try:
                yield from iterator
            finally:
                iterator.close()

    def reference_source_ids(self) -> frozenset[str]:
        if self._reference_ids is None:
            ids: set[str] = set()
            for part in self.parts:
                ids.update(reference_source_ids(part))
            self._reference_ids = frozenset(ids)
        return self._reference_ids

    def __len__(self) -> int:
        return self._count

    def _get(self, index: int) -> T:
        part = bisect_right(self._ends, index)
        start = self._ends[part - 1] if part else 0
        return self.parts[part][index - start]

    def __iter__(self) -> Iterator[T]:
        for part in self.parts:
            iterator = iter(part)
            try:
                yield from iterator
            finally:
                _close_iterator(iterator)


class PointRefSequence(ReadOnlySequence[PointRef]):
    """Filter/map event references without retaining any yielded event or ref."""
    def __init__(self, events: Sequence[TurnEvent], source: str | None = None,
                 fallback_source: str = "") -> None:
        self.events = events
        self.source = source
        self.fallback_source = fallback_source
        self._count: int | None = None

    def __len__(self) -> int:
        if self._count is None:
            summary = summarize_events(self.events, self.fallback_source)
            self._count = (sum(summary.ref_counts.values()) if self.source is None
                           else summary.ref_counts.get(self.source, 0))
        return self._count

    def reference_source_ids(self) -> frozenset[str]:
        summary = summarize_events(self.events, self.fallback_source)
        if self.source is not None:
            return summary.ref_source_ids.get(self.source, frozenset())
        return frozenset(source_id for ids in summary.ref_source_ids.values() for source_id in ids)

    def _get(self, index: int) -> PointRef:
        iterator = iter(self)
        try:
            return next(islice(iterator, index, index + 1))
        finally:
            _close_iterator(iterator)

    def __iter__(self) -> Iterator[PointRef]:
        iterator = iter(self.events)
        try:
            for event in iterator:
                if event.point_ref is not None and (
                    self.source is None or (event.source or self.fallback_source) == self.source
                ):
                    yield event.point_ref
        finally:
            _close_iterator(iterator)
