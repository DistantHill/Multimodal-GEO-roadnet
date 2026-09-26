"""Shared bounded PostgreSQL INSERT and COPY helpers."""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from ..telemetry.heartbeat import heartbeat_progress, operation_heartbeat

_BATCH = 5_000


def _copy_insert(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    observe: bool = True,
) -> None:
    # A streaming caller owns cumulative progress across its short COPYs.
    # Standalone COPYs retain their own heartbeat and local row count.
    with operation_heartbeat(f"写入 {table}") if observe else nullcontext():
        if not rows:
            return
        with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
            for index, row in enumerate(rows, 1):
                copy.write_row(row)
                if observe and index % 10_000 == 0:
                    heartbeat_progress(index, total=len(rows))
            if observe:
                heartbeat_progress(len(rows), total=len(rows))


def _chunked_insert(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    *,
    batch_size: int,
) -> None:
    with operation_heartbeat(f"写入 {table}"):
        if not rows:
            return
        size = min(batch_size, max(1, 65_535 // len(columns)))
        for offset in range(0, len(rows), size):
            heartbeat_progress(offset, total=len(rows))
            batch = rows[offset:offset + size]
            placeholders = ", ".join(
                f"({', '.join(['%s'] * len(columns))})" for _ in batch
            )
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES {placeholders}",
                [value for row in batch for value in row],
            )


def _chunked_insert_returning(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    returning: list[str],
    *,
    batch_size: int,
) -> list[tuple[Any, ...]]:
    with operation_heartbeat(f"写入 {table}"):
        if not rows:
            return []
        size = min(batch_size, max(1, 65_535 // len(columns)))
        returned: list[tuple[Any, ...]] = []
        for offset in range(0, len(rows), size):
            heartbeat_progress(offset, total=len(rows))
            batch = rows[offset:offset + size]
            placeholders = ", ".join(
                f"({', '.join(['%s'] * len(columns))})" for _ in batch
            )
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES {placeholders} "
                f"RETURNING {', '.join(returning)}",
                [value for row in batch for value in row],
            )
            returned.extend(cur.fetchall())
        return returned


def _chunked_template(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    value_template: str,
    *,
    batch_size: int,
) -> None:
    with operation_heartbeat(f"写入 {table}"):
        if not rows:
            return
        params_per_row = len(rows[0])
        size = min(batch_size, max(1, 65_535 // params_per_row))
        for offset in range(0, len(rows), size):
            heartbeat_progress(offset, total=len(rows))
            batch = rows[offset:offset + size]
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                + ", ".join(f"({value_template})" for _ in batch),
                [value for row in batch for value in row],
            )


def _chunked_template_returning(
    cur: Any,
    table: str,
    columns: list[str],
    rows: list[tuple[Any, ...]],
    value_template: str,
    returning: list[str],
) -> list[tuple[Any, ...]]:
    with operation_heartbeat(f"写入 {table}"):
        if not rows:
            return []
        returned: list[tuple[Any, ...]] = []
        params_per_row = len(rows[0])
        size = max(1, 65_535 // params_per_row)
        for offset in range(0, len(rows), size):
            heartbeat_progress(offset, total=len(rows))
            batch = rows[offset:offset + size]
            cur.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                + ", ".join(f"({value_template})" for _ in batch)
                + f" RETURNING {', '.join(returning)}",
                [value for row in batch for value in row],
            )
            returned.extend(cur.fetchall())
        return returned
