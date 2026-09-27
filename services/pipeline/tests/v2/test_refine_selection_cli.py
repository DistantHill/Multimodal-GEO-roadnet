from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from track.v2.cli.refine_selection import (
    _selection_from_row,
    format_refine_history,
    latest_completed_refine,
)
from track.v2.cli.run_pipeline import _bool_arg, _ask_reuse
from track.v2.cli import run_pipeline as cli
from track.v2.config import PostgresConfig


REFINE_ID = "11111111-1111-1111-1111-111111111111"
STARTED = datetime(2026, 9, 26, tzinfo=timezone.utc)
FINISHED = datetime(2026, 9, 26, 0, 1, tzinfo=timezone.utc)
PROJECTION = {
    "coordinate_semantics_version": 2,
    "enforce_area_of_use": True,
    "max_scale_error": 0.01,
}
ROW = (
    REFINE_ID, "public.day01", "completed", STARTED, FINISHED,
    {"semantic_config": {"parser": {"fields": {"timestamp": "ts"}},
                         "refine": {"max_gap_s": 30}}},
    4326, 32650, PROJECTION,
)
PG = PostgresConfig(
    host="localhost", port=5432, database="roadnet", user="test", password="",
    connect_timeout=1, sslmode=None, keepalives=False, keepalives_idle=0,
    keepalives_interval=0, keepalives_count=0, tcp_user_timeout=0,
)


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_refine_resume_last_parses_explicit_boolean(value, expected):
    assert _bool_arg(value) is expected


@pytest.mark.parametrize("value", ["yes", "1", "", "true false"])
def test_refine_resume_last_rejects_non_boolean(value):
    with pytest.raises(Exception, match="expected true or false"):
        _bool_arg(value)


@pytest.mark.parametrize(("answer", "expected"), [
    ("", True), (" y ", True), ("YES", True), (" n ", False), ("NO", False),
])
def test_interactive_prompt_accepts_yes_no_and_default(answer, expected):
    with (
        patch("track.v2.cli.run_pipeline.sys.stdin.isatty", return_value=True),
        patch("track.v2.cli.run_pipeline.sys.stdout.isatty", return_value=True),
        patch("builtins.input", return_value=answer),
    ):
        assert _ask_reuse(object()) is expected


def test_interactive_prompt_retries_unrecognized_answer():
    with (
        patch("track.v2.cli.run_pipeline.sys.stdin.isatty", return_value=True),
        patch("track.v2.cli.run_pipeline.sys.stdout.isatty", return_value=True),
        patch("builtins.input", side_effect=["maybe", "no"]),
        patch("builtins.print") as print_mock,
    ):
        assert _ask_reuse(object()) is False
    print_mock.assert_called_once()


def test_interactive_prompt_refuses_non_tty_and_propagates_eof():
    with (
        patch("track.v2.cli.run_pipeline.sys.stdin.isatty", return_value=False),
        pytest.raises(RuntimeError, match="显式传 --refine-resume-last true"),
    ):
        _ask_reuse(object())
    with (
        patch("track.v2.cli.run_pipeline.sys.stdin.isatty", return_value=True),
        patch("track.v2.cli.run_pipeline.sys.stdout.isatty", return_value=True),
        patch("builtins.input", side_effect=EOFError),
        pytest.raises(EOFError),
    ):
        _ask_reuse(object())


def test_latest_candidate_queries_exact_source_and_metadata_only():
    cursor = MagicMock()
    cursor.fetchone.return_value = ROW
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connect_context = MagicMock()
    connect_context.__enter__.return_value = connection
    with patch("track.v2.cli.refine_selection.connect", return_value=connect_context):
        selection = latest_completed_refine(PG, "public.day01")

    assert selection is not None
    assert selection.id == REFINE_ID
    assert selection.started_at == STARTED
    sql, params = cursor.execute.call_args.args
    assert params == ("public.day01",)
    assert "r.status = 'completed'" in sql
    assert "ORDER BY r.started_at DESC, r.id DESC LIMIT 1" in sql
    assert "kept_trajectory_points" not in sql
    assert "injected_points" not in sql
    assert "excluded_points" not in sql


