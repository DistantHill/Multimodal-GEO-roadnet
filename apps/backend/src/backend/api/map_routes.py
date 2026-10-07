"""HTTP routes for read-only map, replay, and Clip endpoints."""

from __future__ import annotations

from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import psycopg

from .map_contracts import BBox, ClipRequest, FeatureCollection, ReplayRequest, parse_bbox, resolve_clip_limits, resolve_replay_limits
from ..config import MapConfig
from ..database import SCHEMA_VERSION, open_read_connection, postgres_connection_settings, require_current_schema
from ..map_errors import MapDataError, MapDatabaseNotFoundError, MapNotFoundError, MapSchemaMismatchError
from ..stores.map_layers import PostgresMapStore

router = APIRouter(prefix="/api/v1/map", tags=["map"])


def database_query_error(exc: psycopg.Error) -> HTTPException:
    """Expose actionable resource errors without exposing SQL or credentials."""
    sqlstate = exc.sqlstate
    if sqlstate in {"53100", "53200"}:
        primary = exc.diag.message_primary or ""
        shared_memory = "shared memory" in primary.lower()
        message = (
            "PostgreSQL 共享内存空间不足（/dev/shm），查询失败。请检查容器 shm_size 和并行查询占用。"
            if shared_memory else
            "PostgreSQL 可用内存不足，查询失败。请检查数据库内存及查询并发。"
            if sqlstate == "53200" else
            "PostgreSQL 存储空间不足，查询失败。请检查数据盘、临时文件及共享内存空间。"
        )
        return HTTPException(503, detail={
            "error": "database_resource_exhausted", "sqlstate": sqlstate,
            "message": f"{message}（SQLSTATE {sqlstate}）",
        })
    return HTTPException(503, detail={
        "error": "database_query_failed",
        "message": "所选数据库连接或查询失败，请确认访问权限及 QGIS 图层的 database 是否对应。",
    })


class MapStore(Protocol):
    def layer_catalog(self) -> dict[str, Any]: ...
    def run_catalog(self) -> dict[str, Any]: ...
    def validate_layer_context(self, refine_id: str, detect_id: str | None, fit_id: str | None) -> None: ...
    def read_layer(self, layer_id: str, *, bbox: BBox | None, detect_id: str | None, fit_id: str | None, limit: int, offset: int) -> dict[str, Any]: ...
    def replay(self, request: ReplayRequest) -> dict[str, Any]: ...
    def clip(self, request: ClipRequest) -> dict[str, Any]: ...


def _validate_map_schema(connection: Any, *, body_selection: bool) -> None:
    try:
        require_current_schema(connection)
    except RuntimeError as exc:
        message = f"所选数据库不兼容当前 schema v{SCHEMA_VERSION}，请选择业务数据库。"
        detail = {"error": "database_schema_mismatch", "message": message} if body_selection else message
        raise HTTPException(409, detail=detail) from exc


def _selected_map_store(database_name: str | None, *, body_selection: bool = False):
    """Hold one read connection, including while serving explicitly selected DBs."""
    try:
        with open_read_connection() as connection:
            if database_name is None or database_name == postgres_connection_settings()["dbname"]:
                if database_name is not None:
                    _validate_map_schema(connection, body_selection=body_selection)
                yield PostgresMapStore(connection)
                return
            PostgresMapStore(connection)._validate_database(database_name)
        # Release the catalog connection before reading the selected database.
        with open_read_connection(database_name) as target:
            _validate_map_schema(target, body_selection=body_selection)
            yield PostgresMapStore(target)
    except psycopg.Error as exc:
        raise database_query_error(exc) from exc
    except MapDatabaseNotFoundError as exc:
        raise HTTPException(404, detail={"error": "database_not_found", "message": str(exc)}) from exc


DatabaseName = Annotated[str | None, Query(min_length=1, max_length=63)]


def get_map_store(database_name: DatabaseName = None):
    yield from _selected_map_store(database_name)


