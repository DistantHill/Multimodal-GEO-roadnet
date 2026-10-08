"""Exercise HTTP responses against an isolated real archive schema."""
from contextlib import contextmanager
import os
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
import pytest

from backend.api import experiments
from pipeline.experiments.archive import ArchiveStore


def _polygon(identifier, offset=0):
    return {"type":"Feature", "id":identifier,"properties":{},"geometry":{
        "type":"Polygon","coordinates":[[[offset,0],[offset+.001,0],
        [offset+.001,.001],[offset,.001],[offset,0]]]}}


def test_real_archive_api_read_only_and_historical_maps(monkeypatch):
    dsn=os.environ.get("ROADNET_TEST_DSN")
    if not dsn:
        pytest.skip("ROADNET_TEST_DSN not configured")
    schema="eval_api_test_"+uuid.uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped=psycopg.conninfo.make_conninfo(dsn,options=f"-csearch_path={schema},public")
    identifier, other = str(uuid.uuid4()),str(uuid.uuid4())
    try:
        with psycopg.connect(scoped) as connection:
            store=ArchiveStore(connection)
            store.initialize()
            record={"id":identifier,"status":"completed","case_name":"alpha","scheme_name":"turn",
                    "started_at":"2026-10-08T01:00:00+00:00","parameters":{"radius":80},
                    "case_snapshot":{"source_table":"deleted_source_table"}}
            store.save_experiment(record,[_polygon("p")])
            store.save_experiment({**record,"id":other,"case_name":"beta"},[])
            first=store.append_evaluation(identifier,[_polygon("t")],{"mode":"iou","iou_threshold":.5})
            second=store.append_evaluation(identifier,[_polygon("t",.01)],{"mode":"iou","iou_threshold":.5})
        read_only_checks=[]
        @contextmanager
        def read_connection(database_name=None):
            assert database_name == "roadnet_eval"
            with psycopg.connect(scoped) as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                read_only_checks.append(connection.execute("SHOW transaction_read_only").fetchone()[0])
                yield connection
        monkeypatch.setattr(experiments,"open_read_connection",read_connection)
        app=FastAPI()
        app.include_router(experiments.router)
        active=TestClient(app)
        listing=active.get("/api/experiments",params={"case_name":"alpha"})
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["experiment_id"] == identifier
        detail=active.get(f"/api/experiments/{identifier}").json()
        assert detail["parameters"] == {"radius":80}
        assert detail["metrics"]["tp"] == 0
        history=active.get(f"/api/experiments/{identifier}/evaluations").json()["items"]
        assert len(history) == 2 and history[0]["evaluation_id"] == second["evaluation_id"]
        mapped=active.get(f"/api/experiments/{identifier}/map",params={
            "bbox":"-1,-1,1,1","evaluation_id":first["evaluation_id"],"limit":1})
        assert mapped.status_code == 200
        assert mapped.json()["metadata"] == {"has_more":True,"truncated":True,"total":2}
        assert mapped.json()["features"][0]["properties"]["classification"] == "TP"
        assert active.get(f"/api/experiments/{identifier}/map",params={
            "bbox":"-1,-1,1,1","evaluation_id":str(uuid.uuid4())}).status_code == 404
        assert active.get(f"/api/experiments/{uuid.uuid4()}").status_code == 404
        assert read_only_checks and set(read_only_checks) == {"on"}
    finally:
        with psycopg.connect(dsn,autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
