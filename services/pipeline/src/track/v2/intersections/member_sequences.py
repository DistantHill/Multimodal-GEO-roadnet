"""Numeric identity membership helpers; event payloads stay in business batches."""
from __future__ import annotations
from dataclasses import fields
import numpy as np
from ..models import MemberBatch


def concatenate_members(parts: list[MemberBatch]) -> MemberBatch:
    size=sum(len(part) for part in parts)
    return MemberBatch(**{item.name:np.arange(size,dtype=np.int64) if item.name=="member_order" else np.concatenate([getattr(part,item.name) for part in parts]) if parts else np.empty(0,dtype=np.int64 if item.name=="calculation_index" else np.bool_ if item.name.endswith("_valid") else object) for item in fields(MemberBatch)})


def member_source_counts(members: MemberBatch) -> dict[str,int]:
    kinds,counts=np.unique(members.kind,return_counts=True)
    return {str(kind):int(count) for kind,count in zip(kinds,counts)}


def member_source_ids(members: MemberBatch) -> tuple[str,...]:
    return tuple(sorted(set(str(value) for value in members.source_id[members.source_id_valid])))
