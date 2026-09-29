"""Array-only segment preparation and bounded exact cross-subtrack geometry."""
from __future__ import annotations

from dataclasses import fields
from contextlib import nullcontext
from typing import Any
import numpy as np
from .._geometry import _paired_segment_intersections
from ..models import CrossCandidateBatch
from ..projection import CoordinateTransformer


def empty_cross_batch() -> CrossCandidateBatch:
    floats = {"timestamp", "heading", "speed"}
    values = {}
    for item in fields(CrossCandidateBatch):
        name = item.name
        if name in {"xy", "lonlat"}:
            values[name] = np.empty((0, 2), dtype=np.float64)
        elif name.endswith("_valid"):
            values[name] = np.empty(0, dtype=np.bool_)
        elif name in floats:
            values[name] = np.empty(0, dtype=np.float64)
        elif name == "calculation_index" or name.endswith(("subtrack_index", "point_index", "refine_point_id", "source_row_key")):
            values[name] = np.empty(0, dtype=np.int64)
        else:
            values[name] = np.empty(0, dtype=object)
    return CrossCandidateBatch(**values)


def concatenate_cross_batches(batches: list[CrossCandidateBatch]) -> CrossCandidateBatch:
    if not batches:
        return empty_cross_batch()
    return CrossCandidateBatch(**{item.name: np.concatenate([getattr(batch, item.name) for batch in batches]) for item in fields(CrossCandidateBatch)})


def intersect_cross_track_patch(starts, ends, headings, segment_ids_a, segment_ids_b, cfg, *, timing=None):
    with timing.measure("gather_s") if timing else nullcontext():
        arrays = (starts[segment_ids_a], ends[segment_ids_a], headings[segment_ids_a],
                  starts[segment_ids_b], ends[segment_ids_b], headings[segment_ids_b])
    with timing.measure("kernel_s") if timing else nullcontext():
        result = _paired_segment_intersections(*arrays, cfg.cross_track_min_angle_deg, cfg.cross_track_max_angle_deg)
    if timing:
        timing.values["pair_count"] += len(segment_ids_a)
        timing.values["hit_count"] += len(result[2])
    return result


def build_cross_track_patch_candidates(segments, segment_ids_a, segment_ids_b, xs, ys, hit_indices, transformer, *, first_index=0):
    a = segment_ids_a[hit_indices]; b = segment_ids_b[hit_indices]
    if np.any(segments["subtrack_index"][a] == segments["subtrack_index"][b]):
        raise ValueError("cross-track pair belongs to one subtrack")
    xy = np.column_stack((xs, ys)); values = {
        "calculation_index": np.arange(first_index, first_index + len(a), dtype=np.int64),
        "xy": xy, "lonlat": transformer.inverse_many(xy),
        "timestamp": (segments["timestamp"][a] + segments["timestamp"][b]) / 2,
        "heading": segments["heading"][a], "speed": segments["speed"][a],
    }
    for prefix, ids in (("from", a), ("to", b)):
        for name in ("vehicle_id", "subtrack_index", "point_index", "point_kind", "refine_point_id", "refine_point_id_valid", "source_id", "source_row_key", "source_row_key_valid", "generated_reason"):
            values[f"{prefix}_{name}"] = segments[name][ids]
    return CrossCandidateBatch(**values)


def detect_cross_track_patch(segments, starts, ends, headings, segment_ids_a, segment_ids_b, cfg, transformer):
    xs, ys, hits = intersect_cross_track_patch(starts, ends, headings, segment_ids_a, segment_ids_b, cfg)
    return build_cross_track_patch_candidates(segments, segment_ids_a, segment_ids_b, xs, ys, hits, transformer)
