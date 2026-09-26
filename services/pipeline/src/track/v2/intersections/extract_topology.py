"""Topology geometry on whole subtrack arrays and cluster-local crossing batches."""
from __future__ import annotations
import math
from collections import Counter
from dataclasses import fields, replace
import numpy as np
from .._geometry import (_segment_circle_crossings_batch, _branch_angle_from_center, _segment_bearings, _circular_mean_deg, _angle_delta_deg)
from ..models import CrossingBatch, ObservationBatch, IntersectionTopology
from ..projection import SpatialWindow


def empty_crossing_batch(cluster_ref=""):
    strings={"vehicle_id","point_kind","source_id","generated_reason"}
    floats={"branch_angle_deg","segment_fraction"}
    values={"cluster_ref":cluster_ref}
    for item in fields(CrossingBatch):
        name=item.name
        if name=="cluster_ref": continue
        if name in {"xy","lonlat"}: values[name]=np.empty((0,2),dtype=np.float64)
        else: values[name]=np.empty(0,dtype=np.bool_ if name.endswith("_valid") or name=="is_entry" else object if name in strings else np.float64 if name in floats else np.int64)
    return CrossingBatch(**values)


def empty_observation_batch(cluster_index=0):
    values={"cluster_calculate_index":cluster_index}
    for item in fields(ObservationBatch):
        name=item.name
        if name=="cluster_calculate_index": continue
        values[name]=np.empty(0,dtype=object if name=="vehicle_id" else np.bool_ if name.endswith("_valid") else np.int64 if name.endswith("index") else np.float64)
    return ObservationBatch(**values)


def concatenate_crossings(batches, cluster_ref):
    if not batches: return empty_crossing_batch(cluster_ref)
    return CrossingBatch(cluster_ref=cluster_ref, **{item.name:np.concatenate([getattr(batch,item.name) for batch in batches]) for item in fields(CrossingBatch) if item.name!="cluster_ref"})


def concatenate_observations(batches, cluster_index):
    if not batches: return empty_observation_batch(cluster_index)
    return ObservationBatch(cluster_calculate_index=cluster_index, **{item.name:np.concatenate([getattr(batch,item.name) for batch in batches]) for item in fields(ObservationBatch) if item.name!="cluster_calculate_index"})

def _cluster_angles_greedy(
    angles: list[float], threshold_deg: float
) -> list[float]:
    """Greedy angular clustering: return representative branch angles.

    Each input angle is assigned to the nearest existing cluster center
    if within ``threshold_deg``; otherwise a new center is created.
    Centers are updated via circular mean on assignment.

    Input angles are sorted to produce deterministic output regardless
    of the caller's ordering.
    """
    sorted_angles = sorted(angles)
    centers: list[float] = []
    for angle in sorted_angles:
        nearest: int | None = None
        nearest_delta = 999.0
        for i, center in enumerate(centers):
            delta = abs(_angle_delta_deg(center, angle))
            if delta < nearest_delta:
                nearest = i
                nearest_delta = delta
        if nearest is None or nearest_delta > threshold_deg:
            centers.append(angle)
        else:
            centers[nearest] = _circular_mean_deg([centers[nearest], angle])
    return sorted(centers)


def _nearest_angle_id(angle: float, centers: list[float]) -> int:
    """Return index of the nearest branch center to ``angle``."""
    return min(
        range(len(centers)),
        key=lambda i: abs(_angle_delta_deg(centers[i], angle)),
    )


