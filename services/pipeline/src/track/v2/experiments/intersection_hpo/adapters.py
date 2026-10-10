"""Real experiment-runner adapter for strict regional threshold evaluations."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from ..config import ExperimentCase, ExperimentWorkspace, canonical, load_case, parse_time, refine_key
from ..runner import evaluate_local, run_batch, write_manifest
from .metrics import aggregate_scores, reject_overlapping_regions, score_region


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()



def semantic_predictions(predictions: dict) -> tuple[list[dict], dict[str, str]]:
    """Canonical polygon content, excluding IDs allocated by each persisted run."""
    from shapely.geometry import mapping, shape
    from pipeline.experiments.evaluation import validated_features

    transient = {'id', 'detect_id', 'refine_id', 'experiment_id', 'batch_id'}
    features, identifiers = [], {}
    for feature in validated_features(predictions):
        content = {'geometry': mapping(shape(feature['geometry']).normalize()),
                   'properties': {key: value for key, value in feature.get('properties', {}).items()
                                  if key not in transient}}
        features.append(content)
        identifiers[feature['id']] = digest(content)
    return sorted(features, key=canonical), identifiers


def semantic_matches(matches: list[dict], prediction_ids: dict[str, str]) -> list[dict]:
    """Preserve match structure independently of run IDs and projection rounding.

    Exact geometry has a separate hash; raw distance/IoU remain in match artifacts.
    """
    return sorted([{'classification': match['classification'], 'truth_id': match.get('truth_id'),
                    'prediction_id': prediction_ids[match['prediction_id']]
                    if match.get('prediction_id') is not None else None}
                   for match in matches], key=canonical)

def threshold_config(config: Any, threshold: float) -> Any:
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        raise ValueError('threshold must be finite')
    if config.detect.candidates != ('turn_threshold',):
        raise ValueError('HPO requires only turn_threshold')
    definition = config.detect.candidate['turn_threshold']
    if len(definition.output_instances) != 1:
        raise ValueError('HPO requires exactly one turn_threshold output instance')
    name = definition.output_instances[0]
    params = replace(definition.params[name], min_turn_deg=float(threshold))
    isolated = replace(definition, params={name: params})
    return replace(config, detect=replace(config.detect, candidate={'turn_threshold': isolated}))


class RunBatchEvaluator:
    """Each call archives independent region runs; reuse only owned Refine IDs.

    Manifests and base configs are copied so caller changes cannot alter later trials.
    No persistent candidate relation is retained across runner invocations.
    """
    def __init__(self, workspace: ExperimentWorkspace, manifests: dict, scheme: str,
                 evaluation_rules: dict | None = None, *, batch_runner: Callable | None = None,
                 archive_evaluator: Callable | None = None):
        if scheme not in workspace.configs:
            raise ValueError('unknown HPO scheme')
        if workspace.archive_settings['database'] != 'roadnet_eval':
            raise ValueError('HPO archive database must be roadnet_eval')
        self.workspace = copy.deepcopy(workspace)
        self.manifests = copy.deepcopy(manifests)
        self.scheme = scheme
        self.rules = copy.deepcopy(evaluation_rules if evaluation_rules is not None else workspace.evaluation)
        from pipeline.experiments.evaluation import EvaluationRule
        EvaluationRule.parse(self.rules)
        threshold_config(workspace.configs[scheme], 0)
        self.batch_runner = batch_runner or run_batch
        self.archive_evaluator = archive_evaluator or evaluate_local
        self.refines: dict[str, str] = {}

    def __call__(self, threshold: float, fidelity: str, split: str, artifact_dir: str | Path) -> dict:
        if fidelity not in {'L1', 'L2', 'L3'} or split not in {'tune', 'holdout'}:
            raise ValueError('invalid evaluation fidelity/split')
        manifest = self.manifests['holdout' if split == 'holdout' else fidelity]
        if manifest.get('manifest_hash') and manifest['manifest_hash'] != digest({k: v for k, v in manifest.items() if k != 'manifest_hash'}):
            raise ValueError('manifest hash does not match evaluation content')
        regions = manifest['regions']
        for source, expected_hash in manifest.get('sources', {}).items():
            if hashlib.sha256(Path(source).read_bytes()).hexdigest() != expected_hash:
                raise ValueError('manifest source file has changed; rebuild the audited manifests')
        if not regions:
            raise ValueError('evaluation manifest has no regions')
        reject_overlapping_regions(regions)
        if fidelity != 'L1' and any(region.get('reviewed_complete', region.get('fully_verified', False)) is not True for region in regions):
            raise ValueError(f'{fidelity} requires fully verified region labels')
        config = threshold_config(self.workspace.configs[self.scheme], threshold)
        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        frozen = {'threshold': float(threshold), 'fidelity': fidelity, 'split': split,
                  'manifest': manifest, 'rules': self.rules, 'parameters': config.snapshot_parameters(),
                  'threshold_predicate': 'abs(turn_deg) > min_turn_deg'}
        frozen_path = artifact_dir / 'effective-config.json'
        if frozen_path.exists():
            raise ValueError('evaluation artifact directory has already been used')
        write_manifest(frozen_path, frozen)
        results = []
        started = time.perf_counter()
        from pyproj import Geod
        geod = Geod(ellps='WGS84')
        for region in regions:
            snapshot = region.get('case_snapshot')
            if snapshot:
                from shapely.geometry import shape
                if not shape(snapshot['geometry']).equals(shape(region['geometry'])):
                    raise ValueError('frozen case boundary differs from manifest region')
                case = ExperimentCase(
                    region['case'], snapshot['source_table'],
                    parse_time(snapshot['time_start'], 'time_start') if snapshot.get('time_start') else None,
                    parse_time(snapshot['time_end'], 'time_end') if snapshot.get('time_end') else None,
                    copy.deepcopy(region['geometry']))
            else:
                case_raw = copy.deepcopy(self.workspace.payload['cases'][region['case']])
                # Frozen manifest labels supersede legacy per-case truth paths.
                case_raw.pop('truth_file', None)
                case = load_case(region['case'], case_raw, self.workspace.config_path.parent)
            from shapely.geometry import shape
            if not shape(case.geometry).equals(shape(region['geometry'])):
                raise ValueError('manifest region must exactly match manual experiment case boundary')
            # Scoring is owned here: runner automatic scoring would assign FP to L1.
            case = replace(case, truth_file=None)
            payload = copy.deepcopy(self.workspace.payload)
            payload['experiment'] = {**payload.get('experiment', {}), 'note': {'hpo_fidelity': fidelity, 'hpo_split': split, 'manifest_hash': digest(manifest), 'threshold': float(threshold)}}
            selected_scheme = copy.deepcopy(payload.get('schemes', {}).get(self.scheme, {}))
            selected_scheme['detect'] = config.detect.as_dict()
            payload['schemes'] = {self.scheme: selected_scheme}
            regional = replace(self.workspace, case=case, schemes=(self.scheme,),
                               configs={self.scheme: copy.deepcopy(config)}, payload=payload)
            key = refine_key(case, config)
            records = self.batch_runner(regional, explicit_refine_id=self.refines.get(key))
            if len(records) != 1:
                raise RuntimeError('HPO runner must return one region experiment')
            record = records[0]
            if record['status'] != 'completed' or record.get('archive_state') != 'archived':
                raise RuntimeError(f"region experiment failed or was not archived: {record.get('id')}: {record.get('error') or record.get('archive_error')}")
            self.refines[key] = record['refine_id']
            run_path = Path(record['workspace_run_path'])
            predictions = json.loads((run_path / 'predictions.geojson').read_text(encoding='utf-8'))
            score = score_region(predictions, region, self.rules, fidelity)
            truth_path = artifact_dir / f"truth-{len(results):04d}.geojson"
            write_manifest(truth_path, {'type': 'FeatureCollection', 'features': region['truths']})
            if fidelity != 'L1':
                self.archive_evaluator(regional, record['id'], truth_file=truth_path, rules=self.rules)
            stats = record['stats']
            semantic, prediction_ids = semantic_predictions(predictions)
            result = {'region_id': region['id'], 'experiment_id': record['id'],
                      'refine_id': record['refine_id'], 'detect_id': record['detect_id'],
                      'workspace_run_path': str(run_path), 'elapsed_s': stats['elapsed_s'],
                      'candidate_count': stats['candidate_point_count'],
                      'candidate_instances': stats.get('candidate_instances', []),
                      'hashes': {'manifest': digest(region), 'labels': digest(region['truths']),
                                 'predictions': digest(semantic), 'predictions_raw': digest(predictions),
                                 'matches': digest(semantic_matches(score['matches'], prediction_ids)),
                                 'effective_config': digest(frozen)}, **score}
            region_area_km2 = abs(geod.geometry_area_perimeter(shape(region['geometry']))[0]) / 1e6
            result['evaluation_area_km2'] = region_area_km2
            result['kind'] = region.get('kind', 'positive')
            result['candidate_density_per_km2'] = result['candidate_count'] / region_area_km2 if region_area_km2 else None
            result['fp_per_km2'] = score['metrics']['fp'] / region_area_km2 if score['metrics']['fp'] is not None and region_area_km2 else None
            write_manifest(artifact_dir / f"region-{len(results):04d}.json", result)
            results.append(result)
        area_km2 = sum(item['evaluation_area_km2'] for item in results)
        result = {'threshold': float(threshold), 'fidelity': fidelity, 'split': split,
                  'metrics': aggregate_scores(results, fidelity), 'regions': results,
                  'runtime_s': time.perf_counter() - started,
                  'pipeline_elapsed_s': sum(item['elapsed_s'] for item in results),
                  'candidate_count': sum(item['candidate_count'] for item in results),
                  'artifact_dir': str(artifact_dir), 'manifest_hash': digest(manifest),
                  'effective_config_hash': digest(frozen), 'experiment_ids': [item['experiment_id'] for item in results]}
        result.update(result['metrics'])
        result['wall_seconds'] = result['runtime_s']
        result['cpu_seconds'] = None
        result['effective_config'] = frozen
        result['cache'] = {'kind': 'explicit_region_refine_ids', 'scope': 'evaluator_process', 'refine_ids': dict(self.refines)}
        result['evaluation_area_km2'] = area_km2
        result['candidate_density_per_km2'] = result['candidate_count'] / area_km2 if area_km2 else None
        result['fp_per_km2'] = result['fp'] / area_km2 if result['fp'] is not None and area_km2 else None
        negative = [item for item in results if item['kind'] == 'negative']
        negative_area = sum(item['evaluation_area_km2'] for item in negative)
        result['negative_fp_per_km2'] = sum(item['metrics']['fp'] for item in negative) / negative_area if fidelity != 'L1' and negative_area else None
        write_manifest(artifact_dir / 'result.json', result)
        return result
