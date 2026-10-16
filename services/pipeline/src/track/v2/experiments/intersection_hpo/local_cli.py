"""Prepare/run bounded local polygon-F1 optimization without rerunning Refine."""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import tomllib
from urllib.parse import quote, quote_plus
from uuid import UUID

from ..config import load_workspace
from .config import code_revision
from .local_inputs import prepare_local_inputs
from .local_search import LocalSearch, LocalSearchStopped, _write
from .tune import postgres_storage_url


@dataclass(frozen=True)
class LocalConfig:
    config_path: Path
    experiment_config: Path
    scheme: str
    labels_file: Path
    labels_layer: str
    labels_id_field: str
    output_root: Path
    parent_refine_id: str
    processing_srid: int


def load_local_config(path: str | Path) -> LocalConfig:
    path = Path(path).resolve()
    payload = tomllib.loads(path.read_text())
    if set(payload) != {'local_hpo'}:
        raise ValueError('Local HPO configuration must contain only [local_hpo]')
    raw = payload['local_hpo']
    required = {'experiment_config', 'scheme', 'labels_file', 'output_root', 'parent_refine_id'}
    allowed = required | {'labels_layer', 'labels_id_field', 'processing_srid'}
    if set(raw) - allowed or required - set(raw):
        raise ValueError(f'Invalid local_hpo fields: missing={sorted(required-set(raw))}, unknown={sorted(set(raw)-allowed)}')
    for key in required | ({'labels_layer', 'labels_id_field'} & set(raw)):
        if not isinstance(raw[key], str) or not raw[key].strip() or raw[key].startswith('REQUIRED'):
            raise ValueError(f'{key} requires an actual nonempty string')
    parent = str(UUID(raw['parent_refine_id']))
    srid = raw.get('processing_srid', 32649)
    if type(srid) is not int or srid <= 0:
        raise ValueError('processing_srid must be a positive integer')
    resolved = {key: (path.parent / raw[key]).resolve() for key in ('experiment_config', 'labels_file', 'output_root')}
    # Container layouts omit the repository root; fixed parent offsets may
    # resolve to filesystem '/'. Locate the actual checkout marker when present.
    source = Path(__file__).resolve()
    repository = next((parent for parent in source.parents if (parent / 'AGENTS.md').is_file()
                       and (parent / 'services/pipeline').is_dir()), source.parents[4])
    if resolved['output_root'].is_relative_to(repository):
        raise ValueError('Local HPO runtime output_root must be outside the repository')
    return LocalConfig(path, **resolved, scheme=raw['scheme'], parent_refine_id=parent,
                       labels_layer=raw.get('labels_layer', 'TP_intersection_fuping'),
                       labels_id_field=raw.get('labels_id_field', 'fid'), processing_srid=srid)


def _config_identity(config, workspace) -> dict:
    from .local_adapter import RULES, LABEL_SEMANTICS
    return {'evaluation': {**RULES, 'region_policy': 'centroid_covers'}, 'label_semantics': LABEL_SEMANTICS,'config': {key: str(value) if isinstance(value, Path) else value for key, value in asdict(config).items() if key != 'config_path'},
            'experiment_sha256': hashlib.sha256(config.experiment_config.read_bytes()).hexdigest(),
            'labels_sha256': hashlib.sha256(config.labels_file.read_bytes()).hexdigest(),
            'source_revision': code_revision(), 'case': workspace.case.snapshot(),
            'effective_parameters': workspace.configs[config.scheme].snapshot_parameters()}


def _prepare_caches(config, workspace, inputs) -> dict:
    from ...config import connect
    from ...db.pipeline_store import PostgresPipelineStore
    from ...db.artifact_registry import find_refine_artifact
    from ...stages.refine import load_completed_refine_ref
    from .local_refine import prepare_local_refine
    effective = workspace.configs[config.scheme]
    with connect(effective.postgres) as connection:
        store = PostgresPipelineStore(connection)
        record = store.get_refine_run(config.parent_refine_id)
        if record.source_table != workspace.case.source_table:
            raise ValueError('Parent Refine source_table must match the configured experiment case')
        reference = load_completed_refine_ref(store=store, refine_id=config.parent_refine_id, record=record,
                       time_start=workspace.case.time_start, time_end=workspace.case.time_end)
        if record.spatial_context.processing_srid != config.processing_srid:
            raise ValueError('Parent Refine processing CRS must match local HPO processing_srid')
        with connection.cursor() as cursor:
            registered = find_refine_artifact(cursor, config.parent_refine_id)
    return prepare_local_refine(effective.paths.parquet_directory, registered.descriptor, registered.state,
                       effective.runtime, inputs, config.output_root / 'local-refine',
                       time_start=reference.time_start, time_end=reference.time_end)


