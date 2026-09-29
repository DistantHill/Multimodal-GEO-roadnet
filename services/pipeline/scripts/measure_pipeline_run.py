"""Measure one fresh container's Pipeline Run without resetting shared counters.

Launch in a fresh uncapped Pipeline container using the versioned
infra/docker/compose.pipeline-measure.yml override. The Pipeline process logs
ERROR above 6,000,000,000 B without stopping. Docker does not keep swap
disabled when its memory cap is removed, so record the effective swap setting.
The final cgroup sample is taken before teardown. Source and PostgreSQL
snapshots are read-only.

Example (from the repository root, with the existing local .env):
  docker compose --env-file .env -f infra/docker/compose.dev.yml \
    -f infra/docker/compose.pipeline-measure.yml run --rm \
    -e PIPELINE_CONFIG_PATH=/app/config/pipeline.toml pipeline \
    python scripts/measure_pipeline_run.py --source-table public.trajectory_points \
    --record /data/logs/full-measurement.json --revision <fixed-revision> \
    --pipeline-arg=--refine-resume-last --pipeline-arg=false

To reuse a completed Refine Run while still counting its protected source,
pass both --source-table and --refine-id to this measurement wrapper. Only
--refine-id is passed to the Pipeline CLI; no new Refine is started.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib

import psycopg

from track.v2.telemetry.memory_warning import MEMORY_WARNING_BYTES


def read_cgroup(name: str) -> str:
    return (Path('/sys/fs/cgroup') / name).read_text().strip()


def require_uncapped_memory(memory_max: str) -> None:
    if memory_max != 'max':
        raise SystemExit('measurement requires memory.max=max (no container hard memory limit)')


def pipeline_selection_args(source_table: str, refine_id: str | None) -> list[str]:
    return ['--refine-id', refine_id] if refine_id is not None else ['--source-table', source_table]


def require_completed_refine_source(connection, refine_id: str, source_table: str) -> None:
    row = connection.execute(
        'SELECT source_table, status FROM refine_runs WHERE id = %s', (refine_id,)
    ).fetchone()
    if row != (source_table, 'completed'):
        raise SystemExit('explicit Refine Run must be completed and belong to source-table')


def database_snapshot(connection, source_table: str) -> dict:
    from track.v2.db.source_table import validate_source_table
    contract = validate_source_table(connection, source_table)
    source_rows = connection.execute(f'SELECT count(*) FROM {contract.quoted_table}').fetchone()[0]
    counters = connection.execute('SELECT temp_files, temp_bytes FROM pg_stat_database '
                                  'WHERE datname=current_database()').fetchone()
    database_bytes = connection.execute('SELECT pg_database_size(current_database())').fetchone()[0]
    connection.rollback()
    return {'source_rows': source_rows, 'temp_files': counters[0],
            'temp_bytes': counters[1], 'database_bytes': database_bytes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-table', required=True)
    parser.add_argument('--refine-id', help='Explicit completed Refine Run to reuse; source-table remains the count target.')
    parser.add_argument('--record', required=True, type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--pipeline-arg', action='append', default=[],
                        help='Additional explicit Pipeline CLI argument (repeatable).')
    args = parser.parse_args()
    config_path = Path(os.environ['PIPELINE_CONFIG_PATH'])
    config = tomllib.loads(config_path.read_text())['postgres']
    memory_limit = read_cgroup('memory.max')
    swap_limit = read_cgroup('memory.swap.max')
    require_uncapped_memory(memory_limit)
    events_before = read_cgroup('memory.events')
    with psycopg.connect(host=config['host'], port=config['port'], dbname=config['database'],
                        user=config['user'], password=os.environ['POSTGRES_PASSWORD']) as connection:
        if args.refine_id is not None:
            require_completed_refine_source(connection, args.refine_id, args.source_table)
        database_before = database_snapshot(connection, args.source_table)
        stats_path = args.record.with_suffix('.stats.json')
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        return_code = subprocess.call([sys.executable, '-m', 'pipeline', 'run',
                                       *pipeline_selection_args(args.source_table, args.refine_id),
                                       '--stats-json', str(stats_path),
                                       *args.pipeline_arg])
        elapsed = time.monotonic() - started
        database_after = database_snapshot(connection, args.source_table)
    record = {'revision': args.revision, 'config': str(config_path), 'source_table': args.source_table,
              'refine_id': args.refine_id,
              'return_code': return_code, 'elapsed_seconds': elapsed, 'pipeline_args': args.pipeline_arg,
              'stats_path': str(stats_path),
              'pipeline_stats': json.loads(stats_path.read_text()) if stats_path.exists() else None,
              'memory_warning_threshold_bytes': MEMORY_WARNING_BYTES,
              'memory_max': memory_limit, 'memory_swap_max': swap_limit,
              'memory_current_bytes': int(read_cgroup('memory.current')),
              'memory_swap_current_bytes': int(read_cgroup('memory.swap.current')),
              'memory_peak_bytes': int(read_cgroup('memory.peak')),
              'memory_events_before': events_before, 'memory_events_after': read_cgroup('memory.events'),
              'postgres_before': database_before, 'postgres_after': database_after,
              'postgres_temp_bytes_delta': database_after['temp_bytes'] - database_before['temp_bytes'],
              'database_bytes_delta': database_after['database_bytes'] - database_before['database_bytes']}
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, indent=2))
    return return_code


if __name__ == '__main__':
    raise SystemExit(main())
