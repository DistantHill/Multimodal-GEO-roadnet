"""Resolve event identities to source segments, preserving complete subtracks."""

from collections import defaultdict

from ..api.map_contracts import ReplayRequest
from ..map_errors import MapDataError, MapNotFoundError
from ..map_features import create_map_feature, create_map_feature_collection
from .build_segment_features import build_segment_features
from .track_points import build_track_feature, complete_track_groups


def _complete_groups(rows, request):
    if any(row[1] is None or row[4] is None or row[5] is None for row in rows):
        raise MapDataError("所选轨迹的来源 geom 缺失，无法完整回放。")
    grouped, truncated = complete_track_groups(rows, request.max_points)
    if truncated:
        raise MapDataError("完整子轨迹超过 max_points 上限，请提高上限后重试。")
    return grouped


def _collection(features, grouped, **metadata):
    return create_map_feature_collection(
        features, operation="replay", truncated=False,
        returned_track_count=len(grouped),
        returned_point_count=sum(len(points) for points in grouped.values()),
        highlight_segment_count=sum(f["properties"]["replay_role"] == "highlight" for f in features),
        context_segment_count=sum(f["properties"]["replay_role"] == "context" for f in features),
        **metadata,
    )


def _event_segments(store, request, detect_id, cross_ids, turn_ids, *, event_features=None):
    starts = set()
    turns = set()
    detect = store._resolve_detect(detect_id) if event_features is not None else None
    if cross_ids:
        columns = "id, from_segment_start_point_id, to_segment_start_point_id"
        if event_features is not None:
            columns += ", ST_AsGeoJSON(geom)::jsonb, to_jsonb(tracks_cross) - 'geom'"
        rows = store._fetchall(
            f"SELECT {columns} FROM tracks_cross "
            "WHERE detect_id = %s AND id = ANY(%s::bigint[])",
            (detect_id, sorted(cross_ids)),
        )
        if {row[0] for row in rows} != set(cross_ids):
            raise MapNotFoundError("所选 tracks_cross 不属于当前 Detect 或记录缺失。")
        starts.update(point_id for row in rows for point_id in row[1:3])
        if event_features is not None:
            event_features.extend(_event_feature(row[0], row[3], row[4], "cross_points", "tracks_cross") for row in rows)
    if turn_ids:
        if event_features is None:
            rows = store._fetchall(
                "SELECT id, resolved_point_id FROM turn_events "
                "WHERE detect_id = %s AND id = ANY(%s::bigint[])",
                (detect_id, sorted(turn_ids)),
            )
        else:
            refine = detect.refine
            rows = store._fetchall(
                "SELECT event.id, event.resolved_point_id, "
                "ST_AsGeoJSON(COALESCE(source.geom, injected.geom))::jsonb, to_jsonb(event) "
                "FROM turn_events event "
                "LEFT JOIN resolved_subtrack_points point ON point.id = event.resolved_point_id "
                "AND point.detect_id = event.detect_id "
                "LEFT JOIN kept_trajectory_points kept ON kept.id = point.kept_point_id AND kept.refine_id = %s "
                f"LEFT JOIN {refine.quoted_source_table} source "
                f"ON source.id = kept.source_row_key::{refine.source_key_sql_type} "
                "LEFT JOIN injected_points injected ON injected.id = point.injected_point_id AND injected.refine_id = %s "
                "WHERE event.detect_id = %s AND event.id = ANY(%s::bigint[])",
                (refine.refine_id, refine.refine_id, detect_id, sorted(turn_ids)),
            )
        if {row[0] for row in rows} != set(turn_ids):
            raise MapNotFoundError("所选 turn_event 不属于当前 Detect 或记录缺失。")
        turns.update(row[1] for row in rows)
        if event_features is not None:
            event_features.extend(_event_feature(row[0], row[2], row[3], "turn_events", "turn_events") for row in rows)
    if not starts and not turns:
        return [], {}, None
    detect = detect or store._resolve_detect(detect_id)
    seeds = starts | turns
    selected, found, truncated = store._select_resolved_tracks(
        detect, resolved_point_ids=sorted(seeds), source_row_keys=None,
        bbox=None, max_vehicles=request.max_vehicles,
    )
    if seeds != set(found):
        raise MapDataError("事件引用的 resolved 点缺失，无法回放。")
    if truncated:
        raise MapDataError("来源子轨迹超过 max_vehicles 上限，请提高上限后重试。")
    grouped = _complete_groups(
        store._fetch_resolved_tracks(detect, selected, request.max_points + 1), request,
    )
    # Only turn anchors select adjacent segments. Cross events identify exact
    # segment starts; treating these starts as ordinary points highlights neighbors.
    selection = request.model_copy(update={"resolved_point_ids": sorted(turns), "source_row_keys": None})
    features = build_segment_features(store, grouped, selection, detect_id, segment_start_ids=starts)
    highlighted_starts = {
        f["properties"]["segment_start_point_id"] for f in features
        if f["properties"]["replay_role"] == "highlight"
    }
    if not starts <= highlighted_starts:
        raise MapDataError("事件来源 segment 的后继端点缺失，无法完整回放。")
    return features, grouped, detect.refine.refine_id


