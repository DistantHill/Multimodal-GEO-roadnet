"""Compute local Detect faces and commit each evaluation independently."""
from __future__ import annotations

import copy
from dataclasses import asdict
import hashlib
import json
import logging
import math
from pathlib import Path
import time
from uuid import NAMESPACE_URL, uuid5
from shapely.geometry import shape

from pipeline.experiments.archive import ArchiveStore, connect_archive

from ...config import connect
from ...intersections.candidate_geometry import CandidateGeometryNumericalError
from ...intersections.detect_intersections import DetectRuntimeContext, detect_intersections
from ..config import canonical
from ..runner import now, write_manifest
from .adapters import threshold_config
from .config import code_revision
from .local_search import NumericalTrialError


RULES = {"mode": "center_distance", "distance_m": 30.0}
LABEL_SEMANTICS = "location_region"
_LOGGER = logging.getLogger(__name__)
# Scalar geometry calculations use the same expression as the Detect writer.
# There are no table rows to narrow through a spatial index.
FINAL_FACE_SQL = """SELECT ST_AsGeoJSON(ST_Transform(ST_Buffer(
    ST_SetSRID(ST_MakePoint(%s,%s),%s::integer),%s),4326))"""


def final_faces(conn, result, evaluation_region: dict) -> dict:
    """Preserve complete faces using the archive's centroid-covers policy."""
    clusters = {item.cluster_calculate_index: item for item in result.clusters}
    boundary = shape(evaluation_region)
    features = []
    for topology in sorted(result.topologies, key=lambda item: item.cluster_calculate_index):
        if not topology.is_complete:
            continue
        cluster = clusters[topology.cluster_calculate_index]
        radius = topology.clip_radius_m or cluster.radius_m
        if not math.isfinite(radius) or radius <= 0:
            raise ValueError("a complete intersection requires a positive finite clip radius")
        with conn.cursor() as cursor:
            cursor.execute(FINAL_FACE_SQL, (cluster.centroid_x, cluster.centroid_y,
                           result.spatial_context.processing_srid, radius))
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("final intersection scalar geometry returned no row")
        geometry = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if boundary.covers(shape(geometry).centroid):
            identifier = f"intersection-{topology.cluster_calculate_index:012d}"
            features.append({"type": "Feature", "id": identifier,
                             "geometry": geometry,
                             "properties": {"id": identifier,
                                            "observation_count": topology.observation_count}})
    return json.loads(canonical({"type": "FeatureCollection", "features": features}))