def test_history_prints_historical_parameters_projection_and_current_downstream_scope():
    history = format_refine_history(_selection_from_row(ROW))
    assert REFINE_ID in history
    assert "public.day01" in history
    assert '"timestamp": "ts"' in history
    assert '"max_gap_s": 30' in history
    assert "source_srid: 4326" in history
    assert "processing_srid: 32650" in history
    assert "projection_config" in history
    assert "后续 Detect、Clip、Fitting、Network 使用当前启动配置" in history
    assert "整份" not in history


def test_history_marks_missing_parser_or_refine_parameters():
    row = (*ROW[:5], {}, *ROW[6:])
    history = format_refine_history(_selection_from_row(row))
    assert history.count("历史记录未保存") == 2


def test_invalid_selected_history_reports_its_uuid():
    invalid = (*ROW[:8], {})
    with pytest.raises(ValueError, match=f"Refine Run {REFINE_ID}: invalid stored history"):
        _selection_from_row(invalid)


def _prepare_main(monkeypatch, capsys, *, candidates=None, explicit_history=None):
    config = SimpleNamespace(
        postgres=PG,
        logging=SimpleNamespace(directory=Path("/tmp")),
    )
    history = SimpleNamespace(id=REFINE_ID)
    load = MagicMock(return_value=config)
    latest = MagicMock(return_value=candidates)
    explicit = MagicMock(return_value=explicit_history or history)
    run = MagicMock(return_value={})
    monkeypatch.setattr(cli, "load_startup_config", load)
    monkeypatch.setattr(cli, "latest_completed_refine", latest)
    monkeypatch.setattr(cli, "get_refine_selection", explicit)
    monkeypatch.setattr(cli, "format_refine_history", lambda _history: "HISTORY")
    monkeypatch.setattr(cli, "init_logger", lambda _directory: "/tmp/pipeline.log")
    monkeypatch.setattr(cli, "shutdown_logger", lambda: None)
    monkeypatch.setattr(cli, "run_pipeline", run)
    return SimpleNamespace(
        config=config, history=history, load=load, latest=latest,
        explicit=explicit, run=run, output=capsys,
    )


@pytest.mark.parametrize("extra", [[], ["--refine-resume-last", "true"]])
def test_main_source_without_candidate_runs_new_refine(monkeypatch, capsys, extra):
    state = _prepare_main(monkeypatch, capsys)
    monkeypatch.setattr("sys.argv", ["pipeline", "--source-table", "public.day01", *extra])
    assert cli.main() == 0
    state.latest.assert_called_once_with(PG, "public.day01")
    state.run.assert_called_once()
    assert state.run.call_args.kwargs["source_table"] == "public.day01"
    assert state.run.call_args.kwargs["refine_id"] is None


def test_main_false_skips_candidate_lookup_and_starts_new_refine(monkeypatch, capsys):
    state = _prepare_main(monkeypatch, capsys)
    monkeypatch.setattr(
        "sys.argv", ["pipeline", "--source-table", "public.day01",
                     "--refine-resume-last", "false"],
    )
    assert cli.main() == 0
    state.latest.assert_not_called()
    assert state.run.call_args.kwargs["source_table"] == "public.day01"
    assert state.run.call_args.kwargs["refine_id"] is None


def test_main_true_candidate_reuses_without_prompt(monkeypatch, capsys):
    candidate = SimpleNamespace(id=REFINE_ID)
    state = _prepare_main(monkeypatch, capsys, candidates=candidate)
    monkeypatch.setattr(
        "sys.argv", ["pipeline", "--source-table", "public.day01",
                     "--refine-resume-last", "true"],
    )
    with patch("track.v2.cli.run_pipeline._ask_reuse") as prompt:
        assert cli.main() == 0
    prompt.assert_not_called()
    assert state.run.call_args.kwargs == {
        "source_table": None, "refine_id": candidate.id,
    }


