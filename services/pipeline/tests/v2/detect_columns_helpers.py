from dataclasses import replace
import numpy as np
from track.v2.models import RefinementSubtrack, SpatialContext
from track.v2.projection import CoordinateTransformer
from track.v2.trajectories.subtrack_columns import refinement_subtrack_from_columns

CONTEXT=SpatialContext(4326,3857,{"coordinate_semantics_version":2,"enforce_area_of_use":False,"max_scale_error":1.0})
TRANSFORMER=CoordinateTransformer(CONTEXT)

def subtrack(xy, *, subtrack_index=7, vehicle_id="original_vehicle", headings=None, keys=None):
    xy=np.asarray(xy,dtype=np.float64); size=len(xy)
    return refinement_subtrack_from_columns(vehicle_id=vehicle_id,subtrack_index=subtrack_index,source_id="trajectory_points",xy=xy,lonlat=TRANSFORMER.inverse_many(xy),timestamp=np.arange(size,dtype=np.float64),heading=np.zeros(size,dtype=np.float64) if headings is None else np.asarray(headings,dtype=np.float64),speed=np.full(size,20.,dtype=np.float64),kind=np.full(size,"p"),source_row_key=np.arange(size,dtype=np.int64) if keys is None else np.asarray(keys,dtype=np.int64),refine_point_id=np.arange(size,dtype=np.int64)+100*subtrack_index,after_sequence=np.full(size,-1,dtype=np.int64),generated_reason=np.full(size,""))


def sql_turns_from_subtrack(track, cfg, *, first_index=0):
    """Exercise the production DuckDB turn query with a small test relation."""
    from uuid import UUID
    import duckdb
    from track.v2.candidates.detect_turn_events import concatenate_turn_batches
    from track.v2.trajectories.results.read_parquet import RefineDatasetReader

    with duckdb.connect(":memory:") as connection:
        connection.execute("""
            CREATE TABLE refine_points (
                refine_id UUID, point_id BIGINT, vehicle_id VARCHAR,
                subtrack_index BIGINT, refined_sequence_in_subtrack BIGINT,
                timestamp DOUBLE, lon DOUBLE, lat DOUBLE, x DOUBLE, y DOUBLE,
                heading DOUBLE, point_kind VARCHAR, source_row_key BIGINT,
                generated_reason VARCHAR, refine_point_id_valid BOOLEAN,
                source_id VARCHAR
            )
        """)
        rows = [(
            UUID(int=0), int(track.refine_point_id[i]), track.vehicle_id,
            track.subtrack_index, i, float(track.timestamp[i]),
            float(track.lonlat[i, 0]), float(track.lonlat[i, 1]),
            float(track.xy[i, 0]), float(track.xy[i, 1]), float(track.heading[i]),
            "kept" if track.kind[i] == "p" else "injected",
            int(track.source_row_key[i]) if track.source_row_key_valid[i] else None,
            str(track.generated_reason[i]), bool(track.refine_point_id_valid[i]),
            track.source_id,
        ) for i in range(len(track))]
        connection.executemany("INSERT INTO refine_points VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        batch = concatenate_turn_batches(list(RefineDatasetReader._iter_turn_candidates_sql(
            connection, UUID(int=0), track.source_id, cfg, cfg.stream_batch_size,
        )))
    return replace(batch, calculation_index=batch.calculation_index + first_index)


def cluster_candidate_batches(turn_events,cross_candidates,transformer,cfg,**kwargs):
    """Test-only eager oracle for historical turn/cross kernel comparisons."""
    from track.v2.candidates.detect_turn_events import empty_turn_batch
    from track.v2.intersections.detect_intersections import _select_hotspot_events, _build_candidate_clusters, _annotate_source_ablation
    hotspot=_select_hotspot_events(turn_events,cfg,**kwargs) if cfg.use_turn_events and len(turn_events) else empty_turn_batch()
    point_sources=(("turn",hotspot),("orthogonal_cross_track",cross_candidates))
    clusters=_build_candidate_clusters(point_sources,cfg,transformer,**kwargs)
    clusters=[replace(cluster,cluster_calculate_index=i) for i,cluster in enumerate(clusters)]
    return hotspot,_annotate_source_ablation(clusters,point_sources,cfg,transformer,**kwargs)
