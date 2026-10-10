"""The selected config must load through the existing experiment CLI unchanged."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

from track.v2.cli.experiment import toml_text
from track.v2.experiments.config import load_workspace
from track.v2.experiments.intersection_hpo.exports import write_best_config


def test_best_export_is_runnable_and_preserves_fixed_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "test")
    root = Path(__file__).resolve().parents[1]
    payload = tomllib.loads((root / 'config/pipeline-roadnet-test.toml').read_text())
    payload['paths']['data_root'] = str(tmp_path)
    payload['parser']['field_specs_path'] = str(root / 'config/field_specs.toml')
    payload['experiment'] = {'case': 'sample', 'schemes': ['threshold']}
    payload['cases'] = {'sample': {'source_table': 'trajectory_points',
        'time_start': '2023-03-01T00:00:00+08:00', 'time_end': '2023-03-02T00:00:00+08:00',
        'bbox': [[111, 30], [112, 31]]}}
    detect = copy.deepcopy(payload['detect'])
    detect['candidates'] = ['turn_threshold']
    payload['schemes'] = {'threshold': {'detect': detect, 'refine': {'min_speed_mps': .5}}}
    payload['evaluation'] = {'mode': 'center_distance', 'distance_m': 30.0}
    original = tmp_path / 'original.toml'
    original.write_text(toml_text(payload))
    config = SimpleNamespace(experiment_config=original, scheme='threshold', output_root=tmp_path / 'export')
    exported = write_best_config(config, 25.0)
    loaded = load_workspace(exported, password='test').configs['threshold']
    baseline = load_workspace(original, password='test').configs['threshold']
    assert loaded.detect.candidates == ('turn_threshold',)
    assert loaded.detect.candidate['turn_threshold'].params['default'].min_turn_deg == 25
    assert loaded.refine == baseline.refine
    assert loaded.detect.cluster == baseline.detect.cluster
    assert loaded.parser == baseline.parser
    assert 'password' not in tomllib.loads(exported.read_text())['postgres']
    from shapely.geometry import box, mapping
    catalog = tmp_path / 'catalog.geojson'
    catalog.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'id': 'inside', 'properties': {}, 'geometry': mapping(box(111.1, 30.1, 111.2, 30.2))},
        {'type': 'Feature', 'id': 'outside', 'properties': {}, 'geometry': mapping(box(113, 30, 113.1, 30.1))}]}))
    config.labels_file = catalog
    config.labels_layer = 'labeled_intersections'
    config.labels_id_field = 'fid'
    exported = write_best_config(config, 25.0)
    frozen = load_workspace(exported, password='test').case.truth_file
    assert frozen is not None
    assert [feature['id'] for feature in json.loads(frozen.read_text())['features']] == ['inside']
    catalog.unlink()
    assert load_workspace(exported, password='test').case.truth_file == frozen
