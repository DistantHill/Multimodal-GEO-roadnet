"""Write Refine run metadata and result rows."""
from __future__ import annotations

import json
import uuid
import numpy as np
from dataclasses import dataclass
from typing import Any

from ..models import (
    ExcludedBatch, GeneratedReason, RefinementSubtrack,
    SpatialContext,
)
from ..projection import CoordinateTransformer, spatial_context_from_values
from .insert_batches import _BATCH, _chunked_insert, _copy_insert

@dataclass(frozen=True, slots=True)
class RefineRunRecord:
    id: str
    source_table: str
    parameters: dict[str, Any]
    status: str
    spatial_context: SpatialContext
    source_fingerprint: dict[str, object] | None = None


def create_refine_run(
    conn: Any,
    *,
    source_table: str,
    parameters: dict[str, Any],
    spatial_context: SpatialContext,
) -> str:
    CoordinateTransformer(spatial_context)
    refine_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO refine_runs "
            "(id, source_table, source_srid, processing_srid, "
            "projection_config, parameters, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'running')",
            (
                refine_id,
                source_table,
                spatial_context.source_srid,
                spatial_context.processing_srid,
                json.dumps(spatial_context.projection_config, ensure_ascii=False),
                json.dumps(parameters, ensure_ascii=False),
            ),
        )
    return refine_id


def finish_refine_run(
    conn: Any,
    refine_id: str,
    *,
    status: str,
    source_fingerprint: dict[str, object] | None = None,
) -> None:
    if status == "completed" and source_fingerprint is None:
        raise ValueError("a completed Refine Run requires source_fingerprint")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE refine_runs SET status = %s, source_fingerprint = %s, "
            "finished_at = NOW() WHERE id = %s AND status = 'running'",
            (
                status,
                (
                    json.dumps(source_fingerprint, ensure_ascii=False)
                    if source_fingerprint is not None
                    else None
                ),
                refine_id,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError(f"Refine Run {refine_id} is not running")


def fetch_refine_run(conn: Any, refine_id: str) -> RefineRunRecord:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_table, parameters, status, source_srid, "
            "processing_srid, projection_config, source_fingerprint "
            "FROM refine_runs WHERE id = %s",
            (refine_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Refine Run: {refine_id}")
    parameters = _json_object(row[2], "Refine Run parameters")
    fingerprint = (
        None if row[7] is None else _json_object(row[7], "source fingerprint")
    )
    return RefineRunRecord(
        id=str(row[0]),
        source_table=str(row[1]),
        parameters=parameters,
        status=str(row[3]),
        spatial_context=spatial_context_from_values(row[4], row[5], row[6]),
        source_fingerprint=fingerprint,
    )


def insert_excluded_points(conn: Any, excluded: ExcludedBatch, *, refine_id: str,
                           batch_size: int = _BATCH) -> int:
    rows = [(refine_id, int(excluded.source_row_key[i]), str(excluded.reason[i]))
            for i in range(len(excluded))]
    with conn.cursor() as cur:
        _chunked_insert(cur, "excluded_points", ["refine_id", "source_row_key", "reason"],
                        rows, batch_size=batch_size)
    return len(rows)


def insert_kept_trajectory_points(conn: Any, subtracks: list[RefinementSubtrack], *,
                                 refine_id: str) -> int:
    rows = []
    for track in subtracks:
        kept = np.flatnonzero(track.kind == "p")
        rows.extend((refine_id, int(track.source_row_key[i]), track.vehicle_id,
                     track.subtrack_index, sequence, float(track.xy[i,0]), float(track.xy[i,1]))
                    for sequence, i in enumerate(kept))
    with conn.cursor() as cur:
        _copy_insert(cur, "kept_trajectory_points", ["refine_id", "source_row_key", "vehicle_id",
                     "subtrack_index", "sequence_in_subtrack", "x", "y"], rows)
    return len(rows)


def insert_injected_points(conn: Any, subtracks: list[RefinementSubtrack], *, refine_id: str,
                           spatial_context: SpatialContext, batch_size: int = _BATCH) -> int:
    rows = []
    for track in subtracks:
        for i in np.flatnonzero(track.kind == "i"):
            if track.after_sequence[i] < 0:
                raise ValueError("an injected point requires a preceding kept point")
            rows.append((refine_id, track.vehicle_id, track.subtrack_index,
                         int(track.after_sequence[i]), float(track.timestamp[i]),
                         float(track.lonlat[i, 0]), float(track.lonlat[i, 1]),
                         float(track.xy[i, 0]), float(track.xy[i, 1]),
                         float(track.heading[i]), float(track.speed[i]), str(track.generated_reason[i])))
    with conn.cursor() as cur:
        _chunked_insert(cur, "injected_points", ["refine_id", "vehicle_id", "subtrack_index",
                        "after_sequence_in_subtrack", "timestamp", "lon", "lat", "x", "y",
                        "heading", "speed", "generated_reason"], rows, batch_size=batch_size)
    return len(rows)


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError(f"{label} must be a JSON object")

