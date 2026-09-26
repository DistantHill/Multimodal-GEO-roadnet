from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)
"""Small-test adapters for the production writer relation."""
from contextlib import contextmanager
from types import SimpleNamespace as NS

from track.v2.db import write_detect_results as writer
from track.v2.models import RefinedPointRef, RuntimeConfig
from track.v2.intersections.relations.source_associations import WriterRelation


def run_resolved_sequence(
    conn, detect_id, refine_id, result, pending, complete, crossing_ids, *,
    batch_size, data_root,
):
    """Exercise source resolution and return the small-fixture observation."""
    requests = []
    with WriterRelation(runtime_config(), data_root, detect_id, batch_size=batch_size) as relation:
        for consumer, items in enumerate((result.turn_events, result.cross_track_candidates, pending)):
            for ordinal, item in enumerate(items):
                refs = ((item.refined_point_ref,) if consumer == 0 else
                        (item.from_segment_start, item.to_segment_start) if consumer == 1 else
                        (item[1].segment_start,))
                for endpoint, ref in enumerate(refs):
                    if ref is None:
                        raise ValueError("a persisted artifact requires a stable source")
                    relation.add_request(consumer, ordinal, endpoint, ref)
                    requests.append((consumer, ordinal, endpoint, writer._refine_key(ref)))
        count = writer._persist_resolved_sequence(
            conn, detect_id, refine_id, result, pending, complete, crossing_ids,
            batch_size=batch_size, workspace=relation,
        )
        resolved = {
            (consumer, ordinal, endpoint): resolved_id
            for consumer in range(3)
            for ordinal, endpoint, resolved_id in relation.iter_resolved_sources(consumer)
        }
        sparse = {
            key: resolved[(consumer, ordinal, endpoint)]
            for consumer, ordinal, endpoint, key in requests
        }
        return count, sparse


@contextmanager
def prepared_turn_relation(result, mapping, membership, *, batch_size, data_root):
    with WriterRelation(runtime_config(), data_root, "d", batch_size=batch_size) as relation:
        if not hasattr(result, "cross_track_candidates"):
            result.cross_track_candidates = []
        relation.collect_sources(result, [], (), {})
        for (kind, point_id, subtrack), resolved_id in mapping.items():
            relation.add_mapping(RefinedPointRef(kind, point_id, subtrack, 0), resolved_id)
        relation.resolve_sources()
        clusters = [NS(cluster_calculate_index=candidate_id, events=[NS(
            source="turn", event_calculate_index=index, cross_track_calculate_index=None,
        )]) for index, candidate_id in membership.items()]
        relation.prepare_annotations(
            clusters, {value: value for value in membership.values()}, result.hotspot_events,
        )
        yield relation
