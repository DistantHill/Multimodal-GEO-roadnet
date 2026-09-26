"""Shared SQL fragments for reading Refine point rows."""

from __future__ import annotations

_INJECTED_SOURCE_ID = "public.injected_points"

_IP_UNIFIED_COLS = (
    "vehicle_id, timestamp, lon, lat, heading, speed, "
    "%s::text AS source_id, "
    "id::text AS source_row_key, "
    "'{}'::jsonb AS extra, "
    "generated_reason, x, y, 1 AS point_kind, id AS row_id, "
    "subtrack_index, after_sequence_in_subtrack AS anchor_sequence"
)


def _tp_unified_cols(processing_srid: int, *, alias: str = "") -> str:
    """Return raw-point columns plus fixed processing-CRS metre coordinates."""
    prefix = f"{alias}." if alias else ""
    projected = f"ST_Transform({prefix}geom, {int(processing_srid)})"
    return (
        f"{prefix}vehicle_id, {prefix}timestamp, {prefix}lon, {prefix}lat, "
        f"{prefix}heading, {prefix}speed, %s::text AS source_id, "
        f"{prefix}id::text AS source_row_key, {prefix}extra, "
        f"''::text AS generated_reason, ST_X({projected}) AS x, "
        f"ST_Y({projected}) AS y, 0 AS point_kind, {prefix}id AS row_id, "
        "1 AS subtrack_index, 0 AS anchor_sequence"
    )
