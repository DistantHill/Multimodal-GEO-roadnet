"""Backward-compatible Parser 0 exports.

The PRD-aligned implementation lives in ``track.v2.sources`` and
``track.v2.field_config``. This module remains as a thin import adapter for
older local tests and notebooks.
"""

from __future__ import annotations

from pathlib import Path

from .field_config import build_field_mapping as _build_field_mapping
from .field_config import load_field_config
from .models import ColumnSelector, CsvSource, PostgresSource
from .sources import (
    BaseTrajectorySourceHandle as TrajectorySourceHandle,
    detect_encoding,
    get_database,
    open_text_auto,
    parse_row as _parse_row_to_point,
)
from .sources import _parse_timestamp


def _load_field_specs(toml_path: str | Path | None):
    return load_field_config(toml_path).specs


def _detect_csv_encoding(csv_path: str | Path, hint_encoding: str | None = None) -> str:
    return detect_encoding(csv_path, hint_encoding=hint_encoding)


__all__ = [
    "ColumnSelector",
    "CsvSource",
    "PostgresSource",
    "TrajectorySourceHandle",
    "_build_field_mapping",
    "_detect_csv_encoding",
    "_load_field_specs",
    "_parse_row_to_point",
    "_parse_timestamp",
    "get_database",
    "open_text_auto",
]