ARCHIVE_PREFLIGHT_SQL = """SELECT to_regclass('public.eval_experiments'),
    to_regclass('public.eval_predictions'), to_regclass('public.eval_evaluations'),
    to_regclass('public.eval_truths'), postgis_version()"""


def _archive_preflight(workspace) -> None:
    """Read metadata only; never initialize or migrate the existing archive."""
    from pipeline.experiments.archive import connect_archive
    if workspace.archive_settings['database'] != 'roadnet_eval':
        raise ValueError('Local HPO archive must be roadnet_eval')
    with connect_archive(workspace.archive_settings) as connection:
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute(ARCHIVE_PREFLIGHT_SQL)
            row = cursor.fetchone()
    if row is None or len(row) != 5 or any(value is None for value in row[:4]) or not row[4]:
        raise ValueError('roadnet_eval requires the four existing eval archive tables and callable PostGIS')


def prepare(config):
    workspace = load_workspace(config.experiment_config, password=os.environ.get('POSTGRES_PASSWORD', ''), schemes=[config.scheme])
    if workspace.case.time_start is None or workspace.case.time_end is None:
        raise ValueError('Local HPO requires explicit experiment case time_start and time_end')
    effective = workspace.configs[config.scheme]
    if not config.output_root.is_relative_to(effective.paths.data_root.resolve()):
        raise ValueError('Local HPO output_root must be under configured paths.data_root')
    if workspace.archive_settings['database'] != 'roadnet_eval':
        raise ValueError('Local HPO requires independent roadnet_eval archive storage')
    from .adapters import threshold_config
    threshold_config(effective, 45.)
    identity = _config_identity(config, workspace)
    marker = config.output_root / 'preparation.json'
    if marker.exists() and json.loads(marker.read_text()).get('config_identity') != identity:
        raise ValueError('Prepared local HPO identity changed; use a separate output_root')
    _archive_preflight(workspace)
    config.output_root.mkdir(parents=True, exist_ok=True)
    inputs = prepare_local_inputs(config.labels_file, config.output_root / 'inputs', layer=config.labels_layer,
                                 id_field=config.labels_id_field, seed=42, sizes=(20, 50), processing_srid=config.processing_srid)
    caches = _prepare_caches(config, workspace, inputs)
    config_identity = identity
    identity = {**identity, 'inputs_sha256': inputs['sha256'], 'local_refine_cache_key': caches['cache_key']}
    _write(marker, {'config_identity': config_identity, 'identity': identity,
                    'inputs_sha256': inputs['sha256'], 'cache_key': caches['cache_key']})
    return workspace, inputs, caches, identity


