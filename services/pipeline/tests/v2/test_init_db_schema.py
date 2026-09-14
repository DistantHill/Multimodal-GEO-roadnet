from __future__ import annotations

import pytest

from track.v2.cli.init_db import (
    DDL_STATEMENTS,
    POST_DDL_STATEMENTS,
    SCHEMA_COMPONENT,
    SCHEMA_VERSION,
    _require_empty_or_v4,
)


def _ddl(table: str) -> str:
    return dict(DDL_STATEMENTS)[table]


def test_schema_v4_is_an_explicit_destructive_contract() -> None:
    assert SCHEMA_COMPONENT == "track.v2"
    assert SCHEMA_VERSION == 4
    assert "pipeline_schema_versions" in dict(DDL_STATEMENTS)


def test_refine_tables_store_only_v3_source_membership() -> None:
    refine = _ddl("refine_runs")
    kept = _ddl("kept_trajectory_points")
    excluded = _ddl("excluded_points")
    injected = _ddl("injected_points")

    assert "source_table" in refine
    assert "source_fingerprint" in refine
    assert "source_kind" not in refine
    assert "target_table" not in refine
    assert "source_row_key          TEXT NOT NULL" in kept
    assert "trajectory_point_id" not in kept
    assert "PRIMARY KEY (refine_id, source_row_key)" in excluded
    assert "after_sequence_in_subtrack" in injected
    assert "from_source" not in injected
    assert "to_source" not in injected


def test_resolved_sequence_has_one_stable_source_and_two_snapshots() -> None:
    ddl = _ddl("resolved_subtrack_points")

    assert "vehicle_id" not in ddl
    assert "sequence_in_subtrack            INTEGER NOT NULL" in ddl
    assert "refined_sequence_in_subtrack    INTEGER" in ddl
    assert "num_nonnulls(kept_point_id, injected_point_id, crossing_point_id) = 1" in ddl
    assert "UNIQUE (detect_id, subtrack_index, sequence_in_subtrack)" in ddl
    assert "UNIQUE (detect_id, subtrack_index, refined_sequence_in_subtrack)" in ddl


def test_detect_artifacts_use_internal_ids_and_do_not_repeat_derived_fields() -> None:
    track_cross = _ddl("tracks_cross")
    turn = _ddl("turn_events")
    crossing = _ddl("candidate_entry_exit_points")
    entry_exit = _ddl("entry_exit_points")

    assert "from_segment_start_point_id" in track_cross
    assert "to_segment_start_point_id" in track_cross
    assert "from_source" not in track_cross
    assert "resolved_point_id" in turn
    assert "entry_bearing" in turn and "exit_bearing" in turn
    assert "source_row_key" not in turn
    assert "crossing_type               SMALLINT" in crossing
    assert "segment_start_point_id" in crossing
    for removed in ("segment_index", "branch_angle_deg", "origin_lon", "vehicle_id"):
        assert removed not in crossing
    assert "entry_crossing_id   BIGINT NOT NULL" in entry_exit
    assert "exit_crossing_id    BIGINT NOT NULL" in entry_exit
    for removed in ("vehicle_id", "subtrack_index", "turn_deg", "branch_angle"):
        assert removed not in entry_exit


def test_only_verified_intersections_exist_in_intersections_table() -> None:
    assert "is_complete" not in _ddl("intersections")
    assert "candidate_intersection_id" in _ddl("intersections")


def test_centerline_manifest_is_a_thin_crossing_relationship() -> None:
    centerline = _ddl("centerlines")
    manifest = _ddl("centerline_source_clips")

    assert "CHECK (length_m > 0)" in centerline
    assert "forward_clip_count + reverse_clip_count > 0" in centerline
    for field in (
        "centerline_id",
        "detect_id",
        "input_ordinal",
        "start_crossing_id",
        "end_crossing_id",
        "reverse_to_centerline_direction",
    ):
        assert field in manifest
    for removed in (
        "clip_id",
        "vehicle_id",
        "subtrack_index",
        "segment_fraction",
        "interior_start_index",
        "input_point_count",
    ):
        assert removed not in manifest


