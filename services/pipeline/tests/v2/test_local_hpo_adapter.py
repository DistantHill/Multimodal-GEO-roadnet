"""Lightweight local Detect faces, atomic archives and interruption recovery."""
from contextlib import contextmanager
import copy
import json
from types import SimpleNamespace as NS

import pytest
from shapely.geometry import box, mapping

from track.v2.experiments.intersection_hpo import local_adapter as adapter
from track.v2.experiments.intersection_hpo.local_search import NumericalTrialError
from track.v2.intersections.candidate_geometry import CandidateGeometryNumericalError
from track.v2.models import IntersectReport


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = {"records": {}, "metrics": {}, "commits": 0, "detect_calls": 0,
             "closed": 0, "sql": [], "fail_evaluation": False}
    region = mapping(box(0, 0, 10, 10))
    geometry = mapping(box(1, 1, 3, 3))
    config = NS(postgres=NS(database="roadnet"), runtime=NS(),
                paths=NS(data_root=tmp_path), detect=NS(),
                snapshot_parameters=lambda: {"detect": {"candidates": ["turn_threshold"]}})
    workspace = NS(configs={"test": config}, archive_settings={"database": "roadnet_eval"},
                   case=NS(name="local", source_table="trajectory_points",
                           snapshot=lambda: {"name": "local", "source_table": "trajectory_points",
                                             "geometry": region, "time_start": None, "time_end": None}))
    inputs = {"sha256": "frozen-inputs", "levels": {
        name: {"evaluation_region": region, "computation_region": region,
               "truth": {"type": "FeatureCollection", "features": []}} for name in ("L1", "L2", "L3")}}
    caches = {"cache_key": "frozen-cache", "levels": {
        name: {"descriptor": NS(refine_id="parent"), "point_count": 12} for name in ("L1", "L2", "L3")}}

    class Connection:
        def __init__(self):
            self.records, self.metrics = copy.deepcopy(state["records"]), copy.deepcopy(state["metrics"])
            self.depth = 0
        @contextmanager
        def transaction(self):
            records, metrics = copy.deepcopy(self.records), copy.deepcopy(self.metrics)
            self.depth += 1
            try:
                yield self
            except BaseException:
                self.records, self.metrics = records, metrics
                raise
            finally:
                self.depth -= 1
        @contextmanager
        def cursor(self):
            connection = self
            class Cursor:
                def execute(self, sql, parameters):
                    assert sql == adapter.FINAL_FACE_SQL
                    state["sql"].append(parameters)
                def fetchone(self):
                    return (geometry,)
            yield Cursor()

    @contextmanager
    def archive_connection(settings):
        assert settings["database"] == "roadnet_eval"
        connection = Connection()
        yield connection
        state["records"], state["metrics"] = connection.records, connection.metrics
        state["commits"] += 1

    class Archive:
        def __init__(self, connection):
            self.connection = connection
        def get_experiment(self, identifier):
            record = self.connection.records[identifier]
            original = {"rules": state.get("original_rules", {**adapter.RULES, "region_policy": "centroid_covers"}),
                        "metrics": self.connection.metrics.get(identifier),
                        "evaluation_id": "evaluation-" + identifier}
            evaluations = ([state["later_evaluation"]] if state.get("later_evaluation") else [])
            if original["metrics"] is not None:
                evaluations.append(original)
            return {**record, "metrics": self.connection.metrics.get(identifier),
                    "latest_evaluation_id": "evaluation-" + identifier,
                    "evaluations": evaluations}
        def save_experiment(self, record, predictions):
            assert self.connection.depth >= 1
            assert record["resolved_points_persisted"] is False
            assert record["detect_id"] is None
            assert record["case_snapshot"]["geometry"] == json.loads(json.dumps(region))
            assert json.loads(json.dumps(record)) == record
            self.connection.records[record["id"]] = copy.deepcopy(record)
        def append_evaluation(self, identifier, truth, rules):
            assert self.connection.depth >= 1
            assert rules == {"mode": "center_distance", "distance_m": 30.}
            if state["fail_evaluation"]:
                raise OSError("archive unavailable")
            metrics = {"tp": 1, "fp": 0, "fn": 0, "precision": 1., "recall": 1., "f1": 1.}
            self.connection.metrics[identifier] = metrics
            return {"metrics": metrics, "evaluation_id": "evaluation-" + identifier}

    @contextmanager
    def source_connection(settings):
        yield Connection()

    class Reader:
        def __init__(self, cache, runtime, *, data_root):
            assert cache["descriptor"].refine_id == "parent"
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass

    def detect(**kwargs):
        state["detect_calls"] += 1
        assert kwargs["context"].refine_id == "parent"
        assert kwargs["refine_reader"].__class__ is Reader
        return NS(clusters=[NS(cluster_calculate_index=0, radius_m=7., centroid_x=1., centroid_y=2.)],
                  topologies=[NS(cluster_calculate_index=0, is_complete=True, clip_radius_m=12., observation_count=3)],
                  spatial_context=NS(processing_srid=32649),
                  report=IntersectReport(1, 6, 4, 0, 1, 1, 0, 1),
                  close=lambda: state.update(closed=state["closed"] + 1))

    from track.v2.experiments.intersection_hpo import local_refine
    monkeypatch.setattr(local_refine, "LocalRefineReader", Reader)
    monkeypatch.setattr(adapter, "threshold_config", lambda configured, threshold: configured)
    monkeypatch.setattr(adapter, "connect_archive", archive_connection)
    monkeypatch.setattr(adapter, "ArchiveStore", Archive)
    monkeypatch.setattr(adapter, "connect", source_connection)
    monkeypatch.setattr(adapter, "detect_intersections", detect)
    monkeypatch.setattr(adapter, "code_revision", lambda: "fixture-commit:source-sha256:fixture-source")
    evaluator = adapter.LocalEvaluator(workspace, "test", "parent", inputs, caches, tmp_path,
                                       {"inputs_hash": "frozen-inputs",
                                        "source_revision": "fixture-commit:source-sha256:fixture-source"})
    return evaluator, state


