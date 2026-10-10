"""Experiment-only algorithms preserve columnar identities and production defaults."""
from dataclasses import fields, replace
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from track.v2.candidates.turn_threehold import select_turn_threehold_events
from track.v2.candidates.hdbscan import run_hdbscan
from track.v2.candidates.turn_event_relation import TurnEventRelation
from track.v2.candidates.detect_cross_track_pairs import empty_cross_batch
from track.v2.errors import ConfigError
from track.v2.experiments.config import ExperimentDetectConfig, load_experiment_payload
from track.v2.intersections import detect_intersections as detect
from track.v2.config.pipeline_config_loader import load_pipeline_payload
from .config_factory import intersect_config, runtime_config, detect_context
from .detect_columns_helpers import CONTEXT, TRANSFORMER
from .writer_relation_helpers import turn_batch, _batch
from track.v2.models import CrossCandidateBatch


def experiment_config(**changes):
    values = intersect_config().as_dict()
    values.update(changes)
    return ExperimentDetectConfig(**values)


def assert_batch_equal(actual, expected):
    for field in fields(expected):
        np.testing.assert_array_equal(getattr(actual, field.name), getattr(expected, field.name))


@pytest.mark.parametrize("on_disk", [False, True])
def test_turn_threehold_strict_absolute_threshold_preserves_all_columns(tmp_path, on_disk):
    angles = np.asarray([44., 45., -45., 46., -46., 90.])
    batch = turn_batch(6, turn_deg=angles, score=np.abs(angles),
                       g_star_z_valid=np.zeros(6, dtype=np.bool_))
    cfg = experiment_config(turn_candidate_algorithm="turn_threehold", min_turn_deg=45.)
    with TurnEventRelation(runtime_config(), tmp_path, batch_size=2) as relation:
        relation.append_batch(batch)
        relation.seal()
        source = relation if on_disk else batch
        with patch.object(detect, "_compute_getis_ord_g_star", side_effect=AssertionError("G* called")):
            actual = detect._select_hotspot_events(source, cfg)
        assert_batch_equal(actual, batch.take(np.asarray([3, 4, 5])))
        assert_batch_equal(select_turn_threehold_events(source, replace(cfg, min_turn_deg=89.)),
                           batch.take(np.asarray([5])))
        assert len(source) == 6


def test_hdbscan_label_contract_empty_noise_and_repeatability():
    xs = np.asarray([0., 1., 2., 3., 1000., 1001., 1002., 1003.])
    ys = np.zeros(len(xs))
    labels = run_hdbscan(xs, ys, eps_m=10., min_samples=2)
    assert labels == [0] * 4 + [1] * 4
    assert labels == run_hdbscan(xs, ys, eps_m=10., min_samples=2)
    assert labels == detect._run_dbscan(xs, ys, eps_m=10., min_samples=2)
    assert run_hdbscan([], [], eps_m=10., min_samples=2) == []
    assert run_hdbscan([0.], [0.], eps_m=10., min_samples=2) == [-1]
    assert run_hdbscan([0., 1.], [0., 0.], eps_m=10., min_samples=3) == [-1, -1]
    assert run_hdbscan(np.append(xs, [np.nan, np.inf]), np.append(ys, [0., 0.]),
                       eps_m=10., min_samples=2) == labels + [-1, -1]


def test_hdbscan_wrapper_matches_dbscan_schema_and_member_identities():
    batch = turn_batch(8, xy=np.column_stack(([0., 1., 2., 3., 1000., 1001., 1002., 1003.], np.zeros(8))))
    kwargs = dict(eps_m=10., min_samples=2)
    expected = detect._cluster_dbscan(batch, TRANSFORMER, **kwargs)
    actual = detect._cluster_hdbscan(batch, TRANSFORMER, **kwargs)
    assert [c.as_dict() for c in actual] == [c.as_dict() for c in expected]
    assert [c.as_dict() for c in actual] == [c.as_dict() for c in detect._cluster_hdbscan(batch, TRANSFORMER, **kwargs)]


@pytest.mark.parametrize("algorithm", ["dbscan", "hdbscan"])
def test_joint_clustering_and_ablation_use_selected_algorithm(algorithm):
    turns = turn_batch(2, calculation_index=np.asarray([70, 71]), xy=np.asarray([[0., 0.], [1., 0.]]))
    crosses = _batch(CrossCandidateBatch, 2, calculation_index=np.asarray([70, 71]),
                     xy=np.asarray([[2., 0.], [3., 0.]]), from_source_id=np.asarray(["a", "a"]),
                     to_source_id=np.asarray(["b", "b"]))
    cfg = replace(experiment_config(turn_candidate_algorithm="turn_threehold", clustering_algorithm=algorithm),
                  candidate_dbscan_eps_m=10., candidate_dbscan_min_samples=4)
    with patch.object(detect, "_compute_getis_ord_g_star", side_effect=AssertionError("G* called")):
        selected, clusters = detect._cluster_pipeline(turns, crosses, TRANSFORMER, cfg)
    assert_batch_equal(selected, turns)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.source_counts == {"orthogonal_cross_track": 2, "turn": 2}
    assert cluster.critical_sources == ("orthogonal_cross_track", "turn")
    assert {"a", "b"} <= set(cluster.source_ids)
    assert cluster.members.calculation_index.tolist() == [70, 71, 70, 71]
    assert cluster.members.kind.tolist() == ["turn", "turn", "orthogonal_cross_track", "orthogonal_cross_track"]


def test_runtime_preserves_experiment_options_and_parent_refine_context(tmp_path):
    cfg = experiment_config(turn_candidate_algorithm="turn_threehold", clustering_algorithm="hdbscan")
    parent = SimpleNamespace(refine_id="r", source_table="trajectory_points", status="completed", spatial_context=CONTEXT)
    reader = SimpleNamespace(descriptor=SimpleNamespace(refine_id="r"))
    with (
        patch.object(detect, "fetch_refine_run_row", return_value=parent),
        patch.object(detect, "_detect_intersections_db", return_value="result") as run,
    ):
        assert detect.detect_intersections(config=cfg, context=detect_context(refine_id="r"),
            conn=object(), refine_reader=reader, runtime=runtime_config(), data_root=tmp_path) == "result"
    runtime_cfg = run.call_args.args[1]
    assert runtime_cfg.turn_candidate_algorithm == "turn_threehold"
    assert runtime_cfg.clustering_algorithm == "hdbscan"
    assert run.call_args.args[2] == CONTEXT


@pytest.mark.parametrize("switch,value", [("turn_candidate_algorithm", "turn_threehold"), ("clustering_algorithm", "hdbscan")])
def test_regular_pipeline_rejects_experimental_switches(tmp_path, switch, value):
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    base = tomllib.loads((root / "config/pipeline.toml").read_text())
    base["detect"][switch] = value
    with pytest.raises(ConfigError, match="unknown key"):
        load_pipeline_payload(base, base_directory=root / "config", postgres_password="test")
    experiment = load_experiment_payload(base, base_directory=root / "config", postgres_password="test")
    assert getattr(experiment.detect, switch) == value


@pytest.mark.parametrize("switch,value", [("turn_candidate_algorithm", "typo"), ("clustering_algorithm", "typo")])
def test_experiment_rejects_unknown_algorithms(switch, value):
    with pytest.raises(ValueError):
        experiment_config(**{switch: value})
