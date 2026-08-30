from __future__ import annotations

import os
import urllib.error
import urllib.request

import psycopg
from fastapi import FastAPI


app = FastAPI(title="Roadnet Backend", version="0.1.0")


def _postgres_status() -> str:
    try:
        with psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "postgres"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            dbname=os.getenv("POSTGRES_DB", "roadnet"),
            user=os.getenv("POSTGRES_USER", "roadnet"),
            password=os.getenv("POSTGRES_PASSWORD", "change-me"),
            connect_timeout=2,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return "ok"
    except Exception as exc:  # pragma: no cover - depends on external service
        return f"error: {exc.__class__.__name__}"


def _qdrant_status() -> str:
    host = os.getenv("QDRANT_HOST", "qdrant")
    port = os.getenv("QDRANT_PORT", "6333")
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=2) as response:
            return "ok" if response.status == 200 else f"http-{response.status}"
    except (OSError, urllib.error.URLError) as exc:  # pragma: no cover - external service
        return f"error: {exc.__class__.__name__}"


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "backend"}


@app.get("/health/dependencies")
def dependency_health() -> dict[str, object]:
    dependencies = {
        "postgres": _postgres_status(),
        "qdrant": _qdrant_status(),
    }
    healthy = all(value == "ok" for value in dependencies.values())
    return {"status": "ok" if healthy else "degraded", "dependencies": dependencies}
