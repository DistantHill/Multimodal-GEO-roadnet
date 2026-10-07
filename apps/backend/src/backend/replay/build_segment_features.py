"""Classify only source segments adjacent to selected points as participants."""

import numpy as np

from ..map_errors import MapDataError
from .track_points import build_track_feature


def build_segment_features(store, grouped, request, detect_id, *, segment_start_ids=(), highlight_only=False):
    selected_ids = set(request.resolved_point_ids or [])
    selected_keys = set(request.source_row_keys or [])
    crossing_ids = [int(identifier) for track in grouped.values()
                    for identifier in track.crossing_ids[
                        track.resolved_valid & np.isin(track.resolved_point_ids, list(selected_ids))
                        & (track.point_kinds == "crossing") & track.crossing_valid]]
    starts = set(segment_start_ids)
    if crossing_ids:
        rows = store._fetchall(
            "SELECT id, segment_start_point_id FROM candidate_entry_exit_points"
            " WHERE detect_id = %s AND id = ANY(%s::bigint[])",
            (detect_id, crossing_ids),
        )
        if {row[0] for row in rows} != set(crossing_ids):
            raise MapDataError("所选进出点的 segment 溯源记录缺失。")
        starts.update(row[1] for row in rows)

    def selected(track, index):
        return (track.resolved_valid[index] and int(track.resolved_point_ids[index]) in selected_ids) or (
            track.source_valid[index] and str(track.source_row_keys[index]) in selected_keys)

    features = []
    for key, points in grouped.items():
        # Detection segments join consecutive refined points; persisted crossing
        # points inside them do not turn the rest of the subtrack into participants.
        anchors = np.flatnonzero(points.point_kinds != "crossing").tolist()
        if not anchors:
            raise MapDataError("轨迹缺少 segment 端点，无法绘制。")
        spans = list(zip(anchors, anchors[1:]))
        if len(points) == 1:
            spans = [(0, 0)]
        for start, end in spans:
            participating = (
                int(points.resolved_point_ids[start]) in starts
                or selected(points,start) or selected(points,end)
            )
            if highlight_only and not participating:
                continue
            segment = points.slice(start,end+1)
            feature = build_track_feature(key, segment, detect_id)
            feature["id"] = f"{feature['id']}:segment:{start}"
            feature["properties"].update(
                replay_role="highlight" if participating else "context",
                segment_start_point_id=int(points.resolved_point_ids[start]) if points.resolved_valid[start] else None,
                segment_end_point_id=int(points.resolved_point_ids[end]) if points.resolved_valid[end] else None,
            )
            features.append(feature)
    return features
