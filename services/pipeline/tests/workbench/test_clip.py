"""Clipping can be replayed from its own Parquet without source-table joins."""
from copy import deepcopy
import json

import pyarrow.parquet as pq
import pytest
from shapely.geometry import Point, mapping

from pipeline.artifacts import POINT_FIELDS, ParquetSink, key, schema, write_json
from pipeline.clip import run_clip
from pipeline.config import DEFAULTS
from pipeline.resources import ResourceMonitor


def test_clip_artifact_keeps_interpolated_and_source_attributes(tmp_path):
    config = deepcopy(DEFAULTS)
    config['input']['processing_crs'] = 'EPSG:32650'
    run_id = 'clip-fixture'
    refine_id = key(run_id, 'refine')
    refine_dir = tmp_path / 'refine'
    write_json(refine_dir / 'manifest.json', {'stage_id': refine_id})
    with ParquetSink(refine_dir / 'points.parquet', schema(POINT_FIELDS, 'EPSG:32650', 'Point')) as sink:
        for i, x in enumerate((-100., 200.)):
            row = {name: None for name, _ in POINT_FIELDS}
            row.update(source_point_id=f'source-{i}', source_id=str(i), vehicle_id='vehicle', vehicle_code=1,
                       source_seq=i, timestamp=float(30 * i), x=x, y=0., lon=112., lat=0., heading=90.,
                       speed=10., attributes_json=json.dumps({'custom_flag': f'original-{i}'}),
                       geometry=Point(x, 0).wkb, run_id=run_id, refine_id=refine_id, point_id=f'point-{i}',
                       subtrack_id='track', subtrack_index=1, sequence=i, kind='kept', heading_cal=90.,
                       from_source_point_id=f'source-{i}', to_source_point_id=f'source-{i}', ratio_in_gap=0.)
            sink.append(row)
    nodes = [{'type': 'Feature', 'id': str(i), 'geometry': mapping(Point(x, 0)),
              'properties': {'radius_m': 10., 'detect_id': key(run_id, 'detect')}} for i, x in enumerate((0., 100.))]
    with ResourceMonitor(tmp_path / 'metrics', sample_interval_ms=5) as monitor:
        manifest = run_clip(config, run_id, tmp_path / 'clip', refine_dir, nodes, monitor)
    rows = pq.read_table(tmp_path / 'clip' / 'clips.parquet').to_pylist()
    assert manifest['datasets']['clips']['rows'] == len(rows) == 3
    middle = next(row for row in rows if row['clip_type'] == 'between_intersection')
    assert middle['pair_a'] == '0' and middle['pair_b'] == '1'
    assert middle['reverse'] is False
    assert middle['length_m'] == pytest.approx(80.)
    details = [json.loads(value) for value in middle['point_attributes_json']]
    assert details[0]['kind'] == 'clip_boundary'
    assert details[0]['timestamp'] == pytest.approx(11.)
    assert details[0]['parents'][0]['point_id'] == 'point-0'
    assert json.loads(details[0]['parents'][0]['attributes_json']) == {'custom_flag': 'original-0'}
    assert details[0]['point_id'] == middle['point_ids'][0]
    assert details[0]['source_point_id'] is None
    assert len({row['clip_id'] for row in rows}) == 3
    # Geometry and full point metadata remain available after the upstream file is gone.
    (refine_dir / 'points.parquet').unlink()
    replayed = pq.read_table(tmp_path / 'clip' / 'clips.parquet').to_pylist()
    assert replayed[1]['xy'] == middle['xy']
