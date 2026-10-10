"""Sequential Detect experiments, immutable local snapshots and archive bridge."""
from __future__ import annotations

import copy
from contextlib import nullcontext
import json
import os
import resource
import subprocess
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import connect
from ..db.pipeline_store import PostgresPipelineStore
from ..db.schema import require_current_schema
from ..projection import create_spatial_context
from ..stages.detect import run_detect_stage
from ..stages.refine import load_completed_refine_ref, run_refine_stage
from ..telemetry.console_logging import init_logger, shutdown_logger
from .config import ExperimentWorkspace, canonical, independent_instance_configs, refine_identity, refine_key


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_manifest(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def code_commit() -> str | None:
    configured = os.environ.get("PIPELINE_GIT_COMMIT") or os.environ.get("PIPELINE_CODE_COMMIT")
    if configured:
        return configured
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class TrackingStore(PostgresPipelineStore):
    refine_id: str | None = None
    detect_id: str | None = None

    def start_refine_run(self, **kwargs: Any) -> str:
        self.refine_id = super().start_refine_run(**kwargs)
        return self.refine_id

    def start_detect_run(self, **kwargs: Any) -> str:
        self.detect_id = super().start_detect_run(**kwargs)
        return self.detect_id


def predictions_from_detect(config: Any, detect_id: str, case: Any) -> dict[str, Any]:
    with connect(config.postgres) as conn, conn.cursor() as cursor:
        cursor.execute("""SELECT id, ST_AsGeoJSON(geom), observation_count FROM intersections
            WHERE detect_id=%s AND ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),ST_Centroid(geom)) ORDER BY id""",
            (detect_id, canonical(case.geometry)))
        features = [{"type": "Feature", "id": str(row[0]), "geometry": json.loads(row[1]),
                     "properties": {"id": str(row[0]), "observation_count": row[2]}} for row in cursor.fetchall()]
    return {"type": "FeatureCollection", "features": features}


def source_and_candidate_stats(conn: Any, refine_id: str, detect_id: str, *, time_start=None, time_end=None) -> dict[str, int]:
    """Count run-scoped rows, resolving source timestamps only for a time slice."""
    with conn.cursor() as cursor:
        cursor.execute("""SELECT
            (SELECT COUNT(*) FROM kept_trajectory_points WHERE refine_id=%s) +
            (SELECT COUNT(*) FROM excluded_points WHERE refine_id=%s),
            (SELECT COUNT(*) FROM trun_hot_ponits WHERE detect_id=%s),
            (SELECT COUNT(*) FROM tracks_cross WHERE detect_id=%s)""",
            (refine_id, refine_id, detect_id, detect_id))
        source, hotspot, cross = cursor.fetchone()
    if time_start is not None or time_end is not None:
        from ..time_selection import time_bounds
        from ..db.source_table import validate_source_table
        start, end = time_bounds(time_start, time_end)
        with conn.cursor() as cursor:
            cursor.execute("SELECT source_table FROM refine_runs WHERE id=%s", (refine_id,))
            contract = validate_source_table(conn, cursor.fetchone()[0])
            cursor.execute(f"""SELECT count(*) FROM {contract.quoted_table} s
                JOIN (SELECT source_row_key FROM kept_trajectory_points WHERE refine_id=%s
                      UNION ALL SELECT source_row_key FROM excluded_points WHERE refine_id=%s) members
                  ON members.source_row_key=s.id
                WHERE s.timestamp >= %s AND s.timestamp < %s""",
                (refine_id, refine_id, start.timestamp() if start else float("-inf"),
                 end.timestamp() if end else float("inf")))
            source = cursor.fetchone()[0]
    return {"source_point_count": int(source), "hotspot_event_count": int(hotspot),
            "cross_track_candidate_count": int(cross), "candidate_point_count": int(hotspot + cross)}


def candidate_instance_stats(conn, detect_id: str) -> list[dict[str, Any]]:
    """Keep algorithm instances distinct in archive statistics."""
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT algorithm_id,instance_id,params,evidence_kind,evidence_count,candidate_count "
            "FROM detect_candidate_instances WHERE detect_id=%s ORDER BY evidence_kind,evidence_offset",
            (detect_id,),
        )
        return [dict(zip(("algorithm_id", "instance_id", "params", "evidence_kind", "evidence_count", "candidate_count"), row))
                for row in cursor.fetchall()]


def archive_local(workspace: ExperimentWorkspace, run_directory: Path) -> dict[str, Any]:
    from pipeline.experiments.archive import ArchiveStore, connect_archive
    record = json.loads((run_directory / "manifest.json").read_text(encoding="utf-8"))
    predictions_path = run_directory / "predictions.geojson"
    predictions = json.loads(predictions_path.read_text()) if predictions_path.exists() else {"type":"FeatureCollection", "features":[]}
    archived_record = {key: value for key, value in record.items() if key not in {"archive_state", "archive_error", "evaluation_error"}}
    with connect_archive(workspace.archive_settings) as conn:
        archive = ArchiveStore(conn)
        archive.initialize()
        archive.save_experiment(archived_record, predictions)
    record["archive_state"] = "archived"
    write_manifest(run_directory / "manifest.json", record)
    return record


