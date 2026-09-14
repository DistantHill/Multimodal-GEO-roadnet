"""Classify only source segments adjacent to selected points as participants."""

from .map_api import MapDataError, _track_feature


def segment_features(store, grouped, request, detect_id):
    selected_ids = set(request.resolved_point_ids or [])
    selected_keys = set(request.source_row_keys or [])
    crossing_ids = [point.crossing_id for points in grouped.values() for point in points
                    if point.resolved_point_id in selected_ids and point.point_kind == "crossing"]
    starts = set()
    if crossing_ids:
        rows = store._fetchall(
            "SELECT id, segment_start_point_id FROM candidate_entry_exit_points"
            " WHERE detect_id = %s AND id = ANY(%s::bigint[])",
            (detect_id, crossing_ids),
        )
        if {row[0] for row in rows} != set(crossing_ids):
            raise MapDataError("所选进出点的 segment 溯源记录缺失。")
        starts = {row[1] for row in rows}

    def selected(point):
        return point.resolved_point_id in selected_ids or (
            point.source_row_key is not None and point.source_row_key in selected_keys
        )

    features = []
    for key, points in grouped.items():
        # Detection segments join consecutive refined points; persisted crossing
        # points inside them do not turn the rest of the subtrack into participants.
        anchors = [index for index, point in enumerate(points) if point.point_kind != "crossing"]
        if not anchors:
            raise MapDataError("轨迹缺少 segment 端点，无法绘制。")
        spans = list(zip(anchors, anchors[1:]))
        if len(points) == 1:
            spans = [(0, 0)]
        for start, end in spans:
            segment = points[start:end + 1]
            participating = (
                points[start].resolved_point_id in starts
                or selected(points[start]) or selected(points[end])
            )
            feature = _track_feature(key, segment, detect_id)
            feature["id"] = f"{feature['id']}:segment:{start}"
            feature["properties"].update(
                replay_role="highlight" if participating else "context",
                segment_start_point_id=points[start].resolved_point_id,
                segment_end_point_id=points[end].resolved_point_id,
            )
            features.append(feature)
    return features
