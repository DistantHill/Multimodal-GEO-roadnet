from pathlib import Path

import pytest

from backend.config import (
    BackendConfig,
    HealthConfig,
    MapConfig,
    PostgresConfig,
    QdrantConfig,
    RuntimeConfig,
)
from backend.database import configure_database


@pytest.fixture(scope="session")
def backend_config() -> BackendConfig:
    return BackendConfig(
        postgres=PostgresConfig(
            host="postgres", port=5432, database="roadnet", user="roadnet",
            password="test-only", connect_timeout_seconds=5,
            statement_timeout_ms=12_000,
        ),
        qdrant=QdrantConfig(host="qdrant", port=6333),
        map=MapConfig(layer_limit_default=1_000, layer_limit_max=5_000,
                      catalog_exact_count_max_rows=50_000,
                      replay_max_vehicles_default=25, replay_max_vehicles_max=100,
                      replay_max_points_default=10_000, replay_max_points_max=50_000,
                      replay_selection_ids_max=1_000,
                      clip_max_vehicles_default=50, clip_max_vehicles_max=200,
                      clip_max_points_default=20_000, clip_max_points_max=100_000,
                      clip_feature_limit=1_000),
        health=HealthConfig(postgres_timeout_seconds=2, qdrant_timeout_seconds=2.0),
        runtime=RuntimeConfig(title="Roadnet Backend", version="test"),
        source_path=Path("test-backend.toml"),
    )


@pytest.fixture(autouse=True)
def configured_database(backend_config: BackendConfig) -> None:
    configure_database(backend_config)
