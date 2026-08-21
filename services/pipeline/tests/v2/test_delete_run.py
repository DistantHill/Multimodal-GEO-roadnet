from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from track.v2.delete_run import (
    CLEAN_ARTIFACT_TABLES,
    DETECT_ARTIFACT_TABLES,
    DeleteImpact,
    DeleteRunService,
    TableCount,
)


CLEAN_ID = "11111111-1111-1111-1111-111111111111"
DETECT_ID = "22222222-2222-2222-2222-222222222222"


class _Cursor:
    def __init__(self, conn: "_Connection") -> None:
        self.conn = conn
        self.rows: list[tuple[object, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.conn.executions.append((" ".join(sql.split()), params))
        if sql.startswith("LOCK TABLE"):
            self.rows = []
            return
        if sql.startswith("DELETE"):
            self.rows = []
            return
        if sql.endswith("FOR UPDATE") and not sql.startswith((
            "SELECT id FROM clean_runs", "SELECT id FROM detect_runs",
        )):
            self.rows = []
            return
        self.rows = self.conn.responses.pop(0)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, responses: list[list[tuple[object, ...]]]) -> None:
        self.responses = list(responses)
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _detect_preview_responses(*, count: int = 1) -> list[list[tuple[object, ...]]]:
    return [[(DETECT_ID,)]] + [[(count,)] for _ in DETECT_ARTIFACT_TABLES]


def _clean_preview_responses(*, count: int = 1) -> list[list[tuple[object, ...]]]:
    return (
        [[(CLEAN_ID,)]]
        + [[(DETECT_ID,)]]
        + [[(count,)] for _ in CLEAN_ARTIFACT_TABLES]
        + [[(count,)] for _ in DETECT_ARTIFACT_TABLES]
    )


class DeleteRunServiceTest(unittest.TestCase):
    def test_clean_preview_has_clean_tables_and_each_child_detect_tree(self) -> None:
        responses = (
            [[(CLEAN_ID,)]]
            + [[(index,)] for index, _ in enumerate(CLEAN_ARTIFACT_TABLES, start=1)]
            + [[(DETECT_ID,)]]
            + [[(index,)] for index, _ in enumerate(DETECT_ARTIFACT_TABLES, start=10)]
        )
        service = DeleteRunService(_Connection(responses))

        impact = service.preview_clean(CLEAN_ID)

        self.assertEqual(
            [(row.table, row.count) for row in impact.own_rows],
            list(zip(CLEAN_ARTIFACT_TABLES, range(1, 4))),
        )
        self.assertEqual(impact.detects[0].target_id, DETECT_ID)
        self.assertEqual(
            [(row.table, row.count) for row in impact.detects[0].own_rows],
            list(zip(DETECT_ARTIFACT_TABLES, range(10, 21))),
        )

    def test_detect_delete_requires_y_then_exact_uuid_and_rechecks_tree(self) -> None:
        responses = _detect_preview_responses() * 3
        conn = _Connection(responses)
        output: list[str] = []
        answers = iter(("y", DETECT_ID))

        deleted = DeleteRunService(conn).confirm_and_delete_detect(
            DETECT_ID,
            input_fn=lambda _prompt: next(answers),
            output_fn=output.append,
        )

        self.assertTrue(deleted)
        self.assertEqual(conn.commits, 1)
        deletes = [sql for sql, _ in conn.executions if sql.startswith("DELETE")]
        self.assertEqual(
            deletes,
            [f"DELETE FROM {table} WHERE detect_id = %s" for table in DETECT_ARTIFACT_TABLES]
            + ["DELETE FROM detect_runs WHERE id = %s"],
        )
        self.assertFalse(any(sql.startswith("DELETE FROM trajectory_points ") for sql in deletes))
        artifact_locks = [
            sql for sql, _ in conn.executions if sql.startswith("LOCK TABLE")
        ]
        self.assertEqual(
            artifact_locks,
            [
                f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE"
                for table in DETECT_ARTIFACT_TABLES
            ],
        )
        self.assertFalse(any(
            sql.startswith("SELECT id FROM ") and "FOR UPDATE" in sql
            and not sql.startswith("SELECT id FROM detect_runs")
            for sql, _ in conn.executions
        ))
        first_final_count = max(
            index for index, (sql, _) in enumerate(conn.executions)
            if sql == "SELECT COUNT(*) FROM entry_exit_points WHERE detect_id = %s"
        )
        last_table_lock = max(
            index for index, (sql, _) in enumerate(conn.executions)
            if sql.startswith("LOCK TABLE")
        )
        first_delete = next(
            index for index, (sql, _) in enumerate(conn.executions)
            if sql.startswith("DELETE")
        )
        self.assertLess(last_table_lock, first_final_count)
        self.assertLess(last_table_lock, first_delete)

    def test_clean_delete_removes_children_then_clean_artifacts_and_registry(self) -> None:
        conn = _Connection(_clean_preview_responses() * 3)
        answers = iter(("y", CLEAN_ID))

        deleted = DeleteRunService(conn).confirm_and_delete_clean(
            CLEAN_ID,
            input_fn=lambda _prompt: next(answers),
            output_fn=lambda _line: None,
        )

        self.assertTrue(deleted)
        deletes = [sql for sql, _ in conn.executions if sql.startswith("DELETE")]
        expected_detect = [
            f"DELETE FROM {table} WHERE detect_id = %s"
            for table in DETECT_ARTIFACT_TABLES
        ] + ["DELETE FROM detect_runs WHERE id = %s"]
        expected_clean = [
            f"DELETE FROM {table} WHERE clean_id = %s"
            for table in CLEAN_ARTIFACT_TABLES
        ] + ["DELETE FROM clean_runs WHERE id = %s"]
        self.assertEqual(deletes, expected_detect + expected_clean)
        self.assertFalse(any(sql.startswith("DELETE FROM trajectory_points ") for sql in deletes))
        final_clean_lock = max(
            index for index, (sql, _) in enumerate(conn.executions)
            if sql == "LOCK TABLE tracks_cross IN SHARE ROW EXCLUSIVE MODE"
        )
        final_clean_count = max(
            index for index, (sql, _) in enumerate(conn.executions)
            if sql == "SELECT COUNT(*) FROM excluded_points WHERE clean_id = %s"
        )
        self.assertLess(final_clean_lock, final_clean_count)
        self.assertFalse(any(
            sql.startswith("SELECT id FROM ") and "FOR UPDATE" in sql
            and not sql.startswith((
                "SELECT id FROM clean_runs", "SELECT id FROM detect_runs",
            ))
            for sql, _ in conn.executions
        ))
            [
                "DELETE FROM centerline_source_clips WHERE centerline_id IN "
                "(SELECT id FROM centerlines WHERE detect_id = %s)"
            ]
            + [f"DELETE FROM {table} WHERE detect_id = %s" for table in DETECT_ARTIFACT_TABLES[1:]]
            + ["DELETE FROM detect_runs WHERE id = %s"],
        )
        self.assertFalse(any("trajectory_points" in sql for sql in deletes))

    def test_cancellation_and_wrong_uuid_do_not_delete(self) -> None:
        cancel_conn = _Connection(_detect_preview_responses())
        self.assertFalse(DeleteRunService(cancel_conn).confirm_and_delete_detect(
            DETECT_ID, input_fn=lambda _prompt: "n", output_fn=lambda _line: None,
        ))
        self.assertFalse(any(sql.startswith("DELETE") for sql, _ in cancel_conn.executions))

        wrong_conn = _Connection(_detect_preview_responses() * 2)
        answers = iter(("y", CLEAN_ID))
        self.assertFalse(DeleteRunService(wrong_conn).confirm_and_delete_detect(
            DETECT_ID, input_fn=lambda _prompt: next(answers), output_fn=lambda _line: None,
        ))
        self.assertFalse(any(sql.startswith("DELETE") for sql, _ in wrong_conn.executions))

    def test_changed_tree_after_either_confirmation_aborts(self) -> None:
        changed = _detect_preview_responses(count=1) + _detect_preview_responses(count=2)
        conn = _Connection(changed)

        with self.assertRaisesRegex(RuntimeError, "impact tree changed"):
            DeleteRunService(conn).confirm_and_delete_detect(
                DETECT_ID,
                input_fn=lambda _prompt: "y",
                output_fn=lambda _line: None,
            )

        self.assertEqual(conn.rollbacks, 1)
        self.assertFalse(any(sql.startswith("DELETE") for sql, _ in conn.executions))

        after_uuid = _detect_preview_responses() * 2 + _detect_preview_responses(count=2)
        after_uuid_conn = _Connection(after_uuid)
        answers = iter(("y", DETECT_ID))
        with self.assertRaisesRegex(RuntimeError, "impact tree changed"):
            DeleteRunService(after_uuid_conn).confirm_and_delete_detect(
                DETECT_ID,
                input_fn=lambda _prompt: next(answers),
                output_fn=lambda _line: None,
            )
        self.assertEqual(after_uuid_conn.rollbacks, 1)
        self.assertFalse(
            any(sql.startswith("DELETE") for sql, _ in after_uuid_conn.executions)
        )

    def test_missing_run_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown Detect Run"):
            DeleteRunService(_Connection([[]])).preview_detect(DETECT_ID)

    def test_delete_cli_always_shuts_down_logger(self) -> None:
        from track.v2.cli import delete_run as delete_cli

        config = SimpleNamespace(
            host="localhost", port=5432, database="track", user="postgres",
            password="", connect_timeout=5, sslmode="prefer",
        )
        with (
            patch("sys.argv", ["track.v2.cli.delete_run", "--detect-id", DETECT_ID]),
            patch.object(delete_cli, "default_postgres_config", return_value=config),
            patch.object(delete_cli, "init_logger") as init_logger,
            patch.object(delete_cli, "shutdown_logger") as shutdown_logger,
            patch.object(delete_cli, "pg_connect", side_effect=RuntimeError("offline")),
        ):
            self.assertEqual(delete_cli.main(), 2)

        init_logger.assert_called_once_with()
        shutdown_logger.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
