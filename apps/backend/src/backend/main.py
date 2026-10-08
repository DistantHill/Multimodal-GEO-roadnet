from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI

from .config import BackendConfig, load_backend_config_from_environment
from .database import configure_database, postgres_connection_settings, verify_runtime_schema
from .api.map_routes import router as map_router
from .api.database_catalog import router as database_catalog_router
from .api.experiments import router as experiments_router


@asynccontextmanager
async def lifespan(active_app: FastAPI) -> AsyncIterator[None]:
    """Reject a stale database before accepting any Backend request."""
    config = load_backend_config_from_environment()
    active_app.state.config = config
    active_app.title = config.runtime.title
    active_app.version = config.runtime.version
    configure_database(config)
    logging.getLogger(__name__).info(
        "loaded Backend config path=%s digest=%s sections=%s",
        config.source_path, config.digest(), "postgres,qdrant,map,health,runtime",
    )
    verify_runtime_schema()
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(map_router)
app.include_router(database_catalog_router)
app.include_router(experiments_router)


def _postgres_status(config: BackendConfig) -> str:
    try:
        with psycopg.connect(
            **{**postgres_connection_settings(),
               "connect_timeout": config.health.postgres_timeout_seconds},
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return "ok"
    except Exception as exc:  # pragma: no cover - depends on external service
        return f"error: {exc.__class__.__name__}"


def _qdrant_status(config: BackendConfig) -> str:
    try:
        url = f"http://{config.qdrant.host}:{config.qdrant.port}/healthz"
        with urllib.request.urlopen(url, timeout=config.health.qdrant_timeout_seconds) as response:
            return "ok" if response.status == 200 else f"http-{response.status}"
    except (OSError, urllib.error.URLError) as exc:  # pragma: no cover - external service
        return f"error: {exc.__class__.__name__}"


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "backend"}


@app.get("/health/dependencies")
def dependency_health() -> dict[str, object]:
    config: BackendConfig = app.state.config
    dependencies = {
        "postgres": _postgres_status(config),
        "qdrant": _qdrant_status(config),
    }
    healthy = all(value == "ok" for value in dependencies.values())
    return {"status": "ok" if healthy else "degraded", "dependencies": dependencies}