def get_replay_store(request: ReplayRequest, database_name: DatabaseName = None):
    yield from _selected_map_store(
        request.database_name or database_name, body_selection=request.database_name is not None,
    )


def get_clip_store(request: ClipRequest, database_name: DatabaseName = None):
    yield from _selected_map_store(
        request.database_name or database_name, body_selection=request.database_name is not None,
    )


@router.get("/layers")
def layer_catalog(store: MapStore = Depends(get_map_store)) -> dict[str, Any]:
    return store.layer_catalog()


@router.get("/runs")
def run_catalog(store: MapStore = Depends(get_map_store)) -> dict[str, Any]:
    return store.run_catalog()


def get_map_config(request: Request) -> MapConfig:
    return request.app.state.config.map


@router.get("/layers/{layer_id}", response_model=FeatureCollection)
def read_layer(layer_id: str, bbox: str | None = Query(default=None, description="Tile AABB in EPSG:4326: minLon,minLat,maxLon,maxLat"), detect_id: UUID | None = None, fit_id: UUID | None = None, refine_id: UUID | None = None, limit: int | None = Query(default=None, ge=1), offset: Annotated[int, Query(ge=0)] = 0, view_bbox: Annotated[str | None, Query(description="Current view AABB in EPSG:4326")] = None, tile_key: Annotated[str | None, Query(max_length=100, pattern=r"^\d{1,2}/\d+/\d+$")] = None, store: MapStore = Depends(get_map_store), map_config: MapConfig = Depends(get_map_config)) -> dict[str, Any]:
    try:
        resolved_limit = map_config.layer_limit_default if limit is None else limit
        if resolved_limit > map_config.layer_limit_max:
            raise ValueError(f"limit must not exceed configured maximum {map_config.layer_limit_max}")
        if refine_id is not None:
            store.validate_layer_context(str(refine_id), str(detect_id) if detect_id else None, str(fit_id) if fit_id else None)
        parsed_bbox = parse_bbox(bbox)
        parsed_view_bbox = parse_bbox(view_bbox)
        if (tile_key is not None or parsed_view_bbox is not None) and parsed_bbox is None:
            raise ValueError("tile requests require an explicit bbox")
        result = store.read_layer(layer_id, bbox=parsed_bbox, detect_id=str(detect_id) if detect_id else None, fit_id=str(fit_id) if fit_id else None, limit=resolved_limit, offset=offset)
        if tile_key is not None or parsed_view_bbox is not None:
            result.setdefault("metadata", {}).update(
                tile_key=tile_key,
                view_bbox=list(parsed_view_bbox) if parsed_view_bbox is not None else None,
                scope="tile",
            )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MapSchemaMismatchError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except MapDatabaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error": "database_not_found", "message": str(exc)}) from exc
    except MapNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MapDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/replay", response_model=FeatureCollection)
def replay(request: ReplayRequest, store: MapStore = Depends(get_replay_store), map_config: MapConfig = Depends(get_map_config)) -> dict[str, Any]:
    try:
        resolved = resolve_replay_limits(request, map_config)
        result = store.replay(resolved.model_copy(update={"database_name": None}))
        if request.database_name is not None:
            result.setdefault("metadata", {})["database_name"] = request.database_name
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MapSchemaMismatchError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except MapDatabaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error": "database_not_found", "message": str(exc)}) from exc
    except MapNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MapDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/clip", response_model=FeatureCollection)
def clip(request: ClipRequest, store: MapStore = Depends(get_clip_store), map_config: MapConfig = Depends(get_map_config)) -> dict[str, Any]:
    try:
        resolved = resolve_clip_limits(request, map_config)
        result = store.clip(resolved.model_copy(update={"database_name": None}))
        if request.database_name is not None:
            result.setdefault("metadata", {})["database_name"] = request.database_name
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MapSchemaMismatchError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except MapDatabaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error": "database_not_found", "message": str(exc)}) from exc
    except MapNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MapDataError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
