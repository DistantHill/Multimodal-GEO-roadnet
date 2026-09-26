from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import numpy as np

from track.v2.db.pipeline_store import (
    RefineBatchResult,
    DetectPersistenceResult,
    PostgresPipelineStore,
)
from track.v2.models import ExcludedBatch, RefinementSubtrack, SpatialContext



CONTEXT = SpatialContext(
    source_srid=4326,
    processing_srid=32650,
    projection_config={
        "coordinate_semantics_version": 2,
        "enforce_area_of_use": True,
        "max_scale_error": 0.01,
    },
)


class _Connection:
    def __init__(self) -> None:
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


def _track(vehicle: str, subtrack: int, keys: list[int], *, injected: bool = False) -> RefinementSubtrack:
    n = len(keys)
    kind = np.full(n, "p")
    if injected:
        kind[-1] = "i"
    return RefinementSubtrack(
        vehicle, subtrack, "source", np.zeros((n,2)), np.tile([116.,39.], (n,1)),
        np.asarray(keys, dtype=np.float64), np.zeros(n), np.full(n,10.), kind,
        np.asarray(keys,dtype=np.int64), kind == "p", np.zeros(n,dtype=np.int64),
        np.zeros(n,dtype=bool), np.where(kind == "i", "heading_orthogonal", ""),
        np.where(kind == "i", 0, -1).astype(np.int64),
    )


def test_transaction_methods_are_transparent() -> None:
    connection = _Connection()
    store = PostgresPipelineStore(connection)
    assert store.connection is connection
    store.commit()
    store.rollback()
    assert connection.events == ["commit", "rollback"]


def test_refine_lifecycle_uses_source_table_and_fingerprint() -> None:
    connection = object()
    store = PostgresPipelineStore(connection)
    with (
        patch("track.v2.db.pipeline_store.create_refine_run", return_value="refine") as start,
        patch("track.v2.db.pipeline_store.finish_refine_run") as finish,
    ):
        assert store.start_refine_run(
            source_table="public.day01",
            parameters={},
            spatial_context=CONTEXT,
        ) == "refine"
        fingerprint = {"version": 1, "row_count": 2, "sha256": "a" * 64}
        store.finish_refine("refine", status="completed", source_fingerprint=fingerprint)

    start.assert_called_once_with(
        connection,
        source_table="public.day01",
        parameters={},
        spatial_context=CONTEXT,
    )
    finish.assert_called_once_with(
        connection,
        "refine",
        status="completed",
        source_fingerprint=fingerprint,
    )


def test_refine_artifact_is_registered_before_run_completion_without_commit() -> None:
    connection = MagicMock()
    descriptor = SimpleNamespace(stage="refine", refine_id="refine")
    fingerprint = {"version": 1, "row_count": 2, "sha256": "a" * 64}
    calls: list[str] = []
    store = PostgresPipelineStore(connection)
    with (
        patch(
            "track.v2.db.pipeline_store.insert_present",
            side_effect=lambda *args: calls.append("metadata+present"),
        ),
        patch(
            "track.v2.db.pipeline_store.finish_refine_run",
            side_effect=lambda *args, **kwargs: calls.append("completed"),
        ),
    ):
        store.complete_refine_with_artifact("refine", descriptor, fingerprint)

    assert calls == ["metadata+present", "completed"]
    connection.commit.assert_not_called()


def test_detect_artifact_is_registered_before_run_completion_without_commit() -> None:
    connection = MagicMock()
    descriptor = SimpleNamespace(stage="detect", detect_id="detect")
    calls: list[str] = []
    store = PostgresPipelineStore(connection)
    with (
        patch("track.v2.db.pipeline_store.insert_present",
              side_effect=lambda *args: calls.append("metadata+present")),
        patch("track.v2.db.pipeline_store.finish_detect_run",
              side_effect=lambda *args, **kwargs: calls.append("completed")),
    ):
        store.complete_detect_with_artifact("detect", descriptor)

    assert calls == ["metadata+present", "completed"]
    connection.commit.assert_not_called()


