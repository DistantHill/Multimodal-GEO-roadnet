"""Candidate plugin execution preserves instance ordering, evidence and subset semantics."""
from dataclasses import fields, replace
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pytest

from track.v2.candidate_plugins.turn_threshold import TurnThresholdParams, run
from track.v2.plugins.execution import CandidateResult, CompositeRelation, execute_candidates
from track.v2.plugins.core import CandidatePlugin
from track.v2.intersections.detect_intersections import _CandidateExecutionContext
from .config_factory import runtime_config
from .detect_columns_helpers import CONTEXT
from .writer_relation_helpers import turn_batch


def parameters(threshold, window=1):
    return TurnThresholdParams(window, threshold, 179., 1., 0.)


class Reader:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def extent(self):
        return (0., 0., 1., 1.)

    def iter_turn_candidates(self, table, params, *, batch_size):
        self.calls.append(params)
        keep = np.flatnonzero(np.abs(self.values.turn_deg) >= params.min_turn_deg)
        batch = self.values.take(keep)
        yield replace(batch, calculation_index=np.arange(len(batch), dtype=np.int64))


def context(tmp_path, reader):
    cfg = SimpleNamespace(source_table="source", db_table="source", stream_batch_size=2)
    return _CandidateExecutionContext(cfg, CONTEXT, reader, runtime=runtime_config(), data_root=tmp_path, conn=None, refine_id=None, connect_worker=None)


def concatenate(relation):
    from track.v2.candidates.detect_turn_events import concatenate_turn_batches
    return concatenate_turn_batches(list(relation.iter_batches()))


def assert_evidence(actual, expected):
    for item in fields(actual):
        if item.name != "calculation_index":
            np.testing.assert_array_equal(getattr(actual, item.name), getattr(expected, item.name))



def test_turn_subsets_extract_once_keep_strict_boundary_and_repeatable_evidence(tmp_path):
    angles = np.asarray([30., -30., 31., 60., -60., 61., 90.])
    batch = turn_batch(len(angles), turn_deg=angles, score=np.abs(angles))
    reader = Reader(batch)
    execution = context(tmp_path, reader)
    try:
        definitions = {"high": parameters(60.), "low": parameters(30.), "unused": parameters(0.)}
        results = run(execution, definitions, ("high", "low"))
        assert len(reader.calls) == 1
        assert reader.calls[0].min_turn_deg == 30.
        for name, threshold in (("high", 60.), ("low", 30.)):
            expected = batch.take(np.flatnonzero(np.abs(angles) > threshold))
            assert_evidence(concatenate(results[name].candidates), expected)
            assert_evidence(concatenate(results[name].candidates), expected)
            independent_reader = Reader(batch)
            independent = context(tmp_path, independent_reader)
            try:
                standalone = run(independent, {name: definitions[name]}, (name,))[name]
                assert_evidence(concatenate(results[name].candidates), concatenate(standalone.candidates))
            finally:
                independent.resources.close()
    finally:
        execution.resources.close()
    with pytest.raises(RuntimeError, match="closed"):
        len(results["high"].events)


def test_turn_groups_with_changed_window_and_leg_parameters_extract_independently(tmp_path):
    angles = np.asarray([45., 90.])
    reader = Reader(turn_batch(2, turn_deg=angles, score=np.abs(angles)))
    execution = context(tmp_path, reader)
    try:
        definitions = {"a": parameters(30.), "b": parameters(60.), "window": parameters(30., 2), "leg": replace(parameters(30.), min_leg_m=2.)}
        results = run(execution, definitions, tuple(definitions))
        assert len(reader.calls) == 3
        assert set(results) == set(definitions)
    finally:
        execution.resources.close()


def test_executor_requests_only_selected_instances_in_algorithm_and_instance_order():
    calls = []
    batch = turn_batch(1, score=np.asarray([90.]))
    def plugin(identity):
        def execute(ctx, params, requested):
            calls.append((identity, requested, tuple(params)))
            return {name: CandidateResult(identity, name, "turn", batch, batch, params[name]) for name in reversed(requested)}
        return CandidatePlugin(identity, TurnThresholdParams, "candidate_points", execute)
    config = SimpleNamespace(candidates=("b", "a"), candidate={"a": SimpleNamespace(params={"one": parameters(30.), "unused": parameters(60.)}, output_instances=("one",)), "b": SimpleNamespace(params={"low": parameters(30.), "high": parameters(60.)}, output_instances=("high", "low"))})
    with patch("track.v2.plugins.execution.candidate_plugins", return_value={name: plugin(name) for name in ("a", "b")}):
        results = execute_candidates(config, object())
    assert [(result.algorithm_id, result.instance_id) for result in results] == [("b", "high"), ("b", "low"), ("a", "one")]
    assert [result.evidence_offset for result in results] == [0, 1, 2]
    assert calls == [("b", ("high", "low"), ("low", "high")), ("a", ("one",), ("one", "unused"))]
    relation = CompositeRelation((result.candidates, result.evidence_offset) for result in results)
    assert len(relation) == 3
    assert concatenate(relation).calculation_index.tolist() == [0, 1, 2]
    assert concatenate(relation).refine_point_id.tolist() == [100, 100, 100]


