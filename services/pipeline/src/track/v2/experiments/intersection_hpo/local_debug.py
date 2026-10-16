"""Replay the production local Detect boundary into isolated diagnostic tables.

Formal Refine/Detect persistence is deliberately not used: local fragments can
start at subtrack zero or with injected anchor -1. Their original identities
remain intact in this independent roadnet_debug schema.
"""
from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass
from itertools import islice
import json
import logging
import math
import time
from uuid import uuid4

import numpy as np
from psycopg.types.json import Jsonb
from shapely.geometry import shape

from ...config import connect
from ...db.write_refine_results import fetch_refine_run
from ...intersections.detect_intersections import DetectRuntimeContext, detect_intersections
from ...models import TurnBatch, CrossCandidateBatch
from .adapters import threshold_config
from .config import code_revision
from .local_adapter import FINAL_FACE_SQL
from .local_refine import LocalRefineReader

_LOGGER = logging.getLogger(__name__)
TABLES = ("input_points", "candidate_instances", "turn_events", "cross_events", "candidate_points",
          "cluster_members", "candidate_intersections", "topologies", "crossings", "observations", "final_faces",
          "truth_regions", "evaluation_regions", "computation_regions")
COLUMNS = ("run_id", "ordinal", "calculation_index", "cluster_index", "source_key", "payload", "geom")
RUNS_DDL = """CREATE SCHEMA IF NOT EXISTS hpo_debug;
CREATE TABLE IF NOT EXISTS hpo_debug.runs (
    id uuid PRIMARY KEY, snapshot jsonb NOT NULL, counts jsonb,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz,
    status text NOT NULL CHECK(status IN ('running','completed')));
"""
INPUT_SQL = "SELECT * FROM refine_points ORDER BY subtrack_index,refined_sequence_in_subtrack"
RUN_INSERT_SQL = "INSERT INTO hpo_debug.runs(id,snapshot,status) VALUES (%s,%s,'running')"
RUN_FINISH_SQL = "UPDATE hpo_debug.runs SET counts=%s,status='completed',finished_at=clock_timestamp() WHERE id=%s"
ANALYZE_CANDIDATES_SQL = "ANALYZE hpo_debug.candidate_points"
ANALYZE_MEMBERS_SQL = "ANALYZE hpo_debug.cluster_members"
ASSIGN_CLUSTERS_SQL = """UPDATE hpo_debug.candidate_points p SET cluster_index=m.cluster_index
FROM hpo_debug.cluster_members m WHERE p.run_id=%s AND m.run_id=p.run_id
AND m.source_key=p.source_key AND m.calculation_index=p.calculation_index"""
ASSIGN_NOISE_SQL = "UPDATE hpo_debug.candidate_points SET is_noise=(cluster_index IS NULL) WHERE run_id=%s"
MEMBER_GEOMETRY_SQL = """UPDATE hpo_debug.cluster_members m SET geom=p.geom
FROM hpo_debug.candidate_points p WHERE m.run_id=%s AND p.run_id=m.run_id
AND p.source_key=m.source_key AND p.calculation_index=m.calculation_index"""
MISSING_MEMBERS_SQL = """SELECT count(*) FROM hpo_debug.cluster_members m
LEFT JOIN hpo_debug.candidate_points p ON p.run_id=m.run_id
AND p.source_key=m.source_key AND p.calculation_index=m.calculation_index
WHERE m.run_id=%s AND p.ordinal IS NULL"""
NOISE_COUNT_SQL = "SELECT count(*) FROM hpo_debug.candidate_points WHERE run_id=%s AND is_noise"
ROI_FACE_COUNT_SQL = """SELECT count(*) FROM hpo_debug.final_faces
WHERE run_id=%s AND (payload->>'in_evaluation_roi')::boolean"""


