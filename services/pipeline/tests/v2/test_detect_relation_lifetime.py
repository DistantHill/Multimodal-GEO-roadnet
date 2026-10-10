"""Detect owns all plugin scratch relations through persistence consumption."""
from unittest.mock import Mock, patch
from contextlib import ExitStack
import pytest
from .config_factory import intersect_run_config, runtime_config
from .detect_columns_helpers import CONTEXT
from track.v2.intersections.detect_intersections import _detect_intersections_db

MODULE = "track.v2.intersections.detect_intersections"


def run_detect(tmp_path, *, cross=True):
    reader = Mock()
    reader.extent.return_value = (0.0, 0.0, 1.0, 1.0)
    return _detect_intersections_db(object(), intersect_run_config(use_cross_track_candidates=cross, refine_id="refine"), CONTEXT, runtime=runtime_config(), data_root=tmp_path, refine_reader=reader, connect_worker=lambda: object())


def compute_empty(context, config, **kwargs):
    return ()


def test_disabled_cross_track_does_not_allocate_unused_relation(tmp_path):
    with patch(f"{MODULE}.CrossCandidateRelation") as crosses, patch(f"{MODULE}.execute_candidates", return_value=()), patch(f"{MODULE}._build_topologies_from_reader", return_value=([], [], [])):
        result = run_detect(tmp_path, cross=False)
    crosses.assert_not_called()
    result.close()


def test_candidate_constructor_failure_closes_existing_turn_relation(tmp_path):
    turn = Mock()
    def generate(config, execution):
        execution.own(turn)
        execution.run_cross(config.candidate["cross"].params["default"])
    with patch(f"{MODULE}.execute_candidates", side_effect=generate), patch(f"{MODULE}.CrossCandidateRelation", side_effect=RuntimeError("open failed")), pytest.raises(RuntimeError, match="open failed"):
        run_detect(tmp_path)
    turn.close.assert_called_once_with()


def test_candidate_constructor_failure_removes_turn_scratch_directory(tmp_path):
    from track.v2.candidates.turn_event_relation import TurnEventRelation
    def generate(config, execution):
        execution.own(TurnEventRelation(runtime_config(), tmp_path))
        execution.run_cross(config.candidate["cross"].params["default"])
    with patch(f"{MODULE}.execute_candidates", side_effect=generate), patch(f"{MODULE}.CrossCandidateRelation", side_effect=RuntimeError("open failed")), pytest.raises(RuntimeError, match="open failed"):
        run_detect(tmp_path)
    assert list((tmp_path / "duckdb" / "turn-event-relations").iterdir()) == []


def test_detect_failure_closes_all_relations_even_if_one_close_fails(tmp_path):
    turn, cross = Mock(), Mock()
    turn.close.side_effect = RuntimeError("close failed")
    def generate(config, execution):
        execution.own(turn)
        execution.own(cross)
        raise ValueError("compute failed")
    with patch(f"{MODULE}.execute_candidates", side_effect=generate), pytest.raises(RuntimeError, match="close failed"):
        run_detect(tmp_path)
    turn.close.assert_called_once_with()
    cross.close.assert_called_once_with()


def test_success_transfers_all_plugin_relations_to_result(tmp_path):
    turn, cross = Mock(), Mock()
    def generate(config, execution):
        execution.own(turn)
        execution.own(cross)
        return ()
    with patch(f"{MODULE}.execute_candidates", side_effect=generate), patch(f"{MODULE}._build_topologies_from_reader", return_value=([], [], [])):
        result = run_detect(tmp_path)
    turn.close.assert_not_called()
    cross.close.assert_not_called()
    result.close()
    result.close()
    turn.close.assert_called_once_with()
    cross.close.assert_called_once_with()
