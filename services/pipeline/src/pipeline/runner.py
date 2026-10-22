"""The five-stage entry point and isolated experiment worker boundary."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import importlib.metadata
import json
import multiprocessing
from pathlib import Path
import uuid

from . import _native
from .artifacts import digest, sha256, write_json
from .config import validate_config
from .refine import run_refine
from .detect import run_detect
from .clip import run_clip
from .fit import run_fit
from .network import run_network
from .resources import ResourceLimitExceeded, ResourceMonitor
from .source import PreparedSource, prepare_source


def monitor_options(config: dict) -> dict:
    return {name: config["resources"][name] for name in (
        "sample_interval_ms", "soft_memory_mb", "max_wall_s", "include_children", "heap_profile")}


def run_pipeline(config: dict, config_path: Path, *, source: PreparedSource | None = None,
                 region=None, output_dir: Path | None = None) -> dict:
    config = validate_config(config)
    if source is not None and source.crs != config["input"]["processing_crs"]:
        raise ValueError("Prepared input CRS differs from input.processing_crs")
    run_id = uuid.uuid4().hex
    root = Path(output_dir) if output_dir is not None else Path(config["output"]["root"]) / "runs" / run_id
    root.mkdir(parents=True, exist_ok=True)
    if (root / "run.json").exists():
        raise ValueError(f"Run directory already has an attempt: {root}; choose a fresh directory")
    versions = {}
    for name in ("roadnet-pipeline", "duckdb", "pyarrow", "numpy", "pyproj", "shapely", "optuna"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "source checkout"
    manifest = {"version": 1, "run_id": run_id, "status": "RUNNING",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "config_source": str(Path(config_path).resolve()), "config_hash": digest(config),
                "native_sha256": sha256(Path(_native.__file__)), "versions": versions,
                "region_id": region.id if region is not None else None,
                "region_bounds": list(region.bounds) if region is not None else None,
                "input_prepared_outside_run": source is not None, "stages": {}}
    write_json(root / "effective-config.json", config)
    write_json(root / "run.json", manifest)
    try:
        with ResourceMonitor(root, **monitor_options(config)) as monitor:
            if source is None:
                with monitor.stage("input") as span:
                    source = prepare_source(config, Path(config["output"]["root"]), monitor)
                    span.add_counts(input_rows=source.rows + source.rejected_rows,
                                    output_rows=source.rows, rejected_rows=source.rejected_rows)
            manifest["source"] = {"path": str(source.path), **source.identity}
            manifest["stages"]["refine"] = run_refine(config, run_id, root / "refine", source, region, monitor)
            manifest["stages"]["detect"], intersections = run_detect(config, run_id, root / "detect", root / "refine", monitor)
            manifest["stages"]["clip"] = run_clip(config, run_id, root / "clip", root / "refine", intersections, monitor)
            manifest["stages"]["fit"], centerlines = run_fit(config, run_id, root / "fit", root / "clip", monitor)
            manifest["stages"]["network"], network = run_network(config, run_id, root / "network", intersections, centerlines, monitor)
            if config["database"]["enabled"]:
                from .database import publish
                with monitor.stage("publish") as span, span.phase("write"):
                    manifest["database"] = publish(config, intersections, centerlines)
            else:
                manifest["database"] = {"status": "DISABLED"}
        metrics = monitor.summary()
        manifest.update(status="COMPLETE", completed_at=datetime.now(timezone.utc).isoformat(),
                        resources="resources.json", counts={"intersections": len(intersections),
                            "centerlines": len(centerlines), "nodes": len(network["nodes"]), "edges": len(network["edges"])})
        write_json(root / "run.json", manifest)
        if region is None:
            write_json(Path(config["output"]["root"]) / "latest.json", {"run_id": run_id, "run_dir": str(root)})
        return {"run_id": run_id, "run_dir": str(root), "intersections": intersections,
                "centerlines": centerlines, "network": network, "manifest": manifest, "metrics": metrics}
    except BaseException as error:
        native_limit = isinstance(error, getattr(_native, "NativeResourceLimitExceeded", ()))
        limited = native_limit or isinstance(error, ResourceLimitExceeded)
        manifest.update(status="RESOURCE_LIMIT" if limited else "FAILED", error_type=type(error).__name__)
        write_json(root / "run.json", manifest)
        if native_limit:
            raise ResourceLimitExceeded(str(error)) from error
        raise


def _worker(config: dict, config_path: str, source: PreparedSource, region, output_dir: str):
    root = Path(output_dir)
    try:
        result = run_pipeline(config, Path(config_path), source=source, region=region, output_dir=root)
        write_json(root / "worker-result.json", result)
    except BaseException as error:
        write_json(root / "worker-error.json", {"type": type(error).__name__,
                   "resource_limit": isinstance(error, ResourceLimitExceeded),
                   "message": str(error) if isinstance(error, (ResourceLimitExceeded, ValueError)) else "Worker failed; inspect run and resource manifests"})


def run_isolated(config: dict, config_path: Path, *, source: PreparedSource,
                 region, output_dir: Path) -> dict:
    """One fresh process per region attempt avoids retained heaps across trials."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if any((output_dir / name).exists() for name in ("worker-error.json", "worker-result.json", "run.json")):
        raise ValueError("Isolated worker requires a fresh attempt directory")
    process = multiprocessing.get_context("spawn").Process(
        target=_worker, args=(deepcopy(config), str(config_path), source, region, str(output_dir)))
    process.start()
    try:
        process.join()
    except BaseException:
        process.terminate()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join()
        raise
    error_path = output_dir / "worker-error.json"
    if error_path.exists():
        error = json.loads(error_path.read_text())
        if error["resource_limit"]:
            raise ResourceLimitExceeded(error["message"])
        raise RuntimeError(f"{error['type']}: {error['message']}")
    path = output_dir / "worker-result.json"
    if process.exitcode != 0 or not path.is_file():
        raise RuntimeError(f"Pipeline worker exited {process.exitcode}; it may have been killed externally")
    return json.loads(path.read_text())
