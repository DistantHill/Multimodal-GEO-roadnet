"""Pipeline configuration types and strict TOML loader."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    connect_timeout: int
    sslmode: str | None
    keepalives: bool
    keepalives_idle: int
    keepalives_interval: int
    keepalives_count: int
    tcp_user_timeout: int

    def _build_query(self) -> str:
        parts = [f"connect_timeout={self.connect_timeout}"]
        if self.sslmode:
            parts.append(f"sslmode={quote_plus(self.sslmode)}")
        if self.keepalives:
            parts.extend(("keepalives=1", f"keepalives_idle={self.keepalives_idle}",
                          f"keepalives_interval={self.keepalives_interval}",
                          f"keepalives_count={self.keepalives_count}"))
        if self.tcp_user_timeout:
            parts.append(f"tcp_user_timeout={self.tcp_user_timeout}")
        return "&".join(parts)

    def dsn(self) -> str:
        auth = quote_plus(self.user)
        if self.password:
            auth += f":{quote_plus(self.password)}"
        return (f"postgresql://{auth}@{quote_plus(self.host)}:{self.port}/"
                f"{quote_plus(self.database)}?{self._build_query()}")

    def safe_dsn(self) -> str:
        auth = quote_plus(self.user) + (":***" if self.password else "")
        return (f"postgresql://{auth}@{quote_plus(self.host)}:{self.port}/"
                f"{quote_plus(self.database)}?{self._build_query()}")


def connect(config: PostgresConfig):
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg. Install with: pip install 'psycopg[binary]'"
        ) from exc
    return psycopg.connect(config.dsn())


from .pipeline_config_loader import (  # noqa: E402
    LoggingConfig, ParserConfig, PathsConfig, PipelineConfig, ResourcesConfig,
    load_pipeline_config,
)
from .detect_config import DetectConfig, CandidateAlgorithmConfig, ClusterAlgorithmConfig

__all__ = [
    "LoggingConfig", "ParserConfig", "PathsConfig", "PipelineConfig", "PostgresConfig",
    "ResourcesConfig", "connect", "load_pipeline_config",
    "DetectConfig", "CandidateAlgorithmConfig", "ClusterAlgorithmConfig",
]
