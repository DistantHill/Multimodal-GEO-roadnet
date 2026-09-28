"""Spillable turn business batches; no per-event row decoding."""
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
from ..models import RuntimeConfig, TurnBatch
from .detect_turn_events import empty_turn_batch


class TurnEventRelation:
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
        self._temp_directory = root / "duckdb" / "turn-event-relations" / identity
        self._temp_directory.mkdir(parents=True, exist_ok=False)
        self._connection = None
        self._count = 0
        self._sealed = False
        try:
            self._connection = duckdb.connect(":memory:", config={
                "autoload_known_extensions":"false", "autoinstall_known_extensions":"false",
                "memory_limit":runtime.duckdb_event_memory_limit, "threads":str(runtime.duckdb_threads),
                "temp_directory":str(self._temp_directory), "max_temp_directory_size":runtime.duckdb_max_temp_directory_size,
            })
            self._quota = TempQuota(self._connection, runtime.duckdb_max_temp_directory_size, identity)
            self._create_schema()
        except BaseException:
            self.close(); raise

    def _create_schema(self):
        definitions = ["ordinal BIGINT"]
        template = empty_turn_batch()
        for item in fields(TurnBatch):
            name = item.name
            values = getattr(template, name)
            if name in {"xy", "lonlat"}:
                definitions.extend((name + "_0 DOUBLE", name + "_1 DOUBLE"))
            else:
                sql_type = {"b": "BOOLEAN", "i": "BIGINT", "f": "DOUBLE"}.get(values.dtype.kind, "VARCHAR")
                definitions.append(name + " " + sql_type)
        self._connection.execute("CREATE TABLE events (" + ", ".join(definitions) + ")")

    def __len__(self):
        self._require_open()
        return self._count

    def _require_open(self):
        if self._connection is None:
            raise RuntimeError("event relation is closed")
        return self._connection

    def append_batch(self, batch: TurnBatch):
        if self._sealed:
            raise RuntimeError("event relation is sealed")
        if not isinstance(batch, TurnBatch):
            raise TypeError("expected TurnBatch")
        self._quota.run("append_batch", lambda: self._append_transaction(batch))

    def _append_transaction(self, batch):
        connection = self._require_open()
        if np.any(batch.g_star_z_valid & ~np.isfinite(batch.g_star_z)):
            raise ValueError("g_star_z must be finite when valid")
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
                connection.execute("INSERT INTO events BY NAME SELECT " + ", ".join(expressions), parameters)
                self._count += stop-start
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            self._count = initial_count
            raise

    def extend(self, batches: Iterable[TurnBatch], *, batch_size: int | None = None):
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
            guards = [name + " IS NULL" for name in names if name != "g_star_z"]
            guards.append("g_star_z_valid AND g_star_z IS NULL")
            guards.extend(['calculation_index != ordinal', 'NOT isfinite(xy_0) OR NOT isfinite(xy_1)', 'NOT isfinite(lonlat_0) OR NOT isfinite(lonlat_1) OR abs(lonlat_0)>180 OR abs(lonlat_1)>90', "vehicle_id = '' OR source_id = ''", 'refine_point_id_valid AND refine_point_id < 0', 'NOT isfinite(turn_deg) OR turn_deg >= 180 OR turn_deg < -180 OR NOT isfinite(score) OR score != abs(turn_deg)', 'g_star_z_valid AND NOT isfinite(g_star_z)'])
            guards.extend(['NOT isfinite(timestamp)', 'NOT isfinite(entry_bearing)', 'NOT isfinite(exit_bearing)', "refine_point_id_valid AND point_kind NOT IN ('p','i','kept','injected')", 'refine_point_id_valid AND (subtrack_index < 0 OR point_index < 0)'])
            invalid_sql = " OR ".join("(" + guard + ")" for guard in guards)
            count, unique_count, first_ordinal, last_ordinal, ordinal_count, invalid = connection.execute(
                "SELECT count(*), count(DISTINCT calculation_index), min(ordinal), max(ordinal), count(DISTINCT ordinal), count(*) FILTER (WHERE "
                + invalid_sql + ") FROM events"
            ).fetchone()
            if count != self._count or unique_count != count or first_ordinal != 0 or last_ordinal != count - 1 or ordinal_count != count or invalid:
                raise ValueError("event relation has invalid identity, ordinal, geometry or provenance")

    def iter_batches(self, *, indices: np.ndarray | None = None) -> Iterator[TurnBatch]:
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
            template=empty_turn_batch()
            while rows := cursor.fetchmany(self._batch_size):
                # Driver tuples are bounded I/O buffers; never decode an event object.
                positions={name:index for index,name in enumerate(schema)}
                values={}
                for item in fields(TurnBatch):
                    name=item.name
                    if name in {"xy","lonlat"}:
                        values[name]=np.asarray([[row[positions[name+"_0"]],row[positions[name+"_1"]]] for row in rows],dtype=np.float64)
                    else:
                        values[name]=np.asarray([row[positions[name]] for row in rows],dtype=getattr(template,name).dtype)
                yield TurnBatch(**values)
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
            cursor.execute("SELECT xy_0,xy_1,calculation_index" + (",source_id" if include_source else "") + " FROM events ORDER BY ordinal")
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

    def close(self):
        if self._connection is not None:
            self._connection.close(); self._connection = None
        shutil.rmtree(self._temp_directory, ignore_errors=True)

    def __enter__(self):
        self._require_open(); return self

    def __exit__(self, *_args):
        self.close()
