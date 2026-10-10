from dataclasses import replace
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import box, mapping

from track.v2.experiments.intersection_hpo.adapters import threshold_config, RunBatchEvaluator
from track.v2.experiments.intersection_hpo.metrics import score_region, aggregate_scores, reject_overlapping_regions
from track.v2.config.detect_config import CandidateAlgorithmConfig
from track.v2.candidate_plugins.turn_threshold import TurnThresholdParams


def feature(identifier, x=111.01, kind='T'):
    return {'type': 'Feature', 'id': identifier, 'geometry': mapping(box(x, 30.01, x+.001, 30.011)),
            'properties': {'intersection_type': kind}}


def region(complete=False):
    return {'id': 'r1', 'geometry': mapping(box(111, 30, 112, 31)),
            'truths': [feature('t')], 'reviewed_complete': complete}


def test_l1_partial_labels_only_report_recall_and_type_matches():
    predictions = {'type': 'FeatureCollection', 'features': [feature('p'), feature('extra', 111.03)]}
    score = score_region(predictions, region(), {'mode': 'center_distance', 'distance_m': 10}, 'L1')
    assert score['metrics']['tp'] == 1
    assert score['metrics']['fp'] is score['metrics']['f1'] is score['metrics']['precision'] is None
    assert score['metrics']['per_type_recall']['T'] == {'tp': 1, 'fn': 0, 'recall': 1}
    assert all(item['classification'] != 'FP' for item in score['matches'])
    assert aggregate_scores([score], 'L1')['fp'] is None


@pytest.mark.parametrize('stage', ['L2', 'L3'])
def test_full_metrics_require_verified_labels(stage):
    with pytest.raises(ValueError, match='fully verified'):
        score_region([], region(), {'mode': 'iou', 'iou_threshold': .5}, stage)
    score = score_region([feature('p'), feature('extra', 111.03)], region(True),
                         {'mode': 'both', 'distance_m': 10, 'iou_threshold': .5}, stage)
    assert score['metrics']['fp'] == 1
    assert aggregate_scores([score], stage)['f1'] == pytest.approx(2/3)


def test_manual_boundary_filters_predictions_and_rejects_outside_labels():
    score = score_region([feature('outside', 113)], region(), {'mode': 'center_distance', 'distance_m': 10}, 'L1')
    assert score['prediction_count'] == 0
    bad = region(); bad['truths'] = [feature('outside', 113)]
    with pytest.raises(ValueError, match='outside'):
        score_region([], bad, {'mode': 'center_distance', 'distance_m': 10}, 'L1')


def test_overlapping_regions_are_rejected_including_shared_boundary():
    left = region()
    right = {**region(), 'id': 'r2', 'geometry': mapping(box(112, 30, 113, 31))}
    with pytest.raises(ValueError, match='overlap'):
        reject_overlapping_regions([left, right])


def test_threshold_changes_only_selected_parameter_and_keeps_base_immutable():
    from track.v2.config.pipeline_config_loader import load_pipeline_config
    # Use the ordinary validated config, without running Pipeline or connecting to DB.
    root = Path(__file__).resolve().parents[1]
    config = load_pipeline_config(root / 'config/pipeline-roadnet-test.toml', postgres_password='test')
    selected = config.detect.candidate['turn_threshold']
    config = replace(config, detect=replace(config.detect, candidates=('turn_threshold',),
                       candidate={'turn_threshold': replace(selected, output_instances=('default',))}))
    before = config.snapshot_parameters()
    changed = threshold_config(config, 27)
    assert changed.detect.candidate['turn_threshold'].params['default'].min_turn_deg == 27
    assert config.snapshot_parameters() == before
    expected = replace(config.detect.candidate['turn_threshold'].params['default'], min_turn_deg=27)
    assert changed.detect.candidate['turn_threshold'].params['default'] == expected
    assert changed.detect.cluster == config.detect.cluster


