"""Read-only catalog browsing with exact counts for small or unmeasured tables."""

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from .database import get_read_connection, open_read_connection

router = APIRouter(prefix="/api/v1/map/databases", tags=["map"])


def table_counts(target: Any, rows: list[Any]) -> list[dict[str, Any]]:
    result = []
    for schema, name, estimate, kind in rows:
        entry = {
            "schema_name": schema, "table_name": name, "kind": kind,
            "estimated_rows": estimate if estimate >= 0 else None,
            "row_count": estimate if estimate >= 0 else None,
            "row_count_is_estimate": True,
        }
        if estimate < 50_000:  # Includes -1: no statistics, not necessarily empty.
            try:
                # A savepoint isolates timeout/permission errors from other tables.
                with target.transaction():
                    count = target.execute(psycopg.sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                        psycopg.sql.Identifier(schema), psycopg.sql.Identifier(name),
                    )).fetchone()[0]
                entry.update(row_count=count, row_count_is_estimate=False)
            except psycopg.Error:
                entry["count_error"] = "精确计数失败或超时，保留估算值。"
        result.append(entry)
    return result


@router.get("")
def databases(connection: Any = Depends(get_read_connection)) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY datname"
    ).fetchall()
    return {"databases": [row[0] for row in rows],
            "default_database": connection.execute("SELECT current_database()").fetchone()[0]}


@router.get("/tables")
def tables(
    database_name: str = Query(min_length=1, max_length=63),
    connection: Any = Depends(get_read_connection),
) -> dict[str, Any]:
    if connection.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (database_name,)
    ).fetchone() is None:
        raise HTTPException(404, detail={"error": "database_not_found", "message": "数据库不存在，请刷新数据库列表。"})
    try:
        with open_read_connection(database_name) as target:
            rows = target.execute(
                "SELECT n.nspname, c.relname, c.reltuples::bigint, c.relkind "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'p', 'm', 'f') "
                "AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
                "ORDER BY n.nspname, c.relname"
            ).fetchall()
            entries = table_counts(target, rows)
    except psycopg.Error as exc:
        raise HTTPException(503, detail={
            "error": "database_catalog_failed",
            "message": "无法读取所选数据库，请确认 Backend 用户的数据库连接和目录读取权限。",
        }) from exc
    return {
        "database_name": database_name,
        "row_count_is_estimate": any(entry["row_count_is_estimate"] for entry in entries),
        "tables": entries,
    }
