"""DuckDB relations for the first, source-association phase of Detect writing."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from itertools import islice
import math
from numbers import Integral
from pathlib import Path
from uuid import uuid4

import duckdb
import numpy as np

from ...models import RuntimeConfig, TurnBatch, CrossCandidateBatch


@contextmanager
def _closing_optional(iterator):
    try:
        yield iterator
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


def _integer_array(values, *, nullable: bool = False):
    data, nulls = [], []
    for value in values:
        is_null = value is None
        if is_null and nullable:
            data.append(0)
        elif (isinstance(value, bool) or not isinstance(value, Integral)
              or not -(2**63) <= int(value) < 2**63):
            raise TypeError("writer integer field has an invalid type or range")
        else:
            data.append(int(value))
        nulls.append(is_null)
    return np.asarray(data, dtype=np.int64), np.asarray(nulls, dtype=np.bool_)


def _float_arrays(values):
    data, nulls, nans = [], [], []
    for value in values:
        is_null = value is None
        number = 0.0 if is_null else float(value)
        data.append(0.0 if math.isnan(number) else number)
        nulls.append(is_null)
        nans.append(not is_null and math.isnan(number))
    return (np.asarray(data, dtype=np.float64), np.asarray(nulls, dtype=np.bool_),
            np.asarray(nans, dtype=np.bool_))


def _string_array(values):
    values = list(values)
    if any(not isinstance(value, str) for value in values):
        raise TypeError("writer string field has an invalid type")
    return np.asarray(values, dtype=np.str_)


def _next_validated_batch(iterator, size: int, validate):
    batch = []
    for _ in range(size):
        try:
            item = next(iterator)
        except StopIteration:
            break
        validate(item)
        batch.append(item)
    return batch


class WriterRelation:
    """Own normalized raw writer rows and resolve exact Refine source keys."""

    _CONSUMERS = ("turn", "cross", "crossing")

    def __init__(
        self, runtime: RuntimeConfig, data_root: str | Path, detect_id: str, *,
        batch_size: int | None = None, relation_id: str | None = None,
    ) -> None:
        batch_size = runtime.relation_batch_size if batch_size is None else batch_size
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be positive")
        root = Path(data_root)
        if not root.is_absolute() or root == Path(root.anchor):
            raise ValueError("data_root must be absolute and non-root")
        self._validate_identity(detect_id, "detect_id")
        identity = relation_id or uuid4().hex
        self._validate_identity(identity, "relation_id")
        parent = root / "duckdb" / "writer-relations" / detect_id
        parent.mkdir(parents=True, exist_ok=True)
        self._temp_directory = parent / identity
        self._temp_directory.mkdir(exist_ok=False)
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._batch_size = batch_size
        self._detect_id = detect_id
        self._resolved = False
        self._annotations_prepared = False
        self._cross_validated = False
        self._request_order = 0
        self._mapping_order = 0
        self.counts = [0, 0, 0]
        try:
            self._connection = duckdb.connect(database=":memory:", config={
                "allow_unsigned_extensions": "false",
                "autoload_known_extensions": "false",
                "autoinstall_known_extensions": "false",
                "memory_limit": runtime.duckdb_memory_limit,
                "threads": str(runtime.duckdb_threads),
                "temp_directory": str(self._temp_directory),
                "max_temp_directory_size": runtime.duckdb_max_temp_directory_size,
            })
            self._create_schema()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _validate_identity(value: str, name: str) -> None:
        if (not isinstance(value, str) or not value or value in {".", ".."}
                or "\x00" in value or Path(value).name != value):
            raise ValueError(f"{name} must be one path component")

    def _create_schema(self) -> None:
        self._require_open().execute("""
            CREATE TABLE source_requests (
                request_order BIGINT PRIMARY KEY, point_kind VARCHAR NOT NULL,
                point_id BIGINT NOT NULL, subtrack_index BIGINT NOT NULL,
                consumer INTEGER NOT NULL, ordinal BIGINT NOT NULL, endpoint INTEGER NOT NULL
            );
            CREATE TABLE source_mappings (
                mapping_order BIGINT PRIMARY KEY, point_kind VARCHAR NOT NULL,
                point_id BIGINT NOT NULL, subtrack_index BIGINT NOT NULL,
                resolved_id BIGINT NOT NULL
            );
            CREATE TABLE turn_raw (
                ordinal BIGINT PRIMARY KEY, entry_bearing DOUBLE, exit_bearing DOUBLE,
                turn_deg DOUBLE, score DOUBLE
            );
            CREATE TABLE cross_raw (
                ordinal BIGINT PRIMARY KEY, x DOUBLE, y DOUBLE, lon DOUBLE, lat DOUBLE
            );
            CREATE TABLE crossing_raw (
                ordinal BIGINT PRIMARY KEY, crossing_id BIGINT, candidate_id BIGINT,
                is_entry INTEGER, x DOUBLE, y DOUBLE, lon DOUBLE, lat DOUBLE
            );
            CREATE TABLE annotation_requests (
                request_order BIGINT PRIMARY KEY, kind VARCHAR NOT NULL,
                calculation_index BIGINT NOT NULL, ordinal BIGINT NOT NULL
            );
            CREATE TABLE resolved_sources (
                consumer INTEGER NOT NULL, ordinal BIGINT NOT NULL,
                endpoint INTEGER NOT NULL, resolved_id BIGINT NOT NULL
            );
            CREATE TABLE members (
                kind VARCHAR NOT NULL, calculation_index BIGINT NOT NULL,
                encounter BIGINT NOT NULL, candidate_id BIGINT NOT NULL
            );
            CREATE TABLE hotspots (
                calculation_index BIGINT NOT NULL, input_ordinal BIGINT NOT NULL,
                z DOUBLE
            );
            CREATE TABLE annotations (
                request_order BIGINT PRIMARY KEY, kind VARCHAR NOT NULL,
                ordinal BIGINT NOT NULL, candidate_id BIGINT, z DOUBLE
            );
            CREATE TABLE validated_cross (
                ordinal BIGINT PRIMARY KEY, detect_id VARCHAR NOT NULL,
                candidate_id BIGINT, low_resolved_id BIGINT NOT NULL,
                high_resolved_id BIGINT NOT NULL, x DOUBLE, y DOUBLE,
                lon DOUBLE, lat DOUBLE
            )
        """)

    def _require_appendable(self) -> duckdb.DuckDBPyConnection:
        connection = self._require_open()
        if self._resolved:
            raise RuntimeError("writer relation is resolved")
        return connection

    def _register_batch(self, columns: dict[str, np.ndarray]) -> str:
        relation = f"writer_batch_{uuid4().hex}"
        # DuckDB's object-string scanner is expensive even for bounded batches.
        # Pure text columns can use its typed string scanner; nullable/mixed
        # object columns retain their original NULL and scalar semantics.
        columns = {
            name: values.astype(np.str_) if values.dtype.kind == "O"
            and all(isinstance(value, str) for value in values) else values
            for name, values in columns.items()
        }
        self._require_open().register(relation, columns)
        return relation

    def _insert_batch(self, relations, statements: tuple[str, ...]) -> None:
        connection = self._require_appendable()
        names = (relations,) if isinstance(relations, str) else relations
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                parameters = {f"relation{index}": name for index, name in enumerate(names)}
                parameters["relation"] = names[0]
                for statement in statements:
                    connection.execute(statement.format(**parameters))
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            for name in names:
                connection.unregister(name)

    def _insert_registered(self, columns: dict[str, np.ndarray], statement: str) -> None:
        connection = self._require_open()
        relation = self._register_batch(columns)
        try:
            connection.execute(statement.format(relation=relation))
        finally:
            connection.unregister(relation)

    @staticmethod
    def _float_columns(columns):
        result={}
        for name,values in columns.items():
            data=np.asarray(np.ma.getdata(values),dtype=np.float64)
            null=np.ma.getmaskarray(values)
            nan=np.isnan(data)&~null
            result[name]=np.where(nan|null,0.0,data)
            result[name+"_null"]=null
            result[name+"_nan"]=nan
        return result

    @staticmethod
    def _float_sql(names):
        return ",".join(f"CASE WHEN {name}_null THEN NULL WHEN {name}_nan THEN 'NaN'::DOUBLE ELSE {name} END" for name in names)

    def collect_sources(self, result, pending, crossing_ids, candidate_ids):
        from ..detect_intersections import _batches
        self._plugin_results = tuple(getattr(result, "candidate_results", ()))
        self._source_kinds = {item.source_key: "cross" if item.evidence_kind == "orthogonal_cross_track" else "turn"
                              for item in self._plugin_results}
        annotation_order=sum(self.counts[:2])
        for consumer, events in ((0,result.turn_events),(1,result.cross_track_candidates)):
            with _closing_optional(_batches(events)) as iterator:
                for original in iterator:
                    for start in range(0,len(original),self._batch_size):
                        batch=original.take(np.arange(start,min(start+self._batch_size,len(original))))
                        size=len(batch); ordinals=np.arange(self.counts[consumer],self.counts[consumer]+size,dtype=np.int64)
                        if consumer==0:
                            if not np.all(batch.refine_point_id_valid): raise ValueError("persisted turn requires stable resolved point")
                            sources={"point_kind":np.where(batch.point_kind=="p","kept","injected"),"point_id":batch.refine_point_id,"subtrack_index":batch.subtrack_index,"ordinal":ordinals,"endpoint":np.zeros(size,dtype=np.int64)}
                            raw={"ordinal":ordinals,"entry_bearing":batch.entry_bearing,"exit_bearing":batch.exit_bearing,"turn_deg":batch.turn_deg,"score":batch.score}
                            kind="turn"
                        else:
                            if not (np.all(batch.from_refine_point_id_valid) and np.all(batch.to_refine_point_id_valid)): raise ValueError("persisted cross requires stable segment starts")
                            sources={"point_kind":np.concatenate((np.where(batch.from_point_kind=="p","kept","injected"),np.where(batch.to_point_kind=="p","kept","injected"))),"point_id":np.concatenate((batch.from_refine_point_id,batch.to_refine_point_id)),"subtrack_index":np.concatenate((batch.from_subtrack_index,batch.to_subtrack_index)),"ordinal":np.tile(ordinals,2),"endpoint":np.repeat(np.arange(2,dtype=np.int64),size)}
                            raw={"ordinal":ordinals,"x":batch.xy[:,0],"y":batch.xy[:,1],"lon":batch.lonlat[:,0],"lat":batch.lonlat[:,1]}
                            kind="cross"
                        float_names=("entry_bearing","exit_bearing","turn_deg","score") if consumer==0 else ("x","y","lon","lat")
                        raw={"ordinal":ordinals,**self._float_columns({name:raw[name] for name in float_names})}
                        sources["request_order"]=np.arange(self._request_order,self._request_order+len(sources["point_id"]),dtype=np.int64)
                        source_relation=self._register_batch(sources); raw_relation=self._register_batch(raw)
                        annotation_relation=self._register_batch({"request_order":np.arange(annotation_order,annotation_order+size,dtype=np.int64),"calculation_index":batch.calculation_index,"ordinal":ordinals})
                        self._insert_batch((source_relation,raw_relation,annotation_relation),(
                            f"INSERT INTO source_requests SELECT request_order,point_kind,point_id,subtrack_index,{consumer},ordinal,endpoint FROM {{relation0}} ORDER BY request_order",
                            f"INSERT INTO {kind}_raw SELECT ordinal,{self._float_sql(float_names)} FROM {{relation1}} ORDER BY ordinal",
                            f"INSERT INTO annotation_requests SELECT request_order,'{kind}',calculation_index,ordinal FROM {{relation2}} ORDER BY request_order"))
                        self._request_order+=len(sources["point_id"]); self.counts[consumer]+=size; annotation_order+=size
        for cluster,crossings in pending:
            ids=np.asanyarray(crossing_ids[cluster.cluster_calculate_index])
            if ids.dtype!=np.int64 or len(ids)!=len(crossings): raise ValueError("crossing ID columns must be aligned int64")
            for start in range(0,len(crossings),self._batch_size):
                stop=min(start+self._batch_size,len(crossings)); size=stop-start
                if not np.all(crossings.refine_point_id_valid[start:stop]): raise ValueError("persisted crossing requires stable segment start")
                ordinal=np.arange(self.counts[2],self.counts[2]+size,dtype=np.int64)
                candidate=candidate_ids[cluster.cluster_calculate_index]
                floats=self._float_columns({"x":crossings.xy[start:stop,0],"y":crossings.xy[start:stop,1],"lon":crossings.lonlat[start:stop,0],"lat":crossings.lonlat[start:stop,1]})
                relation=self._register_batch({"request_order":np.arange(self._request_order,self._request_order+size,dtype=np.int64),"point_kind":np.where(crossings.point_kind[start:stop]=="p","kept","injected"),"point_id":crossings.refine_point_id[start:stop],"subtrack_index":crossings.subtrack_index[start:stop],"ordinal":ordinal,"crossing_id":np.asarray(np.ma.getdata(ids[start:stop]),dtype=np.int64),"crossing_id_null":np.ma.getmaskarray(ids[start:stop]),"candidate_id":np.full(size,0 if candidate is None else candidate,dtype=np.int64),"candidate_id_null":np.full(size,candidate is None,dtype=np.bool_),"is_entry":crossings.is_entry[start:stop].astype(np.int32),**floats})
                self._insert_batch(relation,("INSERT INTO source_requests SELECT request_order,point_kind,point_id,subtrack_index,2,ordinal,0 FROM {relation} ORDER BY request_order","INSERT INTO crossing_raw SELECT ordinal,CASE WHEN crossing_id_null THEN NULL ELSE crossing_id END,CASE WHEN candidate_id_null THEN NULL ELSE candidate_id END,is_entry,"+self._float_sql(("x","y","lon","lat"))+" FROM {relation} ORDER BY ordinal"))
                self._request_order+=size; self.counts[2]+=size

    def add_mappings(self, point_kind, point_id, subtrack_index, resolved_ids):
        for column in (point_id,subtrack_index,resolved_ids):
            if not isinstance(column,np.ndarray) or column.dtype!=np.int64: raise TypeError("mapping identity columns must be int64")
        if not isinstance(point_kind,np.ndarray) or point_kind.ndim!=1 or any(not isinstance(value,str) for value in point_kind): raise TypeError("point_kind must be a string column")
        size=len(point_id)
        if any(len(value)!=size for value in (point_kind,subtrack_index,resolved_ids)): raise ValueError("source mapping columns differ in length")
        for start in range(0,size,self._batch_size):
            stop=min(start+self._batch_size,size)
            columns={"mapping_order":np.arange(self._mapping_order,self._mapping_order+stop-start,dtype=np.int64),"point_kind":np.asarray(point_kind[start:stop]),"point_id":np.asarray(point_id[start:stop],dtype=np.int64),"subtrack_index":np.asarray(subtrack_index[start:stop],dtype=np.int64),"resolved_id":np.asarray(resolved_ids[start:stop],dtype=np.int64)}
            self._insert_batch(self._register_batch(columns),("INSERT INTO source_mappings SELECT * FROM {relation} ORDER BY mapping_order",))
            self._mapping_order+=stop-start

    def resolve_sources(self) -> None:
        connection = self._require_open()
        if self._resolved:
            return
        missing = connection.execute("""
            SELECT r.point_kind,r.point_id,r.subtrack_index
            FROM source_requests r LEFT JOIN source_mappings m
              USING(point_kind,point_id,subtrack_index)
            WHERE m.mapping_order IS NULL ORDER BY r.request_order LIMIT 1
        """).fetchone()
        if missing is not None:
            raise KeyError(tuple(missing))
        connection.execute("""
            INSERT INTO resolved_sources
            SELECT r.consumer,r.ordinal,r.endpoint,m.resolved_id
            FROM source_requests r JOIN source_mappings m
              USING(point_kind,point_id,subtrack_index)
            QUALIFY row_number() OVER (
                PARTITION BY r.request_order ORDER BY m.mapping_order DESC
            )=1
        """)
        self._resolved = True

    def iter_resolved_sources(self, consumer: int) -> Iterator[tuple[int, int, int]]:
        if consumer not in range(len(self._CONSUMERS)):
            raise ValueError("unknown source consumer")
        connection = self._require_open()
        if not self._resolved:
            raise RuntimeError("writer relation is not resolved")
        yield from self._iterate(connection, """
            SELECT ordinal,endpoint,resolved_id FROM resolved_sources
            WHERE consumer=? ORDER BY ordinal,endpoint
        """, (consumer,))

    def iter_raw(self, kind: str) -> Iterator[tuple]:
        if kind not in self._CONSUMERS:
            raise ValueError("unknown raw writer relation")
        yield from self._iterate(
            self._require_open(), f"SELECT * FROM {kind}_raw ORDER BY ordinal", (),
        )

    def iter_annotation_requests(self) -> Iterator[tuple[str, int, int]]:
        yield from self._iterate(self._require_open(), """
            SELECT kind,calculation_index,ordinal FROM annotation_requests
            ORDER BY request_order
        """, ())

    def prepare_annotations(self, clusters, candidate_ids, hotspots):
        self._source_kinds = getattr(self, "_source_kinds", {})
        connection=self._require_open()
        if not self._resolved: raise RuntimeError("writer relation is not resolved")
        if self._annotations_prepared: raise RuntimeError("writer annotations are already prepared")
        connection.execute("BEGIN TRANSACTION")
        try:
            encounter=0
            for cluster in clusters:
                members=cluster.members
                if members is None: continue
                for start in range(0,len(members),self._batch_size):
                    stop=min(start+self._batch_size,len(members)); size=stop-start
                    self._insert_registered({"kind":np.asarray([self._source_kinds.get(str(kind), "cross" if kind == "orthogonal_cross_track" else str(kind)) for kind in members.kind[start:stop]]),"calculation_index":members.calculation_index[start:stop],"encounter":np.arange(encounter,encounter+size,dtype=np.int64),"candidate_id":np.full(size,candidate_ids[cluster.cluster_calculate_index],dtype=np.int64)},"INSERT INTO members SELECT * FROM {relation} ORDER BY encounter")
                    encounter+=size
            from ..detect_intersections import _batches
            input_offset = 0
            with _closing_optional(_batches(hotspots)) as hotspot_batches:
                for hotspots in hotspot_batches:
                    for start in range(0,len(hotspots),self._batch_size):
                        stop=min(start+self._batch_size,len(hotspots))
                        self._insert_registered({"calculation_index":hotspots.calculation_index[start:stop],"input_ordinal":np.arange(input_offset+start,input_offset+stop,dtype=np.int64),"z":np.where(np.isnan(hotspots.g_star_z[start:stop]),0.0,hotspots.g_star_z[start:stop]),"nan":np.isnan(hotspots.g_star_z[start:stop]),"valid":hotspots.g_star_z_valid[start:stop]},"INSERT INTO hotspots SELECT calculation_index,input_ordinal,CASE WHEN NOT valid THEN NULL WHEN nan THEN 'NaN'::DOUBLE ELSE z END FROM {relation} ORDER BY input_ordinal")
                    input_offset += len(hotspots)
            conflict = connection.execute("""
                SELECT calculation_index FROM (
                    SELECT calculation_index,encounter,candidate_id,
                           first_value(candidate_id) OVER (
                               PARTITION BY kind,calculation_index ORDER BY encounter
                           ) AS first_candidate
                    FROM members
                ) WHERE candidate_id != first_candidate
                ORDER BY encounter LIMIT 1
            """).fetchone()
            if conflict is not None:
                raise ValueError(
                    f"Detect entity {conflict[0]} belongs to multiple candidates"
                )
            connection.execute("""
                INSERT INTO annotations
                WITH unique_members AS (
                    SELECT kind,calculation_index,candidate_id FROM members
                    QUALIFY row_number() OVER (
                        PARTITION BY kind,calculation_index ORDER BY encounter
                    )=1
                ), last_hotspots AS (
                    SELECT calculation_index,z FROM hotspots
                    QUALIFY row_number() OVER (
                        PARTITION BY calculation_index ORDER BY input_ordinal DESC
                    )=1
                )
                SELECT r.request_order,r.kind,r.ordinal,m.candidate_id,
                       CASE WHEN r.kind='turn' THEN h.z ELSE NULL END
                FROM annotation_requests r
                LEFT JOIN unique_members m USING(kind,calculation_index)
                LEFT JOIN last_hotspots h
                  ON r.kind='turn' AND r.calculation_index=h.calculation_index
                ORDER BY r.request_order
            """)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        self._annotations_prepared = True

    def iter_annotations(self, kind: str) -> Iterator[tuple[int, int | None, float | None]]:
        if kind not in self._CONSUMERS[:2]:
            raise ValueError("unknown writer annotation kind")
        connection = self._require_open()
        if not self._annotations_prepared:
            raise RuntimeError("writer annotations are not prepared")
        yield from self._iterate(connection, """
            SELECT ordinal,candidate_id,z FROM annotations
            WHERE kind=? ORDER BY request_order
        """, (kind,))

    def iter_rows(self, consumer: int, detect_id: str) -> Iterator[tuple]:
        """Yield final PostgreSQL COPY rows after checking every association."""
        if consumer not in range(len(self._CONSUMERS)):
            raise ValueError("unknown source consumer")
        connection = self._require_open()
        self._require_detect_id(detect_id)
        if not self._resolved:
            raise RuntimeError("writer relation is not resolved")
        if consumer < 2 and not self._annotations_prepared:
            raise RuntimeError("writer annotations are not prepared")
        if consumer == 1 and not self._cross_validated:
            raise RuntimeError("track cross pairs are not validated")
        self._validate_final_inputs(connection, consumer)
        if consumer == 0:
            query = """
                SELECT ?,a.candidate_id,s.resolved_id,r.entry_bearing,r.exit_bearing,
                       r.turn_deg,r.score,a.z
                FROM turn_raw r
                JOIN resolved_sources s
                  ON s.consumer=0 AND s.ordinal=r.ordinal AND s.endpoint=0
                JOIN annotations a ON a.kind='turn' AND a.ordinal=r.ordinal
                ORDER BY r.ordinal
            """
        elif consumer == 1:
            query = """
                SELECT detect_id,candidate_id,low_resolved_id,high_resolved_id,
                       x,y,lon,lat FROM validated_cross ORDER BY ordinal
            """
        else:
            query = """
                SELECT r.crossing_id,?,r.candidate_id,s.resolved_id,r.is_entry,
                       r.x,r.y,r.lon,r.lat
                FROM crossing_raw r
                JOIN resolved_sources s
                  ON s.consumer=2 AND s.ordinal=r.ordinal AND s.endpoint=0
                ORDER BY r.ordinal
            """
        yield from self._iterate(connection, query, () if consumer == 1 else (detect_id,))

    def _validate_final_inputs(self, connection, consumer: int) -> None:
        kind = self._CONSUMERS[consumer]
        expected_endpoints = 2 if consumer == 1 else 1
        raw_count = connection.execute(
            f"SELECT count(*) FROM {kind}_raw"
        ).fetchone()[0]
        if raw_count != self.counts[consumer]:
            raise RuntimeError("raw writer row count mismatch")
        source_error = connection.execute(f"""
            SELECT 1 FROM (
                SELECT ordinal,endpoint FROM resolved_sources WHERE consumer=?
                EXCEPT
                SELECT r.ordinal,e.endpoint FROM {kind}_raw r
                CROSS JOIN range(?) e(endpoint)
            ) UNION ALL SELECT 1 FROM (
                SELECT r.ordinal,e.endpoint FROM {kind}_raw r
                CROSS JOIN range(?) e(endpoint)
                EXCEPT
                SELECT ordinal,endpoint FROM resolved_sources WHERE consumer=?
            ) UNION ALL SELECT 1 FROM (
                SELECT ordinal,endpoint FROM resolved_sources WHERE consumer=?
                GROUP BY ordinal,endpoint HAVING count(*) != 1
            ) LIMIT 1
        """, (consumer, expected_endpoints, expected_endpoints, consumer, consumer)).fetchone()
        if source_error is not None:
            raise RuntimeError("source association mismatch")
        if consumer < 2:
            annotation_error = connection.execute(f"""
                SELECT 1 FROM (
                    SELECT ordinal FROM annotations WHERE kind=?
                    EXCEPT SELECT ordinal FROM {kind}_raw
                ) UNION ALL SELECT 1 FROM (
                    SELECT ordinal FROM {kind}_raw
                    EXCEPT SELECT ordinal FROM annotations WHERE kind=?
                ) UNION ALL SELECT 1 FROM (
                    SELECT ordinal FROM annotations WHERE kind=?
                    GROUP BY ordinal HAVING count(*) != 1
                ) LIMIT 1
            """, (kind, kind, kind)).fetchone()
            if annotation_error is not None:
                raise RuntimeError("annotation association mismatch")

    def validate_cross_pairs(self, detect_id: str) -> None:
        connection = self._require_open()
        self._require_detect_id(detect_id)
        if self._cross_validated:
            return
        if not self._resolved:
            raise RuntimeError("writer relation is not resolved")
        if not self._annotations_prepared:
            raise RuntimeError("writer annotations are not prepared")
        connection.execute("DELETE FROM validated_cross")
        self._validate_final_inputs(connection, 1)
        ranges = [(item.evidence_offset, item.evidence_offset + len(item.events))
                  for item in getattr(self, "_plugin_results", ())
                  if item.evidence_kind == "orthogonal_cross_track"]
        if not ranges:
            ranges = [(0, self.counts[1])]
        for start, stop in ranges:
            invalid = connection.execute("""
                WITH pairs AS (
                    SELECT least(s0.resolved_id,s1.resolved_id) low_id,
                           greatest(s0.resolved_id,s1.resolved_id) high_id
                    FROM cross_raw r
                    JOIN resolved_sources s0
                      ON s0.consumer=1 AND s0.ordinal=r.ordinal AND s0.endpoint=0
                    JOIN resolved_sources s1
                      ON s1.consumer=1 AND s1.ordinal=r.ordinal AND s1.endpoint=1
                    WHERE r.ordinal >= ? AND r.ordinal < ?
                )
                SELECT 1 FROM pairs GROUP BY low_id,high_id
                HAVING low_id=high_id OR count(*)>1 LIMIT 1
            """, (start, stop)).fetchone()
            if invalid is not None:
                raise ValueError("duplicate/degenerate track cross segment pair")
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute("""
                INSERT INTO validated_cross
                SELECT r.ordinal,?,a.candidate_id,
                       least(s0.resolved_id,s1.resolved_id),
                       greatest(s0.resolved_id,s1.resolved_id),r.x,r.y,r.lon,r.lat
                FROM cross_raw r
                JOIN resolved_sources s0
                  ON s0.consumer=1 AND s0.ordinal=r.ordinal AND s0.endpoint=0
                JOIN resolved_sources s1
                  ON s1.consumer=1 AND s1.ordinal=r.ordinal AND s1.endpoint=1
                JOIN annotations a ON a.kind='cross' AND a.ordinal=r.ordinal
                ORDER BY r.ordinal
            """, (detect_id,))
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            connection.execute("DELETE FROM validated_cross")
            raise
        self._cross_validated = True

    def iter_validated_cross_rows(self) -> Iterator[tuple]:
        connection = self._require_open()
        if not self._cross_validated:
            raise RuntimeError("track cross pairs are not validated")
        yield from self._iterate(connection, """
            SELECT detect_id,candidate_id,low_resolved_id,high_resolved_id,x,y,lon,lat
            FROM validated_cross ORDER BY ordinal
        """, ())

    def _identity_for_ordinal(self, kind, ordinal):
        for item in getattr(self, "_plugin_results", ()):
            evidence = "cross" if item.evidence_kind == "orthogonal_cross_track" else "turn"
            rows = item.events if item.events is not None else item.candidates
            if evidence == kind and item.evidence_offset <= ordinal < item.evidence_offset + len(rows):
                return item.algorithm_id, item.instance_id, ordinal - item.evidence_offset, ordinal
        if getattr(self, "_plugin_results", ()):
            raise ValueError("missing algorithm instance identity for persisted evidence")
        return None, None, None, None

    def with_instance_identity(self, kind, rows):
        with _closing_optional(rows) as iterator:
            for ordinal, row in enumerate(iterator):
                yield (*row, *self._identity_for_ordinal(kind, ordinal))

    def _require_detect_id(self, detect_id: str) -> None:
        if detect_id != self._detect_id:
            raise ValueError("detect_id does not match writer relation")

    def _iterate(self, connection, query: str, params: tuple) -> Iterator[tuple]:
        cursor = connection.cursor()
        try:
            cursor.execute(query, params)
            while rows := cursor.fetchmany(self._batch_size):
                yield from rows
        finally:
            cursor.close()

    def _require_open(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RuntimeError("writer relation is closed")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if hasattr(self, "_temp_directory"):
            try:
                self._temp_directory.rmdir()
            except OSError:
                pass

    def __enter__(self) -> WriterRelation:
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
