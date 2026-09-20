from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from track.v2.artifact_reconcile import ApplyResult, ReconcileAction, ReconcilePlan
from track.v2.cli import artifacts


NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


class Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Connection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0
        self.cur = Cursor()

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def action(kind="registered_present", *, eligible=True, protected=False):
    return ReconcileAction(
        kind, f"refine/{uuid4()}", uuid4(), "stable reason", 700_000, eligible, protected,
    )


def setup(monkeypatch, plan, *, apply_error=None):
    conn = Connection()
    calls = {"build": [], "apply": []}
    monkeypatch.setattr(artifacts, "pg_connect", lambda _config: conn)
    monkeypatch.setattr(
        artifacts, "load_pipeline_config",
        lambda path: SimpleNamespace(runtime=SimpleNamespace(artifact_gc_grace_days=7)),
    )
    monkeypatch.setattr(
        artifacts, "build_reconcile_plan",
        lambda cur, root, now, days: calls["build"].append((cur, root, now, days)) or plan,
    )

    def apply(cur, root, supplied, *, reason):
        calls["apply"].append((cur, root, supplied, reason))
        if apply_error:
            raise apply_error
        return ApplyResult(("refine/removed",), (), (), 1)

    monkeypatch.setattr(artifacts, "apply_reconcile_plan", apply)
    return conn, calls


def test_default_is_dry_run_with_no_apply_or_commit(monkeypatch, tmp_path, capsys):
    plan = ReconcilePlan(NOW, 7, (action(),))
    conn, calls = setup(monkeypatch, plan)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("PARQUET_ROOT", raising=False)

    assert artifacts.main(["reconcile"]) == 0

    assert calls["build"][0][1] == tmp_path / "parquet"
    assert calls["build"][0][3] == 7
    assert not calls["apply"] and conn.commits == 0 and conn.rollbacks == 1
    assert '"mode":"dry-run"' in capsys.readouterr().out


def test_apply_uses_override_and_commits(monkeypatch, tmp_path, capsys):
    plan = ReconcilePlan(NOW, 0, (action(),))
    conn, calls = setup(monkeypatch, plan)

    assert artifacts.main([
        "reconcile", "--apply", "--grace-days", "0", "--parquet-root", str(tmp_path),
    ]) == 0

    assert calls["build"][0][3] == 0 and calls["apply"][0][1] == tmp_path
    assert calls["apply"][0][3] == artifacts._APPLY_REASON
    assert conn.commits == 1 and conn.rollbacks == 0
    output = capsys.readouterr().out
    assert '"mode":"apply"' in output and '"appended_events":1' in output


def test_apply_empty_plan_is_successful_noop(monkeypatch, tmp_path):
    plan = ReconcilePlan(NOW, 7, ())
    conn, calls = setup(monkeypatch, plan)

    assert artifacts.main(["reconcile", "--apply", "--parquet-root", str(tmp_path)]) == 0

    assert len(calls["apply"]) == 1 and conn.commits == 1 and conn.rollbacks == 0


def test_explicit_config_supplies_grace_period(monkeypatch, tmp_path):
    plan = ReconcilePlan(NOW, 12, ())
    conn, calls = setup(monkeypatch, plan)
    seen = []
    monkeypatch.setattr(
        artifacts, "load_pipeline_config",
        lambda path: seen.append(path) or
        SimpleNamespace(runtime=SimpleNamespace(artifact_gc_grace_days=12)),
    )
    config_path = tmp_path / "pipeline.toml"

    assert artifacts.main([
        "reconcile", "--config", str(config_path), "--parquet-root", str(tmp_path),
    ]) == 0

    assert seen == [config_path] and calls["build"][0][3] == 12
    assert conn.commits == 0


@pytest.mark.parametrize("kind", ["malformed_path", "malformed_registry", "registered_missing"])
def test_apply_refuses_entire_plan_on_critical_item(monkeypatch, tmp_path, kind, capsys):
    plan = ReconcilePlan(NOW, 7, (action(), action(kind, eligible=False, protected=True)))
    conn, calls = setup(monkeypatch, plan)

    assert artifacts.main(["reconcile", "--apply", "--parquet-root", str(tmp_path)]) == 2

    assert not calls["apply"] and conn.commits == 0 and conn.rollbacks == 1
    assert "critical items" in capsys.readouterr().err


def test_error_rolls_back_without_printing_password(monkeypatch, tmp_path, capsys):
    secret = "never-print-this"
    plan = ReconcilePlan(NOW, 7, (action(),))
    conn, _calls = setup(monkeypatch, plan, apply_error=RuntimeError("apply failed"))
    monkeypatch.setattr(artifacts.getpass, "getpass", lambda _prompt: secret)

    assert artifacts.main([
        "reconcile", "--apply", "--ask-password", "--parquet-root", str(tmp_path),
    ]) == 2

    captured = capsys.readouterr()
    assert conn.commits == 0 and conn.rollbacks == 1 and conn.closed == 1
    assert "apply failed" in captured.err and secret not in captured.out + captured.err


def test_negative_grace_days_is_rejected():
    with pytest.raises(SystemExit):
        artifacts.main(["reconcile", "--grace-days", "-1"])
