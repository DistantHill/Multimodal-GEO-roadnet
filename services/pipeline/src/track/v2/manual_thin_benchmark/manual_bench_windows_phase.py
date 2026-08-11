"""Render the non-gating Phase 0 view from validated R7 benchmark evidence.

This helper never connects to the database and never invokes the algorithm.
Real-workload phase collection is owned by ``manual_bench_windows.py`` after its
immutable database preflight succeeds; an invalid real gate therefore remains
diagnostic-only and cannot be bypassed through this reporting helper.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract_phase(evidence: dict[str, Any]) -> dict[str, Any]:
    if evidence.get("evidence_version") != "PERF-WINDOWS-R7":
        raise RuntimeError("phase report requires validated PERF-WINDOWS-R7 evidence")
    phase = evidence.get("phase0")
    if not isinstance(phase, dict):
        raise RuntimeError("R7 evidence has no Phase 0 diagnostic")
    return {
        "diagnostic_only": True,
        "algorithm_called_by_reporter": False,
        "case": evidence.get("case"),
        "formal_equivalent": evidence.get("equivalent"),
        "source_evidence": evidence.get("provenance_header"),
        "phase0": phase,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path,
        default=Path("generated/perf_windows/manual_bench_windows_dense_r7.json"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("generated/perf_windows/manual_bench_windows_dense_phase_r7.json"),
    )
    args = parser.parse_args()
    evidence = json.loads(args.input.read_text(encoding="utf-8"))
    report = _extract_phase(evidence)
    report["source_evidence_path"] = str(args.input.resolve())
    report["source_evidence_sha256"] = _sha256(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output": str(args.output), "diagnostic_only": True}, sort_keys=True))


if __name__ == "__main__":
    main()
