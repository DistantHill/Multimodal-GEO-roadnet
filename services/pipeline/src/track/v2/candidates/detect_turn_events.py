"""Turn-event geometry for one track patch; no scheduling or progress output."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .._geometry import _angle_delta_deg, _circular_mean_deg, _distance_m, _point_xy
from ..intersections.performance_timing import PerfTracker
from ..models import TrajectoryPoint, TurnEvent

if TYPE_CHECKING:
    from ..intersections.detect_intersections import IntersectConfig


def detect_turn_patch(
    tracks: dict[str, list[TrajectoryPoint]],
    cfg: IntersectConfig,
    *,
    perf: PerfTracker | None = None,
) -> list[TurnEvent]:
    """Detect bearing changes within one externally scheduled track patch.

    For each track, slides a window of ``turn_window`` points before and
    after each candidate index. Computes in/out bearings via circular mean
    of segment bearings, then detects a turn if the absolute angle delta
    falls within ``[min_turn_deg, max_turn_deg]``.
    """
    events: list[TurnEvent] = []
    w = cfg.turn_window

    track_iter = perf.timed_iter(tracks.items(), name="tracks_turn") if perf else tracks.items()
    for _track_key, track in track_iter:
        if len(track) < 2 * w + 1:
            continue

        xy = [_point_xy(p) for p in track]
        # 直接使用 GPS heading 作为逐段方位角，避免 O(w*(n-2w)) 次 atan2
        headings = [p.heading for p in track]

        for i in range(w, len(track) - w):
            in_leg = _distance_m(xy[i - w], xy[i])
            out_leg = _distance_m(xy[i], xy[i + w])
            elapsed = track[i + w].timestamp - track[i - w].timestamp
            if elapsed <= 0.0:
                continue  # degenerate or duplicate timestamps — skip
            movement_speed = (in_leg + out_leg) / elapsed

            if (
                in_leg < cfg.min_leg_m
                or out_leg < cfg.min_leg_m
                or movement_speed < cfg.min_movement_speed_mps
            ):
                continue

            in_angles = headings[i - w : i]
            out_angles = headings[i : i + w]

            in_bearing = _circular_mean_deg(in_angles)
            out_bearing = _circular_mean_deg(out_angles)
            turn = _angle_delta_deg(in_bearing, out_bearing)
            abs_turn = abs(turn)

            if cfg.min_turn_deg <= abs_turn <= cfg.max_turn_deg:
                p = track[i]
                events.append(
                    TurnEvent(
                        event_calculate_index=len(events),
                        vehicle_id=p.vehicle_id,
                        timestamp=p.timestamp,
                        lon=p.lon,
                        lat=p.lat,
                        x=xy[i][0],
                        y=xy[i][1],
                        entry_bearing=in_bearing,
                        exit_bearing=out_bearing,
                        turn_deg=turn,
                        score=abs_turn,
                        source="turn",
                        point_ref=p.point_ref,
                        refined_point_ref=p.refined_point_ref,
                        source_ids=(p.point_ref.source_id,),
                    )
                )

    return events
