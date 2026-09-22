from __future__ import annotations

from pathlib import Path
from typing import Any
import tomllib

from .errors import ConfigError, FieldSelectionRequired
from .models import ColumnSelector, FieldConfig, FieldMapping, FieldSpec


VALID_FIELD_TYPES = frozenset({"str", "float", "int", "timestamp"})


def load_field_config(path: str | Path) -> FieldConfig:
    resolved = Path(path)
    if not resolved.exists():
        raise ConfigError(f"field config not found: {resolved}")

    try:
        with resolved.open("rb") as f:
            payload = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML field config: {resolved}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read field config: {resolved}") from exc

    errors: list[str] = []
    errors.extend(f"{key}: unknown top-level key" for key in sorted(set(payload) - {"quality", "field"}))
    field_entries = payload.get("field")
    specs: list[FieldSpec] = []
    if not isinstance(field_entries, list) or not field_entries:
        errors.append("field: must contain at least one [[field]] entry")
    else:
        for index, entry in enumerate(field_entries):
            spec = _parse_spec(entry, index, errors)
            if spec is not None:
                specs.append(spec)
    quality = payload.get("quality")
    quality_values: dict[str, Any] = {}
    quality_keys = {"sample_errors_max", "max_consecutive_errors", "max_error_ratio"}
    if not isinstance(quality, dict):
        errors.append("quality: missing required table")
    else:
        errors.extend(f"quality.{key}: unknown key" for key in sorted(set(quality) - quality_keys))
        errors.extend(f"quality.{key}: missing required key" for key in sorted(quality_keys - set(quality)))
        for key in ("sample_errors_max", "max_consecutive_errors"):
            value = quality.get(key)
            if type(value) is not int or value <= 0:
                errors.append(f"quality.{key}: must be an integer > 0")
            else:
                quality_values[key] = value
        value = quality.get("max_error_ratio")
        if type(value) not in (int, float) or not 0.0 <= value <= 1.0:
            errors.append("quality.max_error_ratio: must be a number in [0, 1]")
        else:
            quality_values["max_error_ratio"] = float(value)
    canonicals = [spec.canonical for spec in specs]
    for canonical in sorted({item for item in canonicals if canonicals.count(item) > 1}):
        errors.append(f"field.{canonical}: canonical name is duplicated")
    if errors:
        raise ConfigError("invalid field configuration:\n- " + "\n- ".join(errors))
    return FieldConfig(specs=specs, **quality_values)


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


def _parse_spec(entry: Any, index: int, errors: list[str]) -> FieldSpec | None:
    prefix = f"field[{index}]"
    if not isinstance(entry, dict):
        errors.append(f"{prefix}: must be a table")
        return None
    required_keys = {"canonical", "aliases", "required", "type", "unit_scale", "has_default", "extra"}
    allowed = required_keys | {"default"}
    errors.extend(f"{prefix}.{key}: unknown key" for key in sorted(set(entry) - allowed))
    errors.extend(f"{prefix}.{key}: missing required key" for key in sorted(required_keys - set(entry)))
    canonical = entry.get("canonical")
    aliases = entry.get("aliases")
    field_type = entry.get("type")
    if not isinstance(canonical, str) or not canonical:
        errors.append(f"{prefix}.canonical: must be a non-empty string")
    if not isinstance(aliases, list) or not aliases or not all(isinstance(item, str) for item in aliases):
        errors.append(f"{prefix}.aliases: must be a non-empty string list")
    if field_type not in VALID_FIELD_TYPES:
        errors.append(f"{prefix}.type: unsupported field type {field_type!r}")
    unit_scale = entry.get("unit_scale")
    if type(unit_scale) not in (int, float) or unit_scale <= 0:
        errors.append(f"{prefix}.unit_scale: must be a number > 0")
    for key in ("required", "has_default", "extra"):
        if type(entry.get(key)) is not bool:
            errors.append(f"{prefix}.{key}: must be a boolean")
    has_default = entry.get("has_default")
    if has_default is True and "default" not in entry:
        errors.append(f"{prefix}.default: required when has_default is true")
    if has_default is False and "default" in entry:
        errors.append(f"{prefix}.default: forbidden when has_default is false")
    if any(message.startswith(prefix) for message in errors):
        return None
    return FieldSpec(
        canonical=canonical,
        aliases=tuple(aliases),
        required=entry["required"], type=field_type, unit_scale=float(unit_scale),
        default=entry.get("default"), extra=entry["extra"],
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