def test_runner_adapter_uses_owned_refine_id_freezes_config_and_suppresses_l1_auto_score(tmp_path):
    from track.v2.config.pipeline_config_loader import load_pipeline_config
    from track.v2.experiments.config import ExperimentWorkspace
    root = Path(__file__).resolve().parents[1]
    config = load_pipeline_config(root / 'config/pipeline-roadnet-test.toml', postgres_password='test')
    definition = config.detect.candidate['turn_threshold']
    config = replace(config, detect=replace(config.detect, candidates=('turn_threshold',),
                       candidate={'turn_threshold': replace(definition, output_instances=('default',))}))
    manual = region()
    manual['case'] = 'manual'
    truth_path = tmp_path / 'labels.geojson'
    truth_path.write_text(json.dumps({'type': 'FeatureCollection', 'features': manual['truths']}))
    payload = {'cases': {'manual': {'source_table': 'trajectory_points', 'bbox': [[111,30],[112,31]],
                                    'truth_file': str(tmp_path / 'obsolete-truth.geojson')}},
               'archive': {'database': 'roadnet_eval'},
               'schemes': {'fixed': {'refine': {'max_speed_mps': 99}, 'runtime': {'parallel_workers': 2}}},
               'evaluation': {'mode': 'center_distance', 'distance_m': 10}}
    workspace = ExperimentWorkspace(tmp_path / 'config.toml', tmp_path / 'runs', payload, None, ('fixed',), {'fixed': config})
    calls = []
    def runner(regional, *, explicit_refine_id):
        calls.append((regional, explicit_refine_id))
        assert regional.case.truth_file is None
        assert regional.payload['schemes']['fixed']['refine'] == {'max_speed_mps': 99}
        assert regional.payload['schemes']['fixed']['runtime'] == {'parallel_workers': 2}
        run_path = tmp_path / f'run{len(calls)}'
        run_path.mkdir()
        (run_path / 'predictions.geojson').write_text(json.dumps({'type': 'FeatureCollection', 'features': [feature('p')]}))
        return [{'id': f'e{len(calls)}', 'refine_id': 'owned-refine', 'detect_id': f'd{len(calls)}',
                 'status': 'completed', 'archive_state': 'archived', 'workspace_run_path': str(run_path),
                 'stats': {'elapsed_s': .5, 'candidate_point_count': 2}}]
    complete = {**copy.deepcopy(manual), 'reviewed_complete': True,
                'case_snapshot': {'name': 'manual', 'source_table': 'trajectory_points',
                                  'time_start': '2023-03-01T00:00:00+08:00',
                                  'time_end': '2023-03-02T00:00:00+08:00',
                                  'geometry': copy.deepcopy(manual['geometry'])}}
    sources = {str(truth_path): hashlib.sha256(truth_path.read_bytes()).hexdigest()}
    manifests = {'L1': {'regions': [manual], 'sources': sources}, 'L2': {'regions': [complete], 'sources': sources}}
    archived = []
    def archive_evaluator(regional, experiment_id, *, truth_file, rules):
        assert regional.case.truth_file is None
        labels = json.loads(truth_file.read_text())
        archived.append((experiment_id, labels, rules))
    evaluator = RunBatchEvaluator(workspace, manifests, 'fixed', batch_runner=runner, archive_evaluator=archive_evaluator)
    manifests['L1']['regions'][0]['truths'].clear()
    first = evaluator(20, 'L1', 'tune', tmp_path / 'trial1')
    second = evaluator(30, 'L1', 'tune', tmp_path / 'trial2')
    assert calls[0][1] is None
    assert calls[1][1] == 'owned-refine'
    assert first['recall'] == second['recall'] == 1
    assert first['candidate_density_per_km2'] > 0
    assert first['fp_per_km2'] is first['f1'] is None
    assert calls[0][0].configs['fixed'].detect.candidate['turn_threshold'].params['default'].min_turn_deg == 20
    assert calls[1][0].configs['fixed'].detect.candidate['turn_threshold'].params['default'].min_turn_deg == 30
    assert first['effective_config_hash'] != second['effective_config_hash']
    assert archived == []
    full = evaluator(30, 'L2', 'tune', tmp_path / 'trial3')
    assert full['f1'] == 1
    assert calls[2][0].case.time_start.isoformat() == '2023-02-28T16:00:00+00:00'
    assert calls[2][1] is None  # Different frozen source-time selection cannot reuse L1 Refine.
    assert archived == [('e3', {'type': 'FeatureCollection', 'features': json.loads(json.dumps(complete['truths']))}, payload['evaluation'])]
    with pytest.raises(ValueError, match='already been used'):
        evaluator(20, 'L1', 'tune', tmp_path / 'trial1')
    truth_path.write_text('{}')
    with pytest.raises(ValueError, match='source file has changed'):
        evaluator(20, 'L1', 'tune', tmp_path / 'trial4')
    assert len(calls) == 3


def test_semantic_reproduction_hash_ignores_prediction_uuid_and_ring_order():
    from track.v2.experiments.intersection_hpo.adapters import semantic_predictions, semantic_matches, digest
    one = feature('uuid-one')
    two = feature('uuid-two')
    one['properties']['id'] = 'uuid-one'
    two['properties']['id'] = 'uuid-two'
    two['geometry']['coordinates'] = [list(reversed(two['geometry']['coordinates'][0]))]
    left, left_ids = semantic_predictions({'type': 'FeatureCollection', 'features': [one]})
    right, right_ids = semantic_predictions({'type': 'FeatureCollection', 'features': [two]})
    assert digest(left) == digest(right)
    match_left = [{'classification': 'TP', 'prediction_id': 'uuid-one', 'truth_id': 'stable-gt', 'iou': 1}]
    match_right = [{'classification': 'TP', 'prediction_id': 'uuid-two', 'truth_id': 'stable-gt', 'iou': 1}]
    assert digest(semantic_matches(match_left, left_ids)) == digest(semantic_matches(match_right, right_ids))
    changed, _ = semantic_predictions({'type': 'FeatureCollection', 'features': [feature('uuid-three', 111.02)]})
    assert digest(left) != digest(changed)
    match_right[0]['truth_id'] = 'different-gt'
    assert digest(semantic_matches(match_left, left_ids)) != digest(semantic_matches(match_right, right_ids))


def test_scoring_ties_are_independent_of_persisted_prediction_ids():
    from track.v2.experiments.intersection_hpo.adapters import semantic_predictions, semantic_matches
    left, right = feature('uuid-a', 111.01), feature('uuid-z', 111.03)
    manual = region(True)
    manual['truths'] = [feature('truth-a', 111.015), feature('truth-b', 111.025)]
    rules = {'mode': 'center_distance', 'distance_m': 5000}
    first = {'type': 'FeatureCollection', 'features': [left, right]}
    changed = copy.deepcopy(first)
    changed['features'][0]['id'] = 'uuid-z'
    changed['features'][1]['id'] = 'uuid-a'
    one = score_region(first, manual, rules, 'L3')
    two = score_region(changed, manual, rules, 'L3')
    assert one['metrics'] == two['metrics']
    _, ids_one = semantic_predictions(first)
    _, ids_two = semantic_predictions(changed)
    assert semantic_matches(one['matches'], ids_one) == semantic_matches(two['matches'], ids_two)
