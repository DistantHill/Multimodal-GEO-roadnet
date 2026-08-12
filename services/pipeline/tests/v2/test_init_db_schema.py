"""Auto-migration tests for init_db schema diff logic.

All tests use recording cursors — no real database connection needed.
"""

from __future__ import annotations

import unittest

from track.v2.cli.init_db import (
    DDL_STATEMENTS,
    _auto_add_missing_columns,
    build_auto_migrations,
    column_to_alter_fragment,
    parse_ddl_columns,
)


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

    def test_raises_increments_failures(self) -> None:
        cur = _RaisingCursor()
        # _RaisingCursor.fetchall returns empty → all DDL columns are "missing"
        failures = _auto_add_missing_columns(cur, "test_table", DDL_MINIMAL)
        # name, value, extra, created_at → 4 ALTER attempts all raise
        self.assertEqual(4, failures)


if __name__ == "__main__":
    unittest.main()
