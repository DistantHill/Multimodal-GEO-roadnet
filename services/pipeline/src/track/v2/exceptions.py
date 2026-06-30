"""Backward-compatible exception exports for older v2 imports."""

from __future__ import annotations

from .errors import (
    ConfigError,
    DataSourceCorrupted,
    EncodingDetectionError,
    FatalDataError,
    FieldSelectionRequired,
    ResourceLeakWarning,
    TrajectoryOrderError,
    V2Error,
)

__all__ = [
    "ConfigError",
    "DataSourceCorrupted",
    "EncodingDetectionError",
    "FatalDataError",
    "FieldSelectionRequired",
    "ResourceLeakWarning",
    "TrajectoryOrderError",
    "V2Error",
]
