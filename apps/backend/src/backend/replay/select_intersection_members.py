"""Bounded event-member selection using Pipeline's existing point replay helper."""

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from ..map_errors import MapDataError, MapNotFoundError
from ..map_features import create_map_feature, create_map_feature_collection

if TYPE_CHECKING:
    from ..stores.map_layers import PostgresMapStore
    from ..api.map_contracts import ReplayRequest


def replay_intersection(store: "PostgresMapStore", request: "ReplayRequest") -> dict[str, Any]:
    from track.v2.models import PointRef, TrajectoryPoint
    from track.v2.validation.resolve_point_references import replay_points

    row = store._fetchone(
        "SELECT detect_id::text, candidate_intersection_id FROM intersections WHERE id = %s",
        (request.intersection_id,),
    )
    if row is None:
        raise MapNotFoundError(f"unknown intersection: {request.intersection_id}")
    detect_id, candidate_id = row
    if request.detect_id is not None and str(request.detect_id) != detect_id:
        raise MapNotFoundError("intersection does not belong to the requested Detect Run")
    detect = store._resolve_detect(detect_id)
    refine = detect.refine

    # Segment endpoints use refined order: final order may contain subsequently
    # inserted crossing points which were not part of the detection segment.
    rows = store._fetchall(
        "WITH event_points AS ("
        " SELECT resolved_point_id AS id, 'turn_event' AS kind, id AS event_id, FALSE AS segment"
        " FROM turn_events WHERE detect_id = %s AND candidate_intersection_id = %s"
        " UNION ALL SELECT endpoint.id, 'tracks_cross', event.id, TRUE"
        " FROM tracks_cross AS event CROSS JOIN LATERAL"
        " (VALUES(event.from_segment_start_point_id), (event.to_segment_start_point_id)) endpoint(id)"
        " WHERE event.detect_id = %s AND event.candidate_intersection_id = %s"
        "), members AS ("
        " SELECT id, kind, event_id FROM event_points"
        " UNION SELECT successor.id, event.kind, event.event_id FROM event_points AS event"
        " JOIN LATERAL (SELECT * FROM resolved_subtrack_points WHERE id = event.id AND detect_id = %s LIMIT 1) AS start ON TRUE"
        " LEFT JOIN LATERAL (SELECT successor.id FROM resolved_subtrack_points successor"
        " WHERE successor.detect_id = start.detect_id"
        " AND successor.subtrack_index = start.subtrack_index"
        " AND successor.refined_sequence_in_subtrack = start.refined_sequence_in_subtrack + 1 LIMIT 1) AS successor ON TRUE"
        " WHERE event.segment"
        ") SELECT point.id, kept.source_row_key, injected.id,"
        " COALESCE(kept.vehicle_id, injected.vehicle_id),"
        " COALESCE(source.timestamp, injected.timestamp),"
        " COALESCE(source.lon, injected.lon), COALESCE(source.lat, injected.lat),"
        " COALESCE(source.heading, injected.heading), COALESCE(source.speed, injected.speed),"
        " jsonb_agg(DISTINCT jsonb_build_object('kind', members.kind, 'id', members.event_id))"
        " FROM members LEFT JOIN LATERAL (SELECT * FROM resolved_subtrack_points WHERE id = members.id AND detect_id = %s LIMIT 1) AS point ON TRUE"
        " LEFT JOIN LATERAL (SELECT * FROM kept_trajectory_points WHERE id = point.kept_point_id AND refine_id = %s LIMIT 1) AS kept ON TRUE"
        " LEFT JOIN LATERAL (SELECT * FROM injected_points WHERE id = point.injected_point_id AND refine_id = %s LIMIT 1) AS injected ON TRUE"
        f" LEFT JOIN LATERAL (SELECT * FROM {refine.quoted_source_table} WHERE id = kept.source_row_key::{refine.source_key_sql_type} LIMIT 1) AS source ON TRUE"
        " GROUP BY point.id, kept.source_row_key, injected.id, kept.vehicle_id, injected.vehicle_id,"
        " source.timestamp, injected.timestamp, source.lon, injected.lon, source.lat, injected.lat,"
        " source.heading, injected.heading, source.speed, injected.speed"
        " ORDER BY point.id LIMIT %s",
        (detect_id, candidate_id, detect_id, candidate_id, detect_id, detect_id,
         refine.refine_id, refine.refine_id, request.max_points + 1),
    )
    if len(rows) > request.max_points:
        raise MapDataError("交叉口构成点超过 max_points 上限，请提高上限后重试；未返回部分结果。")
    points = []
    tracks = defaultdict(list)
    for row in rows:
        point_id, source_key, injected_id, vehicle, timestamp, lon, lat, heading, speed, events = row
        if any(value is None for value in (vehicle, timestamp, lon, lat, heading, speed)):
            raise MapDataError("交叉口事件引用的构成点缺失，无法完整回放。")
        # Separate kept/injected namespaces avoid collisions with source keys.
        reference = PointRef(
            source_id=f"{refine.refine_id}:" + ("injected" if injected_id is not None else "source"),
            source_row_key=str(injected_id if injected_id is not None else source_key),
            stable=True,
        )
        point = TrajectoryPoint(reference, str(vehicle), float(timestamp), float(lon), float(lat),
                                float(heading), float(speed), extra={
                                    "resolved_point_id": point_id, "source_row_key": source_key,
                                    "injected_point_id": injected_id, "events": events,
                                })
        points.append(point)
        tracks[point.vehicle_id].append(point)
    replayed = replay_points(points, dict(tracks))
    features = [
        create_map_feature(point.extra["resolved_point_id"],
                 {"type": "Point", "coordinates": [point.lon, point.lat]},
                 **point.extra, vehicle_id=point.vehicle_id, timestamp=point.timestamp,
                 intersection_id=request.intersection_id, detect_id=detect_id,
                 candidate_intersection_id=candidate_id)
        for point in replayed
    ]
    return create_map_feature_collection(features, operation="replay", selection="intersection_id",
                               intersection_id=request.intersection_id, detect_id=detect_id,
                               candidate_intersection_id=candidate_id,
                               returned_point_count=len(features), truncated=False)
