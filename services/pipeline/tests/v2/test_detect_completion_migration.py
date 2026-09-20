from __future__ import annotations

import pytest

from track.v2.db.detect_completion_migration import migrate_completion_v7


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


def test_v6_migration_installs_detect_guard_before_publishing_v7() -> None:
    cur = _Cursor(6)
    migrate_completion_v7(cur)
    sql = [statement for statement, _params in cur.calls]
    assert "CREATE OR REPLACE FUNCTION validate_detect_artifact_completion" in sql[2]
    assert "CREATE CONSTRAINT TRIGGER detect_artifact_completion_check" in sql[3]
    assert cur.calls[-1][1] == (7, "track.v2")


def test_v7_migration_is_idempotent() -> None:
    cur = _Cursor(7)
    migrate_completion_v7(cur)
    assert len(cur.calls) == 2


@pytest.mark.parametrize("version", [3, 4, 5, 8])
def test_detect_completion_migration_rejects_non_v6(version: int) -> None:
    with pytest.raises(RuntimeError, match="existing schema v6"):
        migrate_completion_v7(_Cursor(version))


def test_failure_does_not_publish_v7() -> None:
    cur = _Cursor(6, fail_on="CREATE CONSTRAINT TRIGGER")
    with pytest.raises(RuntimeError, match="rollback probe"):
        migrate_completion_v7(cur)
    assert not any(call[0].startswith("UPDATE pipeline_schema_versions") for call in cur.calls)
