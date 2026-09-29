"""Build grid segments as columns, spilling oversized grids into DuckDB TEMP storage."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import duckdb
import numpy as np

from .temp_quota import TempQuota


class GridSegmentBudgetError(RuntimeError):
    """A grid's in-memory segment fast path exceeded its budget."""


_INT = {"subtrack_index", "start_refined_sequence_in_subtrack", "end_refined_sequence_in_subtrack", "start_point_id", "end_point_id", "start_source_row_key"}
_TEXT = {"vehicle_id", "start_point_kind", "end_point_kind", "start_source_id", "start_generated_reason"}
_FLOAT = {"timestamp", "lon", "lat", "heading", "speed", "x_start", "y_start", "x_end", "y_end", "end_heading"}
_COLUMNS = tuple(sorted(_INT | _TEXT | _FLOAT))
_KEY = ("subtrack_index", "start_point_kind", "start_point_id", "end_point_kind", "end_point_id")


def _empty():
    result = {name: np.empty(0, dtype=np.int64 if name in _INT else object if name in _TEXT else np.float64) for name in _COLUMNS}
    result["segment_identity"] = np.empty((0, 5), dtype=np.int64)
    return result


def _pieces(batch):
    size = len(batch["point_id"])
    if any(len(value) != size for value in batch.values()):
        raise ValueError("point columns differ in length")
    pieces = []
    for start_prefix, end_prefix in (("prev_", ""), ("", "next_")):
        a = np.asarray(batch[start_prefix + "point_id"], dtype=object)
        b = np.asarray(batch[end_prefix + "point_id"], dtype=object)
        ids = np.flatnonzero((a != None) & (b != None))
        if not len(ids):
            continue
        def column(prefix, name, dtype):
            return np.asarray(batch[prefix + name])[ids].astype(dtype, copy=False)
        seq = column(start_prefix, "refined_sequence_in_subtrack", np.int64)
        end_seq = column(end_prefix, "refined_sequence_in_subtrack", np.int64)
        if np.any(end_seq != seq + 1):
            raise ValueError("grid neighbours are not adjacent Refine points")
        start_kind = column(start_prefix, "point_kind", np.int8)
        end_kind = column(end_prefix, "point_kind", np.int8)
        subtrack = np.asarray(batch["subtrack_index"], dtype=np.int64)[ids]
        values = {
            "vehicle_id": np.asarray(batch["vehicle_id"], dtype=object)[ids],
            "subtrack_index": subtrack,
            "start_refined_sequence_in_subtrack": seq,
            "end_refined_sequence_in_subtrack": end_seq,
            "start_point_id": a[ids].astype(np.int64),
            "end_point_id": b[ids].astype(np.int64),
            "start_point_kind": np.where(start_kind == 0, "kept", "injected"),
            "end_point_kind": np.where(end_kind == 0, "kept", "injected"),
            "start_source_id": column(start_prefix, "source_id", object),
            "start_source_row_key": np.asarray([0 if value is None else int(value) for value in np.asarray(batch[start_prefix + "source_row_key"], dtype=object)[ids]], dtype=np.int64),
            "start_generated_reason": np.asarray([value or "" for value in np.asarray(batch[start_prefix + "generated_reason"], dtype=object)[ids]], dtype=object),
        }
        for name in ("timestamp", "lon", "lat", "heading", "speed"):
            values[name] = column(start_prefix, name, np.float64)
        for prefix, label in ((start_prefix, "start"), (end_prefix, "end")):
            values["x_" + label] = column(prefix, "x", np.float64)
            values["y_" + label] = column(prefix, "y", np.float64)
        values["end_heading"] = column(end_prefix, "heading", np.float64)
        values["segment_identity"] = np.column_stack((subtrack, start_kind, a[ids].astype(np.int64), end_kind, b[ids].astype(np.int64)))
        pieces.append(values)
    return pieces


def _finish_memory(result):
    if not result:
        result = _empty()
    if len(result["subtrack_index"]):
        order = np.lexsort((result["start_point_id"], result["start_refined_sequence_in_subtrack"], result["subtrack_index"]))
        result = {name: value[order] for name, value in result.items()}
    for values in result.values():
        values.flags.writeable = False
    return result


def build_grid_segments(point_batches, *, max_segments: int, max_bytes: int):
    """Return the existing small-grid array representation and enforce its budget."""
    if max_segments < 0 or max_bytes < 0:
        raise ValueError("segment budgets must be non-negative")
    result = {}
    iterator = iter(point_batches)
    try:
        for batch in iterator:
            pieces = _pieces(batch)
            if not pieces:
                continue
            combined = {name: np.concatenate(([result[name]] if result else []) + [piece[name] for piece in pieces]) for name in pieces[0]}
            _, unique = np.unique(combined["segment_identity"], axis=0, return_index=True)
            if len(unique) > max_segments:
                raise GridSegmentBudgetError(f"grid exceeds max_segments={max_segments}")
            cost = 3 * sum(value.nbytes for value in combined.values()) + 128 * len(unique)
            if cost > max_bytes:
                raise GridSegmentBudgetError(f"grid exceeds max_bytes={max_bytes}")
            result = {name: value[unique] for name, value in combined.items()}
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
    return _finish_memory(result)


