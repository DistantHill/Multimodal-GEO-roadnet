from __future__ import annotations

from pathlib import Path
from typing import Any
import tomllib

from .errors import ConfigError, FieldSelectionRequired
from .models import ColumnSelector, FieldConfig, FieldMapping, FieldSpec


DEFAULT_FIELD_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "default_field_specs.toml"
VALID_FIELD_TYPES = frozenset({"str", "float", "int", "timestamp"})


def load_field_config(path: str | Path | None = None) -> FieldConfig:
    resolved = Path(path) if path is not None else DEFAULT_FIELD_CONFIG_PATH
    if not resolved.exists():
        if path is None:
            raise ConfigError("built-in default_field_specs.toml is missing")
        raise ConfigError(f"custom field config not found: {resolved}")

    try:
        with resolved.open("rb") as f:
            payload = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML field config: {resolved}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read field config: {resolved}") from exc

    field_entries = payload.get("fields", payload.get("field"))
    if not isinstance(field_entries, list) or not field_entries:
        raise ConfigError("field config must contain [[field]] or [[fields]] entries")

    specs = [_parse_spec(entry) for entry in field_entries]
    quality = payload.get("quality") or {}
    if not isinstance(quality, dict):
        raise ConfigError("[quality] must be a table")

    return FieldConfig(
        specs=specs,
        sample_errors_max=_positive_int(quality.get("sample_errors_max", 50), "sample_errors_max"),
        max_consecutive_errors=_positive_int(
            quality.get("max_consecutive_errors", 50), "max_consecutive_errors"
        ),
        max_error_ratio=_ratio(quality.get("max_error_ratio", 1.0), "max_error_ratio"),
    )


def build_field_mapping(
    specs: list[FieldSpec],
    available_columns: list[str],
    selector: ColumnSelector | None = None,
) -> FieldMapping:
    canonical_to_source: dict[str, str] = {}
    available_exact = set(available_columns)
    available_casefold = {column.casefold(): column for column in available_columns}

    for spec in specs:
        match = _match_alias(spec.aliases, available_exact, available_casefold)
        if match is not None:
            canonical_to_source[spec.canonical] = match

    missing = _missing_required(specs, canonical_to_source)
    if missing and selector is not None:
        selected = selector(list(available_columns))
        if not isinstance(selected, dict):
            raise ConfigError("ColumnSelector must return dict[str, str]")
        for canonical, source_column in selected.items():
            if source_column not in available_exact:
                raise ConfigError(
                    f"ColumnSelector mapped {canonical!r} to unknown column {source_column!r}"
                )
            canonical_to_source[canonical] = source_column
        missing = _missing_required(specs, canonical_to_source)

    if missing:
        raise FieldSelectionRequired(missing, list(available_columns))

    return FieldMapping(specs=list(specs), canonical_to_source=canonical_to_source)


def _parse_spec(entry: Any) -> FieldSpec:
    if not isinstance(entry, dict):
        raise ConfigError("each field spec must be a TOML table")

    canonical = entry.get("canonical")
    aliases = entry.get("aliases")
    field_type = entry.get("type", "float")
    if not isinstance(canonical, str) or not canonical:
        raise ConfigError("field canonical must be a non-empty string")
    if not isinstance(aliases, list) or not aliases or not all(isinstance(item, str) for item in aliases):
        raise ConfigError(f"{canonical}: aliases must be a non-empty string list")
    if field_type not in VALID_FIELD_TYPES:
        raise ConfigError(f"{canonical}: unsupported field type {field_type!r}")

    try:
        unit_scale = float(entry.get("unit_scale", 1.0))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{canonical}: unit_scale must be numeric") from exc

    return FieldSpec(
        canonical=canonical,
        aliases=tuple(aliases),
        required=bool(entry.get("required", True)),
        type=str(field_type),
        unit_scale=unit_scale,
        default=entry.get("default"),
        extra=bool(entry.get("extra", False)),
    )


def _match_alias(
    aliases: tuple[str, ...],
    available_exact: set[str],
    available_casefold: dict[str, str],
) -> str | None:
    for alias in aliases:
        if alias in available_exact:
            return alias
    for alias in aliases:
        match = available_casefold.get(alias.casefold())
        if match is not None:
            return match
    return None


def _missing_required(specs: list[FieldSpec], mapping: dict[str, str]) -> list[str]:
    return [spec.canonical for spec in specs if spec.required and spec.canonical not in mapping]


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return parsed


def _ratio(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be in [0, 1]") from exc
    if not 0.0 <= parsed <= 1.0:
        raise ConfigError(f"{name} must be in [0, 1]")
    return parsed
