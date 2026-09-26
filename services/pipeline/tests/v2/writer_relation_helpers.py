"""Column batch fixtures for production Detect writer tests."""
from contextlib import contextmanager
from dataclasses import fields
from types import SimpleNamespace as NS
import numpy as np
from .config_factory import runtime_config
from track.v2.models import TurnBatch, CrossCandidateBatch, CrossingBatch, MemberBatch
from track.v2.intersections.relations.source_associations import WriterRelation
from track.v2.db import write_detect_results as writer

def _batch(cls, size, **updates):
    columns = {}
    string_fields = {"vehicle_id","source_id","point_kind","generated_reason",
                     "from_vehicle_id","to_vehicle_id","from_source_id","to_source_id",
                     "from_point_kind","to_point_kind","from_generated_reason","to_generated_reason"}
    integer_fields = {"calculation_index","subtrack_index","point_index","source_row_key","refine_point_id",
                      "from_subtrack_index","to_subtrack_index","from_point_index","to_point_index",
                      "from_refine_point_id","to_refine_point_id","from_source_row_key","to_source_row_key"}
    for field in fields(cls):
        name = field.name
        if name == "cluster_ref": columns[name] = "fixture"
        elif name in {"xy","lonlat"}: columns[name] = np.zeros((size,2),dtype=np.float64)
        elif name.endswith("_valid") or name == "is_entry": columns[name] = np.ones(size,dtype=np.bool_)
        elif name in string_fields: columns[name] = np.full(size,"p" if name.endswith("point_kind") else "fixture")
        elif name in integer_fields: columns[name] = np.zeros(size,dtype=np.int64)
        else: columns[name] = np.zeros(size,dtype=np.float64)
    columns.update(updates)
    return cls(**columns)



def turn_batch(count, *, start=0, **updates):
    indices=np.arange(start,start+count,dtype=np.int64)
    columns=dict(calculation_index=indices,refine_point_id=100+indices,
        subtrack_index=np.ones(count,dtype=np.int64),point_index=indices,
        entry_bearing=indices.astype(np.float64),exit_bearing=indices.astype(np.float64)+90.,
        turn_deg=np.full(count,90.),score=indices.astype(np.float64)+.5)
    columns.update(updates)
    return _batch(TurnBatch,count,**columns)


@contextmanager
def prepared_turn_relation(result, mapping, membership, *, batch_size, data_root):
    with WriterRelation(runtime_config(),data_root,"d",batch_size=batch_size) as relation:
        if not hasattr(result,"cross_track_candidates"):
            result.cross_track_candidates=_batch(CrossCandidateBatch,0)
        relation.collect_sources(result,[],{}, {})
        keys=list(mapping)
        relation.add_mappings(np.asarray([key[0] for key in keys]),
            np.asarray([key[1] for key in keys],dtype=np.int64),
            np.asarray([key[2] for key in keys],dtype=np.int64),
            np.asarray([mapping[key] for key in keys],dtype=np.int64))
        relation.resolve_sources()
        clusters=[NS(cluster_calculate_index=candidate_id,members=MemberBatch(
            np.asarray(["turn"]),np.asarray([index],dtype=np.int64),np.asarray([0],dtype=np.int64),
            np.asarray(["fixture"]),np.asarray([True]))) for index,candidate_id in membership.items()]
        relation.prepare_annotations(clusters,{value:value for value in membership.values()},result.hotspot_events)
        yield relation


def run_resolved_sequence(conn, detect_id, refine_id, result, pending, complete,
                          crossing_ids, *, batch_size, data_root):
    with WriterRelation(runtime_config(),data_root,detect_id,batch_size=batch_size) as relation:
        relation.collect_sources(result,pending,{cluster.cluster_calculate_index:np.asarray([crossing_ids.resolve(cluster.cluster_calculate_index,int(index)) for index in batch.calculation_index],dtype=np.int64) for cluster,batch in pending},{cluster.cluster_calculate_index:None for cluster,batch in pending})
        requests=relation._connection.execute(
            "SELECT consumer,ordinal,endpoint,point_kind,point_id,subtrack_index FROM source_requests ORDER BY request_order"
        ).fetchall()
        count=writer._persist_resolved_sequence(conn,detect_id,refine_id,result,pending,
            complete,crossing_ids,batch_size=batch_size,workspace=relation)
        resolved={(consumer,ordinal,endpoint):value for consumer in range(3)
                  for ordinal,endpoint,value in relation.iter_resolved_sources(consumer)}
        sparse={(kind,point_id,subtrack):resolved[(consumer,ordinal,endpoint)]
                for consumer,ordinal,endpoint,kind,point_id,subtrack in requests}
        return count,sparse
