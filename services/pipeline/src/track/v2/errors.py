from __future__ import annotations

from .models import PointRef


class V2Error(RuntimeError):
    """Base exception for all track.v2 errors."""


class FatalDataError(V2Error):
    """Base class for fatal source/configuration failures."""


class ConfigError(FatalDataError):
    """TOML config is missing, malformed, or invalid."""


class EncodingDetectionError(FatalDataError):
    """CSV encoding could not be identified from the configured probes."""


class ResourceLeakWarning(ResourceWarning):
    """A trajectory source handle was garbage-collected while still open."""


class FieldSelectionRequired(FatalDataError):
    """Required canonical fields could not be mapped from source columns."""

    def __init__(self, missing_fields: list[str], available_columns: list[str]) -> None:
        self.missing_fields = list(missing_fields)
        self.available_columns = list(available_columns)
        super().__init__(
            f"missing required fields {self.missing_fields}; "
            f"available columns: {self.available_columns}"
        )

    @property
    def missing(self) -> list[str]:
        return self.missing_fields

    @property
    def available(self) -> list[str]:
        return self.available_columns


class TrajectoryOrderError(FatalDataError):
    """Source order violated the (vehicle_id, timestamp, point_ref) contract."""

    def __init__(
        self,
        *,
        vehicle_id: str,
        previous_ref: PointRef,
        current_ref: PointRef,
        previous_timestamp: float,
        current_timestamp: float,
    ) -> None:
        self.vehicle_id = vehicle_id
        self.previous_ref = previous_ref
        self.current_ref = current_ref
        self.previous_timestamp = previous_timestamp
        self.current_timestamp = current_timestamp
        super().__init__(
            f"order violation for vehicle {vehicle_id!r}: "
            f"previous {previous_ref.source_row_key} at {previous_timestamp}, "
            f"current {current_ref.source_row_key} at {current_timestamp}"
        )

    @property
    def prev_point_ref(self) -> str:
        return self.previous_ref.source_row_key

    @property
    def curr_point_ref(self) -> str:
        return self.current_ref.source_row_key

    @property
    def prev_timestamp(self) -> float:
        return self.previous_timestamp

    @property
    def curr_timestamp(self) -> float:
        return self.current_timestamp


class DataSourceCorrupted(FatalDataError):
    """Bad-row thresholds indicate the source should not be trusted."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)