class LocalEvaluator:
    """Use bounded cached fragments; never persist a Detect point graph.

    An archive connection and outer transaction belong to exactly one call.
    UUID5 links a coordinator call to its committed evaluation after a crash.
    Infrastructure and input failures propagate; only the geometry solver's
    explicit numerical failure is a parameter-local failed trial.
    """
    def __init__(self, workspace, scheme: str, refine_id: str, inputs: dict,
                 caches: dict, root: str | Path, identity: dict):
        if scheme not in workspace.configs:
            raise ValueError("unknown local HPO scheme")
        if workspace.archive_settings.get("database") != "roadnet_eval":
            raise ValueError("local HPO archive must be roadnet_eval")
        threshold_config(workspace.configs[scheme], 45.)
        self.workspace = copy.deepcopy(workspace)
        self.scheme, self.refine_id = scheme, str(refine_id)
        # Shapely mapping() produces tuple coordinates; PostgreSQL JSONB reads
        # arrays back as lists. Freeze JSON-native snapshots before immutable
        # archive equality checks, including identity and configuration stats.
        self.inputs, self.caches = json.loads(canonical(inputs)), copy.deepcopy(caches)
        self.root = Path(root)
        self.identity = json.loads(canonical(identity))
        self.identity_hash = hashlib.sha256(canonical(identity).encode()).hexdigest()
        for level in ("L1", "L2", "L3"):
            if level not in self.inputs["levels"] or level not in self.caches["levels"]:
                raise ValueError("local evaluator requires all three frozen levels")
            descriptor = self.caches["levels"][level]["descriptor"]
            if str(descriptor.refine_id) != self.refine_id:
                raise ValueError("local cached fragments belong to another Refine run")

    def _experiment_id(self, call_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"roadnet-local-hpo:{self.identity_hash}:{call_id}"))

    def lookup(self, call_id: str) -> dict | None:
        """Return only a committed experiment that also has its evaluation."""
        with connect_archive(self.workspace.archive_settings) as connection:
            archive = ArchiveStore(connection)
            try:
                record = archive.get_experiment(self._experiment_id(call_id))
            except KeyError:
                return None
        if record.get("local_identity_hash") != self.identity_hash or record.get("call_id") != call_id:
            raise ValueError("local evaluation archive identity mismatch")
        evaluations = record.get("evaluations", [])
        if record.get("status") != "completed" or not evaluations:
            raise RuntimeError("local experiment exists without an atomic completed evaluation")
        # ArchiveStore orders newest first. The earliest evaluation is the one
        # committed atomically with this local experiment; UI re-evaluations
        # must not change the coordinator's recovered optimization objective.
        original = evaluations[-1]
        if original["rules"] != {**RULES, "region_policy": "centroid_covers"}:
            raise ValueError("original local evaluation rules or region policy differ from the frozen objective")
        return {**record["local_result"], "metrics": original["metrics"],
                "evaluation_id": original["evaluation_id"]}

    def __call__(self, threshold: float, level: str, call_id: str) -> dict:
        if level not in {"L1", "L2", "L3"}:
            raise ValueError("invalid local evaluation level")
        if not isinstance(call_id, str) or not call_id or Path(call_id).name != call_id or call_id in {".", ".."}:
            raise ValueError("invalid local evaluation call ID")
        recovered = self.lookup(call_id)
        if recovered is not None:
            if recovered["threshold"] != float(threshold) or recovered["level"] != level:
                raise ValueError("committed call ID has a different parameter or level")
            return recovered
        if self.identity.get("source_revision") is not None and self.identity["source_revision"] != code_revision():
            raise ValueError("Pipeline source revision changed; stop this local search and use a new preparation identity")
        config = threshold_config(self.workspace.configs[self.scheme], threshold)
        level_inputs = self.inputs["levels"][level]
        level_cache = self.caches["levels"][level]
        experiment_id = self._experiment_id(call_id)
        directory = self.root / "calls" / call_id
        directory.mkdir(parents=True, exist_ok=True)
        started_at, started = now(), time.perf_counter()
        _LOGGER.info("Local Detect started: call_id=%s threshold=%s level=%s local_points=%d",
                     call_id, threshold, level, level_cache["point_count"])
        from .local_refine import LocalRefineReader
        result = None
        try:
            with connect(config.postgres) as connection:
                context = DetectRuntimeContext(
                    db_table=self.workspace.case.source_table, kept_table="kept_trajectory_points",
                    source_table=self.workspace.case.source_table, refine_id=self.refine_id,
                    live_status=None,
                )
                with LocalRefineReader(level_cache, config.runtime,
                                       data_root=config.paths.data_root) as reader:
                    try:
                        result = detect_intersections(
                            conn=connection, connect_worker=lambda: connect(config.postgres),
                            config=config.detect, context=context, refine_reader=reader,
                            runtime=config.runtime, data_root=config.paths.data_root,
                        )
                    except CandidateGeometryNumericalError as error:
                        _LOGGER.warning("Local Detect numerical failure: call_id=%s threshold=%s level=%s diagnostics=%s",
                                        call_id, threshold, level, error.diagnostics)
                        raise NumericalTrialError(str(error)) from error
                    predictions = final_faces(connection, result, level_inputs["evaluation_region"])
                    statistics = asdict(result.report)
                # This connection is only the read/computation source. The archive
                # below owns a separate, independently committed transaction.
        finally:
            if result is not None:
                result.close()
        elapsed = time.perf_counter() - started
        statistics.update(
            elapsed_s=elapsed, source_point_count=level_cache["point_count"],
            candidate_point_count=statistics["hotspot_event_count"]
                                  + statistics["cross_track_candidate_count"],
            intersection_count=len(predictions["features"]),
        )
        statistics = json.loads(canonical(statistics))
        base_result = {"experiment_id": experiment_id, "threshold": float(threshold),
                       "level": level, "call_id": call_id, "refine_id": self.refine_id,
                       "prediction_count": len(predictions["features"]),
                       "candidate_count": statistics["hotspot_event_count"]
                                          + statistics["cross_track_candidate_count"],
                       "runtime_s": elapsed, "stats": statistics,
                       "artifact_dir": str(directory), "input_hash": self.inputs.get("sha256"),
                       "cache_key": self.caches.get("cache_key"),
                       "effective_config_hash": hashlib.sha256(canonical(config.snapshot_parameters()).encode()).hexdigest()}
        revision = self.identity.get("source_revision")
        commit = revision.split(":source-sha256:", 1)[0] if isinstance(revision, str) else None
        record = {"id": experiment_id,
                  "batch_id": str(uuid5(NAMESPACE_URL, f"roadnet-local-hpo:{self.identity_hash}")),
                  "commit": commit, "case_name": self.workspace.case.name,
                  "scheme_name": self.scheme, "status": "completed", "algorithm": "detect",
                  "refine_id": self.refine_id, "detect_id": None,
                  "started_at": started_at, "finished_at": now(), "stats": statistics,
                  "parameters": config.snapshot_parameters(), "call_id": call_id,
                  "local_identity_hash": self.identity_hash, "local_identity": self.identity,
                  "local_result": base_result, "workspace_run_path": str(directory),
                  "evaluation_region": level_inputs["evaluation_region"],
                  "computation_region": level_inputs["computation_region"],
                  "region_policy": "original_complete_face_centroid_covers",
                  "case_snapshot": {**self.workspace.case.snapshot(),
                                    "geometry": level_inputs["evaluation_region"]},
                  "workspace_case_snapshot": self.workspace.case.snapshot(),
                  "source_database": config.postgres.database,
                  "lightweight_local_detect": True, "resolved_points_persisted": False}
        record = json.loads(canonical(record))
        write_manifest(directory / "manifest.json", record)
        write_manifest(directory / "predictions.geojson", predictions)
        write_manifest(directory / "truth.geojson", level_inputs["truth"])
        with connect_archive(self.workspace.archive_settings) as connection:
            with connection.transaction():
                archive = ArchiveStore(connection)
                archive.save_experiment(record, predictions)
                evaluated = archive.append_evaluation(experiment_id, level_inputs["truth"], RULES)
        accepted = {**base_result, "metrics": evaluated["metrics"],
                    "evaluation_id": evaluated["evaluation_id"]}
        _LOGGER.info("Local evaluation archived: call_id=%s experiment_id=%s level=%s threshold=%s "
                     "f1=%s tp=%s fp=%s fn=%s runtime_s=%.3f predictions=%d",
                     call_id, experiment_id, level, threshold,
                     accepted["metrics"]["f1"], accepted["metrics"]["tp"],
                     accepted["metrics"]["fp"], accepted["metrics"]["fn"], elapsed,
                     accepted["prediction_count"])
        # If an artifact write fails here, lookup still recovers the committed call.
        write_manifest(directory / "result.json", accepted)
        return accepted
