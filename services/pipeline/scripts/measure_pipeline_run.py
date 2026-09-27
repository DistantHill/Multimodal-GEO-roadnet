"""Measure one fresh container's Pipeline Run without resetting shared counters.

Launch this in a new container with memory.max=2,000,000,000 and swap disabled.
The subprocess inherits the container cgroup; the final sample is taken before
container teardown. Source row counts and PostgreSQL counters are read-only.
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


def read_cgroup(name: str) -> str:
    return (Path('/sys/fs/cgroup') / name).read_text().strip()


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
    parser.add_argument('--record', required=True, type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--pipeline-arg', action='append', default=[],
                        help='Additional explicit Pipeline CLI argument (repeatable).')
    args = parser.parse_args()
    config_path = Path(os.environ['PIPELINE_CONFIG_PATH'])
    config = tomllib.loads(config_path.read_text())['postgres']
    memory_limit = read_cgroup('memory.max')
    swap_limit = read_cgroup('memory.swap.max')
    # cgroup v2 rounds Docker's byte limit down to an OS page boundary.
    effective_limit = int(memory_limit) if memory_limit != 'max' else 0
    if not 0 <= 2_000_000_000 - effective_limit < os.sysconf('SC_PAGE_SIZE') or swap_limit != '0':
        raise SystemExit('measurement requires memory.max=2000000000 and memory.swap.max=0')
    events_before = read_cgroup('memory.events')
    with psycopg.connect(host=config['host'], port=config['port'], dbname=config['database'],
                        user=config['user'], password=os.environ['POSTGRES_PASSWORD']) as connection:
        database_before = database_snapshot(connection, args.source_table)
        stats_path = args.record.with_suffix('.stats.json')
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        return_code = subprocess.call([sys.executable, '-m', 'pipeline', 'run',
                                       '--source-table', args.source_table, '--stats-json', str(stats_path),
                                       *args.pipeline_arg])
        elapsed = time.monotonic() - started
        database_after = database_snapshot(connection, args.source_table)
    record = {'revision': args.revision, 'config': str(config_path), 'source_table': args.source_table,
              'return_code': return_code, 'elapsed_seconds': elapsed, 'pipeline_args': args.pipeline_arg,
              'stats_path': str(stats_path),
              'pipeline_stats': json.loads(stats_path.read_text()) if stats_path.exists() else None,
              'requested_memory_max_bytes': 2_000_000_000,
              'memory_max_bytes': effective_limit, 'memory_swap_max_bytes': int(swap_limit),
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
