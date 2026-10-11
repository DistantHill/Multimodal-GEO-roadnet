"""Validated settings for the isolated turn-threshold experiment coordinator."""
from __future__ import annotations

import hashlib
import importlib.metadata
import math
import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HPOConfig:
    config_path: Path
    experiment_config: Path
    scheme: str
    regions_file: Path
    output_root: Path
    seed: int
    label_version: str
    trajectory_snapshot: str
    source_revision: str
    threshold_min: float
    threshold_max: float
    baseline: float
    l1_positive_count: int
    l2_positive_count: int
    l2_negative_count: int
    search: dict[str, Any]
    labels_file: Path | None = None
    labels_layer: str = "labeled_intersections"
    labels_id_field: str = "fid"
    regions_layer: str = "regions"
    negative_regions_file: Path | None = None
    negative_regions_layer: str = "negative_regions"
    region_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


def code_revision() -> str:
    """Include the working source hash: a commit alone omits uncommitted edits."""
    root = Path(__file__).resolve().parents[4]
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    lock = root.parent / "requirements.lock"
    if lock.is_file():
        digest.update(lock.read_bytes())
    for package in ("numpy", "shapely", "pyproj", "duckdb", "optuna", "sqlalchemy"):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "unavailable"
        digest.update(f"{package}={version}".encode())
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = os.environ.get("PIPELINE_GIT_COMMIT", "no-commit")
    return f"{commit}:source-sha256:{digest.hexdigest()}"


def load_hpo_config(path: str | Path) -> HPOConfig:
    path = Path(path).resolve()
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(payload) - {"hpo", "search", "region_metadata"}:
        raise ValueError("HPO config accepts only hpo, search and region_metadata tables")
    raw = payload.get("hpo", {})
    required = {"experiment_config", "scheme", "regions_file", "output_root", "label_version",
                "trajectory_snapshot", "threshold_min", "threshold_max", "baseline",
                "l1_positive_count", "l2_positive_count", "l2_negative_count"}
    allowed = required | {"seed", "source_revision", "labels_file", "labels_layer", "labels_id_field", "regions_layer", "negative_regions_file", "negative_regions_layer"}
    if set(raw) - allowed or required - set(raw):
        raise ValueError(f"HPO settings missing={sorted(required-set(raw))}, unknown={sorted(set(raw)-allowed)}")
    for key in ("scheme", "label_version", "trajectory_snapshot"):
        if not isinstance(raw[key], str) or not raw[key].strip() or raw[key].startswith("REQUIRED"):
            raise ValueError(f"{key} must be a nonempty, actual identifier")
    resolved = {}
    for key in ("experiment_config", "regions_file", "output_root"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise ValueError(f"{key} must be a path")
        resolved[key] = (path.parent / raw[key]).resolve()
    for key in ("threshold_min", "threshold_max", "baseline"):
        if type(raw[key]) not in (int, float) or not math.isfinite(raw[key]) or not 0 <= raw[key] <= 180:
            raise ValueError(f"{key} must be a finite angle in [0, 180]")
    if not raw["threshold_min"] < raw["threshold_max"] or not raw["threshold_min"] <= raw["baseline"] <= raw["threshold_max"]:
        raise ValueError("threshold_min < threshold_max and baseline within range are required")
    for key in ("l1_positive_count", "l2_positive_count", "l2_negative_count"):
        if type(raw[key]) is not int or raw[key] <= 0:
            raise ValueError(f"{key} must be a positive integer region count")
    if raw["l1_positive_count"] > raw["l2_positive_count"]:
        raise ValueError("L1 must be nested within L2")
    seed = raw.get("seed", 42)
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    search = payload.get("search", {})
    if not isinstance(search, dict):
        raise ValueError("search must be a table")
    labels_file = None
    if raw.get("labels_file") is not None:
        if not isinstance(raw["labels_file"], str) or not raw["labels_file"].strip():
            raise ValueError("labels_file must be a path")
        labels_file = (path.parent / raw["labels_file"]).resolve()
    for field in ("labels_layer", "labels_id_field"):
        if field in raw and (not isinstance(raw[field], str) or not raw[field].strip()):
            raise ValueError(f"{field} must be a nonempty identifier")
        if field in raw and labels_file is None:
            raise ValueError(f"{field} requires labels_file")
    negative_regions_file = None
    if raw.get("negative_regions_file") is not None:
        value = raw["negative_regions_file"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("negative_regions_file must be a path")
        negative_regions_file = (path.parent / value).resolve()
    region_metadata = payload.get("region_metadata", {})
    if not isinstance(region_metadata, dict) or any(not isinstance(v, dict) for v in region_metadata.values()):
        raise ValueError("region_metadata must map region IDs to metadata tables")
    for name in ("regions_layer", "negative_regions_layer"):
        if name in raw and (not isinstance(raw[name], str) or not raw[name].strip()):
            raise ValueError(f"{name} must be a nonempty layer name")
    # User-provided IDs are supplemental provenance, never a replacement for code hashing.
    revision = code_revision()
    if raw.get("source_revision"):
        revision = f"{raw['source_revision']}:{revision}"
    return HPOConfig(path, **resolved, scheme=raw["scheme"], seed=seed,
                     label_version=raw["label_version"], trajectory_snapshot=raw["trajectory_snapshot"],
                     source_revision=revision, threshold_min=float(raw["threshold_min"]),
                     threshold_max=float(raw["threshold_max"]), baseline=float(raw["baseline"]),
                     l1_positive_count=raw["l1_positive_count"], l2_positive_count=raw["l2_positive_count"],
                     l2_negative_count=raw["l2_negative_count"], search=search, labels_file=labels_file,
                     labels_layer=raw.get("labels_layer", "labeled_intersections"),
                     labels_id_field=raw.get("labels_id_field", "fid"),
                     regions_layer=raw.get("regions_layer", "regions"),
                     negative_regions_file=negative_regions_file,
                     negative_regions_layer=raw.get("negative_regions_layer", "negative_regions"),
                     region_metadata=region_metadata)