class GridSegmentRelation:
    """Single-worker DuckDB TEMP owner; its SQL never exposes a complete Python grid."""

    def __init__(self, runtime, data_root: Path, *, owner: str = "grid-segments"):
        parent = Path(data_root) / "duckdb"
        parent.mkdir(parents=True, exist_ok=True)
        self._directory = TemporaryDirectory(prefix="grid-segments-", dir=parent)
        self._connection = None
        try:
            self._connection = duckdb.connect(":memory:", config={
                "memory_limit": getattr(runtime, "duckdb_grid_memory_limit", runtime.duckdb_memory_limit),
                "threads": min(runtime.duckdb_threads, 4),
                "temp_directory": self._directory.name,
                "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
            })
            self._quota = TempQuota(self._connection, runtime.duckdb_max_temp_directory_size, owner)
            definitions = [f'"{name}" ' + ("BIGINT" if name in _INT else "VARCHAR" if name in _TEXT else "DOUBLE") for name in _COLUMNS]
            self._connection.execute("CREATE TEMP TABLE segment_input (input_order BIGINT, " + ", ".join(definitions) + ")")
        except BaseException:
            if self._connection is not None:
                self._connection.close()
            self._directory.cleanup()
            raise
        self._next_order = 0
        self.count = 0

    def append(self, piece):
        size = len(piece["subtrack_index"])
        if not size:
            return
        columns = {name: piece[name] for name in _COLUMNS}
        columns["input_order"] = np.arange(self._next_order, self._next_order + size, dtype=np.int64)
        relation = "segment_batch_" + uuid4().hex
        self._connection.register(relation, columns)
        try:
            def attempt():
                self._connection.execute("BEGIN")
                try:
                    self._connection.execute(f"INSERT INTO segment_input BY NAME SELECT * FROM {relation}")
                    self._connection.execute("COMMIT")
                except BaseException:
                    try:
                        self._connection.execute("ROLLBACK")
                    except duckdb.TransactionException as rollback_error:
                        if "no transaction is active" not in str(rollback_error):
                            raise
                    raise
            self._quota.run("append_segments", attempt)
        finally:
            self._connection.unregister(relation)
        self._next_order += size

    def finish(self):
        key = ", ".join(_KEY)
        # Memory fast path's np.unique first orders the five-column identity;
        # its stable lexsort then orders by subtrack, sequence and start ID.
        order = ("subtrack_index, start_refined_sequence_in_subtrack, start_point_id, "
                 "CASE start_point_kind WHEN 'kept' THEN 0 ELSE 1 END, "
                 "CASE end_point_kind WHEN 'kept' THEN 0 ELSE 1 END, end_point_id")
        self._quota.run("finish_segments", lambda: self._connection.execute("CREATE TEMP TABLE segment_unique AS SELECT row_number() OVER (ORDER BY " + order + ") - 1 AS segment_order, * EXCLUDE(input_order, rn) FROM (SELECT *, row_number() OVER (PARTITION BY " + key + " ORDER BY input_order) AS rn FROM segment_input) WHERE rn = 1"))
        self.count = self._connection.execute("SELECT count(*) FROM segment_unique").fetchone()[0]

    def block(self, offset: int, size: int):
        def read():
            cursor = self._connection.execute("SELECT " + ", ".join(_COLUMNS) + " FROM segment_unique WHERE segment_order >= ? AND segment_order < ? ORDER BY segment_order", [offset, offset + size])
            return cursor.fetchmany(size)
        rows = self._quota.run("read_segment_block", read)
        result = {name: np.asarray([row[i] for row in rows], dtype=np.int64 if name in _INT else object if name in _TEXT else np.float64) for i, name in enumerate(_COLUMNS)}
        result["segment_identity"] = np.column_stack((result["subtrack_index"], np.where(result["start_point_kind"] == "kept", 0, 1), result["start_point_id"], np.where(result["end_point_kind"] == "kept", 0, 1), result["end_point_id"])) if rows else np.empty((0, 5), dtype=np.int64)
        for values in result.values():
            values.flags.writeable = False
        return result

    def close(self):
        self._connection.close()
        self._directory.cleanup()


@contextmanager
def open_grid_segments(point_batches, *, max_segments: int, max_bytes: int, runtime, data_root, owner="grid-segments"):
    """Switch to native TEMP storage before allocating an oversized concatenation."""
    if max_segments < 0 or max_bytes < 0:
        raise ValueError("segment budgets must be non-negative")
    result = {}
    relation = None
    iterator = iter(point_batches)
    try:
        for batch in iterator:
            pieces = _pieces(batch)
            if not pieces:
                continue
            if relation is None:
                projected = len(result.get("subtrack_index", ())) + sum(len(piece["subtrack_index"]) for piece in pieces)
                cost = 3 * (sum(value.nbytes for value in result.values()) + sum(value.nbytes for piece in pieces for value in piece.values())) + 128 * projected
                if projected > max_segments or cost > max_bytes:
                    relation = GridSegmentRelation(runtime, Path(data_root), owner=owner)
                    if result:
                        relation.append(result)
                    result = {}
            if relation is not None:
                for piece in pieces:
                    relation.append(piece)
                continue
            combined = {name: np.concatenate(([result[name]] if result else []) + [piece[name] for piece in pieces]) for name in pieces[0]}
            _, unique = np.unique(combined["segment_identity"], axis=0, return_index=True)
            result = {name: value[unique] for name, value in combined.items()}
        if relation is not None:
            relation.finish()
            delivered = relation
        else:
            delivered = _finish_memory(result)
        # The contextmanager frame stays alive while the worker consumes a grid.
        # Drop the final cursor batch, pieces, concat/unique workspaces and the
        # unsorted result before handing the completed columns to that worker.
        batch = pieces = combined = unique = _ = result = None
        yield delivered
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()
        if relation is not None:
            relation.close()