def write_local_best(config, workspace, inputs: dict, threshold: float) -> tuple[Path, Path]:
    """Freeze the deduplicated L3 truth and preserve the exact resume settings."""
    from ...cli.experiment import toml_text
    from .adapters import threshold_config
    effective = threshold_config(workspace.configs[config.scheme], threshold)
    root = config.output_root
    root.mkdir(parents=True, exist_ok=True)
    truth_path = root / 'best-L3-truth.geojson'
    _write(truth_path, inputs['levels']['L3']['truth'])
    payload = copy.deepcopy(workspace.payload)
    payload['experiment'].update(case=workspace.case.name, schemes=[config.scheme], workspace=str(workspace.root))
    scheme = copy.deepcopy(payload['schemes'][config.scheme])
    scheme['detect'] = effective.detect.as_dict()
    payload['schemes'] = {config.scheme: scheme}
    case = copy.deepcopy(payload['cases'][workspace.case.name])
    if case.get('region_file'):
        case['region_file'] = str((config.experiment_config.parent / case['region_file']).resolve())
    case['truth_file'] = str(truth_path.resolve())
    payload['cases'] = {workspace.case.name: case}
    from .local_adapter import RULES
    payload['evaluation'] = dict(RULES)
    payload['parser']['field_specs_path'] = str(effective.parser.field_specs_path)
    payload['logging']['directory'] = str(effective.logging.directory)
    payload['paths'] = {key: str(value) for key, value in asdict(effective.paths).items()}
    ordinary = root / 'best_config.toml'
    ordinary.write_text('# Selected parameters for a separate ordinary experiment; not launched automatically.\n'
                        '# Original case region differs from local computation/evaluation ROI.\n'
                        '# Truth faces mark intersection locations; matching uses 30m center distance, not area overlap.\n'
                        '# Truth is the fixed deduplicated L3 truth; a new run is outside the 60-call search budget.\n'
                        + toml_text(payload))
    resume = root / 'best_local_config.toml'
    settings = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(config).items() if key != 'config_path'}
    resume.write_text(f'# Selected threshold: {threshold}; same identity for idempotent search resume.\n'
                      '# The original fixed search settings and input versions are preserved.\n'
                      + toml_text({'local_hpo': settings}))
    return ordinary, resume


def run(config) -> dict:
    workspace, inputs, caches, identity = prepare(config)
    from .local_adapter import LocalEvaluator
    evaluator = LocalEvaluator(workspace, config.scheme, config.parent_refine_id, inputs, caches,
                               config.output_root, identity)
    storage = postgres_storage_url(workspace.archive_settings)
    try:
        summary = LocalSearch(config.output_root / 'search', evaluator, storage, identity, seed=42).run()
    except LocalSearchStopped:
        # A later failure does not discard the best L3 result already archived.
        best_path = config.output_root / 'search' / 'best.json'
        if best_path.exists():
            best = json.loads(best_path.read_text())
            write_local_best(config, workspace, inputs, best['best']['threshold'])
        raise
    if summary['best'] is not None:
        write_local_best(config, workspace, inputs, summary['best']['threshold'])
    return summary


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description='Prepare/run fixed 40/12/8 local polygon-F1 HPO')
    parser.add_argument('--config', dest='global_config', type=Path)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('prepare', 'run', 'status'):
        sub = subparsers.add_parser(command)
        sub.add_argument('--config', type=Path)
    args = parser.parse_args(argv)
    config_path = args.config or args.global_config
    if config_path is None:
        parser.error('--config is required')
    try:
        config = load_local_config(config_path)
        if args.command == 'status':
            path = config.output_root / 'search' / 'ledger.json'
            if not path.exists():
                print(json.dumps({'status': 'NOT_STARTED', 'call_counts': {'L1': 0, 'L2': 0, 'L3': 0}}))
            else:
                ledger = json.loads(path.read_text())
                print(json.dumps({'status': ledger['status'], 'call_counts': {level: sum(c['level'] == level for c in ledger['calls']) for level in ('L1', 'L2', 'L3')},
                                  'stop_reason': ledger.get('stop_reason')}, indent=2))
        elif args.command == 'prepare':
            _, inputs, caches, _ = prepare(config)
            print(json.dumps({'status': 'PREPARED', 'inputs_sha256': inputs['sha256'], 'cache_key': caches['cache_key'],
                              'level_sizes': {key: value['effective_count'] for key, value in inputs['levels'].items()}}, indent=2))
        else:
            summary = run(config)
            print(json.dumps({'status': summary['status'], 'call_counts': summary['call_counts'],
                              'best_threshold': summary['best']['threshold'] if summary['best'] else None}, indent=2))
        return 0
    except Exception as error:
        detail = str(error)
        password = os.environ.get('POSTGRES_PASSWORD')
        if password:
            for secret in (password, quote(password, safe=''), quote_plus(password)):
                detail = detail.replace(secret, '[REDACTED]')
        print(json.dumps({'status': 'BLOCKED', 'error': detail}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
