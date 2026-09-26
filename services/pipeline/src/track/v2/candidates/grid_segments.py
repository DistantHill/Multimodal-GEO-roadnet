"""Build bounded segment columns from grid point columns without row wrappers."""
from __future__ import annotations
import numpy as np

class GridSegmentBudgetError(RuntimeError):
    """One grid exceeds its segment count or retained-buffer budget."""


def build_grid_segments(point_batches, *, max_segments: int, max_bytes: int):
    if max_segments<0 or max_bytes<0: raise ValueError("segment budgets must be non-negative")
    result={}
    iterator=iter(point_batches)
    try:
        for batch in iterator:
            size=len(batch["point_id"])
            if any(len(value)!=size for value in batch.values()): raise ValueError("point columns differ in length")
            pieces=[]
            for start_prefix,end_prefix in (("prev_",""),("","next_")):
                a=np.asarray(batch[start_prefix+"point_id"],dtype=object); b=np.asarray(batch[end_prefix+"point_id"],dtype=object)
                valid=(a!=None)&(b!=None)
                ids=np.flatnonzero(valid)
                if not len(ids): continue
                def column(prefix,name,dtype): return np.asarray(batch[prefix+name])[ids].astype(dtype,copy=False)
                seq=column(start_prefix,"refined_sequence_in_subtrack",np.int64); end_seq=column(end_prefix,"refined_sequence_in_subtrack",np.int64)
                if np.any(end_seq!=seq+1): raise ValueError("grid neighbours are not adjacent Refine points")
                start_kind=column(start_prefix,"point_kind",np.int8); end_kind=column(end_prefix,"point_kind",np.int8)
                subtrack=np.asarray(batch["subtrack_index"],dtype=np.int64)[ids]
                values={"vehicle_id":np.asarray(batch["vehicle_id"],dtype=object)[ids],"subtrack_index":subtrack,"start_refined_sequence_in_subtrack":seq,"end_refined_sequence_in_subtrack":end_seq,"start_point_id":a[ids].astype(np.int64),"end_point_id":b[ids].astype(np.int64),"start_point_kind":np.where(start_kind==0,"kept","injected"),"end_point_kind":np.where(end_kind==0,"kept","injected"),"start_source_id":column(start_prefix,"source_id",object),"start_source_row_key":np.asarray([0 if value is None else int(value) for value in np.asarray(batch[start_prefix+"source_row_key"],dtype=object)[ids]],dtype=np.int64),"start_generated_reason":np.asarray([value or "" for value in np.asarray(batch[start_prefix+"generated_reason"],dtype=object)[ids]],dtype=object)}
                for name in ("timestamp","lon","lat","heading","speed"): values[name]=column(start_prefix,name,np.float64)
                for prefix,label in ((start_prefix,"start"),(end_prefix,"end")):
                    values["x_"+label]=column(prefix,"x",np.float64); values["y_"+label]=column(prefix,"y",np.float64)
                values["end_heading"]=column(end_prefix,"heading",np.float64)
                values["segment_identity"]=np.column_stack((subtrack,start_kind,a[ids].astype(np.int64),end_kind,b[ids].astype(np.int64)))
                pieces.append(values)
            if not pieces: continue
            combined={name:np.concatenate(([result[name]] if result else [])+[piece[name] for piece in pieces]) for name in pieces[0]}
            _,unique=np.unique(combined["segment_identity"],axis=0,return_index=True)
            if len(unique)>max_segments: raise GridSegmentBudgetError(f"grid exceeds max_segments={max_segments}")
            # Includes output, concatenation/sort scratch and string metadata slots.
            cost=3*sum(value.nbytes for value in combined.values())+128*len(unique)
            if cost>max_bytes: raise GridSegmentBudgetError(f"grid exceeds max_bytes={max_bytes}")
            result={name:value[unique] for name,value in combined.items()}
    finally:
        close=getattr(iterator,"close",None)
        if close: close()
    if not result:
        integers={"subtrack_index","start_refined_sequence_in_subtrack","end_refined_sequence_in_subtrack","start_point_id","end_point_id","start_source_row_key"}
        strings={"vehicle_id","start_point_kind","end_point_kind","start_source_id","start_generated_reason"}
        result={name:np.empty(0,dtype=np.int64 if name in integers else object if name in strings else np.float64) for name in integers|strings|{"timestamp","lon","lat","heading","speed","x_start","y_start","x_end","y_end","end_heading"}}
        result["segment_identity"]=np.empty((0,5),dtype=np.int64)
    if len(result["subtrack_index"]):
        order=np.lexsort((result["start_point_id"],result["start_refined_sequence_in_subtrack"],result["subtrack_index"]))
        result={name:value[order] for name,value in result.items()}
    for values in result.values():
        values.flags.writeable = False
    return result
