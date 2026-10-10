"""Export a selected threshold as a runnable existing experiment workspace."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

from ..config import load_workspace, load_case
from ...cli.experiment import toml_text
from .adapters import threshold_config


def write_best_config(config, threshold: float) -> Path:
    workspace = load_workspace(config.experiment_config,
                               password=os.environ.get('POSTGRES_PASSWORD', ''),
                               schemes=[config.scheme])
    effective = threshold_config(workspace.configs[config.scheme], threshold)
    payload = copy.deepcopy(workspace.payload)
    payload['experiment']['schemes'] = [config.scheme]
    payload['experiment']['workspace'] = str(workspace.root)
    # Preserve non-Detect overrides and all production settings.
    selected = copy.deepcopy(payload['schemes'][config.scheme])
    selected['detect'] = effective.detect.as_dict()
    payload['schemes'] = {config.scheme: selected}
    base = config.experiment_config.parent
    labels = None
    if getattr(config, 'labels_file', None) is not None:
        from .labels import load_labels
        labels, _ = load_labels(config.labels_file, layer=config.labels_layer,
                                id_field=config.labels_id_field)
    for name, case in payload['cases'].items():
        for key in ('region_file', 'truth_file'):
            if case.get(key):
                case[key] = str((base / case[key]).resolve())
        if labels is not None:
            from shapely.geometry import shape
            selection = load_case(name, {k: v for k, v in case.items() if k != 'truth_file'}, base)
            boundary = shape(selection.geometry)
            truth = {'type': 'FeatureCollection', 'features': [feature for feature in labels
                     if boundary.covers(shape(feature['geometry']).centroid)]}
            contents = json.dumps(truth, sort_keys=True, ensure_ascii=False)
            frozen = Path(config.output_root) / 'best-inputs' / (hashlib.sha256(contents.encode()).hexdigest() + '.geojson')
            frozen.parent.mkdir(parents=True, exist_ok=True)
            if frozen.exists() and frozen.read_text(encoding='utf-8') != contents:
                raise ValueError('Conflicting frozen best-config labels')
            frozen.write_text(contents, encoding='utf-8')
            case['truth_file'] = str(frozen.resolve())
    payload['parser']['field_specs_path'] = str((base / payload['parser']['field_specs_path']).resolve())
    if payload.get('logging', {}).get('directory'):
        payload['logging']['directory'] = str((base / payload['logging']['directory']).resolve())
    target = Path(config.output_root) / 'best_config.toml'
    target.parent.mkdir(parents=True, exist_ok=True)
    text = '# Selected HPO threshold; run using the existing experiment CLI.\n' + toml_text(payload)
    temporary = target.with_suffix('.toml.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(target)
    return target