@pytest.mark.parametrize(
    "descriptor",
    [SimpleNamespace(stage="refine", detect_id="detect"),
     SimpleNamespace(stage="detect", detect_id="other")],
)
def test_detect_artifact_completion_rejects_wrong_stage_or_owner(descriptor) -> None:
    with pytest.raises(ValueError, match="does not belong"):
        PostgresPipelineStore(MagicMock()).complete_detect_with_artifact("detect", descriptor)


def test_refine_batch_assigns_globally_increasing_subtrack_ids() -> None:
    store = PostgresPipelineStore(object())
    excluded = [ExcludedBatch("source", np.array([9],dtype=np.int64), np.array(["stationary"]), np.array([[116.,39.]]))]
    subtracks = [
        _track("v1",99,[1,2],injected=True),
        _track("v2",1,[3]),
    ]
    calls: list[str] = []
    with (
        patch(
            "track.v2.db.pipeline_store.fetch_refine_run",
            return_value=SimpleNamespace(status="running", spatial_context=CONTEXT),
        ),
        patch(
            "track.v2.db.pipeline_store.insert_excluded_points",
            side_effect=lambda *args, **kwargs: calls.append("excluded"),
        ),
        patch(
            "track.v2.db.pipeline_store.insert_kept_trajectory_points",
            side_effect=lambda *args, **kwargs: calls.append("kept") or 2,
        ) as kept,
        patch(
            "track.v2.db.pipeline_store.insert_injected_points",
            side_effect=lambda *args, **kwargs: calls.append("injected"),
        ) as injected,
    ):
        result = store.persist_refine_batch(
            "refine",
            excluded,
            subtracks,
            3,
            spatial_context=CONTEXT,
            batch_size=17,
        )

    assert result == RefineBatchResult(next_subtrack_index=5, kept_count=2)
    assert calls == ["excluded", "kept", "injected"]
    assigned = kept.call_args.args[1]
    assert [subtrack.subtrack_index for subtrack in assigned] == [3, 4]
    assert injected.call_args.args[1] is assigned
    assert excluded == []
    assert subtracks == []


def test_refine_batch_does_not_clear_pending_data_after_failed_write() -> None:
    store = PostgresPipelineStore(object())
    subtracks = [_track("v1",1,[1])]
    with (
        patch(
            "track.v2.db.pipeline_store.fetch_refine_run",
            return_value=SimpleNamespace(status="running", spatial_context=CONTEXT),
        ),
        patch(
            "track.v2.db.pipeline_store.insert_kept_trajectory_points",
            side_effect=ValueError("write failed"),
        ),
    ):
        with pytest.raises(ValueError, match="write failed"):
                store.persist_refine_batch(
                    "refine", [], subtracks, 1, spatial_context=CONTEXT,
                    batch_size=runtime_config().db_insert_batch,
                )
    assert len(subtracks) == 1


def test_detect_persistence_delegates_to_one_atomic_writer() -> None:
    connection = object()
    store = PostgresPipelineStore(connection)
    result = SimpleNamespace(spatial_context=CONTEXT)
    counts = SimpleNamespace(
        resolved_point_count=10,
        tracks_cross_count=2,
        turn_event_count=3,
        candidate_intersection_count=4,
        candidate_entry_exit_point_count=5,
        intersection_count=2,
        entry_exit_point_count=6,
        resolved_sequence_elapsed_s=0.125,
    )
    with (
        patch(
            "track.v2.db.pipeline_store.fetch_detect_row",
            return_value=SimpleNamespace(status="running", spatial_context=CONTEXT),
        ),
        patch(
            "track.v2.db.pipeline_store.persist_detect_result",
            return_value=counts,
        ) as persist,
    ):
        runtime = runtime_config()
        actual = store.persist_detect(
            "detect", result, runtime=runtime, data_root="/data/runtime", batch_size=23,
        )

    assert actual == DetectPersistenceResult(10, 2, 3, 4, 5, 2, 6, 0.125)
    persist.assert_called_once_with(
        connection, "detect", result,
        runtime=runtime, data_root="/data/runtime", batch_size=23,
    )
