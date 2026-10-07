"""Replay the exact persisted Detect members used to fit selected centerlines."""

from __future__ import annotations

import math
from collections import Counter, OrderedDict
from decimal import Decimal
from typing import Any

from ..api.map_contracts import ReplayRequest
from ..map_errors import MapDataError, MapNotFoundError
from ..map_features import create_map_feature, create_map_feature_collection


def _finite_number(value: Any, label: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise MapDataError(f"centerline replay has invalid {label}")
    number = float(value)
    if not math.isfinite(number):
        raise MapDataError(f"centerline replay has non-finite {label}")
    return number


def replay_centerlines(
    store: Any,
    request: ReplayRequest,
    map_config: Any,
) -> dict[str, Any]:
    """Return selected fitted clips and their exact resolved Detect points.

    The manifest supplies crossing endpoints. Only resolved members between each
    endpoint pair are fetched; no full Detect replay or spatial-neighbor query is
    performed. Coordinates and attributes come from the current source rows, so
    this selection does not claim to verify the historical Refine fingerprint.
    """
    if not request.centerline_ids:
        raise MapDataError("centerline_ids selection is required")
    if request.fit_id is None or request.detect_id is None:
        raise MapDataError("centerline_ids requires fit_id and detect_id")
    if request.max_points is None or request.max_vehicles is None:
        raise MapDataError("Replay limits must be resolved before centerline selection")

    fit_id, fit_detect_id = store._resolve_fit(str(request.fit_id))
    if fit_id != str(request.fit_id):
        raise MapNotFoundError(f"unknown completed Fit Run: {request.fit_id}")
    if fit_detect_id != str(request.detect_id):
        raise MapDataError("Fit ID does not belong to the requested Detect ID")
    detect = store._resolve_detect(fit_detect_id)
    if request.refine_id is not None and str(request.refine_id) != detect.refine.refine_id:
        raise MapDataError("Detect ID does not belong to the requested Refine ID")

    selected_ids = [int(value) for value in request.centerline_ids]
    centerline_rows = store._fetchall(
        "SELECT id, detect_id::text, variant, from_intersection_id, "
        "to_intersection_id, forward_clip_count, reverse_clip_count "
        "FROM centerlines WHERE fit_id = %s AND id = ANY(%s::bigint[]) "
        "ORDER BY id",
        (fit_id, selected_ids),
    )
    centerlines = {int(row[0]): row for row in centerline_rows}
    missing_ids = sorted(set(selected_ids) - set(centerlines))
    if missing_ids:
        raise MapNotFoundError(
            f"centerline IDs are not part of completed Fit Run {fit_id}: {missing_ids}"
        )
    if any(str(row[1]) != fit_detect_id for row in centerline_rows):
        raise MapDataError("selected centerline has inconsistent Detect lineage")

    manifest_by_centerline: dict[int, list[tuple[Any, ...]]] = {}
    total_clips = 0
    clip_limit = int(map_config.clip_feature_limit)
    for centerline_id in selected_ids:
        row = centerlines[centerline_id]
        expected_total = int(row[5]) + int(row[6])
        if expected_total <= 0:
            raise MapDataError(f"Centerline {centerline_id} has no source Clips")
        total_clips += expected_total
        if total_clips > clip_limit:
            raise MapDataError(
                f"centerline replay selects more than the configured clip limit {clip_limit}"
            )
        manifest = store._fetchall(
            "SELECT input_ordinal, start_crossing_id, end_crossing_id, "
            "reverse_to_centerline_direction "
            "FROM centerline_source_clips WHERE centerline_id = %s "
            "AND detect_id = %s ORDER BY input_ordinal LIMIT %s",
            (centerline_id, fit_detect_id, expected_total + 1),
        )
        if len(manifest) != expected_total or expected_total <= 0:
            raise MapDataError(
                f"Centerline {centerline_id} has an incomplete source Clip manifest"
            )
        if len({int(item[0]) for item in manifest}) != len(manifest):
            raise MapDataError(f"Centerline {centerline_id} has duplicate Clip ordinals")
        if [int(item[0]) for item in manifest] != list(range(expected_total)):
            raise MapDataError(f"Centerline {centerline_id} has non-contiguous Clip ordinals")
        forward = sum(not bool(item[3]) for item in manifest)
        reverse = sum(bool(item[3]) for item in manifest)
        if forward != int(row[5]) or reverse != int(row[6]):
            raise MapDataError(
                f"Centerline {centerline_id} source Clip directions disagree with counts"
            )
        manifest_by_centerline[centerline_id] = manifest

    clip_uses = Counter(
        (int(item[1]), int(item[2]))
        for manifest in manifest_by_centerline.values() for item in manifest
    )
    endpoint_ids = sorted({identifier for pair in clip_uses for identifier in pair})
    endpoint_rows = store._fetchall(
        "SELECT crossing.id, crossing.crossing_type, crossing.candidate_intersection_id, "
        "intersection.id, resolved.id, resolved.subtrack_index, "
        "resolved.sequence_in_subtrack "
        "FROM candidate_entry_exit_points AS crossing "
        "JOIN resolved_subtrack_points AS resolved "
        "ON resolved.detect_id = crossing.detect_id "
        "AND resolved.crossing_point_id = crossing.id "
        "JOIN intersections AS intersection "
        "ON intersection.detect_id = crossing.detect_id "
        "AND intersection.candidate_intersection_id = crossing.candidate_intersection_id "
        "WHERE crossing.detect_id = %s AND crossing.id = ANY(%s::bigint[]) "
        "ORDER BY crossing.id",
        (fit_detect_id, endpoint_ids),
    )
    endpoints = {int(item[0]): item for item in endpoint_rows}
    shared_members: dict[tuple[int, int], list[Any]] = {}

    point_records: OrderedDict[int, dict[str, Any]] = OrderedDict()
    clip_features: list[dict[str, Any]] = []
    membership_reference_count = 0
    vehicles: set[str] = set()
    membership_reference_limit = int(map_config.replay_max_points_max)

    for centerline_id in selected_ids:
        centerline = centerlines[centerline_id]
        from_intersection_id = int(centerline[3])
        to_intersection_id = int(centerline[4])
        for ordinal, start_crossing_id, end_crossing_id, reverse in manifest_by_centerline[
            centerline_id
        ]:
            start_crossing_id = int(start_crossing_id)
            end_crossing_id = int(end_crossing_id)
            reverse = bool(reverse)
            if start_crossing_id not in endpoints or end_crossing_id not in endpoints:
                raise MapDataError(
                    f"Centerline {centerline_id} Clip {ordinal} has a missing crossing endpoint"
                )
            start = endpoints[start_crossing_id]
            end = endpoints[end_crossing_id]
            if int(start[1]) != 0 or int(end[1]) != 1:
                raise MapDataError(
                    f"Centerline {centerline_id} Clip {ordinal} endpoint crossing types are invalid"
                )
            if int(start[5]) != int(end[5]) or int(start[6]) >= int(end[6]):
                raise MapDataError(
                    f"Centerline {centerline_id} Clip {ordinal} crossing order is inconsistent"
                )
            start_intersection_id, end_intersection_id = int(start[3]), int(end[3])
            expected_start = to_intersection_id if reverse else from_intersection_id
            expected_end = from_intersection_id if reverse else to_intersection_id
            if (start_intersection_id, end_intersection_id) != (expected_start, expected_end):
                raise MapDataError(
                    f"Centerline {centerline_id} Clip {ordinal} direction disagrees with endpoints"
                )

            remaining_references = membership_reference_limit - membership_reference_count
            if remaining_references < 0:
                raise MapDataError(
                    f"centerline replay exceeds membership reference limit {membership_reference_limit}"
                )
            clip_key = (start_crossing_id, end_crossing_id)
            point_rows = shared_members.get(clip_key)
            if point_rows is None:
                point_rows = store._fetchall(
                    f"SELECT resolved.id, resolved.sequence_in_subtrack, resolved.subtrack_index, "
                    "CASE WHEN resolved.kept_point_id IS NOT NULL THEN 'kept' "
                    "WHEN resolved.injected_point_id IS NOT NULL THEN 'injected' "
                    "WHEN resolved.crossing_point_id IS NOT NULL THEN 'crossing' END AS point_kind, "
                    "kept.source_row_key::text, resolved.injected_point_id, crossing.id, "
                    "intersection.id, crossing.crossing_type, "
                    "COALESCE(source.lon, injected.lon, crossing.crossing_lon), "
                    "COALESCE(source.lat, injected.lat, crossing.crossing_lat), "
                    "COALESCE(source.timestamp, injected.timestamp), "
                    "COALESCE(source.heading, injected.heading), "
                    "COALESCE(source.speed, injected.speed), vehicle.vehicle_id, "
                    "source.vehicle_id, "
                    "COALESCE(kept.vehicle_id, injected.vehicle_id) "
                    "FROM resolved_subtrack_points AS resolved "
                    "LEFT JOIN kept_trajectory_points AS kept "
                    "ON kept.id = resolved.kept_point_id AND kept.refine_id = %s "
                    f"LEFT JOIN {detect.refine.quoted_source_table} AS source "
                    f"ON source.id = kept.source_row_key::{detect.refine.source_key_sql_type} "
                    "LEFT JOIN injected_points AS injected "
                    "ON injected.id = resolved.injected_point_id AND injected.refine_id = %s "
                    "LEFT JOIN candidate_entry_exit_points AS crossing "
                    "ON crossing.id = resolved.crossing_point_id AND crossing.detect_id = resolved.detect_id "
                    "LEFT JOIN intersections AS intersection "
                    "ON intersection.detect_id = crossing.detect_id "
                    "AND intersection.candidate_intersection_id = crossing.candidate_intersection_id "
                    "LEFT JOIN LATERAL (SELECT COALESCE(anchor_kept.vehicle_id, anchor_injected.vehicle_id) "
                    "AS vehicle_id FROM resolved_subtrack_points AS anchor "
                    "LEFT JOIN kept_trajectory_points AS anchor_kept "
                    "ON anchor_kept.id = anchor.kept_point_id AND anchor_kept.refine_id = %s "
                    "LEFT JOIN injected_points AS anchor_injected "
                    "ON anchor_injected.id = anchor.injected_point_id AND anchor_injected.refine_id = %s "
                    "WHERE anchor.detect_id = resolved.detect_id "
                    "AND anchor.subtrack_index = resolved.subtrack_index "
                    "AND (anchor_kept.vehicle_id IS NOT NULL OR anchor_injected.vehicle_id IS NOT NULL) "
                    "ORDER BY anchor.sequence_in_subtrack LIMIT 1) AS vehicle ON TRUE "
                    "WHERE resolved.detect_id = %s AND resolved.subtrack_index = %s "
                    "AND resolved.sequence_in_subtrack BETWEEN %s AND %s "
                    "ORDER BY resolved.sequence_in_subtrack, resolved.id LIMIT %s",
                    (
                        detect.refine.refine_id,
                        detect.refine.refine_id,
                        detect.refine.refine_id,
                        detect.refine.refine_id,
                        fit_detect_id,
                        int(start[5]),
                        int(start[6]),
                        int(end[6]),
                        remaining_references + 1,
                    ),
                )
            if not point_rows:
                raise MapDataError(
                    f"Centerline {centerline_id} Clip {ordinal} has no resolved members"
                )
            if len(point_rows) > remaining_references:
                raise MapDataError(
                    f"centerline replay exceeds membership reference limit {membership_reference_limit}"
                )
            expected_member_count = int(end[6]) - int(start[6]) + 1
            if len(point_rows) != expected_member_count:
                raise MapDataError(
                    f"Centerline {centerline_id} Clip {ordinal} resolved sequence has missing members"
                )
            if int(point_rows[0][0]) != int(start[4]) or int(point_rows[-1][0]) != int(end[4]):
                raise MapDataError(
                    f"Centerline {centerline_id} Clip {ordinal} interval endpoints are inconsistent"
                )
            membership_reference_count += len(point_rows)
            clip_uses[clip_key] -= 1
            if clip_uses[clip_key]:
                shared_members[clip_key] = point_rows
            else:
                shared_members.pop(clip_key, None)

            clip_source = {
                "centerline_id": centerline_id,
                "input_ordinal": int(ordinal),
                "reverse_to_centerline_direction": reverse,
            }
            coordinates: list[list[float]] = []
            clip_resolved_ids: list[int] = []
            for point_row in point_rows:
                resolved_id = int(point_row[0])
                point_kind = point_row[3]
                if point_kind not in {"kept", "injected", "crossing"}:
                    raise MapDataError(
                        f"Centerline {centerline_id} Clip {ordinal} contains an invalid point source"
                    )
                lon = _finite_number(point_row[9], "longitude")
                lat = _finite_number(point_row[10], "latitude")
                assert lon is not None and lat is not None
                if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                    raise MapDataError(
                        f"Centerline {centerline_id} Clip {ordinal} has out-of-range coordinates"
                    )
                coordinates.append([lon, lat])
                clip_resolved_ids.append(resolved_id)
                source = {
                    **clip_source,
                    "resolved_point_id": resolved_id,
                    "sequence_in_subtrack": int(point_row[1]),
                }
                vehicle_id = point_row[14]
                if vehicle_id is None:
                    raise MapDataError(
                        f"Centerline {centerline_id} Clip {ordinal} has no vehicle anchor"
                    )
                if point_kind in {"kept", "injected"}:
                    persisted_member_vehicle = point_row[16]
                    if (
                        persisted_member_vehicle is None
                        or str(persisted_member_vehicle) != str(vehicle_id)
                    ):
                        raise MapDataError(
                            f"Centerline {centerline_id} Clip {ordinal} member vehicle lineage is inconsistent"
                        )
                raw_source_vehicle = point_row[15]
                source_vehicle_id = (
                    str(raw_source_vehicle) if raw_source_vehicle is not None else None
                )
                vehicles.add(str(vehicle_id))
                record = point_records.get(resolved_id)
                if record is None:
                    record = {
                        "coordinates": [lon, lat],
                        "properties": {
                            "detect_id": fit_detect_id,
                            "fit_id": fit_id,
                            "refine_id": detect.refine.refine_id,
                            "resolved_point_id": resolved_id,
                            "sequence_in_subtrack": int(point_row[1]),
                            "subtrack_index": int(point_row[2]),
                            "vehicle_id": str(vehicle_id),
                            "source_vehicle_id": source_vehicle_id,
                            "point_kind": point_kind,
                            "source_row_key": point_row[4],
                            "injected_point_id": int(point_row[5]) if point_row[5] is not None else None,
                            "crossing_id": int(point_row[6]) if point_row[6] is not None else None,
                            "intersection_id": int(point_row[7]) if point_row[7] is not None else None,
                            "crossing_type": int(point_row[8]) if point_row[8] is not None else None,
                            "timestamp": _finite_number(point_row[11], "timestamp", nullable=True),
                            "heading": _finite_number(point_row[12], "heading", nullable=True),
                            "speed": _finite_number(point_row[13], "speed", nullable=True),
                            "value_semantics": "persisted_clip_members_with_current_source_values",
                            "centerline_ids": [],
                            "clip_sources": [],
                        },
                        "clip_memberships": set(),
                    }
                    point_records[resolved_id] = record
                    if len(point_records) > int(request.max_points):
                        raise MapDataError(
                            f"centerline replay exceeds max_points={request.max_points}; refusing partial result"
                        )
                elif record["coordinates"] != [lon, lat]:
                    raise MapDataError(
                        f"resolved point {resolved_id} has inconsistent current source coordinates"
                    )
                elif record["properties"]["source_vehicle_id"] != source_vehicle_id:
                    raise MapDataError(
                        f"resolved point {resolved_id} has inconsistent current source vehicle ID"
                    )
                membership = (centerline_id, int(ordinal))
                if membership in record["clip_memberships"]:
                    raise MapDataError(
                        f"Centerline {centerline_id} Clip {ordinal} repeats resolved point {resolved_id}"
                    )
                record["clip_memberships"].add(membership)
                record["properties"]["clip_sources"].append(source)
                if centerline_id not in record["properties"]["centerline_ids"]:
                    record["properties"]["centerline_ids"].append(centerline_id)

            if reverse:
                coordinates.reverse()
                clip_resolved_ids.reverse()
            clip_features.append(
                create_map_feature(
                    f"centerline:{centerline_id}:clip:{int(ordinal)}",
                    {"type": "LineString", "coordinates": coordinates},
                    fit_id=fit_id,
                    detect_id=fit_detect_id,
                    refine_id=detect.refine.refine_id,
                    centerline_id=centerline_id,
                    centerline_ids=[centerline_id],
                    variant=str(centerline[2]),
                    from_intersection_id=from_intersection_id,
                    to_intersection_id=to_intersection_id,
                    point_kind="source_clip",
                    input_ordinal=int(ordinal),
                    reverse_to_centerline_direction=reverse,
                    value_semantics="persisted_clip_members_with_current_source_values",
                    start_crossing_id=start_crossing_id,
                    end_crossing_id=end_crossing_id,
                    start_intersection_id=start_intersection_id,
                    end_intersection_id=end_intersection_id,
                    oriented_start_resolved_point_id=clip_resolved_ids[0],
                    oriented_end_resolved_point_id=clip_resolved_ids[-1],
                    resolved_point_ids=clip_resolved_ids,
                    persisted_start_resolved_point_id=int(start[4]),
                    persisted_end_resolved_point_id=int(end[4]),
                    vehicle_id=str(point_rows[0][14]),
                    clip_sources=[clip_source],
                )
            )

    if len(vehicles) > int(request.max_vehicles):
        raise MapDataError(
            f"centerline replay selects {len(vehicles)} vehicles; max_vehicles={request.max_vehicles}"
        )

    point_features: list[dict[str, Any]] = []
    for resolved_id, record in point_records.items():
        properties = record["properties"]
        properties["clip_sources"].sort(
            key=lambda item: (item["centerline_id"], item["input_ordinal"])
        )
        properties["centerline_ids"].sort()
        point_features.append(
            create_map_feature(
                f"resolved:{fit_detect_id}:{resolved_id}",
                {"type": "Point", "coordinates": record["coordinates"]},
                **properties,
            )
        )

    features = point_features + clip_features
    return create_map_feature_collection(
        features,
        operation="replay",
        selection="centerline_ids",
        centerline_ids=selected_ids,
        fit_id=fit_id,
        detect_id=fit_detect_id,
        refine_id=detect.refine.refine_id,
        selected_centerline_count=len(selected_ids),
        returned_point_count=len(point_features),
        returned_clip_count=len(clip_features),
        membership_reference_count=membership_reference_count,
        membership_reference_limit=membership_reference_limit,
        vehicle_count=len(vehicles),
        vehicle_identity_semantics="persisted_refine_vehicle_id",
        source_fingerprint_verified=False,
        value_semantics="persisted_clip_members_with_current_source_values",
        truncated=False,
    )
