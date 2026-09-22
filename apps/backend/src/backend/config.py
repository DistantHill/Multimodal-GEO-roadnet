from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


class ConfigError(ValueError):
    """Raised when Backend configuration cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    connect_timeout_seconds: int
    statement_timeout_ms: int


@dataclass(frozen=True, slots=True)
class QdrantConfig:
    host: str
    port: int


@dataclass(frozen=True, slots=True)
class MapConfig:
    layer_limit_default: int
    layer_limit_max: int
    catalog_exact_count_max_rows: int
    replay_max_vehicles_default: int
    replay_max_vehicles_max: int
    replay_max_points_default: int
    replay_max_points_max: int
    replay_selection_ids_max: int
    clip_max_vehicles_default: int
    clip_max_vehicles_max: int
    clip_max_points_default: int
    clip_max_points_max: int
    clip_feature_limit: int


@dataclass(frozen=True, slots=True)
class HealthConfig:
    postgres_timeout_seconds: int
    qdrant_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    title: str
    version: str


@dataclass(frozen=True, slots=True)
class BackendConfig:
    postgres: PostgresConfig
    qdrant: QdrantConfig
    map: MapConfig
    health: HealthConfig
    runtime: RuntimeConfig
    source_path: Path

    def safe_snapshot(self) -> dict[str, Any]:
        result = asdict(self)
        result["postgres"].pop("password")
        # The path is deployment provenance, not part of the effective values.
        # It is logged separately by the lifespan startup hook.
        result.pop("source_path")
        return result

    def digest(self) -> str:
        payload = json.dumps(self.safe_snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


_SCHEMA: dict[str, dict[str, type]] = {
    "postgres": {"host": str, "port": int, "database": str, "user": str,
                 "connect_timeout_seconds": int, "statement_timeout_ms": int},
    "qdrant": {"host": str, "port": int},
    "map": {"layer_limit_default": int, "layer_limit_max": int,
            "catalog_exact_count_max_rows": int,
            "replay_max_vehicles_default": int, "replay_max_vehicles_max": int,
            "replay_max_points_default": int, "replay_max_points_max": int,
            "replay_selection_ids_max": int,
            "clip_max_vehicles_default": int, "clip_max_vehicles_max": int,
            "clip_max_points_default": int, "clip_max_points_max": int,
            "clip_feature_limit": int},
    "health": {"postgres_timeout_seconds": int, "qdrant_timeout_seconds": float},
    "runtime": {"title": str, "version": str},
}


def _validate(raw: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["root: expected table"]
    for section in sorted(set(raw) - set(_SCHEMA)):
        errors.append(f"{section}: unknown section")
    for section, fields in _SCHEMA.items():
        value = raw.get(section)
        if not isinstance(value, dict):
            errors.append(f"{section}: missing or not a table")
            continue
        for field in sorted(set(value) - set(fields)):
            errors.append(f"{section}.{field}: unknown key")
        for field, expected in fields.items():
            path = f"{section}.{field}"
            if field not in value:
                errors.append(f"{path}: missing key")
                continue
            item = value[field]
            valid = isinstance(item, expected) and not isinstance(item, bool)
            if expected is float:
                valid = isinstance(item, (int, float)) and not isinstance(item, bool)
            if not valid:
                errors.append(f"{path}: expected {expected.__name__}")
    if any(
        marker in error
        for error in errors
        for marker in ("missing", "expected", "not a table")
    ):
        return errors
    assert isinstance(raw, dict)
    for section, field in (("postgres", "host"), ("postgres", "database"),
                           ("postgres", "user"), ("qdrant", "host"),
                           ("runtime", "title"), ("runtime", "version")):
        if not raw[section][field].strip():
            errors.append(f"{section}.{field}: must not be empty")
    for section, field in (("postgres", "port"), ("qdrant", "port")):
        if not 1 <= raw[section][field] <= 65535:
            errors.append(f"{section}.{field}: must be between 1 and 65535")
    if not 1 <= raw["postgres"]["connect_timeout_seconds"] <= 300:
        errors.append("postgres.connect_timeout_seconds: must be between 1 and 300")
    if not 100 <= raw["postgres"]["statement_timeout_ms"] <= 60_000:
        errors.append("postgres.statement_timeout_ms: must be between 100 and 60000")
    if not 1 <= raw["health"]["postgres_timeout_seconds"] <= 60:
        errors.append("health.postgres_timeout_seconds: must be between 1 and 60")
    if not 0.1 <= raw["health"]["qdrant_timeout_seconds"] <= 60:
        errors.append("health.qdrant_timeout_seconds: must be between 0.1 and 60")
    for field in _SCHEMA["map"]:
        if raw["map"][field] < 1:
            errors.append(f"map.{field}: must be positive")
    for default, maximum in (
        ("layer_limit_default", "layer_limit_max"),
        ("replay_max_vehicles_default", "replay_max_vehicles_max"),
        ("replay_max_points_default", "replay_max_points_max"),
        ("clip_max_vehicles_default", "clip_max_vehicles_max"),
        ("clip_max_points_default", "clip_max_points_max"),
    ):
        if raw["map"][default] > raw["map"][maximum]:
            errors.append(f"map.{default}: must not exceed map.{maximum}")
    return errors


def load_backend_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> BackendConfig:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"unable to read Backend config {source}: {exc}") from exc
    errors = _validate(raw)
    env = os.environ if environ is None else environ
    password = env.get("POSTGRES_PASSWORD", "")
    if not password or password == "change-me":
        errors.append("POSTGRES_PASSWORD: missing or still uses placeholder")
    if errors:
        raise ConfigError("invalid Backend configuration:\n- " + "\n- ".join(errors))
    return BackendConfig(
        postgres=PostgresConfig(**raw["postgres"], password=password),
        qdrant=QdrantConfig(**raw["qdrant"]),
        map=MapConfig(**raw["map"]),
        health=HealthConfig(**raw["health"]),
        runtime=RuntimeConfig(**raw["runtime"]),
        source_path=source,
    )


def load_backend_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> BackendConfig:
    env = os.environ if environ is None else environ
    path = env.get("BACKEND_CONFIG_PATH")
    if not path:
        raise ConfigError("BACKEND_CONFIG_PATH: missing")
    return load_backend_config(path, environ=env)
