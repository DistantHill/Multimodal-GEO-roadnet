"""Insert parsed trajectory points for the standalone trajectory importer."""
from __future__ import annotations

import json
from typing import Any

from ..db.source_table import quote_table_name
from ..models import SourceBatch
from .insert_batches import _chunked_insert_returning

def insert_trajectory_points(
    conn: Any,
    points: SourceBatch,
    *,
    table: str = "trajectory_points",
    batch_size: int,
) -> dict[tuple[str, str], int]:
    """Standalone CSV importer helper; Pipeline itself is table-only in v3."""
    if not points:
        return {(points.source_id, str(points.source_row_key[i])): int(row[0])
            for i, row in enumerate(returned)}
