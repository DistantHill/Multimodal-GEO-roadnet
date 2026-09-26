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
