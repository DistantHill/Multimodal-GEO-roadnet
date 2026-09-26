"""Shared JSON output helpers for v2 command-line adapters."""

from __future__ import annotations

import json
from typing import Any


def print_json(value: Any) -> None:
    """Serialize one v2 CLI payload with the project-wide JSON settings."""
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def error_payload(status: str, error: str) -> dict[str, str]:
    """Build a uniform JSON error payload for a CLI boundary."""
    return {"status": status, "error": error}
