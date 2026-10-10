"""Explicit plugin descriptions; importing descriptions never executes algorithms."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class CandidatePlugin:
    algorithm_id: str
    params_type: type
    output_type: str
    run: Callable[..., Any]


@dataclass(frozen=True)
class ClusterPlugin:
    algorithm_id: str
    params_type: type
    input_type: str
    run: Callable[..., Any]
