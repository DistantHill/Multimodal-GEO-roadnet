"""Run-local disk storage for complete tracks and real adjacent segments.

SQLite is an ephemeral index, never installed in the source PostgreSQL database.
Its R-tree rounds boxes outwards; exact intersection/circle kernels still decide
hits. Cache and pair batches are bounded independently of the total point count.
Only self-produced pickle records in this private temporary directory are read.
"""
from __future__ import annotations

import math
import os
import pickle
import sqlite3
import tempfile
import zlib
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ._geometry import _angle_delta_deg
from .cross_track import _CrossTrackSegment
from .models import TrajectoryPoint


_HEADING_BUCKET_DEGREES = 5
_HEADING_BUCKET_COUNT = 360 // _HEADING_BUCKET_DEGREES
_UNKNOWN_HEADING_BUCKET = _HEADING_BUCKET_COUNT


def _heading_bucket(heading: float) -> int:
    # Do not normalize unusual inputs: the exact NumPy kernel remains the
    # authority for modulo arithmetic, including huge or non-finite values.
    if not math.isfinite(heading) or not 0.0 <= heading < 360.0:
        return _UNKNOWN_HEADING_BUCKET
    return min(_HEADING_BUCKET_COUNT - 1, int(heading // _HEADING_BUCKET_DEGREES))


def _heading_ranges(min_angle: float, max_angle: float) -> Iterator[tuple[int, int, int]]:
    """Conservative disjoint target ranges, never an exact angle predicate.

    Each heading differs from its bucket centre by at most half a bucket, so
    centre-to-centre circular distance differs by at most one bucket. Expand
    one additional bucket to avoid narrowing on floating-point boundaries.
    Unknown headings and unusual angle configurations always search all buckets.
    """
    ordinary = (math.isfinite(min_angle) and math.isfinite(max_angle)
                and 0.0 <= min_angle <= max_angle <= 180.0)
    padding = 2 * _HEADING_BUCKET_DEGREES
    for source in range(_HEADING_BUCKET_COUNT + 1):
        if not ordinary or source == _UNKNOWN_HEADING_BUCKET:
            yield source, 0, _UNKNOWN_HEADING_BUCKET
            continue
        allowed = []
        for target in range(_HEADING_BUCKET_COUNT):
            difference = abs(source - target) * _HEADING_BUCKET_DEGREES
            distance = min(difference, 360 - difference)
            if distance + padding >= min_angle and distance - padding <= max_angle:
                allowed.append(target)
        # Every normal source also checks unknown targets. Joining disjoint
        # merged ranges visits each target only once, including wraparound.
        allowed.append(_UNKNOWN_HEADING_BUCKET)
        lower = upper = allowed[0]
        for target in allowed[1:]:
            if target == upper + 1:
                upper = target
            else:
                yield source, lower, upper
                lower = upper = target
        yield source, lower, upper


@dataclass
class DiskCrossPatch:
    segment_ids: list[int]
    starts: np.ndarray
    ends: np.ndarray
    headings: np.ndarray
    pair_a: np.ndarray
    pair_b: np.ndarray


class DetectWorkspace(MutableMapping[str, list[TrajectoryPoint]]):
    """Complete tracks live on disk; iteration materializes at most one track.

    One unusually large individual subtrack remains the indivisible unit of the
    existing turn/topology kernels. Global candidates and clustering are owned
    by the caller, not accounted for by this workspace's cache limit.
    """

    def __init__(self, cfg: Any, *, directory: str | Path | None = None) -> None:
        if directory is None:
            directory = os.environ.get("DATA_ROOT") or None
        self._temporary = tempfile.TemporaryDirectory(prefix="detect-workspace-", dir=directory)
        self.path = Path(self._temporary.name)
        self.cfg = cfg
        self._closed = False
        self._tracks = 0
        self._segments = 0
        self.scanned_source_segments = 0
        self.max_pair_rows = 0
        self.max_loaded_segments = 0
        self.max_numeric_segments = 0
        self.loaded_source_points = 0
        self._track_meta: dict[int, tuple[int, int, str]] = {}
        self.db = sqlite3.connect(self.path / "workspace.sqlite")
        try:
            # This is disposable staging, with no durability or recovery role.
            self.db.executescript("""
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                PRAGMA cache_size=-32768;
                PRAGMA mmap_size=0;
                PRAGMA temp_store=FILE;
                CREATE TABLE tracks (
                    id INTEGER PRIMARY KEY, track_key TEXT UNIQUE NOT NULL,
                    vehicle TEXT NOT NULL, payload BLOB NOT NULL
                );
                CREATE INDEX tracks_key_vehicle ON tracks(track_key, vehicle);
                CREATE INDEX tracks_id_vehicle ON tracks(id, vehicle);
                CREATE TABLE segments (
                    id INTEGER PRIMARY KEY, track_id INTEGER NOT NULL,
                    segment_index INTEGER NOT NULL, straight INTEGER NOT NULL,
                    x1 REAL, y1 REAL, x2 REAL, y2 REAL, heading REAL,
                    min_x REAL, max_x REAL, min_y REAL, max_y REAL,
                    payload BLOB, heading_bucket INTEGER NOT NULL
                );
                CREATE INDEX segments_numeric ON segments(
                    id, track_id, segment_index, straight,
                    x1, y1, x2, y2, heading,
                    min_x, max_x, min_y, max_y, heading_bucket
                );
                CREATE VIRTUAL TABLE boxes USING rtree(
                    id, min_x, max_x, min_y, max_y, min_bucket, max_bucket
                );
                CREATE TABLE heading_ranges (
                    source_bucket INTEGER, min_bucket INTEGER, max_bucket INTEGER,
                    PRIMARY KEY(source_bucket, min_bucket)
                );
                CREATE TABLE candidates (
                    vehicle_a INTEGER, vehicle_b INTEGER, segment_a INTEGER,
                    segment_b INTEGER, track_a INTEGER, track_b INTEGER,
                    payload BLOB NOT NULL
                );
            """)
            self.db.executemany("INSERT INTO heading_ranges VALUES(?,?,?)", _heading_ranges(
                cfg.cross_track_min_angle_deg, cfg.cross_track_max_angle_deg,
            ))
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> DetectWorkspace:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._track_meta.clear()
            self.db.close()
            self._temporary.cleanup()

    def __len__(self) -> int:
        return self._tracks

    @property
    def segment_count(self) -> int:
        return self._segments

    def __iter__(self) -> Iterator[str]:
        for (key,) in self.db.execute("SELECT track_key FROM tracks ORDER BY track_key"):
            yield key

    def __getitem__(self, key: str) -> list[TrajectoryPoint]:
        row = self.db.execute("SELECT payload FROM tracks WHERE track_key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return pickle.loads(zlib.decompress(row[0]))

    def __delitem__(self, key: str) -> None:
        raise TypeError("Detect workspace tracks are immutable")

    def __setitem__(self, key: str, track: list[TrajectoryPoint]) -> None:
        if not track:
            raise ValueError("Detect workspace requires non-empty tracks")
        payload = zlib.compress(pickle.dumps(track, protocol=5), level=1)
        cursor = self.db.execute(
            "INSERT INTO tracks(track_key,vehicle,payload) VALUES(?,?,?)",
            (key, track[0].vehicle_id, payload),
        )
        track_id = cursor.lastrowid
        # No global segment preparation and no grid membership replication.
        # Every real segment enters the bbox index for topology, including turns.
        for index, (start, end) in enumerate(zip(track, track[1:])):
            x1, y1, x2, y2 = start.x, start.y, end.x, end.y
            if any(v is None or not math.isfinite(v) for v in (x1, y1, x2, y2)):
                raise ValueError("Detect workspace needs finite projected coordinates")
            straight = bool(self.cfg.use_cross_track_candidates and not (abs(
                _angle_delta_deg(start.heading, end.heading)
            ) > self.cfg.cross_track_straightness_deg))
            # The exact cross kernel consumes only the segment's start metadata.
            # Keep it on disk rather than retaining original track object graphs.
            point = pickle.dumps(start, protocol=5) if straight else None
            self._segments += 1
            bounds = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
            bucket = _heading_bucket(start.heading)
            self.db.execute(
                "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._segments, track_id, index, straight, x1, y1, x2, y2,
                 start.heading, *bounds, point, bucket),
            )
            self.db.execute("INSERT INTO boxes VALUES(?,?,?,?,?,?,?)",
                            (self._segments, *bounds, bucket, bucket))
        self._tracks += 1
        if self._tracks % 100 == 0:
            self.db.commit()

    def iter_nearby_tracks(self, window: Any) -> Iterator[tuple[str, list[TrajectoryPoint]]]:
        # R-tree is a conservative filter. The REAL bounds reject its outward
        # float32 rounding false positives, preserving the in-memory index set.
        rows = self.db.execute("""
            SELECT t.track_key FROM tracks t WHERE t.id IN (
                SELECT s.track_id FROM boxes r CROSS JOIN segments s
                 WHERE r.min_x<=? AND r.max_x>=? AND r.min_y<=? AND r.max_y>=?
                   AND s.id=r.id AND s.min_x<=? AND s.max_x>=?
                   AND s.min_y<=? AND s.max_y>=?
            ) ORDER BY t.track_key
        """, (window.x_max, window.x_min, window.y_max, window.y_min) * 2)
        for (key,) in rows:
            yield key, self[key]

    def iter_cross_patches(self, batch_size: int) -> Iterator[DiskCrossPatch]:
        if batch_size <= 0:
            raise ValueError("cross-track patch size must be positive")
        # Bound materialized metadata even if a very large pair batch is requested.
        batch_size = min(batch_size, 8192)
        self.scanned_source_segments = 0
        cursor: sqlite3.Cursor | None = None
        try:
            self.db.commit()
            vehicle_ranks = {v: n for n, (v,) in enumerate(self.db.execute(
                "SELECT DISTINCT vehicle FROM tracks ORDER BY vehicle"
            ))}
            self._track_meta = {
                track_id: (vehicle_ranks[vehicle], rank, vehicle)
                for rank, (track_id, vehicle) in enumerate(self.db.execute(
                    "SELECT id,vehicle FROM tracks ORDER BY track_key"
                ))
            }
            # Vehicle ranks are now carried by the track metadata; do not retain
            # the separate map for the duration of segment matching.
            del vehicle_ranks
            # CROSS JOIN fixes the loop dependency: each real segment probes R-tree,
            # then looks up numeric metadata in covering indexes. Avoid fetching
            # wide rows containing payloads until the exact kernel finds a hit.
            # Never form a global Cartesian list.
            # Vehicle order is canonical, so every cross-vehicle pair appears once,
            # including arbitrarily long segments crossing multiple spatial cells.
            cursor = self.db.execute("""
                SELECT a.id,b.id
                  FROM segments a INDEXED BY segments_numeric
                  CROSS JOIN tracks ta INDEXED BY tracks_id_vehicle
                  CROSS JOIN heading_ranges h CROSS JOIN boxes r
                  CROSS JOIN segments b INDEXED BY segments_numeric
                  CROSS JOIN tracks tb INDEXED BY tracks_id_vehicle
                 WHERE a.straight=1 AND ta.id=a.track_id
                   AND h.source_bucket=a.heading_bucket
                   AND r.min_bucket<=h.max_bucket AND r.max_bucket>=h.min_bucket
                   AND r.min_x<=a.max_x AND r.max_x>=a.min_x
                   AND r.min_y<=a.max_y AND r.max_y>=a.min_y
                   AND b.id=r.id AND b.straight=1 AND tb.id=b.track_id
                   AND ta.vehicle < tb.vehicle
                   AND b.min_x<=a.max_x AND b.max_x>=a.min_x
                   AND b.min_y<=a.max_y AND b.max_y>=a.min_y
                 ORDER BY a.id ASC
            """)
            while pairs := cursor.fetchmany(batch_size):
                self.max_pair_rows = max(self.max_pair_rows, len(pairs))
                ids = sorted({segment_id for pair in pairs for segment_id in pair})
                lookup = {segment_id: offset for offset, segment_id in enumerate(ids)}
                raw: dict[int, tuple[Any, ...]] = {}
                for offset in range(0, len(ids), 900):
                    block = ids[offset:offset + 900]
                    rows = self.db.execute(
                        "SELECT id,x1,y1,x2,y2,heading "
                        "FROM segments INDEXED BY segments_numeric "
                        f"WHERE id IN ({','.join('?' for _ in block)})", block,
                    )
                    raw.update((row[0], row) for row in rows)
                numeric = np.array([raw[segment_id][1:] for segment_id in ids], dtype=float)
                self.max_numeric_segments = max(self.max_numeric_segments, len(ids))
                yield DiskCrossPatch(
                    ids, numeric[:, :2], numeric[:, 2:4], numeric[:, 4],
                    np.array([lookup[a] for a, _ in pairs], dtype=np.int64),
                    np.array([lookup[b] for _, b in pairs], dtype=np.int64),
                )
                # Resuming means the caller consumed this patch. The current
                # source can span more patches, but every smaller source ID is
                # exhausted. Segment IDs are contiguous, including non-straight
                # and zero-hit segments. Never use target IDs for this bound.
                self.scanned_source_segments = max(a for a, _ in pairs) - 1
            # Only normal exhaustion proves the zero-hit tail is also scanned.
            self.scanned_source_segments = self._segments
        finally:
            # The consumer finishes the yielded patch before requesting the
            # next one. Keep metadata through the final yield, then release it
            # before candidate materialization and the clustering/topology stages.
            self._track_meta.clear()
            if cursor is not None and not self._closed:
                cursor.close()

    def load_hit_segments(
        self, patch: DiskCrossPatch, hit_indices: np.ndarray,
    ) -> dict[int, _CrossTrackSegment]:
        """Decode each hit's source at most once per patch; misses load no payload."""
        offsets = set(map(int, patch.pair_a[hit_indices]))
        offsets.update(map(int, patch.pair_b[hit_indices]))
        local_by_id = {patch.segment_ids[offset]: offset for offset in offsets}
        ids = sorted(local_by_id)
        segments: dict[int, _CrossTrackSegment] = {}
        for offset in range(0, len(ids), 900):
            block = ids[offset:offset + 900]
            rows = self.db.execute(
                "SELECT id,track_id,segment_index,x1,y1,x2,y2,heading,payload "
                f"FROM segments WHERE id IN ({','.join('?' for _ in block)})", block,
            )
            for segment_id, track_id, segment_index, x1, y1, x2, y2, heading, payload in rows:
                vehicle_rank, track_rank, vehicle_id = self._track_meta[track_id]
                start = pickle.loads(payload)
                segments[local_by_id[segment_id]] = _CrossTrackSegment(
                    vehicle_id=vehicle_id, vehicle_rank=vehicle_rank,
                    track_rank=track_rank, segment_index=segment_index,
                    start=start, x1=x1, y1=y1, x2=x2, y2=y2,
                    heading=heading, timestamp=start.timestamp,
                )
        self.loaded_source_points += len(segments)
        self.max_loaded_segments = max(self.max_loaded_segments, len(segments))
        return segments

    def save_candidates(self, candidates: list[tuple[tuple[int, ...], Any]]) -> None:
        self.db.executemany(
            "INSERT INTO candidates VALUES(?,?,?,?,?,?,?)",
            ((*key, pickle.dumps(candidate, protocol=5)) for key, candidate in candidates),
        )

    def iter_candidates(self) -> Iterator[Any]:
        # Sorting may spill to disk. Only the final public result is materialized.
        for (payload,) in self.db.execute(
            "SELECT payload FROM candidates ORDER BY "
            "vehicle_a,vehicle_b,segment_a,segment_b,track_a,track_b"
        ):
            yield pickle.loads(payload)
