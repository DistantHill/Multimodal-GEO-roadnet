"""Centralized pipeline configuration loader.

ZH: 从 ``config/pipeline.toml`` 加载 v2 运行时所有可调参数，映射到各阶段的
    frozen dataclass。TOML 中缺失的键回退到 dataclass 默认值，未知键报错，
    与 :mod:`track.v2.field_config` 的加载风格一致。

EN: Load every tunable v2 runtime parameter from ``config/pipeline.toml`` and
    map it onto the per-stage frozen dataclasses. Missing keys fall back to the
    dataclass defaults; unknown keys raise, matching the loading style of
    :mod:`track.v2.field_config`.
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from ..detect import IntersectConfig
from ..models import (
    RefineConfig,
    ClipConfig,
    FittingConfig,
    NetworkConfig,
    ProjectionConfig,
    RuntimeConfig,
)


DEFAULT_PIPELINE_CONFIG_PATH = Path(__file__).resolve().parent / "pipeline.toml"

# ZH: 顶层 TOML 节名白名单，防止 section 名打错字被静默忽略。
# EN: Top-level section whitelist, so a typo'd section name is not silently ignored.
_SECTIONS = (
    "projection", "refine", "detect", "clip", "fitting", "network", "runtime",
)
_REMOVED_DETECT_KEYS = {
    "use_orthogonal_candidates": "use_cross_track_candidates",
    "dbscan_eps_m_ortho": None,
    "dbscan_min_samples_ortho": None,
}
_STRICT_RUNTIME_INTS = {
    "duckdb_threads",
    "polars_threads",
    "parquet_target_file_size_bytes",
    "parquet_row_group_size_rows",
    "artifact_gc_grace_days",
}


@dataclass(frozen=True)
class PipelineConfig:
    """Composite of all per-stage configs plus the cross-cutting runtime config."""

    projection: ProjectionConfig
    refine: RefineConfig
    detect: IntersectConfig
    clip: ClipConfig
    fitting: FittingConfig
    network: NetworkConfig
    runtime: RuntimeConfig


def _coerce(value: Any, default: Any, name: str) -> Any:
    """Coerce a TOML scalar to the type of ``default``, raising on mismatch."""
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{name} must be a boolean")
        return value
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{name} must be an integer") from exc
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{name} must be a number") from exc
    if isinstance(default, tuple):
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{name} must be an array")
        return tuple(value)
    return value


def _build_section(base: Any, section: Any) -> Any:
    """Build a dataclass instance from a TOML table, overlaying only present keys."""
    cls = base.__class__.__name__
    if not isinstance(section, dict):
        raise ConfigError(f"[{cls}] section must be a table")

    known = {field.name for field in dataclasses.fields(base)}
    unknown = set(section) - known
    if unknown:
        if isinstance(base, IntersectConfig) and unknown & _REMOVED_DETECT_KEYS.keys():
            migrations = []
            for key in sorted(unknown & _REMOVED_DETECT_KEYS.keys()):
                replacement = _REMOVED_DETECT_KEYS[key]
                if replacement is None:
                    migrations.append(f"remove {key}")
                else:
                    migrations.append(f"rename {key} to {replacement}")
            raise ConfigError(
                f"[{cls}] removed Method 2 keys: {', '.join(migrations)}"
            )
        raise ConfigError(f"[{cls}] unknown keys: {', '.join(sorted(unknown))}")

    overrides: dict[str, Any] = {}
    for field in dataclasses.fields(base):
        if field.name in section:
            if field.name == "processing_srid":
                value = section[field.name]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ConfigError(f"{cls}.processing_srid must be an integer")
                overrides[field.name] = value
                continue
            if (
                isinstance(base, RuntimeConfig)
                and field.name in _STRICT_RUNTIME_INTS
                and (
                    isinstance(section[field.name], bool)
                    or not isinstance(section[field.name], int)
                )
            ):
                raise ConfigError(f"{cls}.{field.name} must be an integer")
            overrides[field.name] = _coerce(
                section[field.name], getattr(base, field.name), f"{cls}.{field.name}"
            )
    try:
        return dataclasses.replace(base, **overrides)
    except ValueError as exc:
        raise ConfigError(f"invalid [{cls}] configuration: {exc}") from exc


def load_pipeline_config(path: str | Path | None = None) -> PipelineConfig:
    """Load the pipeline config, falling back to the built-in ``pipeline.toml``.

    ZH: 读取 pipeline TOML 并组装 PipelineConfig。``path`` 为 None 时使用内置
        ``config/pipeline.toml``。文件缺失/格式错误/未知键时抛 ``ConfigError``。
    EN: Read the pipeline TOML into a PipelineConfig. When ``path`` is None, use
        the built-in ``config/pipeline.toml``. Raises ``ConfigError`` on missing
        file, malformed TOML, or unknown keys.
    """
    resolved = Path(path) if path is not None else DEFAULT_PIPELINE_CONFIG_PATH
    if not resolved.exists():
        raise ConfigError(f"pipeline config not found: {resolved}")

    try:
        with resolved.open("rb") as fh:
            payload = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML pipeline config: {resolved}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read pipeline config: {resolved}") from exc

    unknown_top = set(payload) - set(_SECTIONS)
    if unknown_top:
        raise ConfigError(f"unknown pipeline config sections: {', '.join(sorted(unknown_top))}")

    return PipelineConfig(
        projection=_build_section(ProjectionConfig(), payload.get("projection", {})),
        refine=_build_section(RefineConfig(), payload.get("refine", {})),
        detect=_build_section(IntersectConfig(), payload.get("detect", {})),
        clip=_build_section(ClipConfig(), payload.get("clip", {})),
        fitting=_build_section(FittingConfig(), payload.get("fitting", {})),
        network=_build_section(NetworkConfig(), payload.get("network", {})),
        runtime=_build_section(RuntimeConfig(), payload.get("runtime", {})),
    )
