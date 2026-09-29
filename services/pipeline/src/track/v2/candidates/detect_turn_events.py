"""Turn-event batch constructors shared by Detect stages."""
from __future__ import annotations

from dataclasses import fields
import numpy as np

from ..models import TurnBatch


def empty_turn_batch() -> TurnBatch:
    integers = {"calculation_index", "subtrack_index", "point_index", "refine_point_id", "source_row_key"}
    strings = {"vehicle_id", "source_id", "point_kind", "generated_reason"}
    values = {}
    for item in fields(TurnBatch):
        if item.name in {"xy", "lonlat"}:
            values[item.name] = np.empty((0, 2), dtype=np.float64)
        else:
            dtype = np.bool_ if item.name.endswith("_valid") else np.int64 if item.name in integers else object if item.name in strings else np.float64
            values[item.name] = np.empty(0, dtype=dtype)
    return TurnBatch(**values)


def concatenate_turn_batches(batches: list[TurnBatch]) -> TurnBatch:
    batches = list(batches)
    if not batches:
        return empty_turn_batch()
    return TurnBatch(**{item.name: np.concatenate([getattr(batch, item.name) for batch in batches]) for item in fields(TurnBatch)})
