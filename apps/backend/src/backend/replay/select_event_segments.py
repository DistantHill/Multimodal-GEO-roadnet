"""Resolve event identities to source segments, preserving complete subtracks."""

from ..api.map_contracts import ReplayRequest
from ..map_errors import MapDataError, MapNotFoundError
from ..map_features import create_map_feature_collection
from .build_segment_features import build_segment_features
from .track_points import complete_track_groups


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


def _event_segments(store, request, detect_id, cross_ids, turn_ids):
    starts = set()
    turns = set()
    if cross_ids:
        rows = store._fetchall(
            "SELECT id, from_segment_start_point_id, to_segment_start_point_id "
            "FROM tracks_cross WHERE detect_id = %s AND id = ANY(%s::bigint[])",
            (detect_id, sorted(cross_ids)),
        )
        if {row[0] for row in rows} != set(cross_ids):
            raise MapNotFoundError("所选 tracks_cross 不属于当前 Detect 或记录缺失。")
        starts.update(point_id for row in rows for point_id in row[1:])
    if turn_ids:
        rows = store._fetchall(
            "SELECT id, resolved_point_id FROM turn_events "
            "WHERE detect_id = %s AND id = ANY(%s::bigint[])",
            (detect_id, sorted(turn_ids)),
        )
        if {row[0] for row in rows} != set(turn_ids):
            raise MapNotFoundError("所选 turn_event 不属于当前 Detect 或记录缺失。")
        turns.update(row[1] for row in rows)
    if not starts and not turns:
        return [], {}, None
    detect = store._resolve_detect(detect_id)
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


def expand_intersection_segments(store, request, members):
    """Keep the validated intersection provenance, then expand its events once."""
    cross_ids, turn_ids = set(), set()
    for feature in members["features"]:
        for event in feature["properties"].get("events", []):
            if event["kind"] == "tracks_cross":
                cross_ids.add(event["id"])
            elif event["kind"] == "turn_event":
                turn_ids.add(event["id"])
    metadata = members["metadata"]
    features, grouped, refine_id = _event_segments(
        store, request, metadata["detect_id"], cross_ids, turn_ids,
    )
    provenance = {key: value for key, value in metadata.items()
                  if key not in {"operation", "returned_point_count", "truncated"}}
    return _collection(features, grouped, refine_id=refine_id, **provenance)


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