def test_executor_does_not_continue_after_plugin_failure():
    calls = []
    def fail(*args):
        calls.append("fail")
        raise ValueError("failed candidate")
    def later(*args):
        calls.append("later")
    config = SimpleNamespace(candidates=("first", "second"), candidate={name: SimpleNamespace(params={"one": parameters(30.)}, output_instances=("one",)) for name in ("first", "second")})
    with patch("track.v2.plugins.execution.candidate_plugins", return_value={"first": CandidatePlugin("first", TurnThresholdParams, "candidate_points", fail), "second": CandidatePlugin("second", TurnThresholdParams, "candidate_points", later)}):
        with pytest.raises(ValueError, match="failed candidate"):
            execute_candidates(config, object())
    assert calls == ["fail"]


def test_independent_detect_instances_share_batch_extraction_and_close_after_batch(tmp_path):
    from track.v2.plugins.execution import DetectExecutionCache
    from track.v2.config.detect_config import CandidateAlgorithmConfig
    angles = np.asarray([30., 31., 60., 61., 90.])
    batch = turn_batch(len(angles), turn_deg=angles, score=np.abs(angles))
    reader = Reader(batch)
    reader.descriptor = SimpleNamespace(refine_id="refine")
    definitions = {"low": parameters(30.), "high": parameters(60.)}
    contexts = []
    with DetectExecutionCache({"turn_threshold": ("low", "high")}) as cache:
        for selected in ("low", "high"):
            execution = context(tmp_path, reader)
            execution.refine_id = "refine"
            contexts.append(execution)
            config = SimpleNamespace(candidates=("turn_threshold",), candidate={"turn_threshold": CandidateAlgorithmConfig((selected,), definitions)})
            results = execute_candidates(config, execution, candidate_cache=cache)
            assert len(results) == 1 and results[0].instance_id == selected
            expected = batch.take(np.flatnonzero(np.abs(angles) > definitions[selected].min_turn_deg))
            assert_evidence(concatenate(results[0].candidates), expected)
            from track.v2.intersections.detect_intersections import _cluster_from_points
            from .detect_columns_helpers import TRANSFORMER
            clusters = _cluster_from_points(((results[0].source_key, results[0].candidates),), TRANSFORMER, eps_m=1., min_samples=1)
            assert sum(len(cluster.members) for cluster in clusters) == len(expected)
            execution.resources.close()
            assert_evidence(concatenate(results[0].candidates), expected)
        assert len(reader.calls) == 1
        high = results[0]
    with pytest.raises(RuntimeError, match="closed"):
        list(high.candidates.iter_batches())
    assert not list(tmp_path.glob("duckdb/turn-event-relations/*"))


def test_batch_candidate_cache_does_not_reuse_other_time_selection(tmp_path):
    from track.v2.plugins.execution import DetectExecutionCache
    from track.v2.config.detect_config import CandidateAlgorithmConfig
    angles = np.asarray([60., 90.])
    reader = Reader(turn_batch(2, turn_deg=angles, score=np.abs(angles)))
    reader.descriptor = SimpleNamespace(refine_id="refine")
    definitions = {"one": parameters(30.)}
    with DetectExecutionCache({"turn_threshold": ("one",)}) as cache:
        for start in ("first", "second"):
            reader.time_start = start
            execution = context(tmp_path, reader)
            config = SimpleNamespace(candidates=("turn_threshold",), candidate={"turn_threshold": CandidateAlgorithmConfig(("one",), definitions)})
            execute_candidates(config, execution, candidate_cache=cache)
            execution.resources.close()
        assert len(reader.calls) == 2


def test_empty_input_preserves_selected_candidate_instance_metadata(tmp_path):
    from track.v2.intersections.detect_intersections import _run_columnar_db_detect
    from .config_factory import intersect_run_config
    reader = SimpleNamespace(extent=lambda: None)
    cfg = intersect_run_config(refine_id="refine")
    result = _run_columnar_db_detect(cfg, CONTEXT, reader, runtime=runtime_config(), data_root=tmp_path)
    try:
        assert [(item.algorithm_id, item.instance_id) for item in result.candidate_results] == [("g_star", "default"), ("cross", "default")]
        assert all(len(item.events) == len(item.candidates) == 0 for item in result.candidate_results)
        assert result.report.track_count == 0
        assert result.clusters == []
        assert not list(tmp_path.glob("duckdb/*/*"))
    finally:
        result.close()


def test_cluster_description_with_own_parameters_needs_no_algorithm_name_branch():
    from dataclasses import dataclass
    from track.v2.plugins.core import ClusterPlugin
    from track.v2.config.detect_config import ClusterAlgorithmConfig
    from track.v2.intersections.detect_intersections import _build_candidate_clusters
    from .detect_columns_helpers import TRANSFORMER
    @dataclass(frozen=True)
    class CustomParams:
        label: int
    calls = []
    def cluster(x, y, *, params, **kwargs):
        calls.append((len(x), params))
        return [params.label] * len(x)
    params = CustomParams(7)
    cfg = SimpleNamespace(cluster_algorithm="custom", cluster={"custom": ClusterAlgorithmConfig(params)})
    points = turn_batch(2)
    with patch("track.v2.intersections.detect_intersections.cluster_plugins", return_value={"custom": ClusterPlugin("custom", CustomParams, "candidate_points", cluster)}):
        clusters = _build_candidate_clusters((("custom:one", points),), cfg, TRANSFORMER)
    assert calls == [(2, params)]
    assert len(clusters) == 1
    assert clusters[0].cluster_calculate_index == 7
    assert clusters[0].members.calculation_index.tolist() == [0, 1]
