from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api import experiments


def client(monkeypatch):
    app = FastAPI()
    app.include_router(experiments.router)
    monkeypatch.setattr(experiments, "_read", lambda operation: {"ok":True})
    return TestClient(app)


def test_map_requires_valid_bounded_wgs84_bbox(monkeypatch):
    active = client(monkeypatch)
    identifier = "00000000-0000-0000-0000-000000000001"
    for bbox in ("1,2,3", "nan,0,1,1", "0,0,0,1", "0,0,181,1"):
        assert active.get(f"/api/experiments/{identifier}/map", params={"bbox":bbox}).status_code == 422
    assert active.get(f"/api/experiments/{identifier}/map",params={"bbox":"0,0,1,1"}).status_code == 200


def test_list_limit_and_uuid_validation(monkeypatch):
    active = client(monkeypatch)
    assert active.get("/api/experiments",params={"limit":101}).status_code == 422
    assert active.get("/api/experiments/not-a-uuid").status_code == 422
