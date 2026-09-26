"""Refine trajectory result export and read operations."""


class RefineParquetError(RuntimeError):
    """The authoritative Refine result could not be read or exported safely."""