def replay_events(store, request):
    detect_id = str(request.detect_id)
    features, grouped, refine_id = _event_segments(
        store, request, detect_id, request.tracks_cross_ids or [], request.turn_event_ids or [],
    )
    return _collection(
        features, grouped, detect_id=detect_id, refine_id=refine_id,
        selection="tracks_cross_ids" if request.tracks_cross_ids is not None else "turn_event_ids",
        tracks_cross_ids=request.tracks_cross_ids, turn_event_ids=request.turn_event_ids,
    )


def _event_feature(identifier, geometry, properties, layer, table):
    if geometry is None:
        raise MapDataError(f"{table} 事件的来源 geom 缺失，无法完整回放。")
    return create_map_feature(
        f"{layer}:{identifier}", geometry,
        **{**properties, "event_id": identifier, "source_table": table,
           "replay_layer": layer, "replay_role": "event"},
    )


def _event_indexes(event_features):
    by_point, by_start, turns_by_point = defaultdict(list), defaultdict(list), defaultdict(list)
    for event in event_features:
        props = event["properties"]
        source = {"kind": props["source_table"], "id": props["event_id"],
                  "intersection_ids": props["intersection_ids"]}
        if props["source_table"] == "turn_events":
            by_point[props["resolved_point_id"]].append(source)
            turns_by_point[props["resolved_point_id"]].append(source)
        else:
            for point_id in {props["from_segment_start_point_id"], props["to_segment_start_point_id"]}:
                by_point[point_id].append(source)
                by_start[point_id].append(source)
    return by_point, by_start, turns_by_point


def _source_events(feature, indexes):
    """Resolve parent links through indexed anchors instead of scanning all events."""
    by_point, by_start, turns_by_point = indexes
    segment_start = feature["properties"].get("segment_start_point_id")
    sources = {}
    if segment_start is not None:
        for source in by_start[segment_start]:
            sources[(source["kind"], source["id"])] = source
    point_index = turns_by_point if segment_start is not None else by_point
    for point_id in set(feature["properties"]["resolved_point_ids"]):
        for source in point_index[point_id]:
            sources[(source["kind"], source["id"])] = source
    return [sources[key] for key in sorted(sources)]


def expand_intersection_segments(store, request, members):
    """Traverse intersection -> events -> source segments -> complete subtracks."""
    cross_ids, turn_ids = set(), set()
    event_intersections = {}
    metadata = members["metadata"]
    for feature in members["features"]:
        for event in feature["properties"].get("events", []):
            if event["kind"] == "tracks_cross":
                cross_ids.add(event["id"])
            elif event["kind"] == "turn_event":
                turn_ids.add(event["id"])
            else:
                continue
            intersection_id = event.get("intersection_id", feature["properties"].get("intersection_id"))
            table = "turn_events" if event["kind"] == "turn_event" else "tracks_cross"
            roots = event_intersections.setdefault((table, event["id"]), set())
            if intersection_id is not None:
                roots.add(intersection_id)
    if len(cross_ids) + len(turn_ids) > request.max_points:
        raise MapDataError("交叉口来源事件超过 max_points 上限，请提高上限后重试。")
    event_features = []
    features, grouped, refine_id = _event_segments(
        store, request, metadata["detect_id"], cross_ids, turn_ids, event_features=event_features,
    )
    for event in event_features:
        props = event["properties"]
        roots = event_intersections[(props["source_table"], props["event_id"])]
        props["intersection_ids"] = sorted(roots)
        props["refine_id"] = refine_id
    indexes = _event_indexes(event_features)
    segments = [feature for feature in features if feature["properties"]["replay_role"] == "highlight"]
    for feature in segments:
        feature["properties"].update(replay_layer="segments", refine_id=refine_id,
                                     events=_source_events(feature, indexes))
    subtracks = []
    for key, points in grouped.items():
        feature = build_track_feature(key, points, metadata["detect_id"])
        feature["id"] = f"subtrack:{feature['id']}"
        feature["properties"].update(replay_layer="subtracks", replay_role="context", refine_id=refine_id,
                                     events=_source_events(feature, indexes))
        subtracks.append(feature)
    provenance = {key: value for key, value in metadata.items()
                  if key not in {"operation", "returned_point_count", "truncated"}}
    result = _collection(segments, grouped, refine_id=refine_id, **provenance)
    result["features"] = event_features + segments + subtracks
    result["metadata"].update(
        replay_layers=[
            {"id": "turn_events", "geometry_type": "Point"},
            {"id": "cross_points", "geometry_type": "Point"},
            {"id": "segments", "geometry_type": "LineString"},
            {"id": "subtracks", "geometry_type": "LineString"},
        ],
        returned_turn_event_count=len(turn_ids), returned_cross_point_count=len(cross_ids),
        returned_segment_count=len(segments), returned_subtrack_count=len(subtracks),
    )
    return result


