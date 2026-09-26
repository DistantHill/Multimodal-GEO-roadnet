from __future__ import annotations

import pytest

from track.v2.db.migrations.refine_completion_v6 import migrate_completion_v6


class _Cursor:
    def __init__(self, version: int, fail_on: str | None = None) -> None:
        self.version = version
        self.fail_on = fail_on
        self.calls: list[tuple[str, object]] = []

    def execute(self, statement: str, params: object = None) -> None:
        normalized = " ".join(statement.split())
        self.calls.append((normalized, params))
        if self.fail_on and self.fail_on in normalized:
            raise RuntimeError("rollback probe")

    def fetchone(self) -> tuple[int]:
        return (self.version,)


def test_v5_migration_installs_guard_before_publishing_v6() -> None:
    cur = _Cursor(5)
    migrate_completion_v6(cur)
    sql = [statement for statement, _params in cur.calls]

    assert "CREATE OR REPLACE FUNCTION validate_refine_artifact_completion" in sql[2]
    assert "CREATE CONSTRAINT TRIGGER refine_artifact_completion_check" in sql[3]
    assert sql[-1].startswith("UPDATE pipeline_schema_versions SET version")
    assert cur.calls[-1][1] == (6, "track.v2")


def test_v6_migration_is_idempotent() -> None:
    cur = _Cursor(6)
    migrate_completion_v6(cur)
    assert len(cur.calls) == 2


@pytest.mark.parametrize("version", [2, 3, 4, 7])
def test_completion_migration_rejects_non_v5_schema(version: int) -> None:
    with pytest.raises(RuntimeError, match="existing schema v5"):
        migrate_completion_v6(_Cursor(version))


def test_failure_does_not_publish_v6_inside_caller_transaction() -> None:
    cur = _Cursor(5, fail_on="CREATE CONSTRAINT TRIGGER")
    with pytest.raises(RuntimeError, match="rollback probe"):
        migrate_completion_v6(cur)
    assert not any(
        statement.startswith("UPDATE pipeline_schema_versions SET version")
        for statement, _params in cur.calls
    )