def test_success_is_atomic_and_recoverable_without_detect_or_point_writes(harness):
    evaluator, state = harness
    result = evaluator(45., "L1", "call-one")
    assert result["metrics"]["f1"] == 1.
    assert len(state["records"]) == 1
    assert state["closed"] == 1
    archived = state["records"][result["experiment_id"]]
    assert archived["commit"] == "fixture-commit"
    assert archived["stats"]["source_point_count"] == 12
    assert archived["stats"]["candidate_point_count"] == 4
    assert archived["stats"]["intersection_count"] == 1
    assert archived["stats"]["elapsed_s"] >= 0
    assert state["sql"] == [(1., 2., 32649, 12.)]
    assert evaluator.lookup("call-one") == result
    assert evaluator(45., "L1", "call-one") == result
    assert state["detect_calls"] == 1
    assert evaluator.lookup("missing-call") is None


def test_failed_later_archive_does_not_rollback_previous_call(harness):
    evaluator, state = harness
    first = evaluator(45., "L1", "call-one")
    state["fail_evaluation"] = True
    with pytest.raises(OSError, match="archive unavailable"):
        evaluator(60., "L1", "call-two")
    assert list(state["records"]) == [first["experiment_id"]]
    assert evaluator.lookup("call-one") == first
    assert evaluator.lookup("call-two") is None
    assert state["closed"] == 2


def test_post_commit_artifact_failure_can_recover_committed_evaluation(harness, monkeypatch):
    evaluator, state = harness
    original = adapter.write_manifest
    def write(path, value):
        if path.name == "result.json":
            raise OSError("artifact disk full")
        original(path, value)
    monkeypatch.setattr(adapter, "write_manifest", write)
    with pytest.raises(OSError, match="artifact disk full"):
        evaluator(45., "L1", "call-one")
    assert len(state["records"]) == 1
    recovered = evaluator.lookup("call-one")
    assert recovered["metrics"]["f1"] == 1.
    assert evaluator(45., "L1", "call-one") == recovered
    assert state["detect_calls"] == 1