def test_post_ddl_validates_semantics_once_when_runs_complete() -> None:
    post = dict(POST_DDL_STATEMENTS)
    assert "DEFERRABLE INITIALLY DEFERRED" in post["resolved_points_crossing_fkey"]
    detect = post["validate_detect_completion_function"]
    assert "completed Detect Run requires a completed parent Refine Run" in detect
    assert "Detect parent Refine Run" in detect
    assert "complete intersection" in detect
    assert "contains multiple vehicles" in detect
    assert "different vehicles/subtracks" in detect
    assert "refined p/i resolved point" in detect
    assert "entry/exit crossing types must be 1/0" in detect
    assert "entry crossing must precede exit crossing" in detect
    assert "WITH point_sources AS" in detect
    assert "FULL JOIN point_sources" in detect
    assert "resolved p/i set must exactly equal" in detect
    assert "ROW_NUMBER() OVER" in detect
    assert "ORDER BY anchor_sequence, kind_order, source_point_id" in detect
    assert "resolved refined sequence must equal the derived Refine p/i order" in detect
    assert "refined/final sequence must be contiguous from zero" in detect
    assert "LAG(resolved.sequence_in_subtrack) OVER" in detect
    assert "final sequence must strictly increase with refined sequence" in detect
    assert "resolved crossing must lie between its refined segment endpoints" in detect
    assert "segment_start_sequence < sequence_in_subtrack" in detect
    assert "sequence_in_subtrack < successor_sequence" in detect
    assert "FOR EACH ROW" not in detect

    fit = post["validate_fit_completion_function"]
    assert "centerline clip must start at exit and end at entry" in fit
    assert "start crossing must precede end crossing" in fit
    assert "LEFT JOIN centerline_source_clips" in fit
    assert "WHERE NOT manifest.reverse_to_centerline_direction" in fit
    assert "WHERE manifest.reverse_to_centerline_direction" in fit
    assert "manifest direction counts must match persisted clip counts" in fit

    assert "CREATE CONSTRAINT TRIGGER detect_completion_check" in post[
        "detect_completion_trigger"
    ]
    assert "CREATE CONSTRAINT TRIGGER fit_completion_check" in post[
        "fit_completion_trigger"
    ]


def test_artifact_immutability_guards_are_statement_level() -> None:
    post = dict(POST_DDL_STATEMENTS)
    refine_triggers = post["refine_artifact_mutation_triggers"]
    detect_triggers = post["detect_artifact_mutation_triggers"]
    fit_triggers = post["fit_artifact_mutation_triggers"]

    assert "FOR EACH STATEMENT" in refine_triggers
    assert "FOR EACH STATEMENT" in detect_triggers
    assert "FOR EACH STATEMENT" in fit_triggers
    assert "CREATE CONSTRAINT TRIGGER" not in detect_triggers
    assert "CREATE CONSTRAINT TRIGGER" not in fit_triggers
    assert "FOR EACH ROW" not in refine_triggers
    assert "FOR EACH ROW" not in detect_triggers
    assert "FOR EACH ROW" not in fit_triggers
    assert "completed Refine Run % artifacts are immutable" in post[
        "guard_refine_artifact_mutation_function"
    ]
    assert "FOR SHARE" in post["guard_detect_artifact_mutation_function"]
    assert "completed Detect Run % artifacts are immutable" in post[
        "guard_detect_artifact_mutation_function"
    ]
    assert "completed Fit Run % artifacts are immutable" in post[
        "guard_centerline_mutation_function"
    ]
    assert "track_v2.allow_completed_run_delete" in post[
        "guard_centerline_source_clip_mutation_function"
    ]


def test_all_run_deletes_require_the_confirmation_gated_service() -> None:
    post = dict(POST_DDL_STATEMENTS)
    function = post["run_delete_guard_function"]
    triggers = post["run_delete_guard_triggers"]

    assert "track_v2.allow_completed_run_delete" in function
    assert "IS DISTINCT FROM 'on'" in function
    assert "DeleteRunService" in function
    for table in ("refine_runs", "detect_runs", "fit_runs"):
        assert f"BEFORE DELETE ON {table}" in triggers
    assert triggers.count("FOR EACH STATEMENT") == 3


def test_all_runs_must_start_running_and_are_frozen_after_completion() -> None:
    post = dict(POST_DDL_STATEMENTS)
    initial = post["run_initial_status_function"]
    insert_triggers = post["run_initial_status_triggers"]

    assert "NEW.status <> 'running'" in initial
    for table in ("refine_runs", "detect_runs", "fit_runs"):
        assert f"BEFORE INSERT ON {table}" in insert_triggers
    for trigger_name in (
        "refine_run_state_trigger",
        "detect_run_state_trigger",
        "fit_run_state_trigger",
    ):
        assert "BEFORE UPDATE" in post[trigger_name]


def test_old_per_row_semantic_triggers_are_removed() -> None:
    cleanup = dict(POST_DDL_STATEMENTS)["remove_row_semantic_triggers"]
    for trigger_name in (
        "resolved_point_membership_check",
        "candidate_crossing_segment_start_check",
        "tracks_cross_segments_check",
        "turn_event_point_check",
        "entry_exit_observation_check",
        "centerline_source_clip_check",
    ):
        assert f"DROP TRIGGER IF EXISTS {trigger_name}" in cleanup


class _VersionCursor:
    def __init__(self, version: int | None, tables: list[str]) -> None:
        self.version = version
        self.tables = tables
        self.last_sql = ""

    def execute(self, sql: str, _params: object = None) -> None:
        self.last_sql = sql

    def fetchone(self) -> tuple[object, ...]:
        if "to_regclass" in self.last_sql:
            return (("pipeline_schema_versions" if self.version is not None else None),)
        return (self.version,)

    def fetchall(self) -> list[tuple[str]]:
        return [(table,) for table in self.tables]


def test_unversioned_legacy_schema_is_refused_without_reset() -> None:
    with pytest.raises(RuntimeError, match="no in-place migration"):
        _require_empty_or_v4(_VersionCursor(None, ["refine_runs"]))


def test_existing_v3_schema_is_accepted() -> None:
    _require_empty_or_v4(_VersionCursor(4, ["refine_runs"]))
