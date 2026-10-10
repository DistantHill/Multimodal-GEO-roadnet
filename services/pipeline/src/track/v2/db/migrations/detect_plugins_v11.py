"""Add algorithm-instance identities without rewriting historical evidence."""
from __future__ import annotations
from typing import Any
from ..schema import SCHEMA_COMPONENT


def migrate_detect_plugins_v11(cur: Any) -> None:
    from ...cli.init_db import DDL_STATEMENTS
    cur.execute("SELECT version FROM pipeline_schema_versions WHERE component=%s FOR UPDATE", (SCHEMA_COMPONENT,))
    row = cur.fetchone()
    if row is None or int(row[0]) != 10:
        raise RuntimeError("Detect plugin migration requires track.v2 schema v10")
    cur.execute(dict(DDL_STATEMENTS)["detect_candidate_instances"])
    cur.execute("""
        ALTER TABLE turn_events
        ADD COLUMN algorithm_id TEXT, ADD COLUMN instance_id TEXT,
        ADD COLUMN local_candidate_id BIGINT, ADD COLUMN evidence_index BIGINT
    """)
    cur.execute("""
        ALTER TABLE tracks_cross
        ADD COLUMN algorithm_id TEXT, ADD COLUMN instance_id TEXT,
        ADD COLUMN local_candidate_id BIGINT, ADD COLUMN evidence_index BIGINT
    """)
    cur.execute("""
        ALTER TABLE turn_events_geom
        ADD COLUMN algorithm_id TEXT, ADD COLUMN instance_id TEXT,
        ADD COLUMN local_candidate_id BIGINT, ADD COLUMN evidence_index BIGINT
    """)
    cur.execute("""
        ALTER TABLE trun_hot_ponits
        ADD COLUMN algorithm_id TEXT, ADD COLUMN instance_id TEXT,
        ADD COLUMN local_candidate_id BIGINT, ADD COLUMN evidence_index BIGINT
    """)
    cur.execute("""
        ALTER TABLE turn_events
        DROP CONSTRAINT turn_events_resolved_point_key,
        ADD CONSTRAINT turn_events_resolved_point_key
            UNIQUE NULLS NOT DISTINCT (detect_id, algorithm_id, instance_id, resolved_point_id),
        ADD CONSTRAINT turn_events_instance_candidate_key
            UNIQUE (detect_id, algorithm_id, instance_id, local_candidate_id),
        ADD CONSTRAINT turn_events_instance_identity_check CHECK (
            num_nonnulls(algorithm_id, instance_id, local_candidate_id, evidence_index) = 0
            OR (num_nonnulls(algorithm_id, instance_id, local_candidate_id, evidence_index) = 4
                AND local_candidate_id >= 0 AND evidence_index >= 0)
        )
    """)
    cur.execute("""
        ALTER TABLE tracks_cross
        DROP CONSTRAINT tracks_cross_segment_pair_key,
        ADD CONSTRAINT tracks_cross_segment_pair_key
            UNIQUE NULLS NOT DISTINCT (detect_id, algorithm_id, instance_id, from_segment_start_point_id, to_segment_start_point_id),
        ADD CONSTRAINT tracks_cross_instance_candidate_key
            UNIQUE (detect_id, algorithm_id, instance_id, local_candidate_id),
        ADD CONSTRAINT tracks_cross_instance_identity_check CHECK (
            num_nonnulls(algorithm_id, instance_id, local_candidate_id, evidence_index) = 0
            OR (num_nonnulls(algorithm_id, instance_id, local_candidate_id, evidence_index) = 4
                AND local_candidate_id >= 0 AND evidence_index >= 0)
        )
    """)
    cur.execute("UPDATE pipeline_schema_versions SET version=11 WHERE component=%s", (SCHEMA_COMPONENT,))
