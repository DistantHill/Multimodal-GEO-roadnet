"""Audited, deterministic nested region manifests, with strict input boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import HPOConfig
from ..config import ExperimentWorkspace, canonical, load_case, polygon_geometry


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _regions(config: HPOConfig, workspace: ExperimentWorkspace) -> tuple[list[dict], dict[str, str]]:
    from shapely.geometry import shape
    from pipeline.experiments.evaluation import EvaluationRule, validated_features

    EvaluationRule.parse(workspace.evaluation)
    if workspace.archive_settings["database"] != "roadnet_eval":
        raise ValueError("HPO archive must use roadnet_eval")
    if config.scheme not in workspace.payload.get("schemes", {}):
        raise ValueError("HPO scheme must exist in experiment workspace")
    effective = workspace.configs[config.scheme]
    data_root = effective.paths.data_root.resolve()
    pipeline_root = Path(__file__).resolve().parents[5]
    repo_root = next((parent for parent in pipeline_root.parents
                      if (parent / "services/pipeline").is_dir()), pipeline_root)
    if not config.output_root.is_relative_to(data_root) or config.output_root.is_relative_to(repo_root):
        raise ValueError("HPO output_root must be under configured data_root and outside repository")
    if effective.detect.candidates != ("turn_threshold",):
        raise ValueError("HPO requires a scheme using only turn_threshold")
    definition = effective.detect.candidate["turn_threshold"]
    if len(definition.output_instances) != 1:
        raise ValueError("HPO requires one turn_threshold output instance")
    params = definition.params[definition.output_instances[0]]
    if config.baseline != params.min_turn_deg:
        raise ValueError("baseline must equal existing scheme min_turn_deg")
    if config.threshold_max > params.max_turn_deg:
        raise ValueError("threshold_max exceeds fixed max_turn_deg")
    from .labels import load_polygon_file
    region_features, region_import = load_polygon_file(config.regions_file, layer=config.regions_layer)
    source_imports = {str(config.regions_file): region_import}
    if config.negative_regions_file is not None:
        negative_features, negative_import = load_polygon_file(config.negative_regions_file, layer=config.negative_regions_layer)
        source_imports[str(config.negative_regions_file)] = negative_import
        for feature in negative_features:
            feature["id"] = "negative:" + feature["id"]
            feature["properties"] = {**feature.get("properties", {}), "kind": "negative"}
        region_features += negative_features
    raw = {"type": "FeatureCollection", "features": region_features}
    sources = {str(config.regions_file): file_hash(config.regions_file),
               str(config.experiment_config): file_hash(config.experiment_config)}
    if config.negative_regions_file is not None:
        sources[str(config.negative_regions_file)] = file_hash(config.negative_regions_file)
    catalog = None
    if config.labels_file is not None:
        from .labels import load_labels
        catalog, label_import = load_labels(config.labels_file, layer=config.labels_layer, id_field=config.labels_id_field)
        source_imports[str(config.labels_file)] = label_import
        sources[str(config.labels_file)] = file_hash(config.labels_file)
    regions, ids, seen_truth = [], set(), {}
    required = {"case", "spatial_group", "split", "kind", "reviewed_complete"}
    for feature in raw["features"]:
        identifier = feature.get("id", feature.get("properties", {}).get("id"))
        props = {**feature.get("properties", {}), **config.region_metadata.get(str(identifier), {})}
        if str(identifier).startswith("negative:") and props.get("kind") != "negative":
            raise ValueError("negative region catalog cannot be reclassified as positive")
        if not isinstance(props, dict) or required - set(props):
            raise ValueError("each region requires case/spatial_group/split/kind/reviewed_complete metadata")
        identifier = feature.get("id", props.get("id"))
        if identifier is None or not str(identifier).strip() or str(identifier) in ids:
            raise ValueError("region IDs must be stable, nonempty and unique")
        ids.add(str(identifier))
        if any(not isinstance(props[k], str) or not props[k].strip() for k in ("case", "spatial_group")):
            raise ValueError("case and spatial_group must be nonempty strings")
        if props["split"] not in {"tune", "holdout"} or props["kind"] not in {"positive", "negative"}:
            raise ValueError("region split=tune|holdout and kind=positive|negative required")
        if type(props["reviewed_complete"]) is not bool or not props["reviewed_complete"]:
            raise ValueError(f"region {identifier}: complete manual label review required for F1")
        cases = workspace.payload.get("cases", {})
        if props["case"] not in cases:
            raise ValueError(f"region {identifier}: unknown experiment case")
        case_raw = dict(cases[props["case"]])
        if catalog is not None:
            # Global supplied labels supersede any legacy per-case truth path.
            case_raw.pop("truth_file", None)
        case = load_case(props["case"], case_raw, config.experiment_config.parent)
        geometry = polygon_geometry(feature)
        area = shape(geometry)
        if not area.equals(shape(case.geometry)):
            raise ValueError(f"region {identifier}: geometry must equal existing case boundary; no halo")
        if case.time_start is None or case.time_end is None:
            raise ValueError(f"region {identifier}: explicit source time bounds required")
        if catalog is not None:
            truths = [truth for truth in catalog if area.covers(shape(truth["geometry"]).centroid)]
            truth_source = str(config.labels_file)
        else:
            if case.truth_file is None:
                raise ValueError(f"region {identifier}: explicit truth_file or global labels_file required, including explicit negative review")
            truths = validated_features(json.loads(case.truth_file.read_text(encoding="utf-8")), truth=True)
            sources[str(case.truth_file)] = file_hash(case.truth_file)
            truth_source = str(case.truth_file)
        if case.region_file:
            sources[str(case.region_file)] = file_hash(case.region_file)
        types = {}
        for truth_index, truth in enumerate(truths):
            if not area.covers(shape(truth["geometry"]).centroid):
                raise ValueError(f"region {identifier}: truth {truth['id']} centroid outside strict boundary")
            label_type = truth.get("properties", {}).get("type")
            if not isinstance(label_type, str) or not label_type.strip() or label_type in {"unknown", "ambiguous"}:
                label_type = "未分类"
                truth = {**truth, "properties": {**truth.get("properties", {}), "type": label_type}}
                truths[truth_index] = truth
            prior = seen_truth.get(truth["id"])
            if prior is not None and canonical(prior) != canonical(truth):
                raise ValueError(f"truth {truth['id']}: conflicting duplicate ID")
            seen_truth[truth["id"]] = truth
            types[label_type] = types.get(label_type, 0) + 1
        if (props["kind"] == "negative" and truths) or (props["kind"] == "positive" and not truths):
            raise ValueError(f"region {identifier}: positive/negative metadata disagrees with truth")
        regions.append({"id": str(identifier), **{k: props[k] for k in required},
                        "fully_verified": True, "geometry": geometry, "truths": truths,
                        "truth_file": truth_source, "types": types,
                        "case_snapshot": case.snapshot(), "source_imports": source_imports})
    if not regions:
        raise ValueError("no labeled regions available")
    tune = [r for r in regions if r["split"] == "tune"]
    holdout = [r for r in regions if r["split"] == "holdout"]
    if not holdout:
        raise ValueError("independent holdout regions required")
    if not any(r["kind"] == "positive" for r in holdout):
        raise ValueError("holdout requires positive truth")
    for a in tune:
        for b in holdout:
            if a["spatial_group"] == b["spatial_group"] or shape(a["geometry"]).intersects(shape(b["geometry"])):
                raise ValueError(f"tune/holdout spatial leakage: {a['id']} and {b['id']}")
            if {t["id"] for t in a["truths"]} & {t["id"] for t in b["truths"]}:
                raise ValueError("tune/holdout truth ID leakage")
    for pool in (tune, holdout):
        for index, a in enumerate(pool):
            for b in pool[index + 1:]:
                # Shared boundaries also belong to both strict ST_Covers reads.
                if shape(a["geometry"]).intersects(shape(b["geometry"])):
                    raise ValueError(f"overlapping regions within split: {a['id']} and {b['id']}")
    positives = sum(r["kind"] == "positive" for r in tune)
    negatives = sum(r["kind"] == "negative" for r in tune)
    if positives < config.l2_positive_count or negatives < config.l2_negative_count:
        raise ValueError("insufficient manually reviewed tune regions for configured L2 counts")
    return sorted(regions, key=lambda r: r["id"]), sources


def audit_data(config: HPOConfig, workspace: ExperimentWorkspace) -> dict:
    """Report the supplied catalog even when experiment region QA is blocked."""
    label_audit = {}
    try:
        if config.labels_file is not None:
            from .labels import load_labels
            _, label_audit = load_labels(config.labels_file, layer=config.labels_layer, id_field=config.labels_id_field)
        regions, sources = _regions(config, workspace)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"ready": False, "blockers": [str(exc)], "region_count": 0, "label_catalog": label_audit}
    return {"ready": True, "blockers": [], "region_count": len(regions), "label_catalog": label_audit,
            "source_hash": digest(sources), "sources": sources,
            "types": sorted({kind for region in regions for kind in region["types"]})}


def _stratified(regions: list[dict], seed: int) -> list[dict]:
    # Round-robin region strata; a multi-intersection region stays indivisible.
    buckets: dict[str, list[dict]] = {}
    for region in regions:
        stratum = "+".join(sorted(region["types"])) or "negative"
        buckets.setdefault(stratum, []).append(region)
    for values in buckets.values():
        values.sort(key=lambda r: digest({"seed": seed, "id": r["id"]}))
    result = []
    while any(buckets.values()):
        for key in sorted(buckets):
            if buckets[key]:
                result.append(buckets[key].pop(0))
    return result


def build_manifests(config: HPOConfig, workspace: ExperimentWorkspace) -> dict[str, dict]:
    regions, sources = _regions(config, workspace)
    label_catalog = {}
    if config.labels_file is not None:
        from .labels import load_labels
        _, label_catalog = load_labels(config.labels_file, layer=config.labels_layer, id_field=config.labels_id_field)
    tune = [r for r in regions if r["split"] == "tune"]
    positive = _stratified([r for r in tune if r["kind"] == "positive"], config.seed)
    negative = _stratified([r for r in tune if r["kind"] == "negative"], config.seed)
    selected = {"L1": positive[:config.l1_positive_count],
                "L2": positive[:config.l2_positive_count] + negative[:config.l2_negative_count],
                "L3": tune, "holdout": [r for r in regions if r["split"] == "holdout"]}
    output = {}
    for fidelity, values in selected.items():
        values = sorted(values, key=lambda r: r["id"])
        payload = {"schema_version": 1, "fidelity": fidelity, "split": "holdout" if fidelity == "holdout" else "tune",
                   "seed": config.seed, "label_version": config.label_version,
                   "trajectory_snapshot": config.trajectory_snapshot,
                   "source_revision": config.source_revision, "source_hash": digest(sources), "sources": sources,
                   "boundary_mode": "strict_no_expansion", "evaluation": workspace.evaluation,
                   "label_format": "gpkg" if config.labels_file else "geojson", "label_catalog": label_catalog,
                   "source_imports": {path: metadata for region in values for path, metadata in region["source_imports"].items()},
                   "labels_layer": config.labels_layer if config.labels_file else None,
                   "labels_id_field": config.labels_id_field if config.labels_file else None,
                   "region_metadata": config.region_metadata,
                   "region_ids": [r["id"] for r in values], "regions": values,
                   "truth_ids": sorted({t["id"] for r in values for t in r["truths"]}),
                   "sampling_group_ids": sorted({r["spatial_group"] for r in values})}
        output[fidelity] = {**payload, "manifest_hash": digest(payload)}
    return output


def write_manifests(manifests: dict[str, dict], output_root: str | Path) -> dict[str, Path]:
    root = Path(output_root) / "manifests"
    root.mkdir(parents=True, exist_ok=True)
    paths = {}
    for stage, manifest in manifests.items():
        payload = {k: v for k, v in manifest.items() if k != "manifest_hash"}
        if manifest.get("manifest_hash") != digest(payload):
            raise ValueError("manifest hash does not match current content")
        path = root / f"{stage}-{manifest['manifest_hash']}.json"
        contents = canonical(manifest) + "\n"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(contents)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != contents:
                raise ValueError(f"immutable manifest content conflict: {path}")
        paths[stage] = path
    return paths
