"""Build bounded, provenance-preserving Refine segments for a grid."""
from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np


class GridSegmentBudgetError(RuntimeError):
    """One grid exceeds its explicit segment count or memory budget."""


def build_grid_segments(
    point_batches: Iterable[Mapping[str, tuple[Any, ...]]],
    *,
    max_segments: int,
    max_bytes: int,
) -> dict[str, np.ndarray | tuple[Any, ...]]:
    """Deduplicate adjacent Refine segments from one grid's column batches.

    ``max_bytes`` bounds estimated retained Python records, identities,
    provenance strings, sorting references and final NumPy columns. It does
    not measure the input batch, database buffers, or whole-process RSS.
    Exceeding either budget raises ``GridSegmentBudgetError`` immediately.
    This function only constructs segments; it does not test intersections.
    """
    if max_segments < 0 or max_bytes < 0:
        raise ValueError("segment budgets must be non-negative")

    # Record layout: vehicle, subtrack, start sequence/kind/id, end kind/id,
    # end sequence, start source ID/key/reason, start timestamp/lon/lat,
    # start heading/speed, start/end XY, end heading.
    records: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    estimated_bytes = sys.getsizeof(records)
    iterator = iter(point_batches)
    try:
        for batch in iterator:
            count = len(batch["point_id"])
            if any(len(column) != count for column in batch.values()):
                raise ValueError("point batch columns have inconsistent lengths")
            for index in range(count):
                vehicle = batch["vehicle_id"][index]
                subtrack = int(batch["subtrack_index"][index])
                for start_prefix, end_prefix in (("prev_", ""), ("", "next_")):
                    start_id = batch[start_prefix + "point_id"][index]
                    end_id = batch[end_prefix + "point_id"][index]
                    if start_id is None or end_id is None:
                        continue
                    start_seq = int(batch[start_prefix + "refined_sequence_in_subtrack"][index])
                    end_seq = int(batch[end_prefix + "refined_sequence_in_subtrack"][index])
                    if end_seq != start_seq + 1:
                        raise ValueError("grid neighbours are not adjacent Refine points")
                    start_kind = int(batch[start_prefix + "point_kind"][index])
                    end_kind = int(batch[end_prefix + "point_kind"][index])
                    key = (vehicle, subtrack, start_kind, int(start_id),
                           end_kind, int(end_id))
                    if key in records:
                        continue
                    if len(records) >= max_segments:
                        raise GridSegmentBudgetError(
                            f"grid exceeds max_segments={max_segments}"
                        )
                    record = (
                        vehicle, subtrack, start_seq, start_kind, int(start_id),
                        end_kind, int(end_id), end_seq,
                        batch[start_prefix + "source_id"][index],
                        batch[start_prefix + "source_row_key"][index],
                        batch[start_prefix + "generated_reason"][index],
                        float(batch[start_prefix + "timestamp"][index]),
                        float(batch[start_prefix + "lon"][index]),
                        float(batch[start_prefix + "lat"][index]),
                        float(batch[start_prefix + "heading"][index]),
                        float(batch[start_prefix + "speed"][index]),
                        float(batch[start_prefix + "x"][index]),
                        float(batch[start_prefix + "y"][index]),
                        float(batch[end_prefix + "x"][index]),
                        float(batch[end_prefix + "y"][index]),
                        float(batch[end_prefix + "heading"][index]),
                    )
                    # Include key and record containers, unique field values,
                    # dictionary slack, two sorting lists, and 15 float/int
                    # NumPy output columns. This deliberately overestimates
                    # shared strings to make the payload budget conservative.
                    cost = (
                        sys.getsizeof(key) + sys.getsizeof(record)
                        + sum(sys.getsizeof(value) for value in record)
                        + 256 + 32 + 15 * 8
                    )
                    if estimated_bytes + cost > max_bytes:
                        raise GridSegmentBudgetError(
                            f"grid exceeds max_bytes={max_bytes}"
                        )
                    old_dict_bytes = sys.getsizeof(records)
                    records[key] = record
                    estimated_bytes += cost + max(
                        0, sys.getsizeof(records) - old_dict_bytes,
                    )
                    if estimated_bytes > max_bytes:
                        raise GridSegmentBudgetError(
                            f"grid exceeds max_bytes={max_bytes}"
                        )
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()

    ordered = [records[key] for key in sorted(
        records, key=lambda key: (
            key[0], key[1], records[key][2], key[2], key[3], key[4], key[5],
        ),
    )]
    length = len(ordered)
    floats = {
        name: np.fromiter((record[position] for record in ordered),
                          dtype=np.float64, count=length)
        for name, position in (
            ("timestamp", 11), ("lon", 12), ("lat", 13),
            ("heading", 14), ("speed", 15),
            ("x_start", 16), ("y_start", 17),
            ("x_end", 18), ("y_end", 19), ("end_heading", 20),
        )
    }
    integers = {
        name: np.fromiter((record[position] for record in ordered),
                          dtype=np.int64, count=length)
        for name, position in (
            ("subtrack_index", 1),
            ("start_refined_sequence_in_subtrack", 2),
            ("start_point_id", 4), ("end_point_id", 6),
            ("end_refined_sequence_in_subtrack", 7),
        )
    }
    return {
        **floats, **integers,
        "vehicle_id": tuple(record[0] for record in ordered),
        "start_point_kind": tuple(
            "kept" if record[3] == 0 else "injected" for record in ordered
        ),
        "end_point_kind": tuple(
            "kept" if record[5] == 0 else "injected" for record in ordered
        ),
        "start_source_id": tuple(record[8] for record in ordered),
        "start_source_row_key": tuple(record[9] for record in ordered),
        "start_generated_reason": tuple(record[10] for record in ordered),
        "segment_identity": tuple(
            (record[0], record[1], record[3], record[4], record[5], record[6])
            for record in ordered
        ),
    }