@pytest.mark.parametrize(("answer", "expected_source", "expected_refine"), [
    (True, None, REFINE_ID), (False, "public.day01", None),
])
def test_main_prompt_choice_routes_to_reuse_or_new(
    monkeypatch, capsys, answer, expected_source, expected_refine,
):
    candidate = SimpleNamespace(id=REFINE_ID)
    state = _prepare_main(monkeypatch, capsys, candidates=candidate)
    monkeypatch.setattr("sys.argv", ["pipeline", "--source-table", "public.day01"])
    with patch("track.v2.cli.run_pipeline._ask_reuse", return_value=answer) as prompt:
        assert cli.main() == 0
    prompt.assert_called_once_with(candidate)
    assert state.run.call_args.kwargs == {
        "source_table": expected_source, "refine_id": expected_refine,
    }
    if answer:
        assert f"已选择复用 Refine Run {REFINE_ID}" in capsys.readouterr().out


@pytest.mark.parametrize("failure", [
    RuntimeError(
        "发现已完成 Refine，但当前没有交互终端；请显式传 "
        "--refine-resume-last true 自动复用，或 false 新建"
    ),
    EOFError(),
])
def test_main_prompt_failures_exit_without_running_and_explain_choices(
    monkeypatch, capsys, failure,
):
    candidate = SimpleNamespace(id=REFINE_ID)
    state = _prepare_main(monkeypatch, capsys, candidates=candidate)
    monkeypatch.setattr("sys.argv", ["pipeline", "--source-table", "public.day01"])
    with patch("track.v2.cli.run_pipeline._ask_reuse", side_effect=failure):
        assert cli.main() == 2
    state.run.assert_not_called()
    output = capsys.readouterr().err
    assert "--refine-resume-last true" in output
    assert "false" in output


def test_main_explicit_uuid_skips_latest_lookup_and_prompt(monkeypatch, capsys):
    state = _prepare_main(monkeypatch, capsys)
    monkeypatch.setattr("sys.argv", ["pipeline", "--refine-id", REFINE_ID])
    with patch("track.v2.cli.run_pipeline._ask_reuse") as prompt:
        assert cli.main() == 0
    state.latest.assert_not_called()
    state.explicit.assert_called_once_with(PG, REFINE_ID)
    prompt.assert_not_called()
    assert state.run.call_args.kwargs == {"source_table": None, "refine_id": REFINE_ID}


def test_main_rejects_uuid_with_bool_before_loading_config(monkeypatch, capsys):
    state = _prepare_main(monkeypatch, capsys)
    monkeypatch.setattr(
        "sys.argv", ["pipeline", "--refine-id", REFINE_ID,
                     "--refine-resume-last", "false"],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    state.load.assert_not_called()
    state.run.assert_not_called()


def test_main_database_selection_failure_does_not_start_pipeline(monkeypatch, capsys):
    state = _prepare_main(monkeypatch, capsys)
    state.latest.side_effect = RuntimeError("database unavailable")
    monkeypatch.setattr("sys.argv", ["pipeline", "--source-table", "public.day01"])
    assert cli.main() == 2
    state.run.assert_not_called()
    assert "database unavailable" in capsys.readouterr().err


def test_main_reuse_failure_reports_uuid_without_retrying_older_run(monkeypatch, capsys):
    candidate = SimpleNamespace(id=REFINE_ID)
    state = _prepare_main(monkeypatch, capsys, candidates=candidate)
    state.run.side_effect = RuntimeError("artifact missing")
    monkeypatch.setattr(
        "sys.argv", ["pipeline", "--source-table", "public.day01",
                     "--refine-resume-last", "true"],
    )
    assert cli.main() == 2
    assert state.run.call_count == 1
    assert REFINE_ID in capsys.readouterr().out