def _native(value):
    if isinstance(value, np.generic):
        return _native(value.item())
    if isinstance(value, np.ndarray):
        return _native(value.tolist())
    if isinstance(value, dict):
        return {(f"{key[0]},{key[1]}" if isinstance(key, tuple) and len(key) == 2 else str(key)): _native(item)
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_native(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _point(lonlat):
    if lonlat is None:
        return None
    return f"SRID=4326;POINT({float(lonlat[0])!r} {float(lonlat[1])!r})"


def _polygon(geometry):
    return "SRID=4326;" + shape(geometry).wkt


def _batch_rows(batch):
    """Build one row only; never asdict/copy the complete array model."""
    columns = [(item.name, getattr(batch, item.name)) for item in fields(batch)]
    for index in range(len(batch)):
        yield {name: _native(value[index] if isinstance(value, np.ndarray) else value)
               for name, value in columns}


def _batches(events):
    if isinstance(events, (TurnBatch, CrossCandidateBatch)):
        yield events
    else:
        iterator = events.iter_batches()
        try:
            yield from iterator
        finally:
            close = getattr(iterator, "close", None)
            if close:
                close()


def _table_ddl(name):
    if name not in TABLES:
        raise ValueError("unknown local debug artifact table")
    noise_column = ", is_noise boolean" if name == "candidate_points" else ""
    return f"""CREATE TABLE IF NOT EXISTS hpo_debug.{name} (
        run_id uuid NOT NULL REFERENCES hpo_debug.runs(id) ON DELETE CASCADE,
        ordinal bigint NOT NULL, calculation_index bigint, cluster_index bigint,
        source_key text, payload jsonb NOT NULL, geom geometry(Geometry,4326){noise_column},
        PRIMARY KEY(run_id,ordinal));
        CREATE INDEX IF NOT EXISTS {name}_geom ON hpo_debug.{name} USING gist(geom);
        CREATE INDEX IF NOT EXISTS {name}_source ON hpo_debug.{name}(run_id,source_key,calculation_index);
        CREATE INDEX IF NOT EXISTS {name}_cluster ON hpo_debug.{name}(run_id,cluster_index)"""


class DebugWriter:
    """Stream bounded COPY chunks; the caller owns one atomic debug transaction."""
    def __init__(self, connection, run_id, batch_size):
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("debug batch_size must be a positive integer")
        self.connection, self.run_id, self.batch_size = connection, str(run_id), batch_size
        self.counts = {name: 0 for name in TABLES}

    def initialize(self):
        self.connection.execute(RUNS_DDL)
        for name in TABLES:
            self.connection.execute(_table_ddl(name))

    def write(self, name, records):
        if name not in TABLES:
            raise ValueError("unknown local debug artifact table")
        iterator = iter(records)
        try:
            while batch := list(islice(iterator, self.batch_size)):
                # Materialize only this chunk before COPY. Polygon generators
                # execute scalar SQL, which cannot run while COPY is active.
                with self.connection.cursor() as cursor:
                    with cursor.copy(f"COPY hpo_debug.{name} ({','.join(COLUMNS)}) FROM STDIN") as copier:
                        for calculation, cluster, source, payload, geometry in batch:
                            ordinal = self.counts[name]
                            copier.write_row((self.run_id, ordinal, calculation, cluster,
                                              source, Jsonb(_native(payload)), geometry))
                            self.counts[name] += 1
        finally:
            close = getattr(iterator, "close", None)
            if close:
                close()
        _LOGGER.info("Local debug artifact stored: run_id=%s table=%s rows=%d",
                     self.run_id, name, self.counts[name])


def _input_rows(reader, batch_size):
    cursor = reader._require_open().execute(INPUT_SQL)
    names = [column[0] for column in cursor.description]
    while rows := cursor.fetchmany(batch_size):
        for row in rows:
            payload = dict(zip(names, row))
            yield (int(payload["point_id"]), None, str(payload["point_kind"]),
                   payload, _point((payload["lon"], payload["lat"])))


def _evidence_rows(item, candidates=False):
    evidence = item.candidates if candidates else item.events
    for batch in _batches(evidence):
        for row in _batch_rows(batch):
            local = int(row["calculation_index"])
            calculation = local + item.evidence_offset
            row.update(local_calculation_index=local, calculation_index=calculation,
                       algorithm_id=item.algorithm_id, instance_id=item.instance_id,
                       evidence_kind=item.evidence_kind)
            yield calculation, None, item.source_key, row, _point(row.get("lonlat"))


def _cluster_rows(connection, result):
    for cluster in result.clusters:
        payload = {item.name: _native(getattr(cluster, item.name)) for item in fields(cluster)
                   if item.name not in {"members", "crossing_points"}}
        payload["member_count"] = len(cluster.members) if cluster.members is not None else 0
        geometry = _circle(connection, result.spatial_context.processing_srid,
                           cluster.centroid_x, cluster.centroid_y, cluster.radius_m)
        yield None, cluster.cluster_calculate_index, cluster.source, payload, _polygon(geometry)


def _circle(connection, srid, x, y, radius):
    with connection.cursor() as cursor:
        cursor.execute(FINAL_FACE_SQL, (x, y, srid, radius))
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("debug circle scalar returned no geometry")
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def _member_rows(result):
    for cluster in result.clusters:
        if cluster.members is not None:
            for row in _batch_rows(cluster.members):
                yield int(row["calculation_index"]), cluster.cluster_calculate_index, str(row["kind"]), row, None


def _topology_rows(result):
    clusters = {cluster.cluster_calculate_index: cluster for cluster in result.clusters}
    for topology in result.topologies:
        cluster = clusters[topology.cluster_calculate_index]
        yield (None, topology.cluster_calculate_index, None, _native(asdict(topology)),
               _point((cluster.centroid_lon, cluster.centroid_lat)))


def _crossing_rows(result):
    for cluster in result.clusters:
        if cluster.crossing_points is not None:
            for row in _batch_rows(cluster.crossing_points):
                yield int(row["calculation_index"]), cluster.cluster_calculate_index, row.get("source_id"), row, _point(row["lonlat"])


def _observation_rows(result):
    for observations in result.observations:
        for row in _batch_rows(observations):
            yield None, observations.cluster_calculate_index, None, row, None


def _face_rows(connection, result, region):
    boundary = shape(region)
    clusters = {cluster.cluster_calculate_index: cluster for cluster in result.clusters}
    for topology in result.topologies:
        if not topology.is_complete:
            continue
        cluster = clusters[topology.cluster_calculate_index]
        radius = topology.clip_radius_m or cluster.radius_m
        geometry = _circle(connection, result.spatial_context.processing_srid,
                           cluster.centroid_x, cluster.centroid_y, radius)
        payload = {"cluster_calculate_index": topology.cluster_calculate_index,
                   "candidate_radius_m": cluster.radius_m, "clip_radius_m": radius,
                   "observation_count": topology.observation_count,
                   "in_evaluation_roi": bool(boundary.covers(shape(geometry).centroid)),
                   "geometry": geometry}
        yield None, topology.cluster_calculate_index, None, payload, _polygon(geometry)


def persist_local_debug(connection, reader, result, snapshot, *, run_id=None,
                        batch_size=5000, expected_stats=None):
    """Persist every local artifact and verify counts before caller commits."""
    run_id = str(run_id or uuid4())
    writer = DebugWriter(connection, run_id, batch_size)
    writer.initialize()
    connection.execute(RUN_INSERT_SQL, (run_id, Jsonb(_native(snapshot))))
    writer.write("evaluation_regions", [(None, None, "evaluation_roi", {},
                                          _polygon(snapshot["evaluation_region"]))])
    writer.write("computation_regions", [(None, None, "computation_roi", {},
                                           _polygon(snapshot["computation_region"]))])
    writer.write("truth_regions", ((None, None, str(feature["id"]), feature,
                                    _polygon(feature["geometry"])) for feature in snapshot["truth"]["features"]))
    writer.write("input_points", _input_rows(reader, batch_size))
    for item in result.candidate_results:
        writer.write("candidate_instances", [(None, None, item.source_key,
                     {"algorithm_id": item.algorithm_id, "instance_id": item.instance_id,
                      "evidence_kind": item.evidence_kind, "evidence_offset": item.evidence_offset,
                      "event_count": len(item.events), "candidate_count": len(item.candidates),
                      "params": asdict(item.params) if is_dataclass(item.params) else item.params}, None)])
        writer.write("turn_events" if item.evidence_kind == "turn" else "cross_events", _evidence_rows(item))
        writer.write("candidate_points", _evidence_rows(item, candidates=True))
    writer.write("candidate_intersections", _cluster_rows(connection, result))
    writer.write("cluster_members", _member_rows(result))
    writer.write("topologies", _topology_rows(result))
    writer.write("crossings", _crossing_rows(result))
    writer.write("observations", _observation_rows(result))
    writer.write("final_faces", _face_rows(connection, result, snapshot["evaluation_region"]))
    # COPY and the following joins share one transaction. Autovacuum cannot
    # see this run yet; stale run_id statistics can estimate one row and pick
    # a quadratic nested loop instead of matching the actual event identities.
    connection.execute(ANALYZE_CANDIDATES_SQL)
    connection.execute(ANALYZE_MEMBERS_SQL)
    connection.execute(ASSIGN_CLUSTERS_SQL, (run_id,))
    connection.execute(ASSIGN_NOISE_SQL, (run_id,))
    connection.execute(MEMBER_GEOMETRY_SQL, (run_id,))
    missing = connection.execute(MISSING_MEMBERS_SQL, (run_id,)).fetchone()[0]
    if missing:
        raise ValueError(f"debug cluster members missing candidate evidence: {missing}")
    report = asdict(result.report)
    required = {"input_points": snapshot["local_point_count"],
                "candidate_instances": len(result.candidate_results),
                "turn_events": report["turn_event_count"],
                "cross_events": report["cross_track_candidate_count"],
                "candidate_points": report["hotspot_event_count"] + report["cross_track_candidate_count"],
                "candidate_intersections": report["cluster_count"], "topologies": len(result.topologies),
                "cluster_members": sum(len(c.members) for c in result.clusters if c.members is not None),
                "crossings": sum(len(c.crossing_points) for c in result.clusters if c.crossing_points is not None),
                "observations": sum(len(b) for b in result.observations),
                "final_faces": report["complete_intersection_count"],
                "truth_regions": len(snapshot["truth"]["features"]),
                "evaluation_regions": 1, "computation_regions": 1}
    if writer.counts != required:
        raise ValueError(f"local debug artifact count mismatch: actual={writer.counts}, required={required}")
    # Final face ROI filtering is a metadata flag; every complete face remains.
    roi_faces = connection.execute(ROI_FACE_COUNT_SQL, (run_id,)).fetchone()[0]
    measured = {**{name: value for name, value in report.items()
                  if name.endswith(("_count", "_counts"))},
                "source_point_count": writer.counts["input_points"],
                "candidate_point_count": writer.counts["candidate_points"], "intersection_count": roi_faces}
    if expected_stats:
        mismatches = {name: {"actual": value, "expected": expected_stats[name]}
                      for name, value in measured.items() if name in expected_stats and expected_stats[name] != value}
        if mismatches:
            raise ValueError(f"debug replay differs from original archived counts: {mismatches}")
    counts = {**writer.counts, "noise_points": connection.execute(NOISE_COUNT_SQL, (run_id,)).fetchone()[0],
              "evaluation_roi_final_faces": roi_faces, "report": report}
    connection.execute(RUN_FINISH_SQL, (Jsonb(_native(counts)), run_id))
    return {"debug_run_id": run_id, "counts": _native(counts)}


def run_local_debug(workspace, scheme, refine_id, inputs, caches, level, threshold, *,
                    debug_postgres, identity=None, batch_size=None, expected_stats=None):
    """Production Detect replay from bounded caches; write only roadnet_debug."""
    if debug_postgres.database != "roadnet_debug":
        raise ValueError("local debug output database must be roadnet_debug")
    if level not in {"L1", "L2", "L3"}:
        raise ValueError("unknown debug level")
    config = threshold_config(workspace.configs[scheme], threshold)
    cache = caches["levels"][level]
    if str(cache["descriptor"].refine_id) != str(refine_id):
        raise ValueError("debug local cache belongs to another Refine parent")
    size = batch_size if batch_size is not None else config.runtime.db_insert_batch
    _LOGGER.info("Local debug replay started: level=%s threshold=%s local_points=%d",
                 level, threshold, cache["point_count"])
    started = time.perf_counter()
    result = None
    try:
        with connect(config.postgres) as source:
            source.read_only = True
            parent = fetch_refine_run(source, str(refine_id))
            if parent.source_table != workspace.case.source_table:
                raise ValueError("debug parent source table differs from the frozen experiment case")
            context = DetectRuntimeContext(parent.source_table, "kept_trajectory_points",
                                           parent.source_table, str(refine_id), None)
            with LocalRefineReader(cache, config.runtime, data_root=config.paths.data_root) as reader:
                result = detect_intersections(conn=source, config=config.detect, context=context,
                    refine_reader=reader, runtime=config.runtime, data_root=config.paths.data_root)
                snapshot = {"parent_refine_id": str(refine_id), "parent_database": config.postgres.database,
                    "parent_refine": asdict(parent), "level": level, "threshold": float(threshold),
                    "scheme": scheme, "parameters": config.snapshot_parameters(),
                    "debug_source_revision": code_revision(), "original_hpo_identity": identity,
                    "local_point_count": cache["point_count"], "local_fragment_count": cache["fragment_count"],
                    "local_cache_key": caches["cache_key"], "local_cache_descriptor": asdict(cache["descriptor"]),
                    "inputs_hash": inputs["sha256"], "evaluation_region": inputs["levels"][level]["evaluation_region"],
                    "inputs_source": inputs.get("source"),
                    "inputs_level_metadata": {key: value for key, value in inputs["levels"][level].items()
                                               if key not in {"evaluation_region", "computation_region", "truth"}},
                    "computation_region": inputs["levels"][level]["computation_region"],
                    "truth": inputs["levels"][level]["truth"], "spatial_context": asdict(result.spatial_context)}
                with connect(debug_postgres) as output:
                    with output.transaction():
                        stored = persist_local_debug(output, reader, result, snapshot,
                                                     batch_size=size, expected_stats=expected_stats)
    finally:
        if result is not None:
            result.close()
    stored.update(level=level, threshold=float(threshold), runtime_s=time.perf_counter()-started,
                  debug_database="roadnet_debug", debug_schema="hpo_debug")
    _LOGGER.info("Local debug replay committed: run_id=%s level=%s threshold=%s runtime_s=%.3f counts=%s",
                 stored["debug_run_id"], level, threshold, stored["runtime_s"], stored["counts"])
    return stored
