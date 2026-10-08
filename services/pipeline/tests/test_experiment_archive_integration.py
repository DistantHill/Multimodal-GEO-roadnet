"""Opt-in PostgreSQL tests in a unique disposable schema."""
import os
import uuid

import psycopg
from psycopg import sql
import pytest
from pipeline.experiments.archive import ArchiveStore
from test_experiment_evaluation import polygon


@pytest.fixture
def store():
    dsn = os.environ.get("ROADNET_TEST_DSN")
    if not dsn:
        pytest.skip("ROADNET_TEST_DSN not configured")
    schema = "eval_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = psycopg.conninfo.make_conninfo(dsn, options=f"-csearch_path={schema},public")
    connection = psycopg.connect(scoped)
    archive = ArchiveStore(connection)
    archive.initialize()
    connection.commit()
    try:
        yield archive
    finally:
        connection.rollback()
        connection.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def record():
    return {"id":str(uuid.uuid4()),"batch_id":str(uuid.uuid4()),"status":"completed",
            "case_name":"test","scheme_name":"turn","commit":"fixture",
            "case_snapshot":{"source_table":"nonexistent_deleted_source"},
            "parameters":{},"refine_id":str(uuid.uuid4())}


def test_idempotent_archive_and_conflict_rolls_back(store):
    run = record()
    store.save_experiment(run,[polygon("p")])
    store.save_experiment(run,[polygon("p")])
    with pytest.raises(ValueError,match="immutable"):
        store.save_experiment({**run,"commit":"different"},[polygon("p")])
    with pytest.raises(ValueError,match="geometries"):
        store.save_experiment(run,[polygon("p",.01)])
    assert store.get_experiment(run["id"])["commit"] == "fixture"
    assert store.map_features(run["id"],(-1,-1,1,1),10)["features"][0]["geometry"] == polygon("p")["geometry"]


def test_reevaluation_and_map_history_independent_from_source(store):
    run = record()
    store.save_experiment(run,[polygon("p")])
    first=store.append_evaluation(run["id"],[polygon("t")],{"mode":"iou","iou_threshold":.5})
    second=store.append_evaluation(run["id"],[polygon("t",.01)],{"mode":"iou","iou_threshold":.5})
    assert len(store.evaluations(run["id"])) == 2
    assert first["metrics"]["tp"] == 1 and second["metrics"]["tp"] == 0
    old=store.map_features(run["id"],(-1,-1,1,1),10,first["evaluation_id"])
    assert {f["properties"]["classification"] for f in old["features"]} == {"TP"}
    store.mark_cleaned(run["id"])
    assert store.get_experiment(run["id"])["metrics"]["tp"] == 0


def test_shared_cleanup_and_region_scope(store):
    a,b=record(),record()
    b["refine_id"]=a["refine_id"]
    a["case_snapshot"]["geometry"]=polygon("region",width=.002)["geometry"]
    store.save_experiment(a,[polygon("inside"),polygon("outside",.01)])
    store.save_experiment(b,[])
    assert not store.cleanup_guard(a["id"])["can_delete_refine"]
    result=store.append_evaluation(a["id"],[polygon("t"),polygon("outside",.02)],
                                   {"mode":"center_distance","distance_m":10})
    assert result["metrics"]["tp"] == 1 and result["metrics"]["fn"] == 0
    store.mark_cleaned(b["id"])
    assert store.cleanup_guard(a["id"])["can_delete_refine"]


def test_scope_includes_centroid_on_boundary_and_map_limit(store):
    run=record()
    run["case_snapshot"]["geometry"]=polygon("region",width=.002)["geometry"]
    store.save_experiment(run,[polygon("boundary",-.0005, .0005),polygon("inside",.001,.001)])
    result=store.append_evaluation(run["id"],[polygon("boundary",-.0005,.0005),polygon("inside",.001,.001)],
                                  {"mode":"iou","iou_threshold":.5})
    assert result["metrics"]["tp"] == 2
    assert result["rules"]["region_policy"] == "centroid_covers"
    limited=store.map_features(run["id"],(-1,-1,1,1),1)
    assert limited["metadata"] == {"has_more":True,"truncated":True,"total":4}
    assert len([f for f in limited["features"] if f["properties"]["layer"] != "region"]) == 1


def test_snapshots_reject_secret_keys(store):
    run=record()
    run["parameters"]={"nested":{"password":"secret"}}
    with pytest.raises(ValueError,match="credentials"):
        store.save_experiment(run,[])