def replay_injected_points(store, request):
    # Entity IDs are global; derive the Refine Run from the selected records,
    # never from a default/latest Detect Run.
    rows = store._fetchall(
        "SELECT id, refine_id::text, subtrack_index, after_sequence_in_subtrack "
        "FROM injected_points WHERE id = ANY(%s::bigint[]) ORDER BY id",
        (request.injected_point_ids,),
    )
    if {row[0] for row in rows} != set(request.injected_point_ids):
        raise MapNotFoundError("所选 injected_points 记录缺失。")
    refine_ids = {row[1] for row in rows}
    if len(refine_ids) != 1:
        raise MapDataError("注入点必须属于同一个 Refine Run。")
    refine_id = next(iter(refine_ids))
    if request.refine_id is not None and str(request.refine_id) != refine_id:
        raise MapNotFoundError("注入点不属于所选 Refine Run。")
    refine = store._resolve_refine(refine_id)
    brackets = store._fetchall(
        "SELECT injected.id, before.source_row_key, after.source_row_key "
        "FROM injected_points AS injected "
        "LEFT JOIN kept_trajectory_points AS before ON before.refine_id = injected.refine_id "
        "AND before.subtrack_index = injected.subtrack_index "
        "AND before.sequence_in_subtrack = injected.after_sequence_in_subtrack "
        "LEFT JOIN kept_trajectory_points AS after ON after.refine_id = injected.refine_id "
        "AND after.subtrack_index = injected.subtrack_index "
        "AND after.sequence_in_subtrack = injected.after_sequence_in_subtrack + 1 "
        "WHERE injected.id = ANY(%s::bigint[])",
        (request.injected_point_ids,),
    )
    if len(brackets) != len(rows) or any(a is None or b is None for _, a, b in brackets):
        raise MapDataError("注入点的 kept 来源端点缺失，无法回放。")
    indexes = sorted({row[2] for row in rows})
    track_rows = store._fetchall(
        "SELECT kept.subtrack_index, kept.vehicle_id, NULL::bigint, kept.source_row_key, "
        "ST_X(source.geom), ST_Y(source.geom), 'kept', NULL::bigint, NULL::bigint, NULL::smallint "
        "FROM kept_trajectory_points kept "
        f"LEFT JOIN {refine.quoted_source_table} source "
        f"ON source.id = kept.source_row_key::{refine.source_key_sql_type} "
        "WHERE kept.refine_id = %s AND kept.subtrack_index = ANY(%s::integer[]) "
        "ORDER BY kept.subtrack_index, kept.sequence_in_subtrack LIMIT %s",
        (refine_id, indexes, request.max_points + 1),
    )
    grouped = _complete_groups(track_rows, request)
    if len({key[0] for key in grouped}) > request.max_vehicles:
        raise MapDataError("来源车辆超过 max_vehicles 上限，请提高上限后重试。")
    selection = ReplayRequest(source_row_keys=sorted({str(row[1]) for row in brackets}))
    features = build_segment_features(store, grouped, selection, None)
    pairs = {(str(a), str(b)) for _, a, b in brackets}
    found = set()
    for feature in features:
        keys = tuple(feature["properties"]["source_row_keys"])
        highlight = keys in pairs
        feature["properties"].update(replay_role="highlight" if highlight else "context", refine_id=refine_id)
        if highlight:
            found.add(keys)
    if found != pairs:
        raise MapDataError("注入点的完整来源 segment 缺失，无法回放。")
    return _collection(features, grouped, selection="injected_point_ids", refine_id=refine_id,
                       injected_point_ids=request.injected_point_ids, detect_id=None)
