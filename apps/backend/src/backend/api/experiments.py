"""Read-only projections from the independent evaluation archive."""
from datetime import datetime
import math
import os
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
import psycopg

from pipeline.experiments.archive import ArchiveStore
from ..database import open_read_connection

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


def _read(operation):
    try:
        with open_read_connection(os.environ.get("ROADNET_EVAL_DATABASE", "roadnet_eval")) as connection:
            return operation(ArchiveStore(connection))
    except KeyError:
        raise HTTPException(status_code=404, detail="Experiment or evaluation not found")
    except psycopg.Error:
        raise HTTPException(status_code=503, detail="Evaluation archive unavailable; initialize it with the experiment CLI")


@router.get("")
def list_experiments(case_name: str | None = None, scheme_name: str | None = None,
                     commit: str | None = None, status: str | None = None,
                     started_after: datetime | None = None, started_before: datetime | None = None,
                     limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    return _read(lambda store: store.list_experiments(
        limit=limit, offset=offset, case_name=case_name, scheme_name=scheme_name,
        commit=commit, status=status, started_after=started_after, started_before=started_before))


@router.get("/{identifier}")
def experiment_detail(identifier: UUID):
    return _read(lambda store: store.get_experiment(str(identifier)))


@router.get("/{identifier}/evaluations")
def experiment_evaluations(identifier: UUID):
    def query(store):
        store.get_experiment(str(identifier))
        return {"items": store.evaluations(str(identifier))}
    return _read(query)


@router.get("/{identifier}/map")
def experiment_map(identifier: UUID, bbox: str,
                   limit: int = Query(2000, ge=1, le=5000), evaluation_id: UUID | None = None):
    try:
        bounds = tuple(float(v) for v in bbox.split(","))
        if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
            raise ValueError()
        west, south, east, north = bounds
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError()
    except ValueError:
        raise HTTPException(status_code=422, detail="bbox must be west,south,east,north in WGS84")
    return _read(lambda store: store.map_features(
        str(identifier), bounds, limit, str(evaluation_id) if evaluation_id else None))
