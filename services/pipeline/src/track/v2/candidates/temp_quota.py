"""Retry only DuckDB temporary-space quota exhaustion on an idle connection."""
from __future__ import annotations

from collections.abc import Callable

import duckdb

from ..telemetry.console_logging import log_info


_LIMITS = ("50GB", "100GB", "200GB")


def is_temp_quota_exhausted(error: BaseException) -> bool:
    return (isinstance(error, duckdb.OutOfMemoryException)
            and "failed to offload data block" in str(error)
            and "bytes used" in str(error))


class TempQuota:
    """Own one connection's effective temp limit, not a Run-wide quota."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, initial: str, owner: str,
                 *, limits: tuple[str, ...] = _LIMITS,
                 emit: Callable[[str], None] = log_info) -> None:
        self.connection = connection
        self.owner = owner
        self.limits = limits
        self.current = initial
        self.emit = emit

    def run(self, operation: str, attempt: Callable[[], object]) -> object:
        """The attempt must roll back any failed transaction before returning here."""
        while True:
            try:
                return attempt()
            except duckdb.OutOfMemoryException as error:
                if not is_temp_quota_exhausted(error):
                    raise
                try:
                    index = self.limits.index(self.current)
                except ValueError:
                    raise
                if index + 1 >= len(self.limits):
                    raise
                new_limit = self.limits[index + 1]
                # Levels are fixed trusted configuration strings, not SQL input.
                self.connection.execute(f"SET max_temp_directory_size='{new_limit}'")
                self.emit(f"DuckDB temp quota expansion owner={self.owner} operation={operation} "
                          f"old={self.current} new={new_limit} reason=temp_limit")
                self.current = new_limit