def _extract_entry_exit_observations_batch(tracks, *, cluster, transformer, radius_m, bearing_window=2, first_crossing_index=0):
    crossing_pieces=[]; observation_pieces=[]; counter=first_crossing_index
    center=np.asarray((cluster.centroid_x,cluster.centroid_y),dtype=np.float64)
    for track in sorted(tracks.values(), key=lambda value:value.subtrack_index):
        if len(track)<2: continue
        counts, fractions, roots=_segment_circle_crossings_batch(track.xy[:-1],track.xy[1:],center,radius_m)
        seg, slot=np.nonzero(np.isfinite(fractions))
        fraction=fractions[seg,slot]; root_xy=roots[seg,slot]
        delta=track.xy[seg+1]-track.xy[seg]
        radial=np.einsum("ij,ij->i",root_xy-center,delta)
        keep=np.abs(radial)>1e-9
        seg,fraction,root_xy,radial=seg[keep],fraction[keep],root_xy[keep],radial[keep]
        entry=radial<0; progress=seg+fraction
        order=np.lexsort((seg,entry,progress))
        seg,fraction,root_xy,entry,progress=seg[order],fraction[order],root_xy[order],entry[order],progress[order]
        inside=np.linalg.norm(track.xy[0]-center)<radius_m
        accepted=[]; observation_columns=[[] for _ in range(7)]; pending=None; cursor=0
        while cursor<len(seg):
            stop=cursor+1
            while stop<len(seg) and abs(progress[stop]-progress[cursor])<=1e-9: stop+=1
            if np.any(entry[cursor:stop]) and not np.all(entry[cursor:stop]): cursor=stop; continue
            is_entry=bool(entry[cursor])
            if is_entry==inside: cursor=stop; continue
            candidates=np.arange(cursor,stop)
            chosen=int(candidates[np.lexsort((seg[candidates],fraction[candidates]!=0))[0]])
            cursor=stop; inside=is_entry
            crossing_index=counter+len(accepted); accepted.append(chosen)
            if is_entry: pending=(chosen,crossing_index); continue
            if pending is None: continue
            before,entry_id=pending; pending=None
            in_angles=_segment_bearings(track.xy,int(seg[before])-bearing_window,int(seg[before])+1)
            out_angles=_segment_bearings(track.xy,int(seg[chosen]),int(seg[chosen])+bearing_window+1)
            if not in_angles or not out_angles: continue
            in_bearing=_circular_mean_deg(in_angles); out_bearing=_circular_mean_deg(out_angles)
            observation_columns[0].append(_branch_angle_from_center(center,root_xy[before]))
            observation_columns[1].append(_branch_angle_from_center(center,root_xy[chosen]))
            observation_columns[2].append(in_bearing); observation_columns[3].append(out_bearing)
            observation_columns[4].append(_angle_delta_deg(in_bearing,out_bearing))
            observation_columns[5].append(entry_id); observation_columns[6].append(crossing_index)
        ids=np.asarray(accepted,dtype=np.int64); point_ids=seg[ids]; size=len(ids)
        xy=root_xy[ids]
        crossing_pieces.append(CrossingBatch(
            cluster_ref=cluster.cluster_ref, calculation_index=np.arange(counter,counter+size,dtype=np.int64),
            vehicle_id=np.full(size,track.vehicle_id,dtype=object),subtrack_index=np.full(size,track.subtrack_index,dtype=np.int64),point_index=point_ids,
            point_kind=track.kind[point_ids],refine_point_id=track.refine_point_id[point_ids],refine_point_id_valid=track.refine_point_id_valid[point_ids],
            source_id=np.full(size,track.source_id,dtype=object),source_row_key=track.source_row_key[point_ids],source_row_key_valid=track.source_row_key_valid[point_ids],generated_reason=track.generated_reason[point_ids],
            xy=xy,lonlat=transformer.inverse_many(xy),is_entry=entry[ids],
            branch_angle_deg=np.fromiter((_branch_angle_from_center(center,value) for value in xy),dtype=np.float64,count=size),segment_fraction=fraction[ids]))
        counter+=size
        size=len(observation_columns[0])
        if size:
            values=[np.asarray(column,dtype=np.float64 if i<5 else np.int64) for i,column in enumerate(observation_columns)]
            observation_pieces.append(ObservationBatch(cluster.cluster_calculate_index,np.full(size,track.vehicle_id,dtype=object),np.full(size,track.subtrack_index,dtype=np.int64),values[0],values[1],values[2],values[3],values[4],values[5],np.ones(size,dtype=np.bool_),values[6],np.ones(size,dtype=np.bool_)))
    return concatenate_observations(observation_pieces,cluster.cluster_calculate_index), concatenate_crossings(crossing_pieces,cluster.cluster_ref)


