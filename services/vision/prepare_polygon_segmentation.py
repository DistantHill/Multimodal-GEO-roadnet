"""Build spatially separated YOLO segmentation data from buffered raster samples.

Run with the Pipeline Python environment (Shapely 2, pyproj, Pillow).
FP polygons are never foreground. Every known TP in a window is annotated,
including neighbours of the polygon that originally defined that window.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import random

from PIL import Image
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform
from shapely.strtree import STRtree


def read_samples(root: Path, prefix: str):
    result = []
    for path in sorted((root / 'samples').glob('*/annotation.json')):
        item = json.loads(path.read_text())
        item['key'] = prefix + '_' + item['sample_id']
        item['input_directory'] = path.parent
        grid = item['grid']
        item['window'] = box(grid['west'], grid['south'], grid['east'], grid['north'])
        project = Transformer.from_crs(4326, grid['processing_srid'], always_xy=True)
        item['projected'] = transform(project.transform, shape(item['geometry_wgs84']))
        result.append(item)
    if not result:
        raise ValueError(f'no raster samples: {root}')
    return result


def connected_windows(samples):
    windows = [row['window'] for row in samples]
    tree = STRtree(windows)
    parent = list(range(len(windows)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, window in enumerate(windows):
        for j in tree.query(window, predicate='intersects'):
            parent[find(int(j))] = find(i)
    groups = {}
    for i in range(len(samples)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def unique_foreground(samples):
    result = []
    seen = set()
    for row in samples:
        if row['label'] != 'TP':
            continue
        geometry = row['projected']
        key = geometry.normalize().wkb
        if key not in seen:
            seen.add(key)
            result.append(geometry)
    return result


def export_sample(sample, foreground, output: Path, split: str):
    grid = sample['grid']
    image_path = output / 'images' / split / (sample['key'] + '.png')
    label_path = output / 'labels' / split / (sample['key'] + '.txt')
    for path in [image_path, label_path]:
        path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(sample['input_directory'] / 'gray.png') as image:
        image.convert('RGB').save(image_path)
    segments = []
    for geometry in foreground:
        clipped = geometry.intersection(sample['window'])
        if clipped.is_empty or clipped.area == 0:
            continue
        parts = list(clipped.geoms) if hasattr(clipped, 'geoms') else [clipped]
        for polygon in parts:
            if polygon.geom_type != 'Polygon' or polygon.area == 0:
                continue
            if polygon.interiors:
                raise ValueError('YOLO polygon text cannot preserve holes; explicit conversion required')
            coordinates = []
            for x, y, *_ in list(polygon.exterior.coords)[:-1]:
                coordinates.extend([(x - grid['west']) / (grid['east'] - grid['west']),
                                    (grid['north'] - y) / (grid['north'] - grid['south'])])
            if len(coordinates) >= 6:
                segments.append('0 ' + ' '.join(f'{min(1.,max(0.,v)):.9f}' for v in coordinates))
    label_path.write_text('\n'.join(segments) + ('\n' if segments else ''))
    return {'key': sample['key'], 'split': split, 'origin_label': sample['label'],
            'source_file': sample['source_file'], 'fid': sample['fid'],
            'objects': len(segments), 'window_m': list(sample['window'].bounds),
            'image': str(image_path.relative_to(output)), 'label': str(label_path.relative_to(output))}


def prepare(training: Path, validation: Path, output: Path, seed: int = 42):
    if output.exists():
        raise ValueError('dataset output must not already exist')
    train_samples = read_samples(training, 'train')
    test_samples = read_samples(validation, 'zhuangli')
    # The held-out geography must share no raster window area with training.
    for train in train_samples:
        if any(train['window'].intersects(test['window']) for test in test_samples):
            raise ValueError('training windows overlap held-out Zhuangli geography')
    groups = connected_windows(train_samples)
    if len(groups) < 2:
        raise ValueError('all training windows connected; cannot create a spatially independent validation split')
    random.Random(seed).shuffle(groups)
    target = max(1, round(len(train_samples) * .2))
    # Greedy nearest-count split of entire connected components.
    validation_indices = set()
    for group in sorted(groups, key=len, reverse=True):
        if abs(len(validation_indices) + len(group) - target) < abs(len(validation_indices) - target):
            validation_indices.update(group)
    if not validation_indices:
        validation_indices.update(min(groups, key=len))
    foreground = unique_foreground(train_samples)
    heldout_foreground = unique_foreground(test_samples)
    output.mkdir(parents=True)
    rows = [export_sample(sample, foreground, output, 'val' if index in validation_indices else 'train')
            for index, sample in enumerate(train_samples)]
    rows += [export_sample(sample, heldout_foreground, output, 'test') for sample in test_samples]
    # Split paths are relative to the explicit dataset root in this YAML.
    (output / 'data.yaml').write_text(f'path: {output}\ntrain: images/train\nval: images/val\ntest: images/test\nnames:\n  0: intersection\n')
    manifest = {'seed': seed, 'training_raster': str(training), 'zhuangli_raster': str(validation),
                'class_names': ['intersection'], 'fp_policy': 'FP target omitted; known neighbouring TP retained',
                'split_policy': 'connected overlapping windows stay together; Zhuangli held out',
                'component_sizes': sorted(map(len, groups), reverse=True),
                'split_counts': dict(Counter(row['split'] for row in rows)),
                'empty_label_counts': dict(Counter(row['split'] for row in rows if row['objects'] == 0)),
                'unique_training_tp': len(foreground), 'unique_test_tp': len(heldout_foreground), 'samples': rows}
    (output / 'dataset_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    hashes = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(output.rglob('*')) if p.is_file()}
    (output / 'checksums.json').write_text(json.dumps(hashes, indent=2) + '\n')
    print(json.dumps({key: value for key, value in manifest.items() if key != 'samples'}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--training-raster', type=Path, required=True)
    parser.add_argument('--zhuangli-raster', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.training_raster, args.zhuangli_raster, args.output)
