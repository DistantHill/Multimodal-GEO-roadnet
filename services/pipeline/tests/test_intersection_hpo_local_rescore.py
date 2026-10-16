"""Retrospective location scores remain separate from the old optimization run."""
import hashlib
import json
from pathlib import Path

import pytest
from shapely.geometry import Point, mapping

from track.v2.experiments.intersection_hpo.local_rescore import rescore


def fixture(root):
    calls = []
    for level, threshold, aligned in [('L1', 45., True), ('L1', 30., False), ('L2', 30., True), ('L3', 30., True)]:
        identifier = f'{level}-{int(threshold)}'
        truth = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'id': 't', 'properties': {}, 'geometry': mapping(Point(109., 34.).buffer(.0001))}]}
        predictions = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'id': 'p', 'properties': {}, 'geometry': mapping(Point(109. if aligned else 109.01, 34.).buffer(.002))}]}
        path = root / 'calls' / identifier
        path.mkdir(parents=True)
        (path / 'predictions.geojson').write_text(json.dumps(predictions))
        (path / 'truth.geojson').write_text(json.dumps(truth))
        calls.append({'call_id': identifier, 'level': level, 'threshold': threshold, 'status': 'SUCCESS',
                      'result': {'experiment_id': identifier, 'evaluation_id': identifier+'-original',
                                 'metrics': {'tp': 0, 'fp': 1, 'fn': 1, 'precision': 0., 'recall': 0., 'f1': 0.}}})
    (root / 'search').mkdir()
    (root / 'search' / 'ledger.json').write_text(json.dumps({'calls': calls}))


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob('*') if path.is_file()}


def test_rescore_no_database_no_source_changes_and_old_scores_retained(tmp_path, monkeypatch):
    source, output = tmp_path / 'original', tmp_path / 'rescored'
    fixture(source)
    original = hashes(source)
    def forbidden(*args, **kwargs):
        raise AssertionError('Pure rescoring must not connect or start Detect')
    monkeypatch.setattr('track.v2.experiments.intersection_hpo.local_adapter.connect_archive', forbidden)
    monkeypatch.setattr('track.v2.experiments.intersection_hpo.local_adapter.detect_intersections', forbidden)
    summary = rescore(source, output)
    assert hashes(source) == original
    assert summary['rescored_calls'] == 4
    assert summary['rules'] == {'mode': 'center_distance', 'distance_m': 30.}
    first = summary['rankings']['L1'][0]
    assert first['threshold'] == 45.
    assert first['original_metrics']['f1'] == 0.
    assert first['metrics']['f1'] == 1.
    assert first['original_evaluation_id'] == 'L1-45-original'
    assert summary['promotion_bias']['L1_to_L2']['new_top_missing_higher_fidelity'] == [45.]
    assert summary['best_available_l3']['threshold'] == 30.
    assert (output / 'L1-ranking.csv').exists()


@pytest.mark.parametrize('relative', ['', 'new-scores'])
def test_rescore_refuses_original_tree_output(tmp_path, relative):
    source = tmp_path / 'original'
    fixture(source)
    original = hashes(source)
    with pytest.raises(ValueError, match='separate directory'):
        rescore(source, source / relative)
    assert hashes(source) == original
