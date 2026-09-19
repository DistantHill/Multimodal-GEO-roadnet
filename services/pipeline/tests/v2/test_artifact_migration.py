from __future__ import annotations

import pytest

from track.v2.db.artifact_migration import migrate_artifacts_v5


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


def test_v4_migration_installs_registry_before_publishing_v5() -> None:
    cur = _Cursor(4)
    migrate_artifacts_v5(cur)
    sql = [statement for statement, _params in cur.calls]

    assert sql[0] == "LOCK TABLE pipeline_schema_versions IN ACCESS EXCLUSIVE MODE"
    assert any("CREATE TABLE IF NOT EXISTS pipeline_artifacts" in item for item in sql)
    assert any("CREATE TABLE IF NOT EXISTS pipeline_artifact_events" in item for item in sql)
    assert any("CREATE TRIGGER validate_pipeline_artifact_event_insert" in item for item in sql)
    assert sql[-1].startswith("UPDATE pipeline_schema_versions SET version")
    assert cur.calls[-1][1] == (5, "track.v2")


def test_v5_migration_is_idempotent() -> None:
    cur = _Cursor(5)
    migrate_artifacts_v5(cur)
    assert len(cur.calls) == 2


@pytest.mark.parametrize("version", [2, 3, 6])
def test_artifact_migration_rejects_non_v4_schema(version: int) -> None:
    with pytest.raises(RuntimeError, match="existing schema v4"):
        migrate_artifacts_v5(_Cursor(version))


def test_failure_does_not_publish_v5_inside_caller_transaction() -> None:
    cur = _Cursor(4, fail_on="CREATE TRIGGER validate_pipeline_artifact_event_insert")
    with pytest.raises(RuntimeError, match="rollback probe"):
        migrate_artifacts_v5(cur)
    assert not any(
        statement.startswith("UPDATE pipeline_schema_versions SET version")
        for statement, _params in cur.calls
    )
