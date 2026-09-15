"""Small-test adapters; materialized expectations stay outside runtime code."""
from contextlib import contextmanager
from types import SimpleNamespace as NS

from track.v2 import db_writer as writer
from track.v2.models import RefinedPointRef
from track.v2.writer_workspace import WriterWorkspace


def run_resolved_sequence(conn, detect_id, refine_id, result, pending, complete, crossing_ids, *, batch_size):
    """Exercise new source relation, return the old small-fixture observation."""
    requests = []
    with WriterWorkspace(batch_size=batch_size) as ws:
        for consumer, items in enumerate((result.turn_events, result.cross_track_candidates, pending)):
            for ordinal, item in enumerate(items):
                refs = ((item.refined_point_ref,) if consumer == 0 else
                        (item.from_segment_start, item.to_segment_start) if consumer == 1 else
                        (item[1].segment_start,))
                for endpoint, ref in enumerate(refs):
                    if ref is None:
                        raise ValueError('a persisted artifact requires a stable source')
                    ws.add_request(consumer, ordinal, endpoint, ref)
                    requests.append((consumer, ordinal, endpoint, writer._refine_key(ref)))
        count = writer._persist_resolved_sequence(conn, detect_id, refine_id, result, pending,
                                                  complete, crossing_ids, batch_size=batch_size, workspace=ws)
        resolved = {(consumer, ordinal, endpoint): rid for consumer in range(3)
                    for ordinal, endpoint, rid in ws.records(f'resolved_{consumer}')}
        sparse = {key: resolved[(consumer, ordinal, endpoint)]
                  for consumer, ordinal, endpoint, key in requests}
        return count, sparse


@contextmanager
def prepared_turn_workspace(result, mapping, membership, *, batch_size):
    with WriterWorkspace(batch_size=batch_size) as ws:
        if not hasattr(result, 'cross_track_candidates'):
            result.cross_track_candidates = []
        ws.collect_sources(result, [], (), {})
        for (kind, point_id, subtrack), rid in mapping.items():
            ws.add_mapping(RefinedPointRef(kind, point_id, subtrack, 0), rid)
        ws.resolve_sources()
        clusters = [NS(cluster_calculate_index=candidate_id, events=[NS(
            source='turn', event_calculate_index=index, cross_track_calculate_index=None)])
            for index, candidate_id in membership.items()]
        ws.prepare_annotations(clusters, {value: value for value in membership.values()}, result.hotspot_events)
        yield ws
