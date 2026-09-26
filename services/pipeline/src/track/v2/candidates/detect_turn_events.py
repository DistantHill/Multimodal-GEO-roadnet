"""Turn geometry on complete ordered subtrack columns."""
from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING
import numpy as np

from .._geometry import _angle_delta_deg, _circular_mean_deg
from ..models import RefinementSubtrack, TurnBatch

if TYPE_CHECKING:
    from ..intersections.detect_intersections import IntersectConfig


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


def turns_from_subtrack(track: RefinementSubtrack, cfg: IntersectConfig, *, first_index: int = 0) -> TurnBatch:
    w = cfg.turn_window
    if len(track) < 2 * w + 1:
        return empty_turn_batch()
    positions = np.arange(w, len(track) - w, dtype=np.int64)
    in_leg = np.linalg.norm(track.xy[positions] - track.xy[positions - w], axis=1)
    out_leg = np.linalg.norm(track.xy[positions + w] - track.xy[positions], axis=1)
    elapsed = track.timestamp[positions + w] - track.timestamp[positions - w]
    moving = (elapsed > 0) & (in_leg >= cfg.min_leg_m) & (out_leg >= cfg.min_leg_m)
    speed = np.divide(in_leg + out_leg, elapsed, out=np.zeros(len(positions)), where=elapsed > 0)
    positions = positions[moving & (speed >= cfg.min_movement_speed_mps)]
    entry = np.fromiter((_circular_mean_deg(track.heading[i-w:i]) for i in positions), dtype=np.float64, count=len(positions))
    exit_ = np.fromiter((_circular_mean_deg(track.heading[i:i+w]) for i in positions), dtype=np.float64, count=len(positions))
    turn = (exit_ - entry + 180.0) % 360.0 - 180.0
    keep = (np.abs(turn) >= cfg.min_turn_deg) & (np.abs(turn) <= cfg.max_turn_deg)
    positions, entry, exit_, turn = positions[keep], entry[keep], exit_[keep], turn[keep]
    size = len(positions)
    return TurnBatch(
        calculation_index=np.arange(first_index, first_index + size, dtype=np.int64),
        vehicle_id=np.full(size, track.vehicle_id, dtype=object),
        subtrack_index=np.full(size, track.subtrack_index, dtype=np.int64), point_index=positions,
        xy=track.xy[positions], lonlat=track.lonlat[positions], timestamp=track.timestamp[positions],
        entry_bearing=entry, exit_bearing=exit_, turn_deg=turn, score=np.abs(turn),
        source_id=np.full(size, track.source_id, dtype=object),
        source_row_key=track.source_row_key[positions], source_row_key_valid=track.source_row_key_valid[positions],
        point_kind=track.kind[positions], refine_point_id=track.refine_point_id[positions],
        refine_point_id_valid=track.refine_point_id_valid[positions], generated_reason=track.generated_reason[positions],
        g_star_z=np.zeros(size, dtype=np.float64), g_star_z_valid=np.zeros(size, dtype=np.bool_),
    )


def concatenate_turn_batches(batches: list[TurnBatch]) -> TurnBatch:
    batches = list(batches)
    if not batches:
        return empty_turn_batch()
    return TurnBatch(**{item.name: np.concatenate([getattr(batch, item.name) for batch in batches]) for item in fields(TurnBatch)})


def detect_turn_patch(tracks: dict[int, RefinementSubtrack], cfg: IntersectConfig, *, perf=None) -> TurnBatch:
    batches = []
    offset = 0
    iterator = tracks.values() if perf is None else perf.timed_iter(tracks.values(),name="turn_tracks",total=len(tracks))
    for track in iterator:
        batch = turns_from_subtrack(track, cfg, first_index=offset)
        batches.append(batch)
        offset += len(batch)
    return concatenate_turn_batches(batches)