@pytest.mark.parametrize("error, expected", [
    (CandidateGeometryNumericalError("not certified", point_count=3), NumericalTrialError),
    (ValueError("broken input"), ValueError),
    (OSError("database disconnected"), OSError),
])
def test_only_explicit_geometry_failure_maps_to_numerical_trial(harness, monkeypatch, error, expected):
    evaluator, state = harness
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(adapter, "detect_intersections", fail)
    with pytest.raises(expected):
        evaluator(45., "L1", "call-one")
    assert not state["records"]


def test_original_full_faces_on_boundary_are_kept_without_clipping():
    from contextlib import nullcontext
    geometries = [mapping(box(-1, 1, 1, 3)), mapping(box(-2, 1, .5, 3))]
    calls = []
    class Cursor:
        def execute(self, query, parameters):
            assert query == adapter.FINAL_FACE_SQL
            calls.append(parameters)
        def fetchone(self):
            return (geometries[len(calls)-1],)
    connection = NS(cursor=lambda: nullcontext(Cursor()))
    result = NS(clusters=[NS(cluster_calculate_index=i, centroid_x=i, centroid_y=0, radius_m=7.) for i in range(3)],
                topologies=[NS(cluster_calculate_index=i, is_complete=i != 2,
                               clip_radius_m=0., observation_count=2) for i in range(3)],
                spatial_context=NS(processing_srid=32649))
    predictions = adapter.final_faces(connection, result, mapping(box(0, 0, 10, 10)))
    assert len(predictions["features"]) == 1
    assert predictions["features"][0]["geometry"] == json.loads(json.dumps(geometries[0]))
    assert len(calls) == 2
    assert all(parameters[-1] == 7. for parameters in calls)


def test_reused_call_parameter_or_level_mismatch_is_rejected(harness):
    evaluator, _ = harness
    evaluator(45., "L1", "call-one")
    with pytest.raises(ValueError, match="different parameter or level"):
        evaluator(60., "L1", "call-one")
    with pytest.raises(ValueError, match="different parameter or level"):
        evaluator(45., "L2", "call-one")


def test_changed_source_revision_blocks_new_calls_but_allows_committed_recovery(harness, monkeypatch):
    evaluator, state = harness
    accepted = evaluator(45., "L1", "call-one")
    monkeypatch.setattr(adapter, "code_revision", lambda: "different-source")
    assert evaluator(45., "L1", "call-one") == accepted
    with pytest.raises(ValueError, match="source revision changed"):
        evaluator(60., "L1", "call-two")
    assert state["detect_calls"] == 1


def test_later_archive_reevaluation_does_not_change_recovered_search_objective(harness):
    evaluator, state = harness
    accepted = evaluator(45., "L1", "call-one")
    state["later_evaluation"] = {
        "rules": {"mode": "iou", "iou_threshold": .99, "region_policy": "centroid_covers"},
        "metrics": {"tp": 0, "fp": 1, "fn": 1, "precision": 0., "recall": 0., "f1": 0.},
        "evaluation_id": "later-human-evaluation",
    }
    assert evaluator.lookup("call-one") == accepted
    assert evaluator(45., "L1", "call-one") == accepted
    assert state["detect_calls"] == 1


@pytest.mark.parametrize("rules", [
    {"mode": "iou", "iou_threshold": .70, "region_policy": "centroid_covers"},
    {"mode": "both", "distance_m": 30., "iou_threshold": .70, "region_policy": "centroid_covers"},
    {**adapter.RULES, "distance_m": 31., "region_policy": "centroid_covers"},
    {**adapter.RULES, "region_policy": "all_features"},
])
def test_original_archive_rules_and_region_policy_must_match_frozen_objective(harness, rules):
    evaluator, state = harness
    evaluator(45., "L1", "call-one")
    state["original_rules"] = rules
    with pytest.raises(ValueError, match="original local evaluation rules or region policy"):
        evaluator.lookup("call-one")