def evaluate_local(workspace: ExperimentWorkspace, experiment_id: str, *, truth_file: Path | None = None,
                   rules: dict[str, Any] | None = None) -> Any:
    from pipeline.experiments.archive import ArchiveStore, connect_archive
    with connect_archive(workspace.archive_settings) as conn:
        archive = ArchiveStore(conn)
        archive.initialize()
        record = archive.get_experiment(experiment_id)
        if truth_file is None:
            frozen = Path(record["workspace_run_path"]) / "truth.geojson"
            truth_file = frozen if frozen.is_file() else None
        if truth_file is None:
            raise ValueError("truth_file is required for evaluation")
        truth = json.loads(truth_file.read_text(encoding="utf-8"))
        if truth.get("type") != "FeatureCollection":
            raise ValueError("truth must be a GeoJSON FeatureCollection")
        return archive.append_evaluation(experiment_id, truth, rules or workspace.evaluation)


def run_batch(workspace: ExperimentWorkspace, *, explicit_refine_id: str | None = None, independent_instances: bool = False) -> list[dict[str, Any]]:
    commit = code_commit()
    if not commit:
        raise ValueError("current commit is unavailable; use make pipeline-experiment or set PIPELINE_GIT_COMMIT")
    for config in workspace.configs.values():
        for directory in (config.paths.data_root, config.paths.parquet_directory, config.logging.directory):
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise ValueError(f"runtime path is not a directory: {directory}")
    # Validate shared input before starting a scheme; configuration errors abort the batch.
    from ..db.source_table import validate_source_table
    first_config = workspace.configs[workspace.schemes[0]]
    with connect(first_config.postgres) as conn:
        require_current_schema(conn)
        validate_source_table(conn, workspace.case.source_table)
        if explicit_refine_id:
            store = PostgresPipelineStore(conn)
            record = store.get_refine_run(explicit_refine_id)
            if record.source_table != workspace.case.source_table:
                raise ValueError("explicit Refine source_table does not match case")
            load_completed_refine_ref(store=store, refine_id=explicit_refine_id, record=record,
                                      time_start=workspace.case.time_start, time_end=workspace.case.time_end)
    batch_id = str(uuid4())
    workspace.root.mkdir(parents=True, exist_ok=True)
    batch_root = workspace.root / "batches" / batch_id
    batch_root.mkdir(parents=True)
    batch = {"id": batch_id, "case": workspace.case.name, "schemes": list(workspace.schemes), "started_at": now()}
    write_manifest(batch_root / "manifest.json", batch)
    shared_refines: dict[str, Any] = {}
    records = []
    plans = []
    for scheme in workspace.schemes:
        configured = workspace.configs[scheme]
        if independent_instances:
            plans.extend((scheme, isolated, {"algorithm_id": algorithm, "instance_id": instance,
                          "params": asdict(params)})
                         for algorithm, instance, params, isolated in independent_instance_configs(configured))
        else:
            plans.append((scheme, configured, None))
    cache_context = nullcontext(None)
    if independent_instances:
        from ..plugins.execution import DetectExecutionCache
        requests = {}
        for _, planned, _ in plans:
            for algorithm in planned.detect.candidates:
                names = requests.setdefault(algorithm, [])
                for instance in planned.detect.candidate[algorithm].output_instances:
                    if instance not in names:
                        names.append(instance)
        cache_context = DetectExecutionCache({algorithm: tuple(names) for algorithm, names in requests.items()})
    with cache_context as candidate_cache:
        for scheme, config, instance_identity in plans:
            experiment_id = str(uuid4())
            run_root = workspace.root / "runs" / experiment_id
            run_root.mkdir(parents=True)
            config = replace(config, logging=replace(config.logging, directory=run_root / "logs"))
            record = {"id": experiment_id, "batch_id": batch_id, "case_name": workspace.case.name,
                      "scheme_name": scheme, "status": "running", "commit": commit,
                      "case_snapshot": workspace.case.snapshot(), "parameters": config.snapshot_parameters(),
                      "source_database": config.postgres.database, "refine_id": None, "detect_id": None,
                      "started_at": now(), "finished_at": None, "stats": {}, "error": None,
                      "workspace_run_path": str(run_root), "archive_state": "pending"}
            record["candidate_selection"] = {
                algorithm: {"output_instances": list(config.detect.candidate[algorithm].output_instances),
                            "params": {name: asdict(params) for name, params in config.detect.candidate[algorithm].params.items()}}
                for algorithm in config.detect.candidates}
            record["instance_evaluation"] = instance_identity
            # Keep effective TOML payload for editing again, independent of current draft.
            restored = copy.deepcopy(workspace.payload)
            (run_root / "field_specs.toml").write_bytes(config.parser.field_specs_path.read_bytes())
            restored["parser"]["field_specs_path"] = str(run_root / "field_specs.toml")
            restored["experiment"] = {**restored.get("experiment", {}), "case": workspace.case.name, "schemes": [scheme]}
            restored["cases"][workspace.case.name] = {"source_table": workspace.case.source_table,
                "time_start": workspace.case.time_start.isoformat() if workspace.case.time_start else "", "time_end": workspace.case.time_end.isoformat() if workspace.case.time_end else "",
                "region_file": str(run_root / "region.geojson")}
            if workspace.case.truth_file:
                restored["cases"][workspace.case.name]["truth_file"] = str(run_root / "truth.geojson")
            restored["detect"] = config.detect.as_dict()
            restored["schemes"] = {scheme: {"detect": config.detect.as_dict()}}
            record["draft_snapshot"] = restored
            write_manifest(run_root / "manifest.json", record)
            write_manifest(run_root / "region.geojson", {"type": "Feature", "geometry": workspace.case.geometry, "properties": {}})
            if workspace.case.region_file:
                (run_root / "region-source.geojson").write_bytes(workspace.case.region_file.read_bytes())
            if workspace.case.truth_file:
                (run_root / "truth.geojson").write_bytes(workspace.case.truth_file.read_bytes())
            init_logger(config.logging.directory)
            started = time.perf_counter()
            store = None
            try:
                with connect(config.postgres) as conn:
                    store = TrackingStore(conn)
                    parameters = config.snapshot_parameters()
                    parameters["source_selection"] = workspace.case.snapshot()
                    parameters["experiment_input"] = refine_identity(workspace.case, config)
                    parameters["experiment_id"] = experiment_id
                    key = refine_key(workspace.case, config)
                    if explicit_refine_id:
                        refine_ref = load_completed_refine_ref(store=store, refine_id=explicit_refine_id,
                                                              time_start=workspace.case.time_start, time_end=workspace.case.time_end)
                    elif key in shared_refines:
                        refine_ref = shared_refines[key]
                    else:
                        refined = run_refine_stage(store=store, source_table=workspace.case.source_table,
                            spatial_context=create_spatial_context(config.projection, processing_srid=config.projection.processing_srid),
                            postgres_config=config.postgres, parser_config=config.parser, refine_config=config.refine,
                            runtime_config=config.runtime, paths_config=config.paths, parameters=parameters,
                            emit=lambda event: None, selection=workspace.case)
                        refine_ref = refined.ref
                        shared_refines[key] = refine_ref
                        record["stats"]["source_point_count"] = refined.source_point_count
                    record["refine_id"] = refine_ref.refine_id
                    write_manifest(run_root / "manifest.json", record)
                    detected = run_detect_stage(store=store, postgres_config=config.postgres, refine=refine_ref,
                        detect_config=config.detect, runtime_config=config.runtime, paths_config=config.paths,
                        verify_sha256=config.resources.verify_artifact_sha256, parameters=parameters, emit=lambda event: None,
                        candidate_cache=candidate_cache)
                    record["detect_id"] = detected.ref.detect_id
                    record["stats"].update(asdict(detected.persisted))
                    record["stats"]["candidate_instances"] = candidate_instance_stats(conn, detected.ref.detect_id)
                    record["stats"].update(source_and_candidate_stats(conn, refine_ref.refine_id, detected.ref.detect_id,
                        **({"time_start": workspace.case.time_start, "time_end": workspace.case.time_end}
                           if explicit_refine_id and (workspace.case.time_start or workspace.case.time_end) else {})))
                predictions = predictions_from_detect(config, record["detect_id"], workspace.case)
                write_manifest(run_root / "predictions.geojson", predictions)
                record["status"] = "completed"
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}: {exc}"
                if store:
                    record["refine_id"] = record["refine_id"] or store.refine_id
                    record["detect_id"] = record["detect_id"] or store.detect_id
            finally:
                record["finished_at"] = now()
                record["stats"]["elapsed_s"] = time.perf_counter() - started
                record["stats"]["process_peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
                record["stats"]["rss_scope"] = "process_lifetime"
                write_manifest(run_root / "manifest.json", record)
                shutdown_logger()
            try:
                record = archive_local(workspace, run_root)
                if record["status"] == "completed" and workspace.case.truth_file:
                    try:
                        evaluate_local(workspace, record["id"], truth_file=run_root / "truth.geojson")
                    except Exception as exc:
                        record["evaluation_error"] = f"{type(exc).__name__}: {exc}"
                        write_manifest(run_root / "manifest.json", record)
            except Exception as exc:
                record["archive_error"] = f"{type(exc).__name__}: {exc}"
                write_manifest(run_root / "manifest.json", record)
            records.append(record)
            print(json.dumps({k:record.get(k) for k in ("id", "scheme_name", "status", "archive_state", "error", "archive_error", "evaluation_error")}, ensure_ascii=False))
    batch.update(finished_at=now(), experiments=[record["id"] for record in records])
    write_manifest(batch_root / "manifest.json", batch)
    return records
