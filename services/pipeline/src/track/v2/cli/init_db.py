"""Create track.v2 schema v12 or migrate an existing v4 through v11 schema in place.

Schema v11 records Detect plugin instances. Schemas v4 through v11 support ``--migrate-artifacts``.
An existing v3 or older database must be recreated with ``--reset`` so an old column can never retain
an ambiguous meaning behind a new API.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..config import PostgresConfig, connect as pg_connect
from ._load_config import load_startup_config
from ..telemetry.console_logging import init_logger, shutdown_logger


SCHEMA_COMPONENT = "track.v2"
SCHEMA_VERSION = 12


DDL_STATEMENTS: list[tuple[str, str]] = [
    (
        "pipeline_schema_versions",
        """
        CREATE TABLE IF NOT EXISTS pipeline_schema_versions (
            component       TEXT PRIMARY KEY,
            version         INTEGER NOT NULL CHECK (version > 0),
            installed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ),
    (
        "refine_runs",
        """
        CREATE TABLE IF NOT EXISTS refine_runs (
            id                  UUID PRIMARY KEY,
            source_table        TEXT NOT NULL,
            source_srid         INTEGER NOT NULL DEFAULT 4326,
            processing_srid     INTEGER,
            projection_config   JSONB NOT NULL DEFAULT '{}',
            parameters          JSONB NOT NULL DEFAULT '{}',
            source_fingerprint  JSONB,
            status              TEXT NOT NULL
                                CHECK (status IN ('running', 'completed', 'failed')),
            started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at         TIMESTAMPTZ,
            CONSTRAINT refine_runs_source_table_not_blank
                CHECK (BTRIM(source_table) <> ''),
            CONSTRAINT refine_runs_projection_config_object
                CHECK (jsonb_typeof(projection_config) = 'object'),
            CONSTRAINT refine_runs_parameters_object
                CHECK (jsonb_typeof(parameters) = 'object'),
            CONSTRAINT refine_runs_source_fingerprint_object
                CHECK (
                    source_fingerprint IS NULL
                    OR jsonb_typeof(source_fingerprint) = 'object'
                ),
            CONSTRAINT refine_runs_fixed_crs
                CHECK (
                    source_srid = 4326
                    AND (
                        processing_srid IS NULL
                        OR (
                            processing_srid > 0
                            AND processing_srid <> 4326
                            AND projection_config @>
                                '{"coordinate_semantics_version": 2}'::jsonb
                        )
                    )
                )
        )
        """,
    ),
    (
        "detect_runs",
        """
        CREATE TABLE IF NOT EXISTS detect_runs (
            id              UUID PRIMARY KEY,
            refine_id        UUID NOT NULL REFERENCES refine_runs(id) ON DELETE CASCADE,
            parameters      JSONB NOT NULL DEFAULT '{}'
                            CHECK (jsonb_typeof(parameters) = 'object'),
            status          TEXT NOT NULL
                            CHECK (status IN ('running', 'completed', 'failed')),
            started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at     TIMESTAMPTZ
        )
        """,
    ),
    (
        "trajectory_points",
        """
        CREATE TABLE IF NOT EXISTS trajectory_points (
            id              BIGSERIAL PRIMARY KEY,
            vehicle_id      TEXT NOT NULL,
            timestamp       DOUBLE PRECISION NOT NULL,
            lon             DOUBLE PRECISION NOT NULL,
            lat             DOUBLE PRECISION NOT NULL,
            heading         DOUBLE PRECISION NOT NULL DEFAULT 0,
            speed           DOUBLE PRECISION NOT NULL DEFAULT 0,
            geom            geometry(Point, 4326) GENERATED ALWAYS AS (
                                ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                            ) STORED,
            extra           JSONB NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
    ),
    (
        "excluded_points",
        """
        CREATE TABLE IF NOT EXISTS excluded_points (
            refine_id        UUID NOT NULL REFERENCES refine_runs(id) ON DELETE CASCADE,
            source_row_key  BIGINT NOT NULL,
            reason          TEXT NOT NULL,
            geom            geometry(Point, 4326),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (refine_id, source_row_key)
        )
        """,
    ),
    (
        "kept_trajectory_points",
        """
        CREATE TABLE IF NOT EXISTS kept_trajectory_points (
            id                      BIGSERIAL PRIMARY KEY,
            refine_id                UUID NOT NULL
                                    REFERENCES refine_runs(id) ON DELETE CASCADE,
            source_row_key          BIGINT NOT NULL,
            vehicle_id              TEXT NOT NULL,
            subtrack_index          INTEGER NOT NULL CHECK (subtrack_index >= 1),
            sequence_in_subtrack    INTEGER NOT NULL
                                    CHECK (sequence_in_subtrack >= 0),
            heading_cal             DOUBLE PRECISION CHECK (heading_cal >= 0 AND heading_cal < 360),
            x                       DOUBLE PRECISION,
            y                       DOUBLE PRECISION,
            geom                    geometry(Point, 4326),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT kept_points_source_member_key
                UNIQUE (refine_id, source_row_key),
            CONSTRAINT kept_points_sequence_key
                UNIQUE (refine_id, subtrack_index, sequence_in_subtrack)
        )
        """,
    ),
    (
        "injected_points",
        """
        CREATE TABLE IF NOT EXISTS injected_points (
            id                          BIGSERIAL PRIMARY KEY,
            refine_id                    UUID NOT NULL
                                        REFERENCES refine_runs(id) ON DELETE CASCADE,
            vehicle_id                  TEXT NOT NULL,
            subtrack_index              INTEGER NOT NULL CHECK (subtrack_index >= 1),
            after_sequence_in_subtrack  INTEGER NOT NULL
                                        CHECK (after_sequence_in_subtrack >= 0),
            timestamp                   DOUBLE PRECISION NOT NULL,
            lon                         DOUBLE PRECISION NOT NULL,
            lat                         DOUBLE PRECISION NOT NULL,
            x                           DOUBLE PRECISION NOT NULL,
            y                           DOUBLE PRECISION NOT NULL,
            heading                     DOUBLE PRECISION NOT NULL,
            heading_cal                 DOUBLE PRECISION CHECK (heading_cal >= 0 AND heading_cal < 360),
            speed                       DOUBLE PRECISION NOT NULL DEFAULT 0,
            geom                        geometry(Point, 4326) GENERATED ALWAYS AS (
                                            ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                                        ) STORED,
            generated_reason            TEXT NOT NULL DEFAULT 'orthogonal_heading',
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT injected_points_anchor_key
                UNIQUE (refine_id, subtrack_index, after_sequence_in_subtrack)
        )
        """,
    ),
    (
        "resolved_subtrack_points",
        """
        CREATE TABLE IF NOT EXISTS resolved_subtrack_points (
            id                              BIGSERIAL PRIMARY KEY,
            detect_id                       UUID NOT NULL
                                            REFERENCES detect_runs(id) ON DELETE CASCADE,
            subtrack_index                  INTEGER NOT NULL CHECK (subtrack_index >= 1),
            refined_sequence_in_subtrack    INTEGER
                                            CHECK (refined_sequence_in_subtrack >= 0),
            sequence_in_subtrack            INTEGER NOT NULL
                                            CHECK (sequence_in_subtrack >= 0),
            kept_point_id                   BIGINT REFERENCES kept_trajectory_points(id),
            injected_point_id               BIGINT REFERENCES injected_points(id),
            crossing_point_id               BIGINT,
            geom                            geometry(Point, 4326),
            created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT resolved_points_id_detect_key UNIQUE (id, detect_id),
            CONSTRAINT resolved_points_exactly_one_source
                CHECK (num_nonnulls(kept_point_id, injected_point_id, crossing_point_id) = 1),
            CONSTRAINT resolved_points_sequence_kind_check
                CHECK (
                    (crossing_point_id IS NOT NULL AND refined_sequence_in_subtrack IS NULL)
                    OR
                    (crossing_point_id IS NULL AND refined_sequence_in_subtrack IS NOT NULL)
                ),
            CONSTRAINT resolved_points_final_sequence_key
                UNIQUE (detect_id, subtrack_index, sequence_in_subtrack),
            CONSTRAINT resolved_points_refined_sequence_key
                UNIQUE (detect_id, subtrack_index, refined_sequence_in_subtrack),
            CONSTRAINT resolved_points_kept_key UNIQUE (detect_id, kept_point_id),
            CONSTRAINT resolved_points_injected_key UNIQUE (detect_id, injected_point_id),
            CONSTRAINT resolved_points_crossing_key UNIQUE (detect_id, crossing_point_id)
        )
        """,
    ),
    (
        "candidate_intersections",
        """
        CREATE TABLE IF NOT EXISTS candidate_intersections (
            id                      BIGSERIAL PRIMARY KEY,
            detect_id               UUID NOT NULL
                                    REFERENCES detect_runs(id) ON DELETE CASCADE,
            centroid_x              DOUBLE PRECISION NOT NULL,
            centroid_y              DOUBLE PRECISION NOT NULL,
            centroid_lon            DOUBLE PRECISION NOT NULL,
            centroid_lat            DOUBLE PRECISION NOT NULL,
            radius_m                DOUBLE PRECISION NOT NULL CHECK (radius_m > 0),
            geom                    geometry(Polygon, 4326),
            contributors            JSONB NOT NULL DEFAULT '{}'
                                    CHECK (jsonb_typeof(contributors) = 'object'),
            critical_contributors   JSONB NOT NULL DEFAULT '[]'
                                    CHECK (jsonb_typeof(critical_contributors) = 'array'),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT candidate_intersections_id_detect_key UNIQUE (id, detect_id)
        )
        """,
    ),
    (
        "detect_candidate_instances",
        """
        CREATE TABLE IF NOT EXISTS detect_candidate_instances (
            detect_id UUID NOT NULL REFERENCES detect_runs(id) ON DELETE CASCADE,
            algorithm_id TEXT NOT NULL,
            instance_id TEXT NOT NULL,
            params JSONB NOT NULL CHECK (jsonb_typeof(params) = 'object'),
            evidence_kind TEXT NOT NULL,
            evidence_offset BIGINT NOT NULL CHECK (evidence_offset >= 0),
            evidence_count BIGINT NOT NULL CHECK (evidence_count >= 0),
            candidate_count BIGINT NOT NULL CHECK (candidate_count >= 0),
            PRIMARY KEY (detect_id, algorithm_id, instance_id)
        )
        """,
    ),
    (
        "tracks_cross",
        """
        CREATE TABLE IF NOT EXISTS tracks_cross (
            id                              BIGSERIAL PRIMARY KEY,
            detect_id                       UUID NOT NULL
                                            REFERENCES detect_runs(id) ON DELETE CASCADE,
            algorithm_id                    TEXT,
            instance_id                     TEXT,
            local_candidate_id              BIGINT,
            evidence_index                  BIGINT,
            candidate_intersection_id       BIGINT,
            from_segment_start_point_id     BIGINT NOT NULL,
            to_segment_start_point_id       BIGINT NOT NULL,
            x                               DOUBLE PRECISION NOT NULL,
            y                               DOUBLE PRECISION NOT NULL,
            lon                             DOUBLE PRECISION NOT NULL,
            lat                             DOUBLE PRECISION NOT NULL,
            geom                            geometry(Point, 4326) GENERATED ALWAYS AS (
                                                ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                                            ) STORED,
            created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT tracks_cross_instance_identity_check CHECK (
                (num_nonnulls(algorithm_id, instance_id, local_candidate_id, evidence_index) = 0)
                OR (num_nonnulls(algorithm_id, instance_id, local_candidate_id, evidence_index) = 4
                    AND local_candidate_id >= 0 AND evidence_index >= 0)
            ),
            CONSTRAINT tracks_cross_instance_candidate_key
                UNIQUE (detect_id, algorithm_id, instance_id, local_candidate_id),
            CONSTRAINT tracks_cross_candidate_fkey
                FOREIGN KEY (candidate_intersection_id, detect_id)
                REFERENCES candidate_intersections(id, detect_id),
            CONSTRAINT tracks_cross_from_segment_fkey
                FOREIGN KEY (from_segment_start_point_id, detect_id)
                REFERENCES resolved_subtrack_points(id, detect_id),
            CONSTRAINT tracks_cross_to_segment_fkey
                FOREIGN KEY (to_segment_start_point_id, detect_id)
                REFERENCES resolved_subtrack_points(id, detect_id),
            CONSTRAINT tracks_cross_segment_order_check
                CHECK (from_segment_start_point_id < to_segment_start_point_id),
            CONSTRAINT tracks_cross_segment_pair_key
                UNIQUE NULLS NOT DISTINCT (
                    detect_id, algorithm_id, instance_id,
                    from_segment_start_point_id,
                    to_segment_start_point_id
                )
        )
        """,
    ),
    (
        "turn_events",
        """
        CREATE TABLE IF NOT EXISTS turn_events (
            id                          BIGSERIAL PRIMARY KEY,
            detect_id                   UUID NOT NULL
                                        REFERENCES detect_runs(id) ON DELETE CASCADE,
            algorithm_id                    TEXT,
            instance_id                     TEXT,
            local_candidate_id              BIGINT,
            evidence_index                  BIGINT,
            candidate_intersection_id   BIGINT,
            resolved_point_id           BIGINT NOT NULL,
            entry_bearing               DOUBLE PRECISION NOT NULL,
            exit_bearing                DOUBLE PRECISION NOT NULL,
            turn_deg                    DOUBLE PRECISION NOT NULL,
            score                       DOUBLE PRECISION NOT NULL,
            g_star_z                    DOUBLE PRECISION,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT turn_events_instance_identity_check CHECK (
                (num_nonnulls(algorithm_id, instance_id, local_candidate_id, evidence_index) = 0)
                OR (num_nonnulls(algorithm_id, instance_id, local_candidate_id, evidence_index) = 4
                    AND local_candidate_id >= 0 AND evidence_index >= 0)
            ),
            CONSTRAINT turn_events_instance_candidate_key
                UNIQUE (detect_id, algorithm_id, instance_id, local_candidate_id),
            CONSTRAINT turn_events_id_detect_key UNIQUE (id, detect_id),
            CONSTRAINT turn_events_candidate_fkey
                FOREIGN KEY (candidate_intersection_id, detect_id)
                REFERENCES candidate_intersections(id, detect_id),
            CONSTRAINT turn_events_resolved_point_fkey
                FOREIGN KEY (resolved_point_id, detect_id)
                REFERENCES resolved_subtrack_points(id, detect_id),
            CONSTRAINT turn_events_resolved_point_key
                UNIQUE NULLS NOT DISTINCT (detect_id, algorithm_id, instance_id, resolved_point_id)
        )
        """,
    ),
    (
        "turn_events_geom",
        """
        CREATE TABLE IF NOT EXISTS turn_events_geom (
            LIKE turn_events,
            geom geometry(Point, 4326) NOT NULL
        )
        """,
    ),
    (
        "trun_hot_ponits",
        """
        CREATE TABLE IF NOT EXISTS trun_hot_ponits (
            LIKE turn_events,
            geom geometry(Point, 4326) NOT NULL,
            CONSTRAINT trun_hot_ponits_pkey PRIMARY KEY (id),
            CONSTRAINT trun_hot_ponits_event_fkey FOREIGN KEY (id, detect_id)
                REFERENCES turn_events(id, detect_id) ON DELETE CASCADE,
            CONSTRAINT trun_hot_ponits_g_star_required CHECK (g_star_z IS NOT NULL)
        )
        """,
    ),
    (
        "candidate_entry_exit_points",
        """
        CREATE TABLE IF NOT EXISTS candidate_entry_exit_points (
            id                          BIGSERIAL PRIMARY KEY,
            detect_id                   UUID NOT NULL
                                        REFERENCES detect_runs(id) ON DELETE CASCADE,
            candidate_intersection_id   BIGINT NOT NULL,
            segment_start_point_id      BIGINT NOT NULL,
            crossing_type               SMALLINT NOT NULL
                                        CHECK (crossing_type IN (0, 1)),
            crossing_x                  DOUBLE PRECISION NOT NULL,
            crossing_y                  DOUBLE PRECISION NOT NULL,
            crossing_lon                DOUBLE PRECISION NOT NULL,
            crossing_lat                DOUBLE PRECISION NOT NULL,
            geom                        geometry(Point, 4326) GENERATED ALWAYS AS (
                                            ST_SetSRID(
                                                ST_MakePoint(crossing_lon, crossing_lat),
                                                4326
                                            )
                                        ) STORED,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT candidate_crossings_id_detect_key UNIQUE (id, detect_id),
            CONSTRAINT candidate_crossings_candidate_fkey
                FOREIGN KEY (candidate_intersection_id, detect_id)
                REFERENCES candidate_intersections(id, detect_id),
            CONSTRAINT candidate_crossings_segment_start_fkey
                FOREIGN KEY (segment_start_point_id, detect_id)
                REFERENCES resolved_subtrack_points(id, detect_id),
            CONSTRAINT candidate_crossings_segment_type_key
                UNIQUE (
                    detect_id,
                    candidate_intersection_id,
                    segment_start_point_id,
                    crossing_type
                )
        )
        """,
    ),
    (
        "intersections",
        """
        CREATE TABLE IF NOT EXISTS intersections (
            id                          BIGSERIAL PRIMARY KEY,
            detect_id                   UUID NOT NULL
                                        REFERENCES detect_runs(id) ON DELETE CASCADE,
            candidate_intersection_id   BIGINT NOT NULL,
            branch_centers_deg          JSONB NOT NULL DEFAULT '[]'
                                        CHECK (jsonb_typeof(branch_centers_deg) = 'array'),
            transition_counts           JSONB NOT NULL DEFAULT '{}'
                                        CHECK (jsonb_typeof(transition_counts) = 'object'),
            observation_count           INTEGER NOT NULL CHECK (observation_count >= 0),
            clip_radius_m               DOUBLE PRECISION NOT NULL CHECK (clip_radius_m > 0),
            geom                        geometry(Polygon, 4326) NOT NULL,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT intersections_id_detect_key UNIQUE (id, detect_id),
            CONSTRAINT intersections_detect_candidate_key
                UNIQUE (detect_id, candidate_intersection_id),
            CONSTRAINT intersections_candidate_fkey
                FOREIGN KEY (candidate_intersection_id, detect_id)
                REFERENCES candidate_intersections(id, detect_id)
        )
        """,
    ),
    (
        "entry_exit_points",
        """
        CREATE TABLE IF NOT EXISTS entry_exit_points (
            id                  BIGSERIAL PRIMARY KEY,
            detect_id           UUID NOT NULL
                                REFERENCES detect_runs(id) ON DELETE CASCADE,
            intersection_id     BIGINT NOT NULL,
            entry_bearing       DOUBLE PRECISION NOT NULL,
            exit_bearing        DOUBLE PRECISION NOT NULL,
            entry_crossing_id   BIGINT NOT NULL,
            exit_crossing_id    BIGINT NOT NULL,
            geom                geometry(MultiPoint, 4326),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT entry_exit_points_id_detect_key UNIQUE (id, detect_id),
            CONSTRAINT entry_exit_points_intersection_fkey
                FOREIGN KEY (intersection_id, detect_id)
                REFERENCES intersections(id, detect_id),
            CONSTRAINT entry_exit_points_entry_crossing_fkey
                FOREIGN KEY (entry_crossing_id, detect_id)
                REFERENCES candidate_entry_exit_points(id, detect_id),
            CONSTRAINT entry_exit_points_exit_crossing_fkey
                FOREIGN KEY (exit_crossing_id, detect_id)
                REFERENCES candidate_entry_exit_points(id, detect_id),
            CONSTRAINT entry_exit_points_distinct_crossings_check
                CHECK (entry_crossing_id <> exit_crossing_id),
            CONSTRAINT entry_exit_points_pair_key
                UNIQUE (detect_id, intersection_id, entry_crossing_id, exit_crossing_id)
        )
        """,
    ),
    (
        "fit_runs",
        """
        CREATE TABLE IF NOT EXISTS fit_runs (
            id                  UUID PRIMARY KEY,
            detect_id           UUID NOT NULL
                                REFERENCES detect_runs(id) ON DELETE CASCADE,
            clip_algorithm      TEXT NOT NULL,
            clip_version        TEXT NOT NULL,
            fitting_algorithm   TEXT NOT NULL,
            fitting_version     TEXT NOT NULL,
            parameters          JSONB NOT NULL DEFAULT '{}'
                                CHECK (jsonb_typeof(parameters) = 'object'),
            report              JSONB,
            status              TEXT NOT NULL
                                CHECK (status IN ('running', 'completed', 'failed')),
            started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at         TIMESTAMPTZ,
            CONSTRAINT fit_runs_id_detect_key UNIQUE (id, detect_id)
        )
        """,
    ),
    (
        "centerlines",
        """
        CREATE TABLE IF NOT EXISTS centerlines (
            id                      BIGSERIAL PRIMARY KEY,
            fit_id                  UUID NOT NULL,
            detect_id               UUID NOT NULL,
            variant                 TEXT NOT NULL
                                    CHECK (variant IN ('combined', 'directed')),
            from_intersection_id    BIGINT NOT NULL,
            to_intersection_id      BIGINT NOT NULL,
            geom                    geometry(LineString, 4326) NOT NULL,
            length_m                DOUBLE PRECISION NOT NULL CHECK (length_m > 0),
            forward_clip_count      INTEGER NOT NULL CHECK (forward_clip_count >= 0),
            reverse_clip_count      INTEGER NOT NULL CHECK (reverse_clip_count >= 0),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT centerlines_id_detect_key UNIQUE (id, detect_id),
            CONSTRAINT centerlines_fit_fkey
                FOREIGN KEY (fit_id, detect_id) REFERENCES fit_runs(id, detect_id),
            CONSTRAINT centerlines_from_intersection_fkey
                FOREIGN KEY (from_intersection_id, detect_id)
                REFERENCES intersections(id, detect_id),
            CONSTRAINT centerlines_to_intersection_fkey
                FOREIGN KEY (to_intersection_id, detect_id)
                REFERENCES intersections(id, detect_id),
            CONSTRAINT centerlines_fit_variant_endpoints_key
                UNIQUE (fit_id, variant, from_intersection_id, to_intersection_id),
            CONSTRAINT centerlines_distinct_endpoints_check
                CHECK (from_intersection_id <> to_intersection_id),
            CONSTRAINT centerlines_combined_endpoint_order_check
                CHECK (variant <> 'combined' OR from_intersection_id < to_intersection_id),
            CONSTRAINT centerlines_nonempty_evidence_check
                CHECK (forward_clip_count + reverse_clip_count > 0)
        )
        """,
    ),
    (
        "centerline_source_clips",
        """
        CREATE TABLE IF NOT EXISTS centerline_source_clips (
            centerline_id                  BIGINT NOT NULL,
            detect_id                      UUID NOT NULL,
            input_ordinal                  INTEGER NOT NULL CHECK (input_ordinal >= 0),
            start_crossing_id              BIGINT NOT NULL,
            end_crossing_id                BIGINT NOT NULL,
            reverse_to_centerline_direction BOOLEAN NOT NULL,
            PRIMARY KEY (centerline_id, input_ordinal),
            CONSTRAINT centerline_source_clips_centerline_fkey
                FOREIGN KEY (centerline_id, detect_id)
                REFERENCES centerlines(id, detect_id) ON DELETE CASCADE,
            CONSTRAINT centerline_source_clips_start_crossing_fkey
                FOREIGN KEY (start_crossing_id, detect_id)
                REFERENCES candidate_entry_exit_points(id, detect_id),
            CONSTRAINT centerline_source_clips_end_crossing_fkey
                FOREIGN KEY (end_crossing_id, detect_id)
                REFERENCES candidate_entry_exit_points(id, detect_id),
            CONSTRAINT centerline_source_clips_distinct_crossings_check
                CHECK (start_crossing_id <> end_crossing_id),
            CONSTRAINT centerline_source_clips_crossing_pair_key
                UNIQUE (centerline_id, start_crossing_id, end_crossing_id)
        )
        """,
    ),
    (
        "pipeline_artifacts",
        """
        CREATE TABLE IF NOT EXISTS pipeline_artifacts (
            id                  UUID PRIMARY KEY,
            stage               TEXT NOT NULL CHECK (stage IN ('refine', 'detect')),
            refine_id            UUID REFERENCES refine_runs(id) ON DELETE RESTRICT,
            detect_id           UUID REFERENCES detect_runs(id) ON DELETE RESTRICT,
            relative_path       TEXT NOT NULL,
            manifest_version    INTEGER NOT NULL CHECK (manifest_version = 1),
            manifest            JSONB NOT NULL
                                CHECK (jsonb_typeof(manifest) = 'object'),
            manifest_sha256     TEXT NOT NULL
                                CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
            source_fingerprint  JSONB NOT NULL
                                CHECK (jsonb_typeof(source_fingerprint) = 'object'),
            row_count           BIGINT NOT NULL CHECK (row_count >= 0),
            byte_count          BIGINT NOT NULL CHECK (byte_count >= 0),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT pipeline_artifacts_owner_stage_check CHECK (
                (stage = 'refine' AND refine_id IS NOT NULL AND detect_id IS NULL)
                OR
                (stage = 'detect' AND detect_id IS NOT NULL AND refine_id IS NULL)
            ),
            CONSTRAINT pipeline_artifacts_relative_path_check CHECK (
                relative_path = BTRIM(relative_path)
                AND relative_path <> ''
                AND LEFT(relative_path, 1) <> '/'
                AND RIGHT(relative_path, 1) <> '/'
                AND relative_path !~ '(^|/)\\.\\.(/|$)'
                AND relative_path !~ '//'
            )
        )
        """,
    ),
    (
        "pipeline_artifact_events",
        """
        CREATE TABLE IF NOT EXISTS pipeline_artifact_events (
            id              BIGSERIAL PRIMARY KEY,
            artifact_id     UUID NOT NULL
                            REFERENCES pipeline_artifacts(id) ON DELETE RESTRICT,
            state           TEXT NOT NULL CHECK (state IN ('present', 'deleted')),
            event_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reason          TEXT,
            CONSTRAINT pipeline_artifact_events_reason_check CHECK (
                (state = 'present' AND reason IS NULL)
                OR
                (state = 'deleted' AND reason = BTRIM(reason) AND reason <> '')
            ),
            CONSTRAINT pipeline_artifact_events_state_key UNIQUE (artifact_id, state)
        )
        """,
    ),
]


INDEX_STATEMENTS: list[tuple[str, str]] = [
    (
        "idx_trun_hot_ponits_detect",
        "CREATE INDEX IF NOT EXISTS idx_trun_hot_ponits_detect ON trun_hot_ponits(detect_id)",
    ),
    (
        "idx_trun_hot_ponits_geom",
        "CREATE INDEX IF NOT EXISTS idx_trun_hot_ponits_geom ON trun_hot_ponits USING GIST (geom)",
    ),
    (
        "idx_turn_events_geom_id",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_events_geom_id "
        "ON turn_events_geom(id)",
    ),
    (
        "idx_turn_events_geom_detect",
        "CREATE INDEX IF NOT EXISTS idx_turn_events_geom_detect "
        "ON turn_events_geom(detect_id)",
    ),
    (
        "idx_turn_events_geom_geom",
        "CREATE INDEX IF NOT EXISTS idx_turn_events_geom_geom "
        "ON turn_events_geom USING GIST (geom)",
    ),

    (
        "uq_pipeline_artifacts_refine_run",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_artifacts_refine_run "
        "ON pipeline_artifacts(refine_id) WHERE stage = 'refine'",
    ),
    (
        "uq_pipeline_artifacts_detect_run",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_artifacts_detect_run "
        "ON pipeline_artifacts(detect_id) WHERE stage = 'detect'",
    ),
    (
        "idx_refine_runs_source_table",
        "CREATE INDEX IF NOT EXISTS idx_refine_runs_source_table "
        "ON refine_runs(source_table, started_at DESC)",
    ),
    (
        "idx_detect_runs_refine",
        "CREATE INDEX IF NOT EXISTS idx_detect_runs_refine "
        "ON detect_runs(refine_id, started_at DESC)",
    ),
    (
        "idx_fit_runs_detect",
        "CREATE INDEX IF NOT EXISTS idx_fit_runs_detect "
        "ON fit_runs(detect_id, started_at DESC)",
    ),
    (
        "idx_trajectory_points_vehicle_time",
        "CREATE INDEX IF NOT EXISTS idx_trajectory_points_vehicle_time "
        "ON trajectory_points(vehicle_id, timestamp, id)",
    ),
    (
        "idx_trajectory_points_geom",
        "CREATE INDEX IF NOT EXISTS idx_trajectory_points_geom "
        "ON trajectory_points USING GIST (geom)",
    ),
    (
        "idx_trajectory_points_geom_utm32649",
        "CREATE INDEX IF NOT EXISTS idx_trajectory_points_geom_utm32649 "
        "ON trajectory_points USING GIST (ST_Transform(geom, 32649))",
    ),
    (
        "idx_excluded_points_reason",
        "CREATE INDEX IF NOT EXISTS idx_excluded_points_reason "
        "ON excluded_points(refine_id, reason)",
    ),
    (
        "idx_kept_points_subtrack",
        "CREATE INDEX IF NOT EXISTS idx_kept_points_subtrack "
        "ON kept_trajectory_points(refine_id, subtrack_index, sequence_in_subtrack)",
    ),
    (
        "idx_injected_points_subtrack",
        "CREATE INDEX IF NOT EXISTS idx_injected_points_subtrack "
        "ON injected_points(refine_id, subtrack_index, after_sequence_in_subtrack)",
    ),
    (
        "idx_injected_points_geom",
        "CREATE INDEX IF NOT EXISTS idx_injected_points_geom "
        "ON injected_points USING GIST (geom)",
    ),
    (
        "idx_injected_points_geom_utm32649",
        "CREATE INDEX IF NOT EXISTS idx_injected_points_geom_utm32649 "
        "ON injected_points USING GIST (ST_Transform(geom, 32649))",
    ),
    (
        "idx_resolved_points_final",
        "CREATE INDEX IF NOT EXISTS idx_resolved_points_final "
        "ON resolved_subtrack_points(detect_id, subtrack_index, sequence_in_subtrack)",
    ),
    (
        "idx_candidate_intersections_geom",
        "CREATE INDEX IF NOT EXISTS idx_candidate_intersections_geom "
        "ON candidate_intersections USING GIST (geom)",
    ),
    (
        "idx_tracks_cross_candidate",
        "CREATE INDEX IF NOT EXISTS idx_tracks_cross_candidate "
        "ON tracks_cross(detect_id, candidate_intersection_id) "
        "WHERE candidate_intersection_id IS NOT NULL",
    ),
    (
        "idx_tracks_cross_geom",
        "CREATE INDEX IF NOT EXISTS idx_tracks_cross_geom "
        "ON tracks_cross USING GIST (geom)",
    ),
    (
        "idx_turn_events_candidate",
        "CREATE INDEX IF NOT EXISTS idx_turn_events_candidate "
        "ON turn_events(detect_id, candidate_intersection_id) "
        "WHERE candidate_intersection_id IS NOT NULL",
    ),
    (
        "idx_candidate_crossings_candidate",
        "CREATE INDEX IF NOT EXISTS idx_candidate_crossings_candidate "
        "ON candidate_entry_exit_points(detect_id, candidate_intersection_id)",
    ),
    (
        "idx_candidate_crossings_geom",
        "CREATE INDEX IF NOT EXISTS idx_candidate_crossings_geom "
        "ON candidate_entry_exit_points USING GIST (geom)",
    ),
    (
        "idx_intersections_geom",
        "CREATE INDEX IF NOT EXISTS idx_intersections_geom "
        "ON intersections USING GIST (geom)",
    ),
    (
        "idx_entry_exit_points_intersection",
        "CREATE INDEX IF NOT EXISTS idx_entry_exit_points_intersection "
        "ON entry_exit_points(detect_id, intersection_id)",
    ),
    (
        "idx_centerlines_fit",
        "CREATE INDEX IF NOT EXISTS idx_centerlines_fit "
        "ON centerlines(fit_id, variant)",
    ),
    (
        "idx_centerlines_geom",
        "CREATE INDEX IF NOT EXISTS idx_centerlines_geom "
        "ON centerlines USING GIST (geom)",
    ),
]


POST_DDL_STATEMENTS: list[tuple[str, str]] = [
    (
        "refine_and_entry_exit_geometry",
        "ALTER TABLE kept_trajectory_points ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326); "
        "ALTER TABLE excluded_points ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326); "
        "ALTER TABLE entry_exit_points ADD COLUMN IF NOT EXISTS geom geometry(MultiPoint, 4326)",
    ),
    (
        "resolved_subtrack_points_geom",
        "ALTER TABLE resolved_subtrack_points "
        "ADD COLUMN IF NOT EXISTS geom geometry(Point, 4326)",
    ),
    (
        "turn_events_geom_constraints",
        """
        ALTER TABLE turn_events_geom
            ALTER COLUMN id SET NOT NULL,
            ALTER COLUMN detect_id SET NOT NULL,
            ALTER COLUMN geom SET NOT NULL;
        DO $ddl$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'turn_events_geom'::regclass
                  AND conname = 'turn_events_geom_event_fkey'
            ) THEN
                ALTER TABLE turn_events_geom
                    ADD CONSTRAINT turn_events_geom_event_fkey
                    FOREIGN KEY (id, detect_id)
                    REFERENCES turn_events(id, detect_id) ON DELETE CASCADE;
            END IF;
        END
        $ddl$
        """,
    ),

    (
        "guard_pipeline_artifact_immutability_function",
        """
        CREATE OR REPLACE FUNCTION guard_pipeline_artifact_immutability()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
            RAISE EXCEPTION 'pipeline artifact registry is immutable and append-only';
        END
        $function$
        """,
    ),
    (
        "pipeline_artifact_immutability_triggers",
        """
        DROP TRIGGER IF EXISTS guard_pipeline_artifacts_mutation ON pipeline_artifacts;
        CREATE TRIGGER guard_pipeline_artifacts_mutation
        BEFORE UPDATE OR DELETE ON pipeline_artifacts
        FOR EACH ROW EXECUTE FUNCTION guard_pipeline_artifact_immutability();
        DROP TRIGGER IF EXISTS guard_pipeline_artifact_events_mutation
            ON pipeline_artifact_events;
        CREATE TRIGGER guard_pipeline_artifact_events_mutation
        BEFORE UPDATE OR DELETE ON pipeline_artifact_events
        FOR EACH ROW EXECUTE FUNCTION guard_pipeline_artifact_immutability()
        """,
    ),
    (
        "validate_pipeline_artifact_event_function",
        """
        CREATE OR REPLACE FUNCTION validate_pipeline_artifact_event()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
            prior_states TEXT[];
        BEGIN
            PERFORM 1 FROM pipeline_artifacts
             WHERE id = NEW.artifact_id
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'artifact % does not exist', NEW.artifact_id;
            END IF;

            SELECT ARRAY_AGG(state ORDER BY id)
              INTO prior_states
              FROM pipeline_artifact_events
             WHERE artifact_id = NEW.artifact_id;
            IF prior_states IS NULL AND NEW.state <> 'present' THEN
                RAISE EXCEPTION 'first artifact event must be present';
            ELSIF prior_states = ARRAY['present']::TEXT[] AND NEW.state <> 'deleted' THEN
                RAISE EXCEPTION 'present artifact can only transition to deleted';
            ELSIF prior_states IS NOT NULL AND prior_states <> ARRAY['present']::TEXT[] THEN
                RAISE EXCEPTION 'deleted artifact cannot receive more events';
            END IF;
            RETURN NEW;
        END
        $function$
        """,
    ),
    (
        "pipeline_artifact_event_insert_trigger",
        """
        DROP TRIGGER IF EXISTS validate_pipeline_artifact_event_insert
            ON pipeline_artifact_events;
        CREATE TRIGGER validate_pipeline_artifact_event_insert
        BEFORE INSERT ON pipeline_artifact_events
        FOR EACH ROW EXECUTE FUNCTION validate_pipeline_artifact_event()
        """,
    ),
    (
        "validate_refine_artifact_completion_function",
        """
        CREATE OR REPLACE FUNCTION validate_refine_artifact_completion()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
            artifact_count BIGINT;
            present_count BIGINT;
        BEGIN
            IF OLD.status = 'running' AND NEW.status = 'completed' THEN
                SELECT COUNT(*), COUNT(*) FILTER (WHERE latest.state = 'present')
                  INTO artifact_count, present_count
                  FROM pipeline_artifacts AS artifact
                  LEFT JOIN LATERAL (
                      SELECT event.state
                        FROM pipeline_artifact_events AS event
                       WHERE event.artifact_id = artifact.id
                       ORDER BY event.id DESC
                       LIMIT 1
                  ) AS latest ON TRUE
                 WHERE artifact.stage = 'refine'
                   AND artifact.refine_id = NEW.id;
                IF artifact_count <> 1 OR present_count <> 1 THEN
                    RAISE EXCEPTION
                        'completed Refine Run % requires exactly one present Refine artifact',
                        NEW.id;
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$
        """,
    ),
    (
        "refine_artifact_completion_trigger",
        """
        DROP TRIGGER IF EXISTS refine_artifact_completion_check ON refine_runs;
        CREATE CONSTRAINT TRIGGER refine_artifact_completion_check
        AFTER UPDATE OF status ON refine_runs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_refine_artifact_completion()
        """,
    ),
    (
        "validate_detect_artifact_completion_function",
        """
        CREATE OR REPLACE FUNCTION validate_detect_artifact_completion()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
            artifact_count BIGINT;
            present_count BIGINT;
        BEGIN
            IF OLD.status = 'running' AND NEW.status = 'completed' THEN
                SELECT COUNT(*), COUNT(*) FILTER (WHERE latest.state = 'present')
                  INTO artifact_count, present_count
                  FROM pipeline_artifacts AS artifact
                  LEFT JOIN LATERAL (
                      SELECT event.state
                        FROM pipeline_artifact_events AS event
                       WHERE event.artifact_id = artifact.id
                       ORDER BY event.id DESC
                       LIMIT 1
                  ) AS latest ON TRUE
                 WHERE artifact.stage = 'detect'
                   AND artifact.detect_id = NEW.id;
                IF artifact_count <> 1 OR present_count <> 1 THEN
                    RAISE EXCEPTION
                        'completed Detect Run % requires exactly one present Detect artifact',
                        NEW.id;
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$
        """,
    ),
    (
        "detect_artifact_completion_trigger",
        """
        DROP TRIGGER IF EXISTS detect_artifact_completion_check ON detect_runs;
        CREATE CONSTRAINT TRIGGER detect_artifact_completion_check
        AFTER UPDATE OF status ON detect_runs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_detect_artifact_completion()
        """,
    ),
    (
        "resolved_points_crossing_fkey",
        """
        DO $ddl$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'resolved_points_crossing_fkey'
                  AND conrelid = 'resolved_subtrack_points'::regclass
            ) THEN
                ALTER TABLE resolved_subtrack_points
                    ADD CONSTRAINT resolved_points_crossing_fkey
                    FOREIGN KEY (crossing_point_id, detect_id)
                    REFERENCES candidate_entry_exit_points(id, detect_id)
                    DEFERRABLE INITIALLY DEFERRED;
            END IF;
        END
        $ddl$
        """,
    ),
    (
        "remove_row_semantic_triggers",
        """
        DROP TRIGGER IF EXISTS resolved_point_membership_check
            ON resolved_subtrack_points;
        DROP TRIGGER IF EXISTS candidate_crossing_segment_start_check
            ON candidate_entry_exit_points;
        DROP TRIGGER IF EXISTS tracks_cross_segments_check ON tracks_cross;
        DROP TRIGGER IF EXISTS turn_event_point_check ON turn_events;
        DROP TRIGGER IF EXISTS entry_exit_observation_check ON entry_exit_points;
        DROP TRIGGER IF EXISTS centerline_source_clip_check
            ON centerline_source_clips;

        DO $ddl$
        DECLARE function_name TEXT;
        BEGIN
            FOREACH function_name IN ARRAY ARRAY[
                'validate_resolved_point_membership',
                'validate_resolved_segment_start',
                'validate_tracks_cross_segments',
                'validate_turn_event_point',
                'validate_entry_exit_observation',
                'validate_centerline_source_clip'
            ] LOOP
                EXECUTE format('DROP FUNCTION IF EXISTS %I.%I()',
                               current_schema(), function_name);
            END LOOP;
        END
        $ddl$;
        """,
    ),
    (
        "run_state_transition_function",
        """
        CREATE OR REPLACE FUNCTION enforce_pipeline_run_state_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF OLD.status <> 'running' THEN
                RAISE EXCEPTION '% % is immutable after status %',
                    TG_TABLE_NAME, OLD.id, OLD.status;
            END IF;
            RETURN NEW;
        END
        $function$
        """,
    ),
    (
        "run_initial_status_function",
        """
        CREATE OR REPLACE FUNCTION enforce_pipeline_run_initial_status()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF NEW.status <> 'running' THEN
                RAISE EXCEPTION '% must be inserted with status running',
                    TG_TABLE_NAME;
            END IF;
            RETURN NEW;
        END
        $function$
        """,
    ),
    (
        "run_initial_status_triggers",
        """
        DROP TRIGGER IF EXISTS refine_run_initial_status_guard ON refine_runs;
        CREATE TRIGGER refine_run_initial_status_guard
        BEFORE INSERT ON refine_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_pipeline_run_initial_status();
        DROP TRIGGER IF EXISTS detect_run_initial_status_guard ON detect_runs;
        CREATE TRIGGER detect_run_initial_status_guard
        BEFORE INSERT ON detect_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_pipeline_run_initial_status();
        DROP TRIGGER IF EXISTS fit_run_initial_status_guard ON fit_runs;
        CREATE TRIGGER fit_run_initial_status_guard
        BEFORE INSERT ON fit_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_pipeline_run_initial_status()
        """,
    ),
    (
        "refine_run_state_trigger",
        """
        DROP TRIGGER IF EXISTS refine_run_state_guard ON refine_runs;
        CREATE TRIGGER refine_run_state_guard
        BEFORE UPDATE ON refine_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_pipeline_run_state_transition()
        """,
    ),
    (
        "detect_run_state_trigger",
        """
        DROP TRIGGER IF EXISTS detect_run_state_guard ON detect_runs;
        CREATE TRIGGER detect_run_state_guard
        BEFORE UPDATE ON detect_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_pipeline_run_state_transition()
        """,
    ),
    (
        "fit_run_state_trigger",
        """
        DROP TRIGGER IF EXISTS fit_run_state_guard ON fit_runs;
        CREATE TRIGGER fit_run_state_guard
        BEFORE UPDATE ON fit_runs
        FOR EACH ROW EXECUTE FUNCTION enforce_pipeline_run_state_transition()
        """,
    ),
    (
        "run_delete_guard_function",
        """
        CREATE OR REPLACE FUNCTION guard_pipeline_run_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF current_setting(
                'track_v2.allow_completed_run_delete', true
            ) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION
                    '% rows may only be deleted through DeleteRunService',
                    TG_TABLE_NAME;
            END IF;
            RETURN NULL;
        END
        $function$
        """,
    ),
    (
        "run_delete_guard_triggers",
        """
        DROP TRIGGER IF EXISTS refine_run_delete_guard ON refine_runs;
        CREATE TRIGGER refine_run_delete_guard
        BEFORE DELETE ON refine_runs
        FOR EACH STATEMENT EXECUTE FUNCTION guard_pipeline_run_delete();
        DROP TRIGGER IF EXISTS detect_run_delete_guard ON detect_runs;
        CREATE TRIGGER detect_run_delete_guard
        BEFORE DELETE ON detect_runs
        FOR EACH STATEMENT EXECUTE FUNCTION guard_pipeline_run_delete();
        DROP TRIGGER IF EXISTS fit_run_delete_guard ON fit_runs;
        CREATE TRIGGER fit_run_delete_guard
        BEFORE DELETE ON fit_runs
        FOR EACH STATEMENT EXECUTE FUNCTION guard_pipeline_run_delete()
        """,
    ),
    (
        "validate_detect_completion_function",
        """
        CREATE OR REPLACE FUNCTION validate_detect_run_completion()
        RETURNS trigger
        LANGUAGE plpgsql
        -- Completion inspects an entire newly written run. Its statistics
        -- may still estimate one row: nested joins can then repeatedly scan
        -- that run even when unique indexes exist. Prefer set-based joins
        -- only inside this function; PostgreSQL restores the caller setting.
        SET enable_nestloop = off
        AS $function$
        DECLARE
            violation TEXT;
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM refine_runs
                 WHERE id = NEW.refine_id
                   AND status = 'completed'
            ) THEN
                RAISE EXCEPTION
                    'completed Detect Run requires a completed parent Refine Run';
            END IF;

            -- Select the expected time slice before deriving its local p/i order.
            CREATE TEMP TABLE IF NOT EXISTS detect_time_members (
                source_kind TEXT, source_point_id BIGINT,
                PRIMARY KEY (source_kind, source_point_id)
            ) ON COMMIT DROP;
            TRUNCATE pg_temp.detect_time_members;
            IF NEW.parameters->'refine_time_selection'->>'time_start' IS NOT NULL
               OR NEW.parameters->'refine_time_selection'->>'time_end' IS NOT NULL THEN
                EXECUTE format(
                    'INSERT INTO pg_temp.detect_time_members
                     SELECT ''kept'', k.id FROM kept_trajectory_points k
                     JOIN %s s ON s.id=k.source_row_key
                     WHERE k.refine_id=$1 AND s.timestamp >= $2 AND s.timestamp < $3
                     UNION ALL SELECT ''injected'', id FROM injected_points
                     WHERE refine_id=$1 AND timestamp >= $2 AND timestamp < $3',
                    (SELECT source_table::regclass FROM refine_runs WHERE id=NEW.refine_id)
                ) USING NEW.refine_id,
                    COALESCE(EXTRACT(EPOCH FROM
                        (NEW.parameters->'refine_time_selection'->>'time_start')::timestamptz), '-Infinity'::float8),
                    COALESCE(EXTRACT(EPOCH FROM
                        (NEW.parameters->'refine_time_selection'->>'time_end')::timestamptz), 'Infinity'::float8);
            END IF;

            RAISE NOTICE 'track_v2 validation: Detect：校验 Refine 点集合与车辆归属';

            -- This is intentionally one aggregate scan, not one query per
            -- resolved row.  It validates parent-Refine membership, subtrack
            -- membership, and the A80 single-vehicle contract together.
            WITH point_sources AS MATERIALIZED (
                SELECT resolved.subtrack_index,
                       resolved.refined_sequence_in_subtrack,
                       CASE
                           WHEN resolved.kept_point_id IS NOT NULL
                               THEN 'kept'::TEXT
                           ELSE 'injected'::TEXT
                       END AS source_kind,
                       COALESCE(
                           resolved.kept_point_id,
                           resolved.injected_point_id
                       ) AS source_point_id,
                       COALESCE(kept.refine_id, injected.refine_id) AS refine_id,
                       COALESCE(
                           kept.subtrack_index,
                           injected.subtrack_index
                       ) AS source_subtrack,
                       COALESCE(kept.vehicle_id, injected.vehicle_id) AS vehicle_id
                  FROM resolved_subtrack_points AS resolved
                  LEFT JOIN kept_trajectory_points AS kept
                    ON kept.id = resolved.kept_point_id
                   AND kept.refine_id = NEW.refine_id
                  LEFT JOIN injected_points AS injected
                    ON injected.id = resolved.injected_point_id
                   AND injected.refine_id = NEW.refine_id
                 WHERE resolved.detect_id = NEW.id
                   AND resolved.crossing_point_id IS NULL
            ), expected_sources AS (
                SELECT source_kind,
                       source_point_id,
                       subtrack_index,
                       ROW_NUMBER() OVER (
                           PARTITION BY subtrack_index
                           ORDER BY anchor_sequence, kind_order, source_point_id
                       ) - 1 AS expected_refined_sequence
                  FROM (
                        SELECT 'kept'::TEXT AS source_kind,
                               id AS source_point_id,
                               subtrack_index,
                               sequence_in_subtrack AS anchor_sequence,
                               0 AS kind_order
                          FROM kept_trajectory_points
                         WHERE refine_id = NEW.refine_id
                        UNION ALL
                        SELECT 'injected'::TEXT,
                               id,
                               subtrack_index,
                               after_sequence_in_subtrack,
                               1
                          FROM injected_points
                         WHERE refine_id = NEW.refine_id
                       ) AS refine_members
                 WHERE (NEW.parameters->'refine_time_selection'->>'time_start' IS NULL
                    AND NEW.parameters->'refine_time_selection'->>'time_end' IS NULL)
                    OR EXISTS (SELECT 1 FROM pg_temp.detect_time_members timed
                               WHERE timed.source_kind=refine_members.source_kind
                                 AND timed.source_point_id=refine_members.source_point_id)
            ), subtrack_checks AS (
                SELECT subtrack_index,
                       BOOL_OR(
                           refine_id IS DISTINCT FROM NEW.refine_id
                           OR source_subtrack IS DISTINCT FROM subtrack_index
                       ) AS has_bad_membership,
                       MIN(vehicle_id) AS first_vehicle,
                       MAX(vehicle_id) AS last_vehicle
                  FROM point_sources
                 GROUP BY subtrack_index
            )
            SELECT invalid.issue
              INTO violation
              FROM (
                    SELECT CASE
                               WHEN expected.source_point_id IS NULL
                                    OR actual.source_point_id IS NULL THEN
                                   'resolved p/i set must exactly equal the Detect parent Refine p/i set'
                               ELSE
                                   'resolved refined sequence must equal the derived Refine p/i order'
                           END AS issue
                      FROM expected_sources AS expected
                      FULL JOIN point_sources AS actual
                        ON actual.source_kind = expected.source_kind
                       AND actual.source_point_id = expected.source_point_id
                     WHERE expected.source_point_id IS NULL
                        OR actual.source_point_id IS NULL
                        OR actual.subtrack_index IS DISTINCT FROM
                           expected.subtrack_index
                        OR actual.refined_sequence_in_subtrack IS DISTINCT FROM
                           expected.expected_refined_sequence
                    UNION ALL
                    SELECT CASE
                               WHEN has_bad_membership THEN
                                   'resolved p/i point must belong to the Detect parent Refine Run and subtrack'
                               ELSE 'resolved subtrack contains multiple vehicles'
                           END
                      FROM subtrack_checks
                     WHERE has_bad_membership
                        OR first_vehicle IS DISTINCT FROM last_vehicle
                   ) AS invalid
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RAISE NOTICE 'track_v2 validation: Detect：校验连续点序列';

            -- UNIQUE constraints prevent duplicates; min/max/count therefore
            -- prove that both snapshots are dense zero-based sequences.
            SELECT 'resolved subtrack refined/final sequence must be contiguous from zero'
              INTO violation
              FROM resolved_subtrack_points AS resolved
             WHERE resolved.detect_id = NEW.id
             GROUP BY resolved.subtrack_index
            HAVING MIN(resolved.sequence_in_subtrack) <> 0
                OR MAX(resolved.sequence_in_subtrack) <> COUNT(*) - 1
                OR COUNT(resolved.refined_sequence_in_subtrack) = 0
                OR MIN(resolved.refined_sequence_in_subtrack) <> 0
                OR MAX(resolved.refined_sequence_in_subtrack) <>
                   COUNT(resolved.refined_sequence_in_subtrack) - 1
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RAISE NOTICE 'track_v2 validation: Detect：校验精炼点顺序';

            SELECT
                'resolved p/i final sequence must strictly increase with refined sequence'
              INTO violation
              FROM (
                    SELECT resolved.sequence_in_subtrack,
                           LAG(resolved.sequence_in_subtrack) OVER (
                               PARTITION BY resolved.subtrack_index
                               ORDER BY resolved.refined_sequence_in_subtrack
                           ) AS previous_final_sequence
                      FROM resolved_subtrack_points AS resolved
                     WHERE resolved.detect_id = NEW.id
                       AND resolved.refined_sequence_in_subtrack IS NOT NULL
                   ) AS ordered_refined
             WHERE sequence_in_subtrack <= previous_final_sequence
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RAISE NOTICE 'track_v2 validation: Detect：校验交叉点集合与线段位置';

            -- The resolved crossing set must exactly equal the crossing set
            -- of complete intersections.  The same join also validates that
            -- every crossing inherits its refined segment's subtrack.
            -- Use the indexed base table for missing crossings. Rejoining a materialized CTE here
            -- can repeatedly scan the entire unindexed crossing set when a
            -- freshly inserted run is underestimated by the planner.
            -- Derive successors in one ordered pass. A self join on
            -- (subtrack, refined_sequence + 1) can become a merge join on
            -- subtrack alone, comparing every pair in long subtracks.
            WITH refined_segments AS (
                SELECT id, subtrack_index, sequence_in_subtrack,
                       LEAD(sequence_in_subtrack) OVER (
                           PARTITION BY subtrack_index
                           ORDER BY refined_sequence_in_subtrack
                       ) AS successor_sequence
                  FROM resolved_subtrack_points
                 WHERE detect_id = NEW.id
                   AND refined_sequence_in_subtrack IS NOT NULL
            ), actual_crossings AS (
                SELECT resolved.id AS resolved_id,
                       resolved.crossing_point_id,
                       resolved.subtrack_index,
                       resolved.sequence_in_subtrack,
                       crossing.id AS crossing_id,
                       segment_start.subtrack_index AS source_subtrack,
                       segment_start.sequence_in_subtrack AS segment_start_sequence,
                       segment_start.successor_sequence,
                       complete_intersection.id AS complete_intersection_id
                  FROM resolved_subtrack_points AS resolved
                  LEFT JOIN candidate_entry_exit_points AS crossing
                    ON crossing.id = resolved.crossing_point_id
                   AND crossing.detect_id = resolved.detect_id
                  LEFT JOIN refined_segments AS segment_start
                    ON segment_start.id = crossing.segment_start_point_id
                  LEFT JOIN intersections AS complete_intersection
                    ON complete_intersection.detect_id = crossing.detect_id
                   AND complete_intersection.candidate_intersection_id =
                       crossing.candidate_intersection_id
                 WHERE resolved.detect_id = NEW.id
                   AND resolved.crossing_point_id IS NOT NULL
            ), expected_crossings AS (
                SELECT crossing.id
                  FROM candidate_entry_exit_points AS crossing
                  JOIN intersections AS complete_intersection
                    ON complete_intersection.detect_id = crossing.detect_id
                   AND complete_intersection.candidate_intersection_id =
                       crossing.candidate_intersection_id
                 WHERE crossing.detect_id = NEW.id
            )
            SELECT invalid.issue
              INTO violation
              FROM (
                    SELECT CASE
                               WHEN crossing_id IS NULL
                                   THEN 'resolved crossing does not exist'
                               WHEN complete_intersection_id IS NULL
                                   THEN 'resolved crossing is not attached to a complete intersection'
                               WHEN (
                                   segment_start_sequence < sequence_in_subtrack
                                   AND sequence_in_subtrack < successor_sequence
                               ) IS NOT TRUE THEN
                                   'resolved crossing must lie between its refined segment endpoints'
                               ELSE 'resolved crossing belongs to the wrong subtrack'
                           END AS issue
                      FROM actual_crossings
                     WHERE crossing_id IS NULL
                        OR complete_intersection_id IS NULL
                        OR source_subtrack IS DISTINCT FROM subtrack_index
                        OR (
                            segment_start_sequence < sequence_in_subtrack
                            AND sequence_in_subtrack < successor_sequence
                        ) IS NOT TRUE
                    UNION ALL
                    SELECT 'complete-intersection crossing is absent from resolved sequence'
                      FROM expected_crossings AS expected
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM resolved_subtrack_points AS actual
                          WHERE actual.detect_id = NEW.id
                            AND actual.crossing_point_id = expected.id
                     )
                   ) AS invalid
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RAISE NOTICE 'track_v2 validation: Detect：校验线段端点';

            WITH refined_segments AS (
                SELECT id,
                       LEAD(id) OVER (
                           PARTITION BY subtrack_index
                           ORDER BY refined_sequence_in_subtrack
                       ) AS successor_id
                  FROM resolved_subtrack_points
                 WHERE detect_id = NEW.id
                   AND refined_sequence_in_subtrack IS NOT NULL
            ), segment_uses AS (
                SELECT 'candidate crossing'::TEXT AS kind,
                       crossing.segment_start_point_id AS point_id
                  FROM candidate_entry_exit_points AS crossing
                 WHERE crossing.detect_id = NEW.id
                UNION ALL
                SELECT 'track cross', track_cross.from_segment_start_point_id
                  FROM tracks_cross AS track_cross
                 WHERE track_cross.detect_id = NEW.id
                UNION ALL
                SELECT 'track cross', track_cross.to_segment_start_point_id
                  FROM tracks_cross AS track_cross
                 WHERE track_cross.detect_id = NEW.id
            )
            SELECT FORMAT(
                       '%s endpoint %s is not a valid refined segment start',
                       segment_use.kind,
                       segment_use.point_id
                   )
              INTO violation
              FROM segment_uses AS segment_use
              LEFT JOIN refined_segments AS segment_start
                ON segment_start.id = segment_use.point_id
             WHERE segment_start.successor_id IS NULL
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RAISE NOTICE 'track_v2 validation: Detect：校验跨轨迹分区归属';

            SELECT 'track cross segments must belong to different subtracks'
              INTO violation
              FROM tracks_cross AS track_cross
              JOIN resolved_subtrack_points AS from_point
                ON from_point.id = track_cross.from_segment_start_point_id
               AND from_point.detect_id = track_cross.detect_id
              JOIN resolved_subtrack_points AS to_point
                ON to_point.id = track_cross.to_segment_start_point_id
               AND to_point.detect_id = track_cross.detect_id
             WHERE track_cross.detect_id = NEW.id
               AND from_point.subtrack_index IS NOT DISTINCT FROM
                   to_point.subtrack_index
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RAISE NOTICE 'track_v2 validation: Detect：校验转向事件';

            SELECT 'turn event point must be a refined p/i resolved point'
              INTO violation
              FROM turn_events AS turn_event
              LEFT JOIN resolved_subtrack_points AS resolved
                ON resolved.id = turn_event.resolved_point_id
               AND resolved.detect_id = turn_event.detect_id
             WHERE turn_event.detect_id = NEW.id
               AND resolved.refined_sequence_in_subtrack IS NULL
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RAISE NOTICE 'track_v2 validation: Detect：校验进出观测';

            SELECT CASE
                       WHEN entry_resolved.id IS NULL OR exit_resolved.id IS NULL
                           THEN 'entry/exit endpoints must exist in resolved sequence'
                       WHEN entry_crossing.crossing_type IS DISTINCT FROM 1
                            OR exit_crossing.crossing_type IS DISTINCT FROM 0
                           THEN 'entry/exit crossing types must be 1/0'
                       WHEN entry_crossing.candidate_intersection_id
                                IS DISTINCT FROM intersection.candidate_intersection_id
                            OR exit_crossing.candidate_intersection_id
                                IS DISTINCT FROM intersection.candidate_intersection_id
                           THEN 'entry/exit crossings must belong to the intersection candidate'
                       WHEN entry_resolved.subtrack_index
                                IS DISTINCT FROM exit_resolved.subtrack_index
                           THEN 'entry/exit crossings must belong to one subtrack'
                       ELSE 'entry crossing must precede exit crossing'
                   END
              INTO violation
              FROM entry_exit_points AS observation
              JOIN intersections AS intersection
                ON intersection.id = observation.intersection_id
               AND intersection.detect_id = observation.detect_id
              LEFT JOIN candidate_entry_exit_points AS entry_crossing
                ON entry_crossing.id = observation.entry_crossing_id
               AND entry_crossing.detect_id = observation.detect_id
              LEFT JOIN candidate_entry_exit_points AS exit_crossing
                ON exit_crossing.id = observation.exit_crossing_id
               AND exit_crossing.detect_id = observation.detect_id
              LEFT JOIN resolved_subtrack_points AS entry_resolved
                ON entry_resolved.crossing_point_id = entry_crossing.id
               AND entry_resolved.detect_id = observation.detect_id
              LEFT JOIN resolved_subtrack_points AS exit_resolved
                ON exit_resolved.crossing_point_id = exit_crossing.id
               AND exit_resolved.detect_id = observation.detect_id
             WHERE observation.detect_id = NEW.id
               AND (
                   entry_crossing.crossing_type IS DISTINCT FROM 1
                   OR exit_crossing.crossing_type IS DISTINCT FROM 0
                   OR entry_crossing.candidate_intersection_id
                        IS DISTINCT FROM intersection.candidate_intersection_id
                   OR exit_crossing.candidate_intersection_id
                        IS DISTINCT FROM intersection.candidate_intersection_id
                   OR entry_resolved.id IS NULL
                   OR exit_resolved.id IS NULL
                   OR entry_resolved.subtrack_index
                        IS DISTINCT FROM exit_resolved.subtrack_index
                   OR entry_resolved.sequence_in_subtrack >=
                        exit_resolved.sequence_in_subtrack
               )
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RETURN NEW;
        END
        $function$
        """,
    ),
    (
        "detect_completion_trigger",
        """
        DROP TRIGGER IF EXISTS detect_completion_check ON detect_runs;
        CREATE CONSTRAINT TRIGGER detect_completion_check
        AFTER UPDATE OF status ON detect_runs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        WHEN (NEW.status = 'completed' AND OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION validate_detect_run_completion()
        """,
    ),
    (
        "guard_refine_artifact_mutation_function",
        """
        CREATE OR REPLACE FUNCTION guard_refine_artifact_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            changed_refine_id UUID;
            run_status TEXT;
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting(
                   'track_v2.allow_completed_run_delete', true
               ) = 'on' THEN
                RETURN NULL;
            END IF;

            IF TG_OP = 'INSERT' THEN
                FOR changed_refine_id IN
                    SELECT refine_id FROM new_rows
                     GROUP BY refine_id ORDER BY refine_id
                LOOP
                    SELECT status INTO run_status
                      FROM refine_runs
                     WHERE id = changed_refine_id
                     FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Refine Run % artifacts are immutable',
                            changed_refine_id;
                    END IF;
                END LOOP;
            ELSIF TG_OP = 'DELETE' THEN
                FOR changed_refine_id IN
                    SELECT refine_id FROM old_rows
                     GROUP BY refine_id ORDER BY refine_id
                LOOP
                    SELECT status INTO run_status
                      FROM refine_runs
                     WHERE id = changed_refine_id
                     FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Refine Run % artifacts are immutable',
                            changed_refine_id;
                    END IF;
                END LOOP;
            ELSE
                FOR changed_refine_id IN
                    SELECT refine_id
                      FROM (
                            SELECT refine_id FROM old_rows
                            UNION
                            SELECT refine_id FROM new_rows
                           ) AS changed
                     ORDER BY refine_id
                LOOP
                    SELECT status INTO run_status
                      FROM refine_runs
                     WHERE id = changed_refine_id
                     FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Refine Run % artifacts are immutable',
                            changed_refine_id;
                    END IF;
                END LOOP;
            END IF;

            RETURN NULL;
        END
        $function$
        """,
    ),
    (
        "refine_artifact_mutation_triggers",
        """
        DO $ddl$
        DECLARE
            artifact_table TEXT;
        BEGIN
            FOREACH artifact_table IN ARRAY ARRAY[
                'excluded_points',
                'kept_trajectory_points',
                'injected_points'
            ] LOOP
                EXECUTE FORMAT(
                    'DROP TRIGGER IF EXISTS guard_completed_refine_insert ON %I',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'CREATE TRIGGER guard_completed_refine_insert '
                    'AFTER INSERT ON %I REFERENCING NEW TABLE AS new_rows '
                    'FOR EACH STATEMENT '
                    'EXECUTE FUNCTION guard_refine_artifact_mutation()',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'DROP TRIGGER IF EXISTS guard_completed_refine_update ON %I',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'CREATE TRIGGER guard_completed_refine_update '
                    'AFTER UPDATE ON %I '
                    'REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows '
                    'FOR EACH STATEMENT '
                    'EXECUTE FUNCTION guard_refine_artifact_mutation()',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'DROP TRIGGER IF EXISTS guard_completed_refine_delete ON %I',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'CREATE TRIGGER guard_completed_refine_delete '
                    'AFTER DELETE ON %I REFERENCING OLD TABLE AS old_rows '
                    'FOR EACH STATEMENT '
                    'EXECUTE FUNCTION guard_refine_artifact_mutation()',
                    artifact_table
                );
            END LOOP;
        END
        $ddl$
        """,
    ),
    (
        "guard_detect_artifact_mutation_function",
        """
        CREATE OR REPLACE FUNCTION guard_detect_artifact_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            changed_detect_id UUID;
            run_status TEXT;
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting(
                   'track_v2.allow_completed_run_delete', true
               ) = 'on' THEN
                RETURN NULL;
            END IF;

            IF TG_OP = 'INSERT' THEN
                FOR changed_detect_id IN
                    SELECT detect_id FROM new_rows
                     GROUP BY detect_id ORDER BY detect_id
                LOOP
                    SELECT status INTO run_status
                      FROM detect_runs
                     WHERE id = changed_detect_id
                     FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Detect Run % artifacts are immutable',
                            changed_detect_id;
                    END IF;
                END LOOP;
            ELSIF TG_OP = 'DELETE' THEN
                FOR changed_detect_id IN
                    SELECT detect_id FROM old_rows
                     GROUP BY detect_id ORDER BY detect_id
                LOOP
                    SELECT status INTO run_status
                      FROM detect_runs
                     WHERE id = changed_detect_id
                     FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Detect Run % artifacts are immutable',
                            changed_detect_id;
                    END IF;
                END LOOP;
            ELSE
                FOR changed_detect_id IN
                    SELECT detect_id
                      FROM (
                            SELECT detect_id FROM old_rows
                            UNION
                            SELECT detect_id FROM new_rows
                           ) AS changed
                     ORDER BY detect_id
                LOOP
                    SELECT status INTO run_status
                      FROM detect_runs
                     WHERE id = changed_detect_id
                     FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Detect Run % artifacts are immutable',
                            changed_detect_id;
                    END IF;
                END LOOP;
            END IF;

            RETURN NULL;
        END
        $function$
        """,
    ),
    (
        "detect_artifact_mutation_triggers",
        """
        DO $ddl$
        DECLARE
            artifact_table TEXT;
        BEGIN
            FOREACH artifact_table IN ARRAY ARRAY[
                'resolved_subtrack_points',
                'candidate_intersections',
                'tracks_cross',
                'turn_events',
                'turn_events_geom',
                'trun_hot_ponits',
                'candidate_entry_exit_points',
                'intersections',
                'entry_exit_points'
            ] LOOP
                EXECUTE FORMAT(
                    'DROP TRIGGER IF EXISTS guard_completed_detect_insert ON %I',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'CREATE TRIGGER guard_completed_detect_insert '
                    'AFTER INSERT ON %I REFERENCING NEW TABLE AS new_rows '
                    'FOR EACH STATEMENT '
                    'EXECUTE FUNCTION guard_detect_artifact_mutation()',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'DROP TRIGGER IF EXISTS guard_completed_detect_update ON %I',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'CREATE TRIGGER guard_completed_detect_update '
                    'AFTER UPDATE ON %I '
                    'REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows '
                    'FOR EACH STATEMENT '
                    'EXECUTE FUNCTION guard_detect_artifact_mutation()',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'DROP TRIGGER IF EXISTS guard_completed_detect_delete ON %I',
                    artifact_table
                );
                EXECUTE FORMAT(
                    'CREATE TRIGGER guard_completed_detect_delete '
                    'AFTER DELETE ON %I REFERENCING OLD TABLE AS old_rows '
                    'FOR EACH STATEMENT '
                    'EXECUTE FUNCTION guard_detect_artifact_mutation()',
                    artifact_table
                );
            END LOOP;
        END
        $ddl$
        """,
    ),
    (
        "validate_fit_completion_function",
        """
        CREATE OR REPLACE FUNCTION validate_fit_run_completion()
        RETURNS trigger
        LANGUAGE plpgsql
        -- Completion inspects an entire newly written run. Its statistics
        -- may still estimate one row: nested joins can then repeatedly scan
        -- that run even when unique indexes exist. Prefer set-based joins
        -- only inside this function; PostgreSQL restores the caller setting.
        SET enable_nestloop = off
        AS $function$
        DECLARE
            violation TEXT;
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM detect_runs
                 WHERE id = NEW.detect_id
                   AND status = 'completed'
            ) THEN
                RAISE EXCEPTION
                    'completed Fit Run requires a completed Detect Run';
            END IF;

            RAISE NOTICE 'track_v2 validation: Fitting：校验来源片段数量';

            -- Counts are relative to the persisted centerline direction:
            -- false manifests are forward evidence and true manifests are
            -- reverse evidence.  LEFT JOIN also exposes a missing manifest.
            SELECT
                'centerline manifest direction counts must match persisted clip counts'
              INTO violation
              FROM centerlines AS centerline
              LEFT JOIN centerline_source_clips AS manifest
                ON manifest.centerline_id = centerline.id
               AND manifest.detect_id = centerline.detect_id
             WHERE centerline.fit_id = NEW.id
               AND centerline.detect_id = NEW.detect_id
             GROUP BY centerline.id,
                      centerline.forward_clip_count,
                      centerline.reverse_clip_count
            HAVING COUNT(manifest.centerline_id) FILTER (
                       WHERE NOT manifest.reverse_to_centerline_direction
                   ) <> centerline.forward_clip_count
                OR COUNT(manifest.centerline_id) FILTER (
                       WHERE manifest.reverse_to_centerline_direction
                   ) <> centerline.reverse_clip_count
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RAISE NOTICE 'track_v2 validation: Fitting：校验来源片段端点';

            SELECT CASE
                       WHEN start_resolved.id IS NULL OR end_resolved.id IS NULL
                           THEN 'centerline clip endpoints must exist in resolved sequence'
                       WHEN start_crossing.crossing_type IS DISTINCT FROM 0
                            OR end_crossing.crossing_type IS DISTINCT FROM 1
                           THEN 'centerline clip must start at exit and end at entry'
                       WHEN start_resolved.subtrack_index
                                IS DISTINCT FROM end_resolved.subtrack_index
                           THEN 'centerline clip crossings must belong to one subtrack'
                       WHEN start_resolved.sequence_in_subtrack >=
                            end_resolved.sequence_in_subtrack
                           THEN 'centerline clip start crossing must precede end crossing'
                       WHEN manifest.reverse_to_centerline_direction
                           THEN 'reversed clip endpoints do not match centerline endpoints'
                       ELSE 'clip endpoints do not match centerline endpoints'
                   END
              INTO violation
              FROM centerline_source_clips AS manifest
              JOIN centerlines AS centerline
                ON centerline.id = manifest.centerline_id
               AND centerline.detect_id = manifest.detect_id
              JOIN intersections AS source_intersection
                ON source_intersection.id = centerline.from_intersection_id
               AND source_intersection.detect_id = centerline.detect_id
              JOIN intersections AS target_intersection
                ON target_intersection.id = centerline.to_intersection_id
               AND target_intersection.detect_id = centerline.detect_id
              LEFT JOIN candidate_entry_exit_points AS start_crossing
                ON start_crossing.id = manifest.start_crossing_id
               AND start_crossing.detect_id = manifest.detect_id
              LEFT JOIN candidate_entry_exit_points AS end_crossing
                ON end_crossing.id = manifest.end_crossing_id
               AND end_crossing.detect_id = manifest.detect_id
              LEFT JOIN resolved_subtrack_points AS start_resolved
                ON start_resolved.crossing_point_id = start_crossing.id
               AND start_resolved.detect_id = manifest.detect_id
              LEFT JOIN resolved_subtrack_points AS end_resolved
                ON end_resolved.crossing_point_id = end_crossing.id
               AND end_resolved.detect_id = manifest.detect_id
             WHERE centerline.fit_id = NEW.id
               AND centerline.detect_id = NEW.detect_id
               AND (
                   start_crossing.crossing_type IS DISTINCT FROM 0
                   OR end_crossing.crossing_type IS DISTINCT FROM 1
                   OR start_resolved.id IS NULL
                   OR end_resolved.id IS NULL
                   OR start_resolved.subtrack_index
                        IS DISTINCT FROM end_resolved.subtrack_index
                   OR start_resolved.sequence_in_subtrack >=
                        end_resolved.sequence_in_subtrack
                   OR (
                       manifest.reverse_to_centerline_direction
                       AND (
                           start_crossing.candidate_intersection_id
                                IS DISTINCT FROM
                                target_intersection.candidate_intersection_id
                           OR end_crossing.candidate_intersection_id
                                IS DISTINCT FROM
                                source_intersection.candidate_intersection_id
                       )
                   )
                   OR (
                       NOT manifest.reverse_to_centerline_direction
                       AND (
                           start_crossing.candidate_intersection_id
                                IS DISTINCT FROM
                                source_intersection.candidate_intersection_id
                           OR end_crossing.candidate_intersection_id
                                IS DISTINCT FROM
                                target_intersection.candidate_intersection_id
                       )
                   )
               )
             LIMIT 1;
            IF violation IS NOT NULL THEN
                RAISE EXCEPTION '%', violation;
            END IF;

            RETURN NEW;
        END
        $function$
        """,
    ),
    (
        "fit_completion_trigger",
        """
        DROP TRIGGER IF EXISTS fit_completion_check ON fit_runs;
        CREATE CONSTRAINT TRIGGER fit_completion_check
        AFTER UPDATE OF status ON fit_runs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        WHEN (NEW.status = 'completed' AND OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION validate_fit_run_completion()
        """,
    ),
    (
        "guard_centerline_mutation_function",
        """
        CREATE OR REPLACE FUNCTION guard_centerline_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            changed_fit_id UUID;
            run_status TEXT;
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting(
                   'track_v2.allow_completed_run_delete', true
               ) = 'on' THEN
                RETURN NULL;
            END IF;

            IF TG_OP = 'INSERT' THEN
                FOR changed_fit_id IN
                    SELECT fit_id FROM new_rows
                     GROUP BY fit_id ORDER BY fit_id
                LOOP
                    SELECT status INTO run_status
                      FROM fit_runs WHERE id = changed_fit_id FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Fit Run % artifacts are immutable',
                            changed_fit_id;
                    END IF;
                END LOOP;
            ELSIF TG_OP = 'DELETE' THEN
                FOR changed_fit_id IN
                    SELECT fit_id FROM old_rows
                     GROUP BY fit_id ORDER BY fit_id
                LOOP
                    SELECT status INTO run_status
                      FROM fit_runs WHERE id = changed_fit_id FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Fit Run % artifacts are immutable',
                            changed_fit_id;
                    END IF;
                END LOOP;
            ELSE
                FOR changed_fit_id IN
                    SELECT fit_id
                      FROM (
                            SELECT fit_id FROM old_rows
                            UNION
                            SELECT fit_id FROM new_rows
                           ) AS changed
                     ORDER BY fit_id
                LOOP
                    SELECT status INTO run_status
                      FROM fit_runs WHERE id = changed_fit_id FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Fit Run % artifacts are immutable',
                            changed_fit_id;
                    END IF;
                END LOOP;
            END IF;
            RETURN NULL;
        END
        $function$
        """,
    ),
    (
        "guard_centerline_source_clip_mutation_function",
        """
        CREATE OR REPLACE FUNCTION guard_centerline_source_clip_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            changed_fit_id UUID;
            run_status TEXT;
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting(
                   'track_v2.allow_completed_run_delete', true
               ) = 'on' THEN
                RETURN NULL;
            END IF;

            IF TG_OP = 'INSERT' THEN
                FOR changed_fit_id IN
                    SELECT centerline.fit_id
                      FROM new_rows AS changed
                      JOIN centerlines AS centerline
                        ON centerline.id = changed.centerline_id
                       AND centerline.detect_id = changed.detect_id
                     GROUP BY centerline.fit_id
                     ORDER BY centerline.fit_id
                LOOP
                    SELECT status INTO run_status
                      FROM fit_runs WHERE id = changed_fit_id FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Fit Run % artifacts are immutable',
                            changed_fit_id;
                    END IF;
                END LOOP;
            ELSIF TG_OP = 'DELETE' THEN
                FOR changed_fit_id IN
                    SELECT centerline.fit_id
                      FROM old_rows AS changed
                      JOIN centerlines AS centerline
                        ON centerline.id = changed.centerline_id
                       AND centerline.detect_id = changed.detect_id
                     GROUP BY centerline.fit_id
                     ORDER BY centerline.fit_id
                LOOP
                    SELECT status INTO run_status
                      FROM fit_runs WHERE id = changed_fit_id FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Fit Run % artifacts are immutable',
                            changed_fit_id;
                    END IF;
                END LOOP;
            ELSE
                FOR changed_fit_id IN
                    SELECT centerline.fit_id
                      FROM (
                            SELECT centerline_id, detect_id FROM old_rows
                            UNION
                            SELECT centerline_id, detect_id FROM new_rows
                           ) AS changed
                      JOIN centerlines AS centerline
                        ON centerline.id = changed.centerline_id
                       AND centerline.detect_id = changed.detect_id
                     GROUP BY centerline.fit_id
                     ORDER BY centerline.fit_id
                LOOP
                    SELECT status INTO run_status
                      FROM fit_runs WHERE id = changed_fit_id FOR SHARE;
                    IF run_status = 'completed' THEN
                        RAISE EXCEPTION
                            'completed Fit Run % artifacts are immutable',
                            changed_fit_id;
                    END IF;
                END LOOP;
            END IF;
            RETURN NULL;
        END
        $function$
        """,
    ),
    (
        "fit_artifact_mutation_triggers",
        """
        DROP TRIGGER IF EXISTS guard_completed_fit_insert ON centerlines;
        CREATE TRIGGER guard_completed_fit_insert
        AFTER INSERT ON centerlines REFERENCING NEW TABLE AS new_rows
        FOR EACH STATEMENT EXECUTE FUNCTION guard_centerline_mutation();
        DROP TRIGGER IF EXISTS guard_completed_fit_update ON centerlines;
        CREATE TRIGGER guard_completed_fit_update
        AFTER UPDATE ON centerlines
        REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
        FOR EACH STATEMENT EXECUTE FUNCTION guard_centerline_mutation();
        DROP TRIGGER IF EXISTS guard_completed_fit_delete ON centerlines;
        CREATE TRIGGER guard_completed_fit_delete
        AFTER DELETE ON centerlines REFERENCING OLD TABLE AS old_rows
        FOR EACH STATEMENT EXECUTE FUNCTION guard_centerline_mutation();

        DROP TRIGGER IF EXISTS guard_completed_fit_insert
            ON centerline_source_clips;
        CREATE TRIGGER guard_completed_fit_insert
        AFTER INSERT ON centerline_source_clips
        REFERENCING NEW TABLE AS new_rows
        FOR EACH STATEMENT
        EXECUTE FUNCTION guard_centerline_source_clip_mutation();
        DROP TRIGGER IF EXISTS guard_completed_fit_update
            ON centerline_source_clips;
        CREATE TRIGGER guard_completed_fit_update
        AFTER UPDATE ON centerline_source_clips
        REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
        FOR EACH STATEMENT
        EXECUTE FUNCTION guard_centerline_source_clip_mutation();
        DROP TRIGGER IF EXISTS guard_completed_fit_delete
            ON centerline_source_clips;
        CREATE TRIGGER guard_completed_fit_delete
        AFTER DELETE ON centerline_source_clips
        REFERENCING OLD TABLE AS old_rows
        FOR EACH STATEMENT
        EXECUTE FUNCTION guard_centerline_source_clip_mutation()
        """,
    ),
]


LEGACY_TABLES = (
    "turn_evnets_gemo",
    "clean_runs",
    "candidate_intersection_events",
    "pipeline_runs",
)


def _schema_version(cur: Any) -> int | None:
    cur.execute("SELECT to_regclass('pipeline_schema_versions')")
    if cur.fetchone()[0] is None:
        return None
    cur.execute(
        "SELECT version FROM pipeline_schema_versions WHERE component = %s",
        (SCHEMA_COMPONENT,),
    )
    row = cur.fetchone()
    return None if row is None else int(row[0])


def _existing_pipeline_tables(cur: Any) -> list[str]:
    names = [
        name for name, _ in DDL_STATEMENTS
        if name not in {"pipeline_schema_versions", "trajectory_points"}
    ]
    names.extend(LEGACY_TABLES)
    cur.execute(
        """
        SELECT table_name
          FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name = ANY(%s)
         ORDER BY table_name
        """,
        (names,),
    )
    return [str(row[0]) for row in cur.fetchall()]


def _require_empty_or_current(cur: Any) -> None:
    version = _schema_version(cur)
    if version == SCHEMA_VERSION:
        return
    if version is not None:
        raise RuntimeError(
            f"database schema is v{version}; track.v2 requires v{SCHEMA_VERSION}. "
            "For v4 through v11 use init-db --migrate-artifacts; "
            "v3 and older schemas require --reset."
        )
    existing = _existing_pipeline_tables(cur)
    if existing:
        preview = ", ".join(existing[:8])
        suffix = " ..." if len(existing) > 8 else ""
        raise RuntimeError(
            "unversioned/legacy pipeline tables exist "
            f"({preview}{suffix}); schema v12 has no unversioned in-place migration. "
            "Run init-db --reset."
        )


def reset_tables(config: PostgresConfig) -> int:
    """Discard Pipeline results and legacy tables, preserving source trajectories."""
    current_names = [
        name for name, _ in reversed(DDL_STATEMENTS) if name != "trajectory_points"
    ]
    drop_names = [*current_names, *LEGACY_TABLES]
    with pg_connect(config) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, current_schema()")
            database, user, schema = cur.fetchone()
            print("\n── 重置目标核对 / Reset Target ──")
            print(f"  database={database} user={user} schema={schema}")
            print("  保留 / Preserved: trajectory_points (data and sequence)")
            print("\n── 重置数据表 / Resetting Tables ──")
            for name in drop_names:
                cur.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')
                print(f"  [OK] DROP {name:40s} {cur.statusmessage}")
            for function_name in (
                "validate_resolved_point_membership",
                "validate_resolved_segment_start",
                "validate_tracks_cross_segments",
                "validate_turn_event_point",
                "validate_entry_exit_observation",
                "validate_centerline_source_clip",
                "enforce_pipeline_run_state_transition",
                "enforce_pipeline_run_initial_status",
                "guard_pipeline_run_delete",
                "validate_detect_run_completion",
                "guard_refine_artifact_mutation",
                "guard_detect_artifact_mutation",
                "validate_fit_run_completion",
                "guard_centerline_mutation",
                "guard_centerline_source_clip_mutation",
                "guard_pipeline_artifact_immutability",
                "validate_pipeline_artifact_event",
                "validate_refine_artifact_completion",
                "validate_detect_artifact_completion",
            ):
                cur.execute(f"DROP FUNCTION IF EXISTS {function_name}() CASCADE")
        conn.commit()
    print(f"\n  已删除 / Dropped: {len(drop_names)} known tables")
    return 0


def create_tables(
    config: PostgresConfig,
    *,
    migrate_artifacts: bool = False,
) -> int:
    """Create schema v12, optionally migrating v4 through v11 in one transaction."""
    with pg_connect(config) as conn:
        with conn.cursor() as cur:
            installed_before = _schema_version(cur)
            if migrate_artifacts and installed_before != SCHEMA_VERSION:
                from ..db.migrations.artifacts_v5 import migrate_artifacts_v5
                from ..db.migrations.refine_completion_v6 import migrate_completion_v6
                from ..db.migrations.detect_completion_v7 import migrate_completion_v7
                from ..db.migrate_source_row_key import migrate_source_row_key_v8

                if _schema_version(cur) == 4:
                    migrate_artifacts_v5(cur)
                if _schema_version(cur) == 5:
                    migrate_completion_v6(cur)
                if _schema_version(cur) == 6:
                    migrate_completion_v7(cur)
                if _schema_version(cur) == 7:
                    migrate_source_row_key_v8(cur)
                if _schema_version(cur) == 8:
                    from ..db.migrations.kept_xy_v9 import migrate_kept_xy_v9

                    migrate_kept_xy_v9(cur)
                if _schema_version(cur) == 9:
                    from ..db.migrations.detect_time_v10 import migrate_detect_time_v10
                    migrate_detect_time_v10(cur)
                if _schema_version(cur) == 10:
                    from ..db.migrations.detect_plugins_v11 import migrate_detect_plugins_v11
                    migrate_detect_plugins_v11(cur)
                if _schema_version(cur) == 11:
                    from ..db.migrations.refine_heading_v12 import migrate_refine_heading_v12
                    migrate_refine_heading_v12(cur)
            _require_empty_or_current(cur)

            print("\n── 创建数据表 / Creating Tables ──")
            for name, ddl in DDL_STATEMENTS:
                cur.execute(ddl)
                print(f"  [OK] {name:40s} {cur.statusmessage}")

            cur.execute(
                """
                INSERT INTO pipeline_schema_versions(component, version)
                VALUES (%s, %s)
                ON CONFLICT (component) DO NOTHING
                """,
                (SCHEMA_COMPONENT, SCHEMA_VERSION),
            )
            cur.execute(
                "SELECT version FROM pipeline_schema_versions WHERE component = %s",
                (SCHEMA_COMPONENT,),
            )
            installed_version = int(cur.fetchone()[0])
            if installed_version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"track.v2 schema version is {installed_version}; "
                    f"expected {SCHEMA_VERSION}"
                )

            print("\n── 创建关系约束 / Creating Relational Guards ──")
            for name, ddl in POST_DDL_STATEMENTS:
                cur.execute(ddl)
                print(f"  [OK] {name:40s} {cur.statusmessage}")

            print("\n── 创建索引 / Creating Indexes ──")
            for name, ddl in INDEX_STATEMENTS:
                cur.execute(ddl)
                print(f"  [OK] {name:40s} {cur.statusmessage}")
        conn.commit()

    print("\n── 汇总 / Summary ──")
    print(f"  Schema: {SCHEMA_COMPONENT} v{SCHEMA_VERSION}")
    print(f"  表 / Tables: {len(DDL_STATEMENTS)} processed")
    print(f"  索引 / Indexes: {len(INDEX_STATEMENTS)} processed")
    print("  [OK] 全部成功 / All succeeded")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize the track.v2 PostgreSQL schema v9."
    )
    parser.add_argument(
        "--ask-password",
        action="store_true",
        help="Prompt for a password instead of using the environment value.",
    )
    migration = parser.add_mutually_exclusive_group()
    migration.add_argument(
        "--reset",
        action="store_true",
        help="Drop Pipeline result and legacy tables before creating v9; "
        "preserve trajectory_points data and its ID sequence.",
    )
    migration.add_argument(
        "--migrate-artifacts", action="store_true",
        help="Preserve v4/v5/v6/v7/v8 data and migrate through schema v9.",
    )
    args = parser.parse_args()

    pipeline_config = load_startup_config(ask_password=args.ask_password)
    config = pipeline_config.postgres

    print(f"连接目标 / Target: {config.safe_dsn()}")
    init_logger(pipeline_config.logging.directory)
    try:
        if args.reset:
            reset_tables(config)
        return create_tables(
            config,
            migrate_artifacts=args.migrate_artifacts,
        )
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 2
    except Exception as exc:
        print(f"[FAIL] 连接或执行失败 / Connection or execution failed: {exc}")
        return 1
    finally:
        shutdown_logger()


if __name__ == "__main__":
    raise SystemExit(main())
