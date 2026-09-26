"""Global nonzero-cell Q95 and uint8 sqrt rendering."""

from __future__ import annotations

import numpy as np


def fit_q95_profile(counts: np.ndarray) -> dict[str, object]:
    nonzero = counts[counts > 0]
    if nonzero.size == 0:
        return {"status": "empty", "q95": None, "nonzero_cell_count": 0,
                "quantile": 0.95, "method": "linear", "normalizer": "sqrt"}
    return {"status": "complete", "q95": float(np.quantile(nonzero, 0.95, method="linear")),
            "nonzero_cell_count": int(nonzero.size), "quantile": 0.95,
            "method": "linear", "normalizer": "sqrt"}


def render_sqrt_uint8(counts: np.ndarray, q95: float | None) -> np.ndarray:
    if q95 is None:
        return np.zeros(counts.shape, dtype=np.uint8)
    values = np.minimum(counts.astype(np.float64), q95) / q95
    return np.floor(255 * np.sqrt(values) + 0.5).astype(np.uint8)
