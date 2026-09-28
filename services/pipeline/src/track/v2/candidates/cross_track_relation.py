"""Spillable cross business batches; no per-event row decoding."""
from __future__ import annotations
from collections.abc import Iterable, Iterator
from dataclasses import fields
from pathlib import Path
from uuid import uuid4
import shutil
import json
import duckdb
import numpy as np
from .temp_quota import TempQuota
from ..models import RuntimeConfig, CrossCandidateBatch
from .detect_cross_track_pairs import empty_cross_batch


class CrossCandidateRelation:
    def __init__(self, runtime: RuntimeConfig, data_root: str | Path, *, batch_size: int | None = None, relation_id: str | None = None):
        self._batch_size = runtime.relation_batch_size if batch_size is None else batch_size
        if isinstance(self._batch_size, bool) or self._batch_size < 1:
            raise ValueError("batch_size must be positive")
        root = Path(data_root)
        if not root.is_absolute() or root == Path(root.anchor):
            raise ValueError("data_root must be absolute and non-root")
        identity = relation_id or uuid4().hex
        if not identity or identity in {".", ".."} or Path(identity).name != identity or "\x00" in identity:
            raise ValueError("relation_id must be one path component")
        self._temp_directory = root / "duckdb" / "cross-event-relations" / identity
        self._temp_directory.mkdir(parents=True, exist_ok=False)
        self._connection = None
        self._count = 0
        self._sealed = False
        self._finished_grids: set[int] = set()
        try:
            self._connection = duckdb.connect(":memory:", config={
                "autoload_known_extensions":"false", "autoinstall_known_extensions":"false",
                "memory_limit":runtime.duckdb_memory_limit, "threads":str(runtime.duckdb_threads),
                "temp_directory":str(self._temp_directory), "max_temp_directory_size":runtime.duckdb_max_temp_directory_size,
            })
            self._quota = TempQuota(self._connection, runtime.duckdb_max_temp_directory_size, identity)
            self._create_schema()
        except BaseException:
            self.close(); raise

    def _create_schema(self):
        definitions = ["ordinal BIGINT"]
        template = empty_cross_batch()
        for item in fields(CrossCandidateBatch):
            name = item.name
            values = getattr(template, name)
            if name in {"xy", "lonlat"}:
                definitions.extend((name + "_0 DOUBLE", name + "_1 DOUBLE"))
            else:
                sql_type = {"b": "BOOLEAN", "i": "BIGINT", "f": "DOUBLE"}.get(values.dtype.kind, "VARCHAR")
                definitions.append(name + " " + sql_type)
        self._connection.execute("CREATE TABLE events (" + ", ".join(definitions) + ")")
        self._connection.execute("CREATE TEMP TABLE staged AS SELECT *, 0::BIGINT AS grid_order, "
                                 "0::BIGINT AS local_order FROM events WHERE false")

    def __len__(self):
        self._require_open()
        return self._count

    def _require_open(self):
        if self._connection is None:
            raise RuntimeError("event relation is closed")
        return self._connection

    def append_batch(self, batch: CrossCandidateBatch):
        if self._sealed:
            raise RuntimeError("event relation is sealed")
        if not isinstance(batch, CrossCandidateBatch):
            raise TypeError("expected CrossCandidateBatch")
        self._quota.run("append_batch", lambda: self._append_transaction(batch))

    def _append_transaction(self, batch):
        connection = self._require_open()
        initial_count = self._count
        connection.execute("BEGIN")
        try:
            for start in range(0, len(batch), self._batch_size):
                stop = min(start + self._batch_size, len(batch))
                columns = {}
                for item in fields(batch):
                    values = getattr(batch, item.name)[start:stop]
                    if item.name in {"xy", "lonlat"}:
                        columns[item.name + "_0"] = values[:,0]
                        columns[item.name + "_1"] = values[:,1]
                    else:
                        columns[item.name] = values
                columns["ordinal"] = np.arange(self._count, self._count + stop - start, dtype=np.int64)
                self._insert_columns("events", columns)
                self._count += stop-start
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            self._count = initial_count
            raise

    def _insert_columns(self, table: str, columns: dict[str, np.ndarray]) -> None:
        if table not in {"events", "staged"}:
            raise ValueError("invalid relation table")
        expressions, parameters = [], []
        def parameter(values, sql_type):
            # One scalar string per typed column avoids the driver's
            # expensive per-value adaptation of nested Python lists.
            parameters.extend((json.dumps(values.tolist(), ensure_ascii=False, separators=(",", ":")), json.dumps([sql_type])))
            return "unnest(from_json(?, ?))"
        for name, values in columns.items():
            # Bounded serialization buffers, never per-event rows. NaN
            # and SQL NULL remain independent of business validity masks.
            nulls = np.ma.getmaskarray(values)
            data = np.asarray(np.ma.getdata(values))
            sql_type = {"b": "BOOLEAN", "i": "BIGINT", "f": "DOUBLE"}.get(data.dtype.kind, "VARCHAR")
            if data.dtype.kind == "f":
                nan = np.isnan(data) & ~nulls
                safe = np.where(nulls | nan, 0.0, data)
                null_sql = parameter(nulls, "BOOLEAN")
                nan_sql = parameter(nan, "BOOLEAN")
                data_sql = parameter(safe, "DOUBLE")
                expressions.append(f"CASE WHEN {null_sql} THEN NULL WHEN {nan_sql} THEN 'NaN'::DOUBLE ELSE {data_sql} END AS {name}")
            elif np.any(nulls):
                null_sql = parameter(nulls, "BOOLEAN")
                data_sql = parameter(data, sql_type)
                expressions.append(f"CASE WHEN {null_sql} THEN NULL ELSE {data_sql} END AS {name}")
            else:
                expressions.append(parameter(data, sql_type) + " AS " + name)
        self._require_open().execute("INSERT INTO " + table + " BY NAME SELECT " + ", ".join(expressions), parameters)

    def append_staged(self, grid_order: int, local_start: int, batch: CrossCandidateBatch) -> None:
        """Accept one bounded worker batch; only the main thread owns this connection."""
        if self._sealed or grid_order in self._finished_grids:
            raise RuntimeError("grid staging is sealed")
        if grid_order < 0 or local_start < 0 or not isinstance(batch, CrossCandidateBatch):
            raise ValueError("invalid grid candidate batch")
        self._quota.run("append_staged", lambda: self._append_staged_transaction(grid_order, local_start, batch))

    def _append_staged_transaction(self, grid_order: int, local_start: int, batch: CrossCandidateBatch) -> None:
        connection = self._require_open()
        connection.execute("BEGIN")
        try:
            for start in range(0, len(batch), self._batch_size):
                stop = min(start + self._batch_size, len(batch))
                columns = {}
                for item in fields(batch):
                    values = getattr(batch, item.name)[start:stop]
                    if item.name in {"xy", "lonlat"}:
                        columns[item.name + "_0"] = values[:, 0]
                        columns[item.name + "_1"] = values[:, 1]
                    else:
                        columns[item.name] = values
                columns["ordinal"] = np.arange(local_start + start, local_start + stop, dtype=np.int64)
                columns["grid_order"] = np.full(stop - start, grid_order, dtype=np.int64)
                columns["local_order"] = columns["ordinal"]
                self._insert_columns("staged", columns)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def finish_grid(self, grid_order: int) -> None:
        self._require_open()
        if grid_order in self._finished_grids:
            raise ValueError("grid already finished")
        self._finished_grids.add(grid_order)

    def discard_grid(self, grid_order: int) -> None:
        connection = self._require_open()
        self._finished_grids.discard(grid_order)
        self._quota.run("discard_grid", lambda: connection.execute("DELETE FROM staged WHERE grid_order=?", [grid_order]))

    def finalize_staged(self) -> None:
        """Choose the first grid/local representative, then assign stable IDs."""
        self._require_open()
        if self._sealed or self._count:
            raise RuntimeError("staging cannot be finalized after direct events")
        pending = self._quota.run(
            "pending_grid_check", lambda: self._require_open().execute(
                "SELECT DISTINCT grid_order FROM staged").fetchall())
        if any(int(row[0]) not in self._finished_grids for row in pending):
            raise RuntimeError("unfinished grid candidates cannot be published")
        self._quota.run("finalize_staged", self._finalize_staged_transaction)
        self.seal()

    def _finalize_staged_transaction(self) -> None:
        connection = self._require_open()
        identity = ("from_subtrack_index", "to_subtrack_index",
                    "from_point_index", "to_point_index")
        key = ", ".join(identity)
        columns = [row[1] for row in connection.execute("PRAGMA table_info('events')").fetchall()]
        values = ", ".join(name for name in columns if name not in {"ordinal", "calculation_index"})
        serial = f"row_number() OVER (ORDER BY {key}) - 1"
        query = ("WITH ranked AS (SELECT *, row_number() OVER "
                 f"(PARTITION BY {key} ORDER BY grid_order, local_order) AS candidate_rank FROM staged), "
                 "chosen AS (SELECT * FROM ranked WHERE candidate_rank=1) "
                 f"SELECT {serial} AS ordinal, {serial} AS calculation_index, {values} FROM chosen")
        initial_count = self._count
        connection.execute("BEGIN")
        try:
            connection.execute("INSERT INTO events BY NAME " + query)
            self._count = int(connection.execute("SELECT count(*) FROM events").fetchone()[0])
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            self._count = initial_count
            raise

    def extend(self, batches: Iterable[CrossCandidateBatch], *, batch_size: int | None = None):
        for batch in batches:
            self.append_batch(batch)

    append_many = extend

    def seal(self):
        self._require_open()
        try:
            self._quota.run("seal", self._validate_before_seal)
            self._sealed = True
        except BaseException:
            self.close()
            raise

    def _validate_before_seal(self):
        connection = self._require_open()
        if self._count:
            names = [row[1] for row in connection.execute("PRAGMA table_info('events')").fetchall()]
            guards = [name + " IS NULL" for name in names]
            guards.extend(['calculation_index != ordinal', 'NOT isfinite(xy_0) OR NOT isfinite(xy_1)', 'NOT isfinite(lonlat_0) OR NOT isfinite(lonlat_1) OR abs(lonlat_0)>180 OR abs(lonlat_1)>90', "from_vehicle_id = '' OR from_source_id = ''", 'from_refine_point_id_valid AND from_refine_point_id < 0', "to_vehicle_id = '' OR to_source_id = ''", 'to_refine_point_id_valid AND to_refine_point_id < 0'])
            guards.extend(['NOT isfinite(timestamp)', 'NOT isfinite(heading)', 'NOT isfinite(speed)', "from_refine_point_id_valid AND from_point_kind NOT IN ('p','i','kept','injected')", "to_refine_point_id_valid AND to_point_kind NOT IN ('p','i','kept','injected')", 'from_refine_point_id_valid AND (from_subtrack_index < 0 OR from_point_index < 0)', 'to_refine_point_id_valid AND (to_subtrack_index < 0 OR to_point_index < 0)'])
            invalid_sql = " OR ".join("(" + guard + ")" for guard in guards)
            count, unique_count, first_ordinal, last_ordinal, ordinal_count, invalid = connection.execute(
                "SELECT count(*), count(DISTINCT calculation_index), min(ordinal), max(ordinal), count(DISTINCT ordinal), count(*) FILTER (WHERE "
                + invalid_sql + ") FROM events"
            ).fetchone()
            if count != self._count or unique_count != count or first_ordinal != 0 or last_ordinal != count - 1 or ordinal_count != count or invalid:
                raise ValueError("event relation has invalid identity, ordinal, geometry or provenance")

    def iter_batches(self, *, indices: np.ndarray | None = None) -> Iterator[CrossCandidateBatch]:
        connection = self._require_open()
        if not self._sealed:
            raise RuntimeError("event relation must be sealed")
        if indices is not None and (not isinstance(indices, np.ndarray) or indices.ndim != 1 or indices.dtype != np.int64):
            raise ValueError("selected calculation indices must be a one-dimensional int64 array")
        if not self._count:
            return
        cursor = connection.cursor()
        selected = None
        try:
            if indices is None:
                query = "SELECT * EXCLUDE(ordinal) FROM events ORDER BY ordinal"
            else:
                selected = "selected_" + uuid4().hex
                cursor.register(selected, {"calculation_index":indices})
                query = f"SELECT e.* EXCLUDE(ordinal) FROM events e JOIN {selected} s USING(calculation_index) ORDER BY ordinal"
            cursor.execute(query)
            schema=[item[0] for item in cursor.description]
            template=empty_cross_batch()
            while rows := cursor.fetchmany(self._batch_size):
                # Driver tuples are bounded I/O buffers; never decode an event object.
                positions={name:index for index,name in enumerate(schema)}
                values={}
                for item in fields(CrossCandidateBatch):
                    name=item.name
                    if name in {"xy","lonlat"}:
                        values[name]=np.asarray([[row[positions[name+"_0"]],row[positions[name+"_1"]]] for row in rows],dtype=np.float64)
                    else:
                        values[name]=np.asarray([row[positions[name]] for row in rows],dtype=getattr(template,name).dtype)
                yield CrossCandidateBatch(**values)
        finally:
            if selected is not None:
                cursor.unregister(selected)
            cursor.close()

    def iter_coordinate_batches(self, *, include_source: bool = True):
        connection = self._require_open()
        if not self._sealed:
            raise RuntimeError("event relation must be sealed")
        if not self._count: return
        cursor=connection.cursor()
        try:
            cursor.execute("SELECT xy_0,xy_1,calculation_index" + (",from_source_id" if include_source else "") + " FROM events ORDER BY ordinal")
            while rows := cursor.fetchmany(self._batch_size):
                columns = (np.asarray([[row[0],row[1]] for row in rows],dtype=np.float64),
                           np.asarray([row[2] for row in rows],dtype=np.int64))
                if include_source:
                    columns += (np.asarray([row[3] for row in rows],dtype=object),)
                for column in columns:
                    column.flags.writeable = False
                yield columns
        finally:
            cursor.close()

    def iter_source_batches(self):
        """Read only candidate identity and both provenance columns for summaries."""
        connection = self._require_open()
        if not self._sealed:
            raise RuntimeError("event relation must be sealed")
        if not self._count:
            return
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT calculation_index,from_source_id,to_source_id FROM events ORDER BY ordinal")
            while rows := cursor.fetchmany(self._batch_size):
                columns = (np.asarray([row[0] for row in rows], dtype=np.int64),
                           np.asarray([row[1] for row in rows], dtype=object),
                           np.asarray([row[2] for row in rows], dtype=object))
                for column in columns:
                    column.flags.writeable = False
                yield columns
        finally:
            cursor.close()

    def close(self):
        if self._connection is not None:
            self._connection.close(); self._connection = None
        shutil.rmtree(self._temp_directory, ignore_errors=True)

    def __enter__(self):
        self._require_open(); return self

    def __exit__(self, *_args):
        self.close()
