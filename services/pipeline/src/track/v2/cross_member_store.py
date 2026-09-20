"""Private disk candidate/member storage and immutable, repeatable member views.

This prototype keeps candidate payloads outside Python object graphs. Owners must
outlive persistence/replay; closing a member pass invalidates only that pass.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
import pickle
import sqlite3
import struct
from tempfile import TemporaryDirectory
from .member_sequences import (
    ConcatSequence, EventSummary, PointRefSequence, ReadOnlySequence, SliceSequence,
    _close_iterator, iter_member_identities, reference_source_ids, summarize_events,
)

from .models import CrossTrackCandidate, TurnEvent


class CandidateStore(ReadOnlySequence[CrossTrackCandidate]):
    def __init__(self, *, directory: str | None = None) -> None:
        self._directory = TemporaryDirectory(prefix="roadnet-cross-members-", dir=directory)
        self._closed = False
        self._sealed = False
        self._count = 0
        self._next_pass = 0
        self._db = sqlite3.connect(f"{self._directory.name}/members.sqlite")
        self._db.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA cache_size=-4096;
            PRAGMA mmap_size=0;
            PRAGMA temp_store=FILE;
            CREATE TABLE candidates(ordinal INTEGER PRIMARY KEY, coordinates BLOB NOT NULL, payload BLOB NOT NULL);
            CREATE INDEX candidates_xy ON candidates(ordinal,coordinates);
            CREATE TABLE members(pass_id INTEGER, label INTEGER, member_order INTEGER,
                ordinal INTEGER, PRIMARY KEY(pass_id,label,member_order)) WITHOUT ROWID;
        """)

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("candidate store is closed")

    def __len__(self) -> int:
        return self._count

    def append(self, candidate: CrossTrackCandidate) -> None:
        self._check_open()
        if self._sealed:
            raise RuntimeError("candidate store is sealed")
        if candidate.cross_track_calculate_index != self._count:
            raise ValueError("candidate global index must equal its sorted ordinal")
        self._db.execute("INSERT INTO candidates VALUES(?,?,?)", (
            self._count, struct.pack("!dd", candidate.x, candidate.y), pickle.dumps(candidate, protocol=5),
        ))
        self._count += 1
        if self._count % 1000 == 0:
            self._db.commit()

    def seal(self) -> None:
        self._check_open()
        self._db.commit()
        self._sealed = True

    def _get(self, index: int) -> CrossTrackCandidate:
        self._check_open()
        row = self._db.execute("SELECT payload FROM candidates WHERE ordinal=?", (index,)).fetchone()
        return pickle.loads(row[0])

    def __iter__(self) -> Iterator[CrossTrackCandidate]:
        self._check_open()
        cursor = self._db.execute("SELECT payload FROM candidates ORDER BY ordinal")
        try:
            for (payload,) in cursor:
                yield pickle.loads(payload)
        finally:
            if not self._closed:
                cursor.close()

    def iter_coordinates(self) -> Iterator[tuple[float, float]]:
        self._check_open()
        cursor = self._db.execute("SELECT coordinates FROM candidates INDEXED BY candidates_xy ORDER BY ordinal")
        try:
            for (coordinates,) in cursor:
                yield struct.unpack("!dd", coordinates)
        finally:
            if not self._closed:
                cursor.close()

    def create_members(self, labels: Sequence[int]) -> MemberPass:
        self.seal()
        if len(labels) != len(self):
            raise ValueError("one DBSCAN label is required per candidate")
        pass_id = self._next_pass
        self._next_pass += 1
        counts: dict[int, int] = {}
        event_ids: dict[int, set[str]] = {}
        ref_counts: dict[int, int] = {}
        ref_ids: dict[int, set[str]] = {}
        batch = []
        try:
            # One forward payload pass builds all cluster summaries. Noise
            # payload bytes are read but never decoded into candidate objects.
            cursor = self._db.execute("SELECT ordinal,payload FROM candidates ORDER BY ordinal")
            try:
                for (ordinal, payload), raw_label in zip(cursor, labels, strict=True):
                    label = int(raw_label)
                    if label < 0:
                        continue
                    position = counts.get(label, 0)
                    counts[label] = position + 1
                    candidate = pickle.loads(payload)
                    ids = event_ids.setdefault(label, set())
                    ids.update(source_id for source_id in (
                        candidate.from_segment_start_point_ref.source_id,
                        candidate.to_segment_start_point_ref.source_id,
                    ) if source_id)
                    if candidate.point_ref is not None:
                        ref_counts[label] = ref_counts.get(label, 0) + 1
                        ids = ref_ids.setdefault(label, set())
                        if candidate.point_ref.source_id:
                            ids.add(candidate.point_ref.source_id)
                    del candidate
                    batch.append((pass_id, label, position, ordinal))
                    if len(batch) == 1000:
                        self._db.executemany("INSERT INTO members VALUES(?,?,?,?)", batch)
                        batch.clear()
            finally:
                cursor.close()
            if batch:
                self._db.executemany("INSERT INTO members VALUES(?,?,?,?)", batch)
            self._db.commit()
        except BaseException:
            self._db.execute("DELETE FROM members WHERE pass_id=?", (pass_id,))
            self._db.commit()
            raise
        summaries = {
            label: EventSummary(
                frozenset(event_ids[label]),
                {"orthogonal_cross_track": ref_counts[label]} if label in ref_counts else {},
                {"orthogonal_cross_track": frozenset(ref_ids[label])} if label in ref_ids else {},
            ) for label in counts
        }
        return MemberPass(self, pass_id, counts, summaries)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._db.close()
            self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __del__(self):
        if hasattr(self, "_db"):
            self.close()


