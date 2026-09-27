"""CLI-only discovery and display of completed Refine runs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import PostgresConfig, connect
from ..models import SpatialContext
from ..projection import spatial_context_from_values


@dataclass(frozen=True, slots=True)
class RefineSelection:
    id: str
    source_table: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    parameters: dict[str, Any]
    spatial_context: SpatialContext


_SELECTION_SQL = """
SELECT r.id, r.source_table, r.status, r.started_at, r.finished_at,
       r.parameters, r.source_srid, r.processing_srid, r.projection_config
FROM refine_runs AS r
"""


def latest_completed_refine(
    postgres_config: PostgresConfig, source_table: str,
) -> RefineSelection | None:
    """Return the latest completed record for an exact source_table string."""
    with connect(postgres_config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                _SELECTION_SQL
                + " WHERE r.source_table = %s AND r.status = 'completed' "
                + "ORDER BY r.started_at DESC, r.id DESC LIMIT 1",
                (source_table,),
            )
            row = cur.fetchone()
    return None if row is None else _selection_from_row(row)


def get_refine_selection(
    postgres_config: PostgresConfig, refine_id: str,
) -> RefineSelection:
    """Read history displayed for an explicit Refine UUID."""
    with connect(postgres_config) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECTION_SQL + " WHERE r.id = %s", (refine_id,))
            row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown Refine Run {refine_id}")
    return _selection_from_row(row)


def _selection_from_row(row: tuple[Any, ...]) -> RefineSelection:
    refine_id = str(row[0])
    try:
        parameters = _json_object(row[5], "Refine Run parameters")
        projection_config = _json_object(row[8], "projection_config")
        return RefineSelection(
            id=refine_id,
            source_table=str(row[1]),
            status=str(row[2]),
            started_at=row[3],
            finished_at=row[4],
            parameters=parameters,
            spatial_context=spatial_context_from_values(row[6], row[7], projection_config),
        )
    except Exception as exc:
        raise ValueError(f"Refine Run {refine_id}: invalid stored history: {exc}") from exc


def format_refine_history(selection: RefineSelection) -> str:
    """Format the inherited history without presenting the full snapshot as active."""
    parser, parser_missing = _history_config(selection.parameters, "parser")
    refine, refine_missing = _history_config(selection.parameters, "refine")
    context = selection.spatial_context
    lines = [
        f"  Refine UUID: {selection.id}",
        f"  来源表 / Source table: {selection.source_table}",
        f"  开始时间 / Started: {_format_time(selection.started_at)}",
        f"  完成时间 / Finished: {_format_time(selection.finished_at)}",
        f"  状态 / Status: {selection.status}",
        "  复用数据范围 / Reused data scope: 历史 kept_trajectory_points、injected_points 与对应的 Refine 产物；不重新执行 Parser 或 Refine。",
        "  历史生成参数 / Historical generation parameters:",
        f"    Parser: {'历史记录未保存' if parser_missing else _json_text(parser)}",
        f"    Refine: {'历史记录未保存' if refine_missing else _json_text(refine)}",
        "  继承的历史投影 / Inherited historical projection:",
        f"    source_srid: {context.source_srid}",
        f"    processing_srid: {context.processing_srid}",
        f"    projection_config: {_json_text(context.projection_config)}",
        "  当前配置 / Current configuration: Parser 与 Refine 跳过；后续 Detect、Clip、Fitting、Network 使用当前启动配置。",
    ]
    return "\n".join(lines)


def _history_config(parameters: dict[str, Any], section: str) -> tuple[Any, bool]:
    semantic = parameters.get("semantic_config")
    if isinstance(semantic, dict) and isinstance(semantic.get(section), dict):
        return semantic[section], False
    return None, True


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise ValueError(f"{label} must be a JSON object")


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _format_time(value: datetime | None) -> str:
    return "历史记录未保存" if value is None else value.isoformat(sep=" ", timespec="seconds")
