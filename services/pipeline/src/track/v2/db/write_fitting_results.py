"""Write Fitting run metadata, centerlines, and source-clip links."""
from __future__ import annotations

import json
import uuid
from typing import Any

from ..models import CenterlineFitBundle, ClipConfig, ClipResult, FittingConfig
from ..telemetry.heartbeat import observed_operation
from .insert_batches import _copy_insert, _chunked_template_returning


def create_fit_run(
    conn: Any,
    *,
    detect_id: str,
    clip_config: ClipConfig,
    fitting_config: FittingConfig,
    parameters: dict[str, Any],
) -> str:
    fit_id = str(uuid.uuid4())
    parameters = {
        **parameters,
        "clip": clip_config.as_dict(),
        "fitting": fitting_config.as_dict(),
    }
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fit_runs "
            "(id, detect_id, clip_algorithm, clip_version, fitting_algorithm, "
            "fitting_version, parameters, status) "
            "SELECT %s, id, 'resolved_active_set', 'v3', "
            "'normalized_arc_coordinate_median', 'v3', %s, 'running' "
            "FROM detect_runs WHERE id = %s AND status = 'completed' RETURNING id",
            (fit_id, json.dumps(parameters, ensure_ascii=False), detect_id),
        )
        if cur.fetchone() is None:
            raise ValueError(f"Detect Run {detect_id} is not completed")
    return fit_id


def finish_fit_run(
    conn: Any,
    fit_id: str,
    *,
    status: str,
    report: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fit_runs SET status = %s, report = %s, finished_at = NOW() "
            "WHERE id = %s AND status = 'running'",
            (
                status,
                json.dumps(report, ensure_ascii=False) if report is not None else None,
                fit_id,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Fit Run {fit_id} is not running")


@observed_operation("写入中心线与来源片段")
def insert_centerline_bundle(
    conn: Any,
    bundle: CenterlineFitBundle,
    clips: ClipResult,
    *,
    fit_id: str,
    detect_id: str,
) -> tuple[int, ...]:
    if clips.detect_id is not None and clips.detect_id != detect_id:
        raise ValueError("ClipResult belongs to a different Detect Run")
    centerlines = [
        centerline
        for fit_result in (bundle.combined, bundle.a_to_b, bundle.b_to_a)
        for centerline in fit_result.centerlines
    ]
    rows = [(
        fit_id,
        detect_id,
        centerline.variant,
        centerline.from_intersection_id,
        centerline.to_intersection_id,
        f"LINESTRING({', '.join(f'{lon} {lat}' for lon, lat in centerline.points)})",
        centerline.length_m,
        centerline.forward_clip_count,
        centerline.reverse_clip_count,
    ) for centerline in centerlines]
    with conn.cursor() as cur:
        returned = _chunked_template_returning(
            cur,
            "centerlines",
            [
                "fit_id", "detect_id", "variant", "from_intersection_id",
                "to_intersection_id", "geom", "length_m",
                "forward_clip_count", "reverse_clip_count",
            ],
            rows,
            ", ".join(["%s"] * 5 + ["ST_GeomFromText(%s, 4326)"] + ["%s"] * 3),
            ["id", "variant", "from_intersection_id", "to_intersection_id"],
        )
    id_by_key = {
        (str(variant), int(from_id), int(to_id)): int(centerline_id)
        for centerline_id, variant, from_id, to_id in returned
    }
    expected_keys = {
        (
            centerline.variant,
            centerline.from_intersection_id,
            centerline.to_intersection_id,
        )
        for centerline in centerlines
    }
    if id_by_key.keys() != expected_keys:
        missing = sorted(expected_keys - id_by_key.keys())
        unexpected = sorted(id_by_key.keys() - expected_keys)
        raise RuntimeError(
            "centerline insert returned an incomplete id mapping: "
            f"missing={missing}, unexpected={unexpected}"
        )
    ordered_ids = tuple(
        id_by_key[(
            centerline.variant,
            centerline.from_intersection_id,
            centerline.to_intersection_id,
        )]
        for centerline in centerlines
    )
    get_clip = getattr(clips.clips, "get_by_clip_id", None)
    if not callable(get_clip):
        clips_by_id = {clip.clip_id: clip for clip in clips.clips}
        get_clip = clips_by_id.__getitem__
    manifest_rows: list[tuple[Any, ...]] = []
    for centerline_id, centerline in zip(ordered_ids, centerlines, strict=True):
        for ordinal, clip_id in enumerate(centerline.source_clip_ids):
            clip = get_clip(clip_id)
            if clip.clip_type != "between_intersection":
                raise ValueError("only between_intersection clips may back a centerline")
            if clip.start.crossing_id is None or clip.end.crossing_id is None:
                raise ValueError("centerline source clips require crossing IDs")
            manifest_rows.append((
                centerline_id,
                detect_id,
                ordinal,
                clip.start.crossing_id,
                clip.end.crossing_id,
                clip.start.intersection_id != centerline.from_intersection_id,
            ))
    with conn.cursor() as cur:
        _copy_insert(
            cur,
            "centerline_source_clips",
            [
                "centerline_id", "detect_id", "input_ordinal",
                "start_crossing_id", "end_crossing_id",
                "reverse_to_centerline_direction",
            ],
            manifest_rows,
        )
    return ordered_ids
