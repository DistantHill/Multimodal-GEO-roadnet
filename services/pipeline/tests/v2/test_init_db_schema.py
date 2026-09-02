"""Auto-migration tests for init_db schema diff logic.

All tests use recording cursors — no real database connection needed.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from track.v2.cli.init_db import (
    DDL_STATEMENTS,
    INDEX_STATEMENTS,
    _auto_add_missing_columns,
    build_auto_migrations,
    column_to_alter_fragment,
    parse_ddl_columns,
)
from track.v2.db.legacy_migration import (
    LegacyPipelineRun,
    build_legacy_migration_plan,
    load_legacy_clean_map,
    migrate_legacy_pipeline_runs,
)


class CleanDetectSchemaContractTest(unittest.TestCase):
    def test_schema_separates_clean_and_detect_runs_with_same_detect_domain_constraints(self) -> None:
        ddl_by_table = {name: ddl for name, ddl in DDL_STATEMENTS}

        self.assertIn("clean_runs", ddl_by_table)
        self.assertIn("detect_runs", ddl_by_table)
        self.assertNotIn("pipeline_runs", ddl_by_table)
        self.assertIn(
            "clean_id UUID NOT NULL REFERENCES clean_runs(id)",
            " ".join(ddl_by_table["detect_runs"].split()),
        )
        for table in ("excluded_points", "kept_trajectory_points", "injected_points"):
            normalized = " ".join(ddl_by_table[table].split())
            self.assertIn("clean_id", normalized, table)
            self.assertNotIn("run_id", normalized, table)
        for table in (
            "tracks_cross", "turn_events", "candidate_intersections",
            "candidate_entry_exit_points",
            "intersections", "entry_exit_points",
        ):
            normalized = " ".join(ddl_by_table[table].split())
            self.assertIn("detect_id", normalized, table)
            self.assertNotIn("run_id", normalized, table)

        nested = " ".join(
            ddl_by_table[table]
            for table in (
                "candidate_entry_exit_points",
                "intersections", "entry_exit_points",
            )
        )
        self.assertNotIn("candidate_intersection_events", ddl_by_table)
        for table in ("turn_events", "tracks_cross"):
            self.assertIn("candidate_intersection_id", ddl_by_table[table])
        self.assertIn("FOREIGN KEY (candidate_intersection_id, detect_id)", nested)
        self.assertIn("REFERENCES candidate_intersections(id, detect_id)", nested)
        self.assertIn("FOREIGN KEY (intersection_id, detect_id)", nested)
        self.assertIn("REFERENCES intersections(id, detect_id)", nested)

        all_schema_sql = "\n".join(ddl_by_table.values()) + "\n" + "\n".join(
            sql for _, sql in INDEX_STATEMENTS
        )
        self.assertNotIn("ON DELETE CASCADE", all_schema_sql.upper())
        self.assertIn("clean_id", "\n".join(sql for _, sql in INDEX_STATEMENTS))
        self.assertIn("detect_id", "\n".join(sql for _, sql in INDEX_STATEMENTS))


class LegacyRunMigrationPlanTest(unittest.TestCase):
    def test_full_run_backfills_same_uuid_into_clean_and_detect(self) -> None:
        run = LegacyPipelineRun(
            id="11111111-1111-1111-1111-111111111111",
            source_id="raw_points",
            source_kind="postgres_table",
            parameters={"skip_clean": False, "target_table": "raw_points"},
            status="completed",
            started_at="start",
            finished_at="finish",
        )

        plan = build_legacy_migration_plan([run], {})

        self.assertEqual([item.id for item in plan.clean_runs], [run.id])
        self.assertEqual([item.id for item in plan.detect_runs], [run.id])
        self.assertEqual(plan.detect_runs[0].clean_id, run.id)

    def test_ambiguous_legacy_run_fails_before_migration_without_complete_map(self) -> None:
        clean = LegacyPipelineRun(
            id="11111111-1111-1111-1111-111111111111",
            source_id="raw_points", source_kind="postgres_table",
            parameters={"skip_clean": False}, status="completed",
            started_at="start", finished_at="finish",
        )
        ambiguous = LegacyPipelineRun(
            id="22222222-2222-2222-2222-222222222222",
            source_id="raw_points", source_kind="postgres_table",
            parameters={"skip_clean": True}, status="completed",
            started_at="start", finished_at="finish",
        )

        with self.assertRaisesRegex(ValueError, ambiguous.id):
            build_legacy_migration_plan([clean, ambiguous], {})

        plan = build_legacy_migration_plan(
            [clean, ambiguous], {ambiguous.id: clean.id}
        )
        self.assertEqual(plan.detect_runs[1].clean_id, clean.id)
        self.assertEqual(len(plan.clean_runs), 1)

    def test_map_rejects_non_uuid_and_unavailable_parent_clean(self) -> None:
        ambiguous = LegacyPipelineRun(
            id="22222222-2222-2222-2222-222222222222",
            source_id="raw_points", source_kind="postgres_table",
            parameters={}, status="completed", started_at="start", finished_at=None,
        )
        with self.assertRaisesRegex(ValueError, "real legacy run"):
            build_legacy_migration_plan(
                [ambiguous],
                {ambiguous.id: "33333333-3333-3333-3333-333333333333"},
            )

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "map.json"
            path.write_text('{"not-a-uuid": "also-bad"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "UUID"):
                load_legacy_clean_map(path)

    def test_ambiguous_run_can_be_explicitly_declared_as_its_own_clean(self) -> None:
        run = LegacyPipelineRun(
            id="22222222-2222-2222-2222-222222222222",
            source_id="raw_points", source_kind="postgres_table",
            parameters={}, status="completed", started_at="start", finished_at=None,
        )

        plan = build_legacy_migration_plan([run], {run.id: run.id})

        self.assertEqual([item.id for item in plan.clean_runs], [run.id])
        self.assertEqual(plan.detect_runs[0].clean_id, run.id)

    def test_ambiguous_run_can_parent_another_ambiguous_detect(self) -> None:
        parent = LegacyPipelineRun(
            id="11111111-1111-1111-1111-111111111111",
            source_id="raw_points", source_kind="postgres_table",
            parameters={}, status="completed", started_at="start", finished_at=None,
        )
        child = LegacyPipelineRun(
            id="22222222-2222-2222-2222-222222222222",
            source_id="raw_points", source_kind="postgres_table",
            parameters={"skip_clean": True}, status="completed",
            started_at="start", finished_at=None,
        )

        plan = build_legacy_migration_plan(
            [parent, child],
            {parent.id: parent.id, child.id: parent.id},
        )

        self.assertEqual([item.id for item in plan.clean_runs], [parent.id])
        self.assertEqual(
            [item.clean_id for item in plan.detect_runs],
            [parent.id, parent.id],
        )


class _LegacyMigrationCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.pipeline_rows = rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.last_sql = ""
        self.last_params: tuple[object, ...] = ()
        self.registry_created = False

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.last_sql = " ".join(sql.split())
        self.last_params = params
        self.executed.append((self.last_sql, params))
        if self.last_sql.startswith("CREATE TABLE"):
            self.registry_created = True

    def fetchone(self):
        if "to_regclass('pipeline_runs')" in self.last_sql:
            return ("pipeline_runs",)
        if "COUNT(*), COUNT(DISTINCT id)" in self.last_sql:
            if "FILTER" in self.last_sql:
                return (0, 0, 0, 0)
            return (0, 0, 0)
        if self.last_sql == "SELECT COUNT(*) FROM clean_runs":
            return (1,)
        if self.last_sql == "SELECT COUNT(*) FROM detect_runs":
            return (1,)
        if "COUNT(*)" in self.last_sql:
            return (0,)
        return None

    def fetchall(self):
        if self.last_sql.startswith("SELECT id, source_id"):
            return self.pipeline_rows
        if "information_schema.columns" in self.last_sql:
            return [("id",), ("run_id",)]
        if "FROM pg_constraint" in self.last_sql:
            return [("legacy_run_fk",)]
        if self.last_sql.startswith("SELECT id, source_id") and "FROM clean_runs" in self.last_sql:
            return []
        return []


class LegacyMigrationExecutionTest(unittest.TestCase):
    def test_ambiguous_preflight_executes_no_mutating_statement(self) -> None:
        detect_id = "22222222-2222-2222-2222-222222222222"
        cur = _LegacyMigrationCursor([
            (
                detect_id, "raw_points", "postgres_table", {"skip_clean": True},
                "completed", "start", "finish",
            ),
        ])

        with self.assertRaisesRegex(ValueError, detect_id):
            migrate_legacy_pipeline_runs(
                cur,
                {},
                registry_ddl=dict(DDL_STATEMENTS),
                index_statements=INDEX_STATEMENTS,
            )

        mutations = [
            sql for sql, _ in cur.executed
            if sql.startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP"))
        ]
        self.assertEqual(mutations, [])

        statements = [sql for sql, _ in cur.executed]
        plan_read = next(
            index for index, sql in enumerate(statements)
            if sql.startswith("SELECT id, source_id")
        )
        expected_lock_order = [
            "pipeline_runs", "excluded_points", "kept_trajectory_points",
            "injected_points", "tracks_cross", "turn_events",
            "candidate_intersections", "candidate_intersection_events",
            "candidate_entry_exit_points", "intersections", "entry_exit_points",
        ]
        self.assertEqual(
            statements[1:plan_read],
            [
                f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"
                for table in expected_lock_order
            ],
        )

    def test_full_run_migration_renames_in_place_validates_then_drops_registry(self) -> None:
        run_id = "11111111-1111-1111-1111-111111111111"
        cur = _LegacyMigrationCursor([
            (
                run_id, "raw_points", "postgres_table",
                {"skip_clean": False, "target_table": "raw_points"},
                "completed", "start", "finish",
            ),
        ])

        migrated = migrate_legacy_pipeline_runs(
            cur,
            {},
            registry_ddl=dict(DDL_STATEMENTS),
            index_statements=INDEX_STATEMENTS,
        )

        self.assertTrue(migrated)
        sql = "\n".join(statement for statement, _ in cur.executed)
        self.assertIn("ALTER TABLE kept_trajectory_points RENAME COLUMN run_id TO clean_id", sql)
        self.assertIn("ALTER TABLE intersections RENAME COLUMN run_id TO detect_id", sql)
        self.assertIn("FOREIGN KEY (clean_id) REFERENCES clean_runs(id)", sql)
        self.assertIn("FOREIGN KEY (detect_id) REFERENCES detect_runs(id)", sql)
        self.assertIn("FOREIGN KEY (candidate_intersection_id, detect_id)", sql)
        self.assertLess(sql.index("CREATE TABLE IF NOT EXISTS clean_runs"), sql.index("DROP TABLE pipeline_runs"))
        self.assertTrue(sql.rstrip().endswith("DROP TABLE pipeline_runs"))

    def test_no_pipeline_registry_is_an_idempotent_noop(self) -> None:
        cur = _LegacyMigrationCursor([])
        original_fetchone = cur.fetchone
        cur.fetchone = lambda: (None,) if "to_regclass" in cur.last_sql else original_fetchone()

        self.assertFalse(migrate_legacy_pipeline_runs(
            cur,
            {},
            registry_ddl=dict(DDL_STATEMENTS),
            index_statements=INDEX_STATEMENTS,
        ))
        self.assertEqual(len(cur.executed), 1)


class CleanDetectSchemaContractTest(unittest.TestCase):
    def test_schema_separates_clean_and_detect_runs_with_same_detect_domain_constraints(self) -> None:
        ddl_by_table = {name: ddl for name, ddl in DDL_STATEMENTS}

        self.assertIn("clean_runs", ddl_by_table)
        self.assertIn("detect_runs", ddl_by_table)
        self.assertNotIn("pipeline_runs", ddl_by_table)
        self.assertIn(
            "clean_id UUID NOT NULL REFERENCES clean_runs(id)",
            " ".join(ddl_by_table["detect_runs"].split()),
        )
        for table in ("excluded_points", "kept_trajectory_points", "injected_points"):
            normalized = " ".join(ddl_by_table[table].split())
            self.assertIn("clean_id", normalized, table)
            self.assertNotIn("run_id", normalized, table)
        for table in (
            "tracks_cross", "turn_events", "candidate_intersections",
            "candidate_entry_exit_points",
            "intersections", "entry_exit_points",
        ):
            normalized = " ".join(ddl_by_table[table].split())
            self.assertIn("detect_id", normalized, table)
            self.assertNotIn("run_id", normalized, table)

        nested = " ".join(
            ddl_by_table[table]
            for table in (
                "candidate_entry_exit_points",
                "intersections", "entry_exit_points",
            )
        )
        self.assertNotIn("candidate_intersection_events", ddl_by_table)
        for table in ("turn_events", "tracks_cross"):
            self.assertIn("candidate_intersection_id", ddl_by_table[table])
        self.assertIn("FOREIGN KEY (candidate_intersection_id, detect_id)", nested)
        self.assertIn("REFERENCES candidate_intersections(id, detect_id)", nested)
        self.assertIn("FOREIGN KEY (intersection_id, detect_id)", nested)
        self.assertIn("REFERENCES intersections(id, detect_id)", nested)

        all_schema_sql = "\n".join(ddl_by_table.values()) + "\n" + "\n".join(
            sql for _, sql in INDEX_STATEMENTS
        )
        self.assertNotIn("ON DELETE CASCADE", all_schema_sql.upper())
        self.assertIn("clean_id", "\n".join(sql for _, sql in INDEX_STATEMENTS))
        self.assertIn("detect_id", "\n".join(sql for _, sql in INDEX_STATEMENTS))

        for table in ("fit_runs", "centerlines", "centerline_source_clips"):
            self.assertIn(table, ddl_by_table)
        fit_sql = " ".join(ddl_by_table["fit_runs"].split())
        centerline_sql = " ".join(ddl_by_table["centerlines"].split())
        manifest_sql = " ".join(ddl_by_table["centerline_source_clips"].split())
        self.assertIn("UNIQUE (id, detect_id)", fit_sql)
        self.assertIn("geometry(LineString, 4326)", centerline_sql)
        self.assertIn("UNIQUE (fit_id, variant, from_cluster_id, to_cluster_id)", centerline_sql)
        self.assertIn("PRIMARY KEY (centerline_id, input_ordinal)", manifest_sql)
        self.assertNotIn("source_clip_ids", centerline_sql)


class LegacyRunMigrationPlanTest(unittest.TestCase):
    def test_full_run_backfills_same_uuid_into_clean_and_detect(self) -> None:
        run = LegacyPipelineRun(
            id="11111111-1111-1111-1111-111111111111",
            source_id="raw_points",
            source_kind="postgres_table",
            parameters={"skip_clean": False, "target_table": "raw_points"},
            status="completed",
            started_at="start",
            finished_at="finish",
        )

        plan = build_legacy_migration_plan([run], {})

        self.assertEqual([item.id for item in plan.clean_runs], [run.id])
        self.assertEqual([item.id for item in plan.detect_runs], [run.id])
        self.assertEqual(plan.detect_runs[0].clean_id, run.id)

    def test_ambiguous_legacy_run_fails_before_migration_without_complete_map(self) -> None:
        clean = LegacyPipelineRun(
            id="11111111-1111-1111-1111-111111111111",
            source_id="raw_points", source_kind="postgres_table",
            parameters={"skip_clean": False}, status="completed",
            started_at="start", finished_at="finish",
        )
        ambiguous = LegacyPipelineRun(
            id="22222222-2222-2222-2222-222222222222",
            source_id="raw_points", source_kind="postgres_table",
            parameters={"skip_clean": True}, status="completed",
            started_at="start", finished_at="finish",
        )

        with self.assertRaisesRegex(ValueError, ambiguous.id):
            build_legacy_migration_plan([clean, ambiguous], {})

        plan = build_legacy_migration_plan(
            [clean, ambiguous], {ambiguous.id: clean.id}
        )
        self.assertEqual(plan.detect_runs[1].clean_id, clean.id)
        self.assertEqual(len(plan.clean_runs), 1)

    def test_map_rejects_non_uuid_and_unavailable_parent_clean(self) -> None:
        ambiguous = LegacyPipelineRun(
            id="22222222-2222-2222-2222-222222222222",
            source_id="raw_points", source_kind="postgres_table",
            parameters={}, status="completed", started_at="start", finished_at=None,
        )
        with self.assertRaisesRegex(ValueError, "real legacy run"):
            build_legacy_migration_plan(
                [ambiguous],
                {ambiguous.id: "33333333-3333-3333-3333-333333333333"},
            )

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "map.json"
            path.write_text('{"not-a-uuid": "also-bad"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "UUID"):
                load_legacy_clean_map(path)


class _LegacyMigrationCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.pipeline_rows = rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.last_sql = ""
        self.last_params: tuple[object, ...] = ()
        self.registry_created = False

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.last_sql = " ".join(sql.split())
        self.last_params = params
        self.executed.append((self.last_sql, params))
        if self.last_sql.startswith("CREATE TABLE"):
            self.registry_created = True

    def fetchone(self):
        if "to_regclass('pipeline_runs')" in self.last_sql:
            return ("pipeline_runs",)
        if "COUNT(*), COUNT(DISTINCT id)" in self.last_sql:
            if "FILTER" in self.last_sql:
                return (0, 0, 0, 0)
            return (0, 0, 0)
        if self.last_sql == "SELECT COUNT(*) FROM clean_runs":
            return (1,)
        if self.last_sql == "SELECT COUNT(*) FROM detect_runs":
            return (1,)
        if "COUNT(*)" in self.last_sql:
            return (0,)
        return None

    def fetchall(self):
        if self.last_sql.startswith("SELECT id, source_id"):
            return self.pipeline_rows
        if "information_schema.columns" in self.last_sql:
            return [("id",), ("run_id",)]
        if "FROM pg_constraint" in self.last_sql:
            return [("legacy_run_fk",)]
        if self.last_sql.startswith("SELECT id, source_id") and "FROM clean_runs" in self.last_sql:
            return []
        return []


class LegacyMigrationExecutionTest(unittest.TestCase):
    def test_ambiguous_preflight_executes_no_mutating_statement(self) -> None:
        detect_id = "22222222-2222-2222-2222-222222222222"
        cur = _LegacyMigrationCursor([
            (
                detect_id, "raw_points", "postgres_table", {"skip_clean": True},
                "completed", "start", "finish",
            ),
        ])

        with self.assertRaisesRegex(ValueError, detect_id):
            migrate_legacy_pipeline_runs(
                cur,
                {},
                registry_ddl=dict(DDL_STATEMENTS),
                index_statements=INDEX_STATEMENTS,
            )

        mutations = [
            sql for sql, _ in cur.executed
            if sql.startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP"))
        ]
        self.assertEqual(mutations, [])

    def test_full_run_migration_renames_in_place_validates_then_drops_registry(self) -> None:
        run_id = "11111111-1111-1111-1111-111111111111"
        cur = _LegacyMigrationCursor([
            (
                run_id, "raw_points", "postgres_table",
                {"skip_clean": False, "target_table": "raw_points"},
                "completed", "start", "finish",
            ),
        ])

        migrated = migrate_legacy_pipeline_runs(
            cur,
            {},
            registry_ddl=dict(DDL_STATEMENTS),
            index_statements=INDEX_STATEMENTS,
        )

        self.assertTrue(migrated)
        sql = "\n".join(statement for statement, _ in cur.executed)
        self.assertIn("ALTER TABLE kept_trajectory_points RENAME COLUMN run_id TO clean_id", sql)
        self.assertIn("ALTER TABLE intersections RENAME COLUMN run_id TO detect_id", sql)
        self.assertIn("FOREIGN KEY (clean_id) REFERENCES clean_runs(id)", sql)
        self.assertIn("FOREIGN KEY (detect_id) REFERENCES detect_runs(id)", sql)
        self.assertIn("FOREIGN KEY (candidate_intersection_id, detect_id)", sql)
        self.assertLess(sql.index("CREATE TABLE IF NOT EXISTS clean_runs"), sql.index("DROP TABLE pipeline_runs"))
        self.assertTrue(sql.rstrip().endswith("DROP TABLE pipeline_runs"))

    def test_no_pipeline_registry_is_an_idempotent_noop(self) -> None:
        cur = _LegacyMigrationCursor([])
        original_fetchone = cur.fetchone
        cur.fetchone = lambda: (None,) if "to_regclass" in cur.last_sql else original_fetchone()

        self.assertFalse(migrate_legacy_pipeline_runs(
            cur,
            {},
            registry_ddl=dict(DDL_STATEMENTS),
            index_statements=INDEX_STATEMENTS,
        ))
        self.assertEqual(len(cur.executed), 1)


# ── Recording cursor / connection ────────────────────────────────────


class _RecordingCursor:
    """Tracks executed SQL and returns a configurable set of existing columns."""

    def __init__(self, existing_columns: set[str]) -> None:
        self.existing_columns = existing_columns
        self.executed: list[str] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.executed.append(sql)

    def fetchall(self) -> list[tuple[str]]:
        return [(c,) for c in sorted(self.existing_columns)]

    @property
    def statusmessage(self) -> str:
        return "ALTER TABLE"


class _RaisingCursor:
    """A cursor that succeeds on SELECT but raises on ALTER TABLE."""

    def __init__(self) -> None:
        self.existing_columns: set[str] = set()

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        if "ALTER TABLE" in sql:
            raise RuntimeError("simulated DB error")

    def fetchall(self) -> list[tuple[str]]:
        return []

    @property
    def statusmessage(self) -> str:
        return ""


# ── Real DDL snippets (simplified) for fast tests ────────────────────

DDL_MINIMAL = """
CREATE TABLE IF NOT EXISTS test_table (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    value           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    extra           JSONB,
    geom            geometry(Point, 4326) GENERATED ALWAYS AS (
                        ST_SetSRID(ST_MakePoint(0, 0), 4326)
                    ) STORED,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


# ═══════════════════════════════════════════════════════════════════════
# ParseDDLColumnsTest
# ═══════════════════════════════════════════════════════════════════════


class ParseBasicsTest(unittest.TestCase):
    """Parse a minimal DDL and verify column names and fragments."""

    def test_extracts_regular_columns(self) -> None:
        cols = parse_ddl_columns(DDL_MINIMAL)
        self.assertIn("name", cols)
        self.assertIn("value", cols)
        self.assertIn("extra", cols)
        self.assertIn("created_at", cols)

    def test_includes_bigserial_in_parse(self) -> None:
        """parse_ddl_columns returns ALL columns — filtering happens in the transform."""
        cols = parse_ddl_columns(DDL_MINIMAL)
        self.assertIn("id", cols)
        # But the transform will skip it
        self.assertIsNone(column_to_alter_fragment("id", cols["id"]))

    def test_includes_generated_in_parse(self) -> None:
        """parse_ddl_columns returns ALL columns — filtering happens in the transform."""
        cols = parse_ddl_columns(DDL_MINIMAL)
        self.assertIn("geom", cols)
        # But the transform will skip it
        self.assertIsNone(column_to_alter_fragment("geom", cols["geom"]))

    def test_preserves_geometry_type_with_parens(self) -> None:
        """A column whose type has nested parens must stay intact."""
        ddl = """
        CREATE TABLE t (geom geometry(Polygon, 4326), x INTEGER)
        """
        cols = parse_ddl_columns(ddl)
        self.assertIn("geom", cols)
        self.assertIn("geometry(Polygon, 4326)", cols["geom"])

    def test_preserves_default_clause(self) -> None:
        cols = parse_ddl_columns(DDL_MINIMAL)
        self.assertIn("TIMESTAMPTZ NOT NULL DEFAULT NOW()", cols["created_at"])

    def test_strips_sql_comments(self) -> None:
        ddl = """
        CREATE TABLE t (
            geom geometry(Polygon, 4326),  -- app-layer: ST_Buffer(geography)
            x INTEGER
        )
        """
        cols = parse_ddl_columns(ddl)
        self.assertIn("geom", cols)
        self.assertIn("x", cols)
        # The comment should be gone; no trailing comma/parens in fragment
        self.assertEqual("geometry(Polygon, 4326)", cols["geom"])


class ParseRealDDLTest(unittest.TestCase):
    """Smoke-test every real DDL_STATEMENTS entry."""

    def test_all_ddl_tables_parse_without_exception(self) -> None:
        for name, ddl in DDL_STATEMENTS:
            with self.subTest(table=name):
                cols = parse_ddl_columns(ddl)
                self.assertIsInstance(cols, dict)
                # Every table should have at least 2 parsed columns
                self.assertGreaterEqual(
                    len(cols), 2,
                    f"{name}: expected >= 2 non-generated, non-serial columns"
                )


class TurnEventProvenanceSchemaTest(unittest.TestCase):
    def test_new_and_existing_turn_event_tables_get_point_provenance_columns(self) -> None:
        turn_events_ddl = dict(DDL_STATEMENTS)["turn_events"]
        columns = parse_ddl_columns(turn_events_ddl)

        self.assertEqual(columns["tp_source_id"], "TEXT")
        self.assertEqual(columns["tp_source_row_key"], "TEXT")

        existing = set(columns) - {"tp_source_id", "tp_source_row_key"}
        migrations = build_auto_migrations("turn_events", turn_events_ddl, existing)
        self.assertEqual(
            [(column, sql) for column, _fragment, sql in migrations],
            [
                (
                    "tp_source_id",
                    "ALTER TABLE turn_events ADD COLUMN IF NOT EXISTS tp_source_id TEXT",
                ),
                (
                    "tp_source_row_key",
                    "ALTER TABLE turn_events ADD COLUMN IF NOT EXISTS tp_source_row_key TEXT",
                ),
            ],
        )


# ═══════════════════════════════════════════════════════════════════════
# ColumnTransformTest
# ═══════════════════════════════════════════════════════════════════════


class ColumnTransformTest(unittest.TestCase):
    """column_to_alter_fragment edge cases."""

    def test_generated_column_returns_none(self) -> None:
        result = column_to_alter_fragment(
            "geom",
            "geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED",
        )
        self.assertIsNone(result)

    def test_serial_returns_none(self) -> None:
        self.assertIsNone(column_to_alter_fragment("id", "BIGSERIAL PRIMARY KEY"))
        self.assertIsNone(column_to_alter_fragment("id", "SERIAL"))
        self.assertIsNone(column_to_alter_fragment("id", "SMALLSERIAL"))

    def test_not_null_with_default_kept(self) -> None:
        result = column_to_alter_fragment("x", "BOOLEAN NOT NULL DEFAULT TRUE")
        self.assertIsNotNone(result)
        self.assertIn("NOT NULL", result)
        self.assertIn("DEFAULT TRUE", result)

    def test_not_null_without_default_stripped(self) -> None:
        result = column_to_alter_fragment("x", "DOUBLE PRECISION NOT NULL")
        self.assertIsNotNone(result)
        self.assertNotIn("NOT NULL", result)
        self.assertIn("DOUBLE PRECISION", result)

    def test_not_null_without_default_with_references_stripped(self) -> None:
        """run_id UUID NOT NULL REFERENCES pipeline_runs(id) — strip NOT NULL."""
        result = column_to_alter_fragment(
            "run_id", "UUID NOT NULL REFERENCES pipeline_runs(id)"
        )
        self.assertIsNotNone(result)
        self.assertNotIn("NOT NULL", result)
        self.assertIn("UUID", result)
        self.assertIn("REFERENCES pipeline_runs(id)", result)

    def test_nullable_column_unchanged(self) -> None:
        result = column_to_alter_fragment("finished_at", "TIMESTAMPTZ")
        self.assertIsNotNone(result)
        self.assertEqual("TIMESTAMPTZ", result)

    def test_primary_key_stripped(self) -> None:
        result = column_to_alter_fragment("x", "UUID PRIMARY KEY")
        self.assertIsNotNone(result)
        self.assertNotIn("PRIMARY KEY", result)
        self.assertIn("UUID", result)

    def test_unique_stripped(self) -> None:
        result = column_to_alter_fragment("x", "TEXT UNIQUE")
        self.assertIsNotNone(result)
        self.assertNotIn("UNIQUE", result)

    def test_not_null_with_default_not_null_kept(self) -> None:
        """NOT NULL must survive when DEFAULT is also present."""
        result = column_to_alter_fragment(
            "event_kind", "TEXT NOT NULL DEFAULT 'turn'"
        )
        self.assertIsNotNone(result)
        self.assertIn("NOT NULL", result)
        self.assertIn("DEFAULT 'turn'", result)


# ═══════════════════════════════════════════════════════════════════════
# BuildAutoMigrationsTest
# ═══════════════════════════════════════════════════════════════════════


class BuildAutoMigrationsTest(unittest.TestCase):
    """Pure-function tests for build_auto_migrations."""

    def test_missing_plain_column_generates_sql(self) -> None:
        entries = build_auto_migrations("t", DDL_MINIMAL, {"id", "geom", "name"})
        # 'value' and 'extra' and 'created_at' are missing
        names = {e[0] for e in entries}
        self.assertIn("value", names)
        self.assertIn("extra", names)
        self.assertIn("created_at", names)

        # Verify the generated SQL looks right
        for col, fragment, sql in entries:
            if col == "value":
                self.assertIsNotNone(fragment)
                self.assertIsNotNone(sql)
                self.assertIn("ALTER TABLE t ADD COLUMN IF NOT EXISTS", sql or "")
                # NOT NULL stripped (no DEFAULT on 'value'? Actually value has DEFAULT 0.0)
                # value: DOUBLE PRECISION NOT NULL DEFAULT 0.0 → keeps NOT NULL
                self.assertIn("NOT NULL", fragment or "")

    def test_missing_generated_has_none_sql(self) -> None:
        entries = build_auto_migrations("t", DDL_MINIMAL, {"id", "name", "value", "extra", "created_at"})
        # Only 'geom' is missing — it's generated, so sql should be None
        self.assertEqual(1, len(entries))
        col, fragment, sql = entries[0]
        self.assertEqual("geom", col)
        self.assertIsNone(fragment)
        self.assertIsNone(sql)

    def test_missing_serial_has_none_sql(self) -> None:
        entries = build_auto_migrations("t", DDL_MINIMAL, {"geom"})
        # 'id' is SERIAL → skipped
        ids = [e for e in entries if e[0] == "id"]
        self.assertEqual(1, len(ids))
        self.assertIsNone(ids[0][1])
        self.assertIsNone(ids[0][2])

    def test_all_columns_present_returns_empty(self) -> None:
        entries = build_auto_migrations(
            "t", DDL_MINIMAL,
            {"id", "name", "value", "extra", "geom", "created_at"},
        )
        self.assertEqual([], entries)


# ═══════════════════════════════════════════════════════════════════════
# AutoAddMissingColumnsTest
# ═══════════════════════════════════════════════════════════════════════


class AutoAddMissingColumnsTest(unittest.TestCase):
    """Integration tests for _auto_add_missing_columns with recording cursors."""

    def test_adds_missing_column(self) -> None:
        cur = _RecordingCursor({"id", "geom", "name"})
        failures = _auto_add_missing_columns(cur, "test_table", DDL_MINIMAL)
        self.assertEqual(0, failures)

        alter_stmts = [s for s in cur.executed if "ALTER TABLE" in s]
        # 'value', 'extra', 'created_at' should be added
        self.assertEqual(3, len(alter_stmts), f"Expected 3 ALTER TABLE, got: {alter_stmts}")
        self.assertTrue(any("value" in s for s in alter_stmts))
        self.assertTrue(any("extra" in s for s in alter_stmts))
        self.assertTrue(any("created_at" in s for s in alter_stmts))

    def test_no_missing_columns_no_alter(self) -> None:
        cur = _RecordingCursor({"name", "value", "extra", "created_at"})
        failures = _auto_add_missing_columns(cur, "test_table", DDL_MINIMAL)
        self.assertEqual(0, failures)

        alter_stmts = [s for s in cur.executed if "ALTER TABLE" in s]
        self.assertEqual([], alter_stmts)

    def test_database_error_propagates_for_transaction_rollback(self) -> None:
        cur = _RaisingCursor()
        with self.assertRaisesRegex(RuntimeError, "simulated DB error"):
            _auto_add_missing_columns(cur, "test_table", DDL_MINIMAL)


if __name__ == "__main__":
    unittest.main()
