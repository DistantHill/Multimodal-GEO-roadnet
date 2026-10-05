"""Detect owns scratch relations until the result takes ownership."""
from unittest.mock import Mock, patch

import pytest

from .config_factory import intersect_run_config, runtime_config
from .detect_columns_helpers import CONTEXT
from track.v2.intersections.detect_intersections import _detect_intersections_db


MODULE = "track.v2.intersections.detect_intersections"


def run_detect(tmp_path, *, cross=True):
    reader = Mock()
    reader.extent.return_value = (0.0, 0.0, 1.0, 1.0)
    return _detect_intersections_db(
        object(), intersect_run_config(use_cross_track_candidates=cross), CONTEXT,
        runtime=runtime_config(), data_root=tmp_path, refine_reader=reader,
    )


def test_disabled_cross_track_does_not_allocate_unused_relation(tmp_path):
    with (
        patch(f"{MODULE}.TurnEventRelation") as turns,
        patch(f"{MODULE}.CrossCandidateRelation") as crosses,
        patch(f"{MODULE}._run_columnar_db_detect") as compute,
    ):
        assert run_detect(tmp_path, cross=False) is compute.return_value
    crosses.assert_not_called()
    assert compute.call_args.kwargs["candidate_store"] is None
    turns.return_value.close.assert_not_called()


def test_candidate_constructor_failure_closes_existing_turn_relation(tmp_path):
    with (
        patch(f"{MODULE}.TurnEventRelation") as turns,
        patch(f"{MODULE}.CrossCandidateRelation", side_effect=RuntimeError("open failed")),
        pytest.raises(RuntimeError, match="open failed"),
    ):
        run_detect(tmp_path)
    turns.return_value.close.assert_called_once_with()


def test_candidate_constructor_failure_removes_turn_scratch_directory(tmp_path):
    with (
        patch(f"{MODULE}.CrossCandidateRelation", side_effect=RuntimeError("open failed")),
        pytest.raises(RuntimeError, match="open failed"),
    ):
        run_detect(tmp_path)
    assert list((tmp_path / "duckdb" / "turn-event-relations").iterdir()) == []


def test_detect_failure_closes_both_relations_even_if_one_close_fails(tmp_path):
    with (
        patch(f"{MODULE}.TurnEventRelation") as turns,
        patch(f"{MODULE}.CrossCandidateRelation") as crosses,
        patch(f"{MODULE}._run_columnar_db_detect", side_effect=ValueError("compute failed")),
    ):
        turns.return_value.close.side_effect = RuntimeError("close failed")
        with pytest.raises(RuntimeError, match="close failed"):
            run_detect(tmp_path)
    turns.return_value.close.assert_called_once_with()
    crosses.return_value.close.assert_called_once_with()


def test_success_transfers_both_relations_to_result(tmp_path):
    with (
        patch(f"{MODULE}.TurnEventRelation") as turns,
        patch(f"{MODULE}.CrossCandidateRelation") as crosses,
        patch(f"{MODULE}._run_columnar_db_detect") as compute,
    ):
        assert run_detect(tmp_path) is compute.return_value
    assert compute.call_args.kwargs["event_store"] is turns.return_value
    assert compute.call_args.kwargs["candidate_store"] is crosses.return_value
    turns.return_value.close.assert_not_called()
    crosses.return_value.close.assert_not_called()
