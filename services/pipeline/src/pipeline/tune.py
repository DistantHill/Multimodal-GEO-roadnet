"""Restartable Optuna experiments over fixed, cumulatively scored regions.

The journal is an experiment artifact, not a trajectory/result database. Every
trial uses the same ordered regions; only completed trials enter final ranking.
Trial pruning skips remaining region pipeline executions, not arbitrary stages.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import random
import re
import time
from typing import Any, Callable

import optuna
import tomlkit

from .evaluation import aggregate_scores, objective_value, score_region
from .labels import LabelSet, Region, canonical_hash, intersection_truths, load_labels, prepare_labels, write_json
from .resources import ResourceLimitExceeded


RunRegion = Callable[[dict[str, Any], Region, Path], dict[str, Any]]


class RegionMedianPruner(optuna.pruners.BasePruner):
    """Compare the current cumulative prefix with completed peers at that prefix.

    Standard MedianPruner uses the trial's best intermediate value so far. That
    assumption is unsuitable for non-monotone scores on growing region sets.
    """

    def __init__(self, startup_trials: int, warmup_steps: int):
        self.startup_trials = startup_trials
        self.warmup_steps = warmup_steps

    def prune(self, study: optuna.Study, trial: optuna.trial.FrozenTrial) -> bool:
        import statistics
        step = trial.last_step
        if step is None or step < self.warmup_steps:
            return False
        completed = study.get_trials(deepcopy=False, states=(optuna.trial.TrialState.COMPLETE,))
        if len(completed) < self.startup_trials:
            return False
        peers = [other.intermediate_values[step] for other in completed
                 if step in other.intermediate_values and math.isfinite(other.intermediate_values[step])]
        if not peers:
            return False
        return trial.intermediate_values[step] < statistics.median(peers)


def validate_search_space(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tuning = config.get("tuning", {})
    objective = tuning.get("objective", "intersection_f1")
    if objective not in {"intersection_f1", "centerline_f1", "combined_f1"}:
        raise ValueError("unknown tuning objective")
    space = tuning.get("search_space", {})
    if not isinstance(space, dict) or not space:
        raise ValueError("tuning.search_space must contain at least one parameter")
    for name, spec in space.items():
        parts = name.split(".")
        if (len(parts) != 2 or parts[0] not in {"refine", "detect", "clip", "fit", "network"}
                or parts[1] not in config.get(parts[0], {})):
            raise ValueError(f"search parameter must name an existing algorithm setting: {name}")
        if objective == "intersection_f1" and parts[0] in {"clip", "fit", "network"}:
            raise ValueError(f"{name} cannot affect intersection_f1; use labeled centerlines and a line objective")
        if parts[0] == "network":
            raise ValueError("network parameters need a topology objective, which is not yet implemented")
        if name in {"clip.include_boundary_clips", "fit.emit_directional"}:
            raise ValueError(f"{name} controls diagnostic exports, not the scored physical-road geometry")
        if not isinstance(spec, dict) or spec.get("type") not in {"float", "int", "categorical"}:
            raise ValueError(f"search space {name} requires type float, int, or categorical")
        kind = spec["type"]
        allowed = {"type", "choices"} if kind == "categorical" else {"type", "low", "high", "step", "log"}
        if set(spec) - allowed:
            raise ValueError(f"unknown search options for {name}: {sorted(set(spec) - allowed)}")
        current = config[parts[0]][parts[1]]
        if kind == "categorical":
            choices = spec.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"{name}.choices must be a nonempty list")
            if any(type(choice) is not type(current) for choice in choices):
                raise ValueError(f"{name}.choices must have the same type as the current parameter")
            if any(isinstance(choice, float) and not math.isfinite(choice) for choice in choices):
                raise ValueError(f"{name}.choices must be finite")
            if len({canonical_hash(choice) for choice in choices}) != len(choices):
                raise ValueError(f"{name}.choices contains duplicates")
            continue
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError(f"{name} is not a numeric algorithm parameter")
        if kind == "float" and isinstance(current, int):
            raise ValueError(f"{name} is integer-valued; use type=int")
        if kind == "int" and type(current) is not int:
            raise ValueError(f"{name} is not integer-valued")
        for bound in ("low", "high"):
            value = spec.get(bound)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or kind == "int" and type(value) is not int):
                raise ValueError(f"{name}.{bound} is not a valid {kind}")
        if spec["low"] > spec["high"]:
            raise ValueError(f"{name}.low exceeds high")
        if "log" in spec and type(spec["log"]) is not bool:
            raise ValueError(f"{name}.log must be Boolean")
        if spec.get("log") and (spec["low"] <= 0 or "step" in spec):
            raise ValueError(f"{name}: log search requires low>0 and no explicit step")
        if "step" in spec and (isinstance(spec["step"], bool) or not isinstance(spec["step"], (int, float))
                or not math.isfinite(spec["step"]) or spec["step"] <= 0
                or kind == "int" and type(spec["step"]) is not int):
            raise ValueError(f"{name}.step must be positive and match its parameter type")
    return space


def _suggest(trial: optuna.Trial, config: dict[str, Any], space: dict[str, dict]) -> dict[str, Any]:
    effective = copy.deepcopy(config)
    for name, spec in sorted(space.items()):
        kind = spec["type"]
        if kind == "categorical":
            value = trial.suggest_categorical(name, spec["choices"])
        else:
            arguments = {key: value for key, value in spec.items() if key != "type"}
            value = (trial.suggest_int if kind == "int" else trial.suggest_float)(name, **arguments)
        section, field = name.split(".")
        effective[section][field] = value
    # No experiment trial writes to PostgreSQL. Only an explicit best/full run
    # may publish the final human-facing feature layers.
    if "database" in effective:
        effective["database"]["enabled"] = False
    return effective


def _code_identity() -> dict[str, Any]:
    root = Path(__file__).parent
    result = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(root.glob("*.py"))}
    try:
        from . import _native
        binary = Path(_native.__file__)
        result["native_binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    except ImportError:
        result["native_binary_sha256"] = "not-importable"
    versions = {}
    for name in ("optuna", "numpy", "scipy", "pyarrow", "shapely", "pyproj", "pyogrio"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return {"files": result, "versions": versions, "controller_version": 1}


@contextmanager
def _coordinator_lock(root: Path):
    with (root / "coordinator.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another coordinator owns this study") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _trials_export(study: optuna.Study, root: Path) -> None:
    records = [{"number": trial.number, "state": trial.state.name, "value": trial.value,
                "params": trial.params,
                "intermediate_values": {str(step): value for step, value in trial.intermediate_values.items()},
                "user_attrs": trial.user_attrs} for trial in study.trials]
    write_json(root / "trials.json", records)


def _validate_regions(labels: LabelSet, objective: str) -> tuple[list[Region], list[Region]]:
    tune_regions = [region for region in labels.regions if region.split == "tune"]
    holdout_regions = [region for region in labels.regions if region.split == "holdout"]
    if not tune_regions:
        raise ValueError("tuning requires reviewed regions or labels.reviewed_bbox")
    if any(not region.reviewed_complete for region in labels.regions):
        raise ValueError("all evaluation regions must explicitly declare reviewed_complete=true")
    if objective in {"intersection_f1", "combined_f1"}:
        if "intersections" not in labels.metadata["layers"]:
            raise ValueError("intersection objective requires labels.intersections")
        if not any(intersection_truths(labels.intersections, region, labels.regions) for region in tune_regions):
            raise ValueError("intersection objective requires at least one truth inside tune regions")
    if objective in {"centerline_f1", "combined_f1"}:
        from shapely.geometry import shape
        if "centerlines" not in labels.metadata["layers"]:
            raise ValueError("centerline objective requires labels.centerlines")
        if not any(shape(row["geometry"]).intersection(region.geometry).length > 0
                   for row in labels.centerlines for region in tune_regions):
            raise ValueError("centerline objective requires positive truth length inside tune regions")
    return tune_regions, holdout_regions


def tune(config: dict[str, Any], config_path: str | Path, run_region: RunRegion,
         input_identity: dict[str, Any], *, labels: LabelSet | None = None) -> dict[str, Any]:
    """Execute a total-budget, sequential study and independently score its best.

    ``run_region`` runs the complete five-stage pipeline with surrounding source
    context and returns processing-CRS features. Scoring clips to ``region`` only.
    Changing labels/input/code/search semantics requires a new study name/root.
    Increasing ``n_trials`` is permitted and continues the same study.
    """
    config_path = Path(config_path).resolve()
    settings = config.get("tuning", {})
    space = validate_search_space(config)
    objective = settings.get("objective", "intersection_f1")
    labels = labels or load_labels(config.get("labels", {}), config["input"]["processing_crs"], config_path.parent)
    tune_regions, holdout_regions = _validate_regions(labels, objective)
    seed = int(settings.get("seed", 42))
    random.Random(seed).shuffle(tune_regions)
    budget = settings.get("n_trials", 30)
    if type(budget) is not int or budget < 1:
        raise ValueError("tuning.n_trials must be a positive integer")
    study_name = settings.get("study_name", "roadnet")
    if not isinstance(study_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", study_name):
        raise ValueError("study_name must be a safe filename without path separators")
    root = Path(config["output"]["root"]) / "studies" / study_name
    root.mkdir(parents=True, exist_ok=True)
    semantic = copy.deepcopy(config)
    semantic.get("tuning", {}).pop("n_trials", None)
    semantic.get("tuning", {}).pop("timeout_s", None)
    semantic.get("output", {}).pop("root", None)
    identity = {"config": semantic, "input": input_identity, "labels": labels.identity,
                "region_order": [region.id for region in tune_regions], "code": _code_identity()}
    identity_hash = canonical_hash(identity)
    warnings = list(labels.metadata.get("warnings", []))
    if len(tune_regions) == 1:
        warnings.append("Only one tune region: no intermediate pruning opportunity")
    with _coordinator_lock(root):
        identity_path = root / "identity.json"
        if identity_path.exists():
            stored = json.loads(identity_path.read_text(encoding="utf-8"))
            if stored.get("identity_hash") != identity_hash or stored.get("identity") != identity:
                raise ValueError("study identity changed (data/labels/config/code); use a new study_name")
        else:
            write_json(identity_path, {"identity_hash": identity_hash, "identity": identity})
        prepare_labels(labels, root / "labels")
        storage = optuna.storages.JournalStorage(optuna.storages.journal.JournalFileBackend(str(root / "optuna.journal")))
        startup = int(settings.get("startup_trials", 5))
        warmup = int(settings.get("warmup_steps", 1))
        if startup < 0 or warmup < 0:
            raise ValueError("startup_trials and warmup_steps must be non-negative")
        # The median is compared at a fixed region count, never between stages.
        study = optuna.create_study(study_name=study_name, direction="maximize", storage=storage,
                                   load_if_exists=True,
                                   sampler=optuna.samplers.TPESampler(seed=seed, n_startup_trials=startup),
                                   pruner=RegionMedianPruner(startup, warmup))
        if study.user_attrs.get("identity_hash", identity_hash) != identity_hash:
            raise ValueError("Optuna journal identity differs from experiment manifest")
        study.set_user_attr("identity_hash", identity_hash)
        study.set_user_attr("region_order", identity["region_order"])
        study.set_user_attr("objective", objective)
        # An interrupted attempt consumes its budget and cannot silently become
        # a completed or pruned observation. The next launch gets a new trial.
        for old in study.get_trials(deepcopy=False):
            if old.state == optuna.trial.TrialState.RUNNING:
                study.tell(old.number, state=optuna.trial.TrialState.FAIL)
                record_path = root / "trials" / f"{old.number:06d}" / "trial.json"
                record = json.loads(record_path.read_text()) if record_path.exists() else {"number": old.number}
                record.update(status="INTERRUPTED", error_type="InterruptedRun",
                              reason="Previous coordinator ended before a durable trial result")
                write_json(record_path, record)
        _trials_export(study, root)
        started = time.perf_counter()
        timeout = settings.get("timeout_s")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                   or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("tuning.timeout_s must be positive")
        while len(study.trials) < budget:
            if timeout is not None and time.perf_counter() - started >= timeout:
                break
            # Per-trial deterministic seed survives process restarts. TPE still
            # conditions suggestions on every previously completed/pruned trial.
            study.sampler = optuna.samplers.TPESampler(seed=seed + len(study.trials), n_startup_trials=startup)
            trial = study.ask()
            trial_root = root / "trials" / f"{trial.number:06d}"
            trial_root.mkdir(parents=True, exist_ok=True)
            record: dict[str, Any] = {"number": trial.number, "status": "RUNNING", "regions": []}
            try:
                effective = _suggest(trial, config, space)
                record["params"] = dict(trial.params)
                write_json(trial_root / "effective-config.json", effective)
                write_json(trial_root / "trial.json", record)
                for step, region in enumerate(tune_regions):
                    region_root = trial_root / f"region-{step:04d}"
                    predicted = run_region(effective, region, region_root)
                    score = score_region(predicted, labels, region, settings)
                    write_json(region_root / "evaluation.json", score)
                    record["regions"].append(score)
                    metrics = aggregate_scores(record["regions"])
                    value = objective_value(metrics, objective)
                    record.update(metrics=metrics, value=value, last_step=step)
                    write_json(trial_root / "trial.json", record)
                    trial.report(value, step=step)
                    # There is no compute to save after the final region.
                    if step + 1 < len(tune_regions) and trial.should_prune():
                        raise optuna.TrialPruned(f"cumulative {objective}={value:.6f} after {step + 1} regions")
                trial.set_user_attr("metrics", record["metrics"])
                trial.set_user_attr("artifact_dir", str(trial_root))
                study.tell(trial, record["value"])
                record["status"] = "COMPLETE"
            except (optuna.TrialPruned, ResourceLimitExceeded) as error:
                reason_kind = "resource_budget" if isinstance(error, ResourceLimitExceeded) else "quality"
                record.update(status="PRUNED", reason=str(error), reason_kind=reason_kind)
                trial.set_user_attr("regions_evaluated", len(record["regions"]))
                trial.set_user_attr("artifact_dir", str(trial_root))
                trial.set_user_attr("reason_kind", reason_kind)
                if reason_kind == "resource_budget":
                    trial.set_user_attr("resource_budget_semantics", "sampled soft budget, stopped at a safe boundary")
                study.tell(trial, state=optuna.trial.TrialState.PRUNED)
            except BaseException as error:
                # Infrastructure/input exceptions stop the invocation. Failed
                # work is never converted into a low-quality score or pruning.
                record.update(status="FAILED", error_type=type(error).__name__)
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
                write_json(trial_root / "trial.json", record)
                _trials_export(study, root)
                raise
            write_json(trial_root / "trial.json", record)
            _trials_export(study, root)
        completed = [trial for trial in study.trials if trial.state == optuna.trial.TrialState.COMPLETE]
        if not completed:
            raise RuntimeError("study has no completed trial; inspect failed/pruned trial artifacts")
        # Explicit deterministic tie break: earlier completed trial wins.
        best = min(completed, key=lambda trial: (-trial.value, trial.number))
        if objective in {"centerline_f1", "combined_f1"}:
            line_scores = [trial.user_attrs.get("metrics", {}).get("centerlines", {}) for trial in completed]
            if all(score.get("predicted_length_m", 0) <= 0 for score in line_scores):
                warnings.append("No fitted centerline length inside tune regions in any completed trial. "
                                "The context must contain both neighboring intersection endpoints; increase "
                                "tuning.context_halo_m or use larger reviewed regions and inspect clip/fit artifacts.")
            elif best.user_attrs.get("metrics", {}).get("centerlines", {}).get("f1", 0) == 0:
                warnings.append("The selected trial has zero centerline geometric coverage; inspect labels, "
                                "CRS, context, and fitting outputs before using these parameters.")
        best_config = copy.deepcopy(config)
        for name, value in best.params.items():
            section, field = name.split(".")
            best_config[section][field] = value
        best_path = root / "best.pipeline.toml"
        temporary = best_path.with_suffix(".toml.tmp")
        temporary.write_text("# Generated best configuration; original pipeline.toml is unchanged.\n" +
                             tomlkit.dumps(best_config), encoding="utf-8")
        temporary.replace(best_path)
        holdout_path = root / f"holdout-best-{best.number:06d}.json"
        if holdout_path.exists():
            holdout = json.loads(holdout_path.read_text(encoding="utf-8"))
        elif holdout_regions:
            holdout_scores = []
            effective = copy.deepcopy(best_config)
            if "database" in effective:
                effective["database"]["enabled"] = False
            holdout_root = root / f"holdout-best-{best.number:06d}"
            attempt = 0
            while (holdout_root / f"attempt-{attempt:04d}").exists():
                attempt += 1
            attempt_root = holdout_root / f"attempt-{attempt:04d}"
            attempt_root.mkdir(parents=True, exist_ok=False)
            for step, region in enumerate(holdout_regions):
                region_root = attempt_root / f"region-{step:04d}"
                predicted = run_region(effective, region, region_root)
                score = score_region(predicted, labels, region, settings)
                write_json(region_root / "evaluation.json", score)
                holdout_scores.append(score)
            metrics = aggregate_scores(holdout_scores)
            holdout = {"status": "COMPLETE", "best_trial": best.number, "regions": holdout_scores,
                       "metrics": metrics, "value": objective_value(metrics, objective),
                       "used_for_selection": False, "attempt_dir": str(attempt_root)}
            write_json(holdout_path, holdout)
        else:
            holdout = {"status": "UNAVAILABLE", "used_for_selection": False,
                       "reason": "No holdout regions configured"}
        summary = {"study_name": study_name, "identity_hash": identity_hash, "objective": objective,
                   "selection_scope": "best completed observed trial; no claim of global optimality",
                   "best_trial": best.number, "best_value": best.value, "best_params": best.params,
                   "best_config_path": str(best_path), "best_config": best_config,
                   "best_metrics": best.user_attrs.get("metrics"), "holdout": holdout,
                   "trial_counts": {state.name: sum(trial.state == state for trial in study.trials)
                                    for state in optuna.trial.TrialState},
                   "region_order": identity["region_order"], "warnings": warnings,
                   "pruning_semantics": "cumulative micro score over identical ordered tune regions",
                   "elapsed_this_invocation_s": time.perf_counter() - started}
        write_json(root / "best.json", summary)
        _trials_export(study, root)
        return summary
