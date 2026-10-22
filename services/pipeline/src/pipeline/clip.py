"""Clip orchestration: native geometry, self-contained Parquet, immutable lineage."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString, shape

from . import _native
from .artifacts import CLIP_FIELDS, ParquetSink, key, schema, seal_stage
from .source import _scalar, connection, iter_groups


_QUERY = "SELECT * FROM read_parquet(?) ORDER BY vehicle_id, subtrack_id, sequence"


def _point_attributes(rows: list[dict], item: dict, clip_stage_id: str,
                      geographic: Transformer) -> tuple[list[str], list[str]]:
    """Keep every original field; interpolated boundaries carry both parent rows."""
    point_ids, attributes = [], []
    lon, lat = geographic.transform(item['xy'][:, 0], item['xy'][:, 1])
    for position, source_index in enumerate(item['source_index']):
        a, b = int(item['from_index'][position]), int(item['to_index'][position])
        ratio = float(item['ratio_in_gap'][position])
        if source_index >= 0:
            record = {name: value for name, value in rows[int(source_index)].items() if name != 'geometry'}
            point_id = record['point_id']
        else:
            left = {name: value for name, value in rows[a].items() if name != 'geometry'}
            right = {name: value for name, value in rows[b].items() if name != 'geometry'}
            point_id = key(clip_stage_id, left['subtrack_id'], 'boundary', left['point_id'], right['point_id'], ratio)
            record = {**left, 'point_id': point_id, 'kind': 'clip_boundary',
                      'source_id': None, 'source_point_id': None, 'source_seq': None,
                      'sequence': None, 'heading_cal': None,
                      'timestamp': left['timestamp'] + ratio * (right['timestamp'] - left['timestamp']),
                      'from_point_id': left['point_id'], 'to_point_id': right['point_id'],
                      'ratio_in_gap': ratio, 'parents': [left, right]}
            for name in ('speed', 'heading'):
                if left.get(name) is None or right.get(name) is None:
                    record[name] = None
                elif name == 'heading':
                    delta = (right[name] - left[name] + 180.0) % 360.0 - 180.0
                    record[name] = (left[name] + ratio * delta) % 360.0
                else:
                    record[name] = left[name] + ratio * (right[name] - left[name])
            record['from_source_point_id'] = left.get('source_point_id')
            record['to_source_point_id'] = right.get('source_point_id')
            record['attributes_json'] = json.dumps({
                'generated_reason': 'intersection_disk_boundary',
                'from': json.loads(left['attributes_json']),
                'to': json.loads(right['attributes_json']),
            }, ensure_ascii=False, allow_nan=False)
        record.update(x=float(item['xy'][position, 0]), y=float(item['xy'][position, 1]),
                      lon=float(lon[position]), lat=float(lat[position]))
        point_ids.append(point_id)
        attributes.append(json.dumps(_scalar(record), ensure_ascii=False, allow_nan=False))
    return point_ids, attributes


def run_clip(config: dict, run_id: str, directory: Path, refine_dir: Path,
             intersections: list[dict], monitor) -> dict:
    """Read one complete subtrack at a time; never reconnect a temporal break."""
    directory, refine_dir = Path(directory), Path(refine_dir)
    directory.mkdir(parents=True, exist_ok=True)
    refine_id, detect_id, clip_stage_id = (key(run_id, name) for name in ('refine', 'detect', 'clip'))
    marker = refine_dir / 'manifest.json'
    if marker.exists():
        refine_id = json.loads(marker.read_text())['stage_id']
    resources = config['resources']
    native_config = {**config['clip'], 'cross_grid_size_m': config['detect']['cross_grid_size_m'],
                     **{name: resources[name] for name in ('max_candidates', 'max_pair_checks', 'max_grid_memberships')}}
    # Run IDs are intentionally unique. Geometry, not a hash containing run_id,
    # defines the orientation and resampling anchor across repeated experiments.
    nodes = sorted(intersections, key=lambda item: (shape(item['geometry']).x,
                                                    shape(item['geometry']).y, str(item['id'])))
    node_rank = {str(item['id']): index for index, item in enumerate(nodes)}
    centers = np.asarray([[shape(item['geometry']).x, shape(item['geometry']).y] for item in nodes], dtype=float).reshape(-1, 2)
    radii = np.asarray([item['properties']['radius_m'] for item in nodes], dtype=float)
    crs = config['input']['processing_crs']
    geographic = Transformer.from_crs(crs, 4326, always_xy=True)
    path = directory / 'clips.parquet'
    with monitor.stage('clip') as span:
        with connection(config, directory / 'spill') as database, ParquetSink(path, schema(CLIP_FIELDS, crs, 'LineString'), resources['batch_rows']) as sink:
            groups = iter_groups(database, _QUERY, [str(refine_dir / 'points.parquet')],
                                 ('vehicle_id', 'subtrack_id'), resources['batch_rows'], resources['max_vehicle_points'])
            while True:
                with span.phase('read'):
                    rows = next(groups, None)
                if rows is None:
                    break
                with span.phase('compute'):
                    xy = np.asarray([[row['x'], row['y']] for row in rows], dtype=float)
                    clips = _native.clip_track(xy, centers, radii, native_config)
                with span.phase('write'):
                    for ordinal, item in enumerate(clips):
                        start, end = int(item['start_intersection_index']), int(item['end_intersection_index'])
                        from_id = str(nodes[start]['id']) if start >= 0 else None
                        to_id = str(nodes[end]['id']) if end >= 0 else None
                        pair = sorted((from_id, to_id), key=node_rank.__getitem__) if from_id is not None and to_id is not None and from_id != to_id else (None, None)
                        point_ids, attributes = _point_attributes(rows, item, clip_stage_id, geographic)
                        clip_id = key(clip_stage_id, rows[0]['subtrack_id'], ordinal)
                        sink.append({'clip_id': clip_id, 'run_id': run_id, 'refine_id': refine_id,
                                     'detect_id': detect_id, 'clip_stage_id': clip_stage_id,
                                     'vehicle_id': rows[0]['vehicle_id'], 'subtrack_id': rows[0]['subtrack_id'],
                                     'from_intersection_id': from_id, 'to_intersection_id': to_id,
                                     'pair_a': pair[0], 'pair_b': pair[1],
                                     'reverse': bool(from_id is not None and to_id is not None and node_rank[from_id] > node_rank[to_id]),
                                     'clip_type': item['clip_type'], 'point_ids': point_ids,
                                     'point_attributes_json': attributes, 'xy': item['xy'].tolist(),
                                     'length_m': float(item['length_m']), 'geometry': LineString(item['xy']).wkb})
                span.add_counts(input_points=len(rows), subtracks=1, clips=len(clips))
            with span.phase('write'):
                sink.close()
        with span.phase('write'):
            return seal_stage(directory, 'clip', clip_stage_id,
                              {'refine_id': refine_id, 'detect_id': detect_id},
                              {'clips': (path, 'clip_id')}, native_config)