def _build_topology_for_cluster(cluster, observations, cfg):
    angles=np.concatenate((observations.entry_branch_angle,observations.exit_branch_angle))
    centers=_cluster_angles_greedy(angles,cfg.branch_threshold_deg) if len(angles) else []
    transitions=Counter((_nearest_angle_id(float(a),centers),_nearest_angle_id(float(b),centers)) for a,b in zip(observations.entry_branch_angle,observations.exit_branch_angle)) if centers else {}
    complete=len(centers)>=cfg.min_branches and len(observations)>=cfg.min_observations and len(transitions)>=cfg.min_directed_transitions
    reason="complete" if complete else f"branches={len(centers)}, observations={len(observations)}, directed_transitions={len(transitions)}"
    topology=IntersectionTopology(cluster.cluster_calculate_index,centers,dict(transitions),len(observations),complete,reason,max(cfg.min_intersection_radius_m,cluster.radius_m+cfg.topology_radius_padding_m))
    return topology,cluster,observations


def _build_topologies_from_tracks(clusters, tracks, cfg, transformer, *, refine_reader=None, **_kwargs):
    windows=[SpatialWindow(c.centroid_x-max(cfg.min_intersection_radius_m,c.radius_m+cfg.topology_radius_padding_m),c.centroid_y-max(cfg.min_intersection_radius_m,c.radius_m+cfg.topology_radius_padding_m),c.centroid_x+max(cfg.min_intersection_radius_m,c.radius_m+cfg.topology_radius_padding_m),c.centroid_y+max(cfg.min_intersection_radius_m,c.radius_m+cfg.topology_radius_padding_m)) for c in clusters]
    crossing_pieces=[[] for _ in clusters]; observation_pieces=[[] for _ in clusters]; offsets=np.zeros(len(clusters),dtype=np.int64)
    reader_selections = None
    if refine_reader is None:
        selections=((i,track) for i in range(len(clusters)) for track in tracks.values())
    else:
        reader_selections = refine_reader.iter_topology_tracks(cfg.source_table or cfg.db_table,windows,batch_size=cfg.stream_batch_size)
        selections=((i,selection.points) for selection in reader_selections for i in selection.window_indices)
    try:
        for i,track in selections:
            cluster=clusters[i]
            obs,cross=_extract_entry_exit_observations_batch({track.subtrack_index:track},cluster=cluster,transformer=transformer,radius_m=max(cfg.min_intersection_radius_m,cluster.radius_m+cfg.topology_radius_padding_m),bearing_window=cfg.bearing_window,first_crossing_index=int(offsets[i]))
            offsets[i]+=len(cross); crossing_pieces[i].append(cross); observation_pieces[i].append(obs)
    finally:
        close=getattr(selections,"close",None)
        if close: close()
        close_reader = getattr(reader_selections, "close", None)
        if close_reader is not None:
            close_reader()
    topologies=[]; annotated=[]; all_observations=[]
    for i,cluster in enumerate(clusters):
        obs=concatenate_observations(observation_pieces[i],cluster.cluster_calculate_index)
        cross=concatenate_crossings(crossing_pieces[i],cluster.cluster_ref)
        topology,cluster,_=_build_topology_for_cluster(replace(cluster,crossing_points=cross),obs,cfg)
        topologies.append(topology); annotated.append(cluster); all_observations.append(obs)
    return topologies,annotated,all_observations


def _build_intersection_topologies(clusters,tracks,transformer,cfg,**kwargs):
    return _build_topologies_from_tracks(clusters,tracks,cfg,transformer,**kwargs)