class MemberPass:
    def __init__(self, store: CandidateStore, pass_id: int, counts: dict[int, int],
                 summaries: dict[int, EventSummary]) -> None:
        self.store = store
        self.pass_id = pass_id
        self.counts = counts
        self.summaries = summaries
        self._closed = False

    def _check_open(self) -> None:
        self.store._check_open()
        if self._closed:
            raise RuntimeError("member pass is closed")

    def iter_groups(self) -> Iterator[tuple[int, ClusterMemberSequence]]:
        self._check_open()
        for label in sorted(self.counts):
            yield label, ClusterMemberSequence(self, label, self.counts[label])

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if not self.store._closed:
                self.store._db.execute("DELETE FROM members WHERE pass_id=?", (self.pass_id,))
                self.store._db.commit()
            self.summaries.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __del__(self):
        self.close()


class ClusterMemberSequence(ReadOnlySequence[TurnEvent]):
    def __init__(self, pass_owner: MemberPass, label: int, count: int) -> None:
        self.pass_owner = pass_owner
        self.label = label
        self._count = count

    def provenance_summary(self, fallback_source: str = "") -> EventSummary:
        self.pass_owner._check_open()
        return self.pass_owner.summaries[self.label]

    def __len__(self) -> int:
        return self._count

    def _rows(self, columns: str, *, index: int | None = None, numeric: bool = False):
        self.pass_owner._check_open()
        store = self.pass_owner.store
        candidate_index = " INDEXED BY candidates_xy" if numeric else ""
        query = (f"SELECT {columns} FROM members m JOIN candidates c{candidate_index} ON c.ordinal=m.ordinal "
                 "WHERE m.pass_id=? AND m.label=?")
        params = [self.pass_owner.pass_id, self.label]
        if index is not None:
            query += " AND m.member_order=?"
            params.append(index)
        query += " ORDER BY m.member_order"
        cursor = store._db.execute(query, params)
        try:
            # Do not delegate generator.close() to sqlite's cursor: its
            # connection may already have been closed by the owning store.
            for row in cursor:
                yield row
        finally:
            if not store._closed:
                cursor.close()

    @staticmethod
    def _event(candidate: CrossTrackCandidate, index: int) -> TurnEvent:
        return TurnEvent(
            event_calculate_index=index, vehicle_id=candidate.vehicle_id,
            timestamp=candidate.timestamp, lon=candidate.lon, lat=candidate.lat,
            x=candidate.x, y=candidate.y, entry_bearing=0.0, exit_bearing=0.0,
            turn_deg=0.0, score=0.0, source="orthogonal_cross_track",
            point_ref=candidate.point_ref,
            cross_track_calculate_index=candidate.cross_track_calculate_index,
            source_ids=tuple(sorted({candidate.from_segment_start_point_ref.source_id,
                                     candidate.to_segment_start_point_ref.source_id})),
        )

    def _get(self, index: int) -> TurnEvent:
        rows = self._rows("m.member_order,c.payload", index=index)
        try:
            position, payload = next(rows)
            return self._event(pickle.loads(payload), position)
        finally:
            rows.close()

    def __iter__(self) -> Iterator[TurnEvent]:
        rows = self._rows("m.member_order,c.payload")
        try:
            for position, payload in rows:
                yield self._event(pickle.loads(payload), position)
        finally:
            rows.close()

    def iter_member_identities(self) -> Iterator[tuple[str, int, int | None]]:
        self.pass_owner._check_open()
        store = self.pass_owner.store
        cursor = store._db.execute(
            "SELECT member_order,ordinal FROM members WHERE pass_id=? AND label=? ORDER BY member_order",
            (self.pass_owner.pass_id, self.label),
        )
        try:
            for member_order, ordinal in cursor:
                yield "orthogonal_cross_track", member_order, ordinal
        finally:
            if not store._closed:
                cursor.close()

    def iter_ordinals(self) -> Iterator[int]:
        self.pass_owner._check_open()
        store = self.pass_owner.store
        cursor = store._db.execute(
            "SELECT ordinal FROM members WHERE pass_id=? AND label=? ORDER BY member_order",
            (self.pass_owner.pass_id, self.label),
        )
        try:
            for (ordinal,) in cursor:
                yield ordinal
        finally:
            if not store._closed:
                cursor.close()

    def iter_coordinates(self) -> Iterator[tuple[float, float]]:
        rows = self._rows("c.coordinates", numeric=True)
        try:
            for (coordinates,) in rows:
                yield struct.unpack("!dd", coordinates)
        finally:
            rows.close()

    def iter_candidates(self) -> Iterator[CrossTrackCandidate]:
        rows = self._rows("c.payload")
        try:
            for (payload,) in rows:
                yield pickle.loads(payload)
        finally:
            rows.close()
