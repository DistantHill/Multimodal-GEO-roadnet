"""Isolated prototype: bounded external sorts for Detect writer associations.

Private, run-local pickle files contain only data emitted by this process. No
external serialized input is accepted. PostgreSQL transactions remain caller-owned.
"""
from __future__ import annotations

from contextlib import closing
import heapq
from pathlib import Path
import pickle
import tempfile
from typing import Iterable, Iterator, Any

from .member_sequences import iter_member_identities
from .heartbeat import operation_heartbeat, heartbeat_progress


def _records(path: Path) -> Iterator[tuple]:
    with path.open('rb') as stream:
        while True:
            try:
                row = pickle.load(stream)
            except EOFError:
                return
            yield row


def _write(path: Path, rows: Iterable[tuple]) -> None:
    with path.open('wb') as stream:
        for row in rows:
            pickle.dump(row, stream, protocol=5)


class WriterWorkspace:
    """Own narrow relations; associate by sorted streams, never per-row lookups."""

    def __init__(self, *, directory=None, batch_size=5000, merge_fan_in=16):
        if batch_size < 1 or merge_fan_in < 2:
            raise ValueError('positive batch size and merge fan-in >= 2 required')
        self._temp = tempfile.TemporaryDirectory(prefix='roadnet-writer-', dir=directory)
        self.path = Path(self._temp.name)
        self.batch_size = batch_size
        self.merge_fan_in = merge_fan_in
        self._streams: dict[str, Any] = {}
        self._sorted: set[str] = set()
        self._closed = False
        self.counts = [0, 0, 0]
        self._map_ordinal = 0
        self.sort_peak_records = 0
        self.merge_peak_readers = 0
        for name in ('requests', 'mapping', 'turn', 'cross', 'crossing',
                     'annotation_requests', 'members', 'hotspots'):
            (self.path / name).touch()

    def append(self, name: str, row: tuple) -> None:
        stream = self._streams.get(name)
        if stream is None:
            stream = (self.path / name).open('ab')
            self._streams[name] = stream
        pickle.dump(row, stream, protocol=5)

    def flush(self, name: str) -> None:
        stream = self._streams.pop(name, None)
        if stream is not None:
            stream.close()

    def records(self, name: str) -> Iterator[tuple]:
        self.flush(name)
        path = self.path / name
        if path.exists():
            yield from _records(path)

    def sorted_records(self, name: str, key) -> Iterator[tuple]:
        """Sort fixed-size runs; bounded fan-in and no list of all run paths."""
        self.flush(name)
        output = self.path / f'{name}.sorted'
        if name not in self._sorted:
            with operation_heartbeat(f'写入暂存排序：{name}'):
                count = 0
                chunk = []
                run_count = 0
                with closing(self.records(name)) as source:
                    for row in source:
                        chunk.append(row)
                        if len(chunk) == self.batch_size:
                            chunk.sort(key=key)
                            _write(self.path / f'{name}.run.0.{run_count}', chunk)
                            self.sort_peak_records = max(self.sort_peak_records, len(chunk))
                            count += len(chunk)
                            heartbeat_progress(count)
                            chunk.clear()
                            run_count += 1
                    if chunk:
                        chunk.sort(key=key)
                        _write(self.path / f'{name}.run.0.{run_count}', chunk)
                        self.sort_peak_records = max(self.sort_peak_records, len(chunk))
                        run_count += 1
                chunk.clear()
                if run_count == 0:
                    output.touch()
                else:
                    pass_index = 0
                    while run_count > 1:
                        next_count = 0
                        for start in range(0, run_count, self.merge_fan_in):
                            paths = [self.path / f'{name}.run.{pass_index}.{i}'
                                     for i in range(start, min(start + self.merge_fan_in, run_count))]
                            sources = [_records(path) for path in paths]
                            self.merge_peak_readers = max(self.merge_peak_readers, len(sources))
                            try:
                                _write(self.path / f'{name}.run.{pass_index+1}.{next_count}',
                                       heapq.merge(*sources, key=key))
                            finally:
                                for source in sources:
                                    source.close()
                            for path in paths:
                                path.unlink()
                            next_count += 1
                            heartbeat_progress(min(start + self.merge_fan_in, run_count), total=run_count)
                        pass_index += 1
                        run_count = next_count
                    (self.path / f'{name}.run.{pass_index}.0').replace(output)
                self._sorted.add(name)
        yield from _records(output)

    def add_request(self, consumer: int, ordinal: int, endpoint: int, ref) -> None:
        self.append('requests', (ref.point_kind, ref.point_id, ref.subtrack_index,
                                 consumer, ordinal, endpoint))

    def collect_sources(self, result, pending, crossing_ids, candidate_ids) -> None:
        with operation_heartbeat('写入暂存：收集来源请求'):
            with _closing_optional(iter(result.turn_events)) as events:
                for ordinal, event in enumerate(events):
                    if event.refined_point_ref is None:
                        raise ValueError('a persisted turn event requires a stable resolved point')
                    self.add_request(0, ordinal, 0, event.refined_point_ref)
                    self.append('turn', (ordinal, event.entry_bearing, event.exit_bearing,
                                         event.turn_deg, event.score))
                    self.append('annotation_requests', ('turn', event.event_calculate_index, ordinal))
                    self.counts[0] += 1
            with _closing_optional(iter(result.cross_track_candidates)) as candidates:
                for ordinal, candidate in enumerate(candidates):
                    for endpoint, ref in enumerate((candidate.from_segment_start, candidate.to_segment_start)):
                        if ref is None:
                            raise ValueError('a persisted track cross requires stable segment starts')
                        self.add_request(1, ordinal, endpoint, ref)
                    self.append('cross', (ordinal, candidate.x, candidate.y, candidate.lon, candidate.lat))
                    self.append('annotation_requests', ('cross', candidate.cross_track_calculate_index, ordinal))
                    self.counts[1] += 1
            for ordinal, ((cluster, crossing), crossing_id) in enumerate(zip(pending, crossing_ids, strict=True)):
                if crossing.segment_start is None:
                    raise ValueError('a persisted crossing requires a stable segment start')
                self.add_request(2, ordinal, 0, crossing.segment_start)
                self.append('crossing', (ordinal, crossing_id, candidate_ids[cluster.cluster_calculate_index],
                                         int(crossing.is_entry), crossing.crossing_x, crossing.crossing_y,
                                         crossing.crossing_lon, crossing.crossing_lat))
                self.counts[2] += 1
            heartbeat_progress(sum(self.counts))

    def add_mapping(self, ref, resolved_id: int) -> None:
        self.append('mapping', (ref.point_kind, ref.point_id, ref.subtrack_index,
                                self._map_ordinal, resolved_id))
        self._map_ordinal += 1

    def resolve_sources(self) -> None:
        """Merge exact three-field keys; duplicate source keys keep last ID."""
        with operation_heartbeat('写入暂存：来源顺序关联'):
            with closing(self.sorted_records('mapping', lambda row: row[:4])) as mappings, \
                 closing(self.sorted_records('requests', lambda row: row[:3])) as requests:
                mapping = next(mappings, None)
                current_key = None
                resolved_id = None
                for count, request in enumerate(requests, 1):
                    key = request[:3]
                    if key != current_key:
                        while mapping is not None and mapping[:3] < key:
                            mapping = next(mappings, None)
                        if mapping is None or mapping[:3] != key:
                            raise KeyError(key)
                        while mapping is not None and mapping[:3] == key:
                            resolved_id = mapping[4]
                            mapping = next(mappings, None)
                        current_key = key
                    self.append(f'resolved_{request[3]}', (request[4], request[5], resolved_id))
                    if count % self.batch_size == 0:
                        heartbeat_progress(count)

    def prepare_annotations(self, clusters, candidate_ids, hotspots) -> None:
        """Validate relation conflicts and merge membership/z into ordinal rows."""
        encounter = 0
        with operation_heartbeat('写入暂存：候选归属关联'):
            for cluster in clusters:
                candidate_id = candidate_ids[cluster.cluster_calculate_index]
                with _closing_optional(iter_member_identities(cluster.events)) as identities:
                    for source, event_index, cross_index in identities:
                        kind = index = None
                        if source == 'turn':
                            kind, index = 'turn', event_index
                        elif source == 'orthogonal_cross_track' and cross_index is not None:
                            kind, index = 'cross', cross_index
                        if kind is not None:
                            self.append('members', (kind, index, encounter, candidate_id))
                            encounter += 1
            previous = None
            first_candidate = None
            first_conflict = None
            with closing(self.sorted_records('members', lambda row: row[:3])) as members:
                for kind, index, position, candidate_id in members:
                    key = (kind, index)
                    if key != previous:
                        self.append('unique_members', (kind, index, candidate_id))
                        previous, first_candidate = key, candidate_id
                    elif candidate_id != first_candidate:
                        if first_conflict is None or position < first_conflict[0]:
                            first_conflict = (position, index)
            if first_conflict is not None:
                raise ValueError(f'Detect entity {first_conflict[1]} belongs to multiple candidates')
            for ordinal, event in enumerate(hotspots):
                self.append('hotspots', (event.event_calculate_index, ordinal, event.g_star_z))
            with closing(self.records('unique_members')) as members, \
                 closing(self.sorted_records('hotspots', lambda row: row[:2])) as hotspots_sorted, \
                 closing(self.sorted_records('annotation_requests', lambda row: row[:2])) as requests:
                member = next(members, None)
                hotspot = next(hotspots_sorted, None)
                previous_hotspot = None
                z = None
                for kind, index, ordinal in requests:
                    key = (kind, index)
                    while member is not None and member[:2] < key:
                        member = next(members, None)
                    candidate_id = member[2] if member is not None and member[:2] == key else None
                    if kind == 'turn' and previous_hotspot != index:
                        while hotspot is not None and hotspot[0] < index:
                            hotspot = next(hotspots_sorted, None)
                        z = None
                        while hotspot is not None and hotspot[0] == index:
                            z = hotspot[2]
                            hotspot = next(hotspots_sorted, None)
                        previous_hotspot = index
                    self.append(f'annotation_{kind}', (ordinal, candidate_id, z if kind == 'turn' else None))

    def iter_rows(self, consumer: int, detect_id: str) -> Iterator[tuple]:
        kind = ('turn', 'cross', 'crossing')[consumer]
        with closing(self.records(kind)) as raw, \
             closing(self.sorted_records(f'resolved_{consumer}', lambda row: row[:2])) as refs, \
             closing(self.sorted_records(f'annotation_{kind}', lambda row: row[0])) as annotations:
            for row in raw:
                ordinal = row[0]
                ref = next(refs)
                if ref[:2] != (ordinal, 0):
                    raise RuntimeError('source association order mismatch')
                if consumer == 2:
                    yield (row[1], detect_id, row[2], ref[2], *row[3:])
                    continue
                annotation = next(annotations)
                if annotation[0] != ordinal:
                    raise RuntimeError('membership association order mismatch')
                if consumer == 0:
                    yield (detect_id, annotation[1], ref[2], *row[1:], annotation[2])
                else:
                    ref2 = next(refs)
                    if ref2[:2] != (ordinal, 1):
                        raise RuntimeError('source association order mismatch')
                    low, high = sorted((ref[2], ref2[2]))
                    yield (detect_id, annotation[1], low, high, *row[1:])
            if next(refs, None) is not None or next(annotations, None) is not None:
                raise RuntimeError('unused writer associations')

    def validate_cross_pairs(self, detect_id: str) -> None:
        with operation_heartbeat('写入暂存：跨轨迹配对校验'):
            with closing(self.iter_rows(1, detect_id)) as rows:
                for ordinal, row in enumerate(rows):
                    if row[2] == row[3]:
                        raise ValueError('duplicate/degenerate track cross segment pair')
                    self.append('pairs', (row[2], row[3]))
                    self.append('validated_cross', row)
            previous = None
            with closing(self.sorted_records('pairs', lambda row: row)) as pairs:
                for pair in pairs:
                    if pair == previous:
                        raise ValueError('duplicate/degenerate track cross segment pair')
                    previous = pair

    def iter_validated_cross_rows(self):
        return self.records('validated_cross')

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                for stream in self._streams.values():
                    stream.close()
            finally:
                self._streams.clear()
                self._temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class _closing_optional:
    def __init__(self, iterator):
        self.iterator = iterator

    def __enter__(self):
        return self.iterator

    def __exit__(self, *_args):
        close = getattr(self.iterator, 'close', None)
        if callable(close):
            close()
