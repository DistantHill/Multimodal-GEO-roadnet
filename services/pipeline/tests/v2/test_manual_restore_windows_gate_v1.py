"""Static contract tests for the guarded Windows Gate v1 restore helper."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from track.v2.manual_thin_benchmark.manual_restore_windows_gate_v1 import (
    COPY_COLUMNS,
    REQUIRED_TARGET_COLUMNS,
    ROW_LIMIT,
    Fingerprint,
    _matches_expected,
    _apply_restore,
    _source_rows,
)
from track.v2.manual_thin_benchmark.manual_bench_windows import (
    DENSE_RSS_RATIO_CEILING,
    REAL_RSS_RATIO_CEILING,
    _ledger_digest,
    _load_semantic_sidecar,
    _finalize_real_ledger,
    _canonical_digest,
    _load_legacy_attestation,
    _real_plan,
    _validate_real_ledger,
    _write_semantic_sidecar,
)


class WindowsGateRestoreContractTest(unittest.TestCase):
    def test_source_copy_contract_matches_the_frozen_seed_order_and_limit(self):
        class Cursor:
            def __init__(self):
                self.name = None
                self.itersize = None
                self.query = None
                self.params = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, params):
                self.query = str(query)
                self.params = params

            def __iter__(self):
                return iter(())

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()

            def cursor(self, name=None):
                self.cursor_instance.name = name
                return self.cursor_instance

        conn = Connection()
        self.assertEqual(list(_source_rows(conn)), [])
        query = conn.cursor_instance.query
        self.assertIn("ORDER BY vehicle_id, timestamp LIMIT %s", query)
        self.assertEqual(conn.cursor_instance.params, (ROW_LIMIT,))
        self.assertEqual(conn.cursor_instance.itersize, 10_000)
        for column in COPY_COLUMNS:
            self.assertIn(column, query)

    def test_match_requires_provenance_columns_stable_rows_and_exact_legacy_values(self):
        expected = {
            "row_count": 1_000_000,
            "extent": [108.8, 34.6, 110.9, 35.899982],
            "stable_id_checksum": "frozen",
        }
        valid = Fingerprint(
            1_000_000, expected["extent"], "frozen", 1_000_000,
            [*REQUIRED_TARGET_COLUMNS, "id"],
        )
        self.assertTrue(_matches_expected(valid, expected))
        self.assertFalse(_matches_expected(
            Fingerprint(1_000_000, expected["extent"], "frozen", 999_999, valid.columns),
            expected,
        ))
        self.assertFalse(_matches_expected(
            Fingerprint(1_000_000, expected["extent"], "frozen", 1_000_000, ["id"]),
            expected,
        ))

    def test_apply_holds_the_backup_inside_the_target_write_fence_and_never_cascades(self):
        source = inspect.getsource(_apply_restore)
        self.assertLess(source.index("pg_advisory_xact_lock"), source.index("IN SHARE MODE"))
        self.assertLess(source.index("IN SHARE MODE"), source.index("_backup_target"))
        self.assertLess(source.index("_backup_target"), source.index("IN ACCESS EXCLUSIVE MODE"))
        self.assertNotIn("CASCADE", source)
        self.assertIn('audit["backup"]', source)
        self.assertIn("locked_target_before", source)
        self.assertIn("analyze_warning", source)

    def test_legacy_attestation_is_required_and_pinned_for_real_formal(self):
        frozen = {
            "database": "track_test", "table": "trajectory_points",
            "row_count": 1_000_000, "extent": [108.8, 34.6, 110.9, 35.899982],
            "stable_id_checksum": "frozen",
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "required"):
                _load_legacy_attestation(Path(directory) / "missing.json", "deadbeef", frozen)

    def test_tampered_legacy_attestation_fails_closed_before_db_access(self):
        frozen = {
            "database": "track_test", "table": "trajectory_points",
            "row_count": 1_000_000, "extent": [108.8, 34.6, 110.9, 35.899982],
            "stable_id_checksum": "frozen",
        }
        with tempfile.TemporaryDirectory() as directory:
            sidecar = Path(directory) / "attestation.json"
            sidecar.write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                _load_legacy_attestation(sidecar, "0" * 64, frozen)

    def test_rss_one_keeps_real_gate_strict_and_dense_non_regressing(self):
        self.assertEqual(DENSE_RSS_RATIO_CEILING, 1.05)
        self.assertEqual(REAL_RSS_RATIO_CEILING, 0.70)

    def test_real_ledger_rejects_tampering_and_preserves_alternating_plan(self):
        plan = _real_plan(3, 7)
        self.assertEqual(len(plan), 24)
        self.assertEqual([item["side"] for item in plan[4:10]], [
            "baseline", "optimized", "optimized", "baseline", "baseline", "optimized",
        ])
        source = {
            "source_label": "baseline", "source_commit": "base", "source_hashes": {"x": "y"},
            "dirty_diff_sha256": "d", "harness_bundle_sha256": "h", "harness_files": {"a": "b"},
            "lock_sha256": "l", "baseline_archive_sha256": "a", "parameters": {"p": 1},
        }
        header = {"sources": {"baseline": source, "optimized": {**source, "source_label": "optimized"}}}
        ledger = {"version": "PERF-WINDOWS-R7-REAL-LEDGER-3", "header": header, "plan": plan, "entries": []}
        ledger["ledger_sha256"] = _ledger_digest(ledger)
        _validate_real_ledger(ledger, header, plan)
        ledger["header"]["sources"]["baseline"]["lock_sha256"] = "tampered"
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            _validate_real_ledger(ledger, header, plan)

    def test_real_ledger_resume_smoke_finishes_all_24_samples_in_order(self):
        plan = _real_plan(3, 7)
        common = {
            "dirty_diff_sha256": "d", "harness_bundle_sha256": "h", "harness_files": {"a": "b"},
            "lock_sha256": "l", "baseline_archive_sha256": "a", "parameters": {"p": 1},
        }
        sources = {
            side: {**common, "source_label": side, "source_commit": side, "source_hashes": {"x": side}}
            for side in ("baseline", "optimized")
        }
        baseline_result = {"case": "real", "candidate_count": 0, "pair_count": 0, "total_pairs": 0}
        result_hash = __import__("hashlib").sha256(__import__("json").dumps(baseline_result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            header = {"static": {"parameters": {"p": 1}}, "sources": sources, "baseline_archive": {"sha256": "a"}, "ledger_root": str(output_dir)}
            entries = []
            for sequence, item in enumerate(plan, start=1):
                sample = {"run_kind": item["run_kind"], "provenance": deepcopy(sources[item["side"]]), "result_snapshot_sha256": result_hash, "wall_seconds": 2.0 if item["side"] == "baseline" else 1.0, "peak_working_set_bytes": 200 if item["side"] == "baseline" else 100}
                if item["stage"] == "semantic":
                    full = {**sample, "snapshot": baseline_result, "result_snapshot": baseline_result, "snapshot_sha256": result_hash}
                    sample = _write_semantic_sidecar(output_dir / f"manual_bench_windows_real_semantic_{item['side']}_r7.json", full)
                    sample["run_kind"] = "semantic"
                elif item["stage"] == "phase0":
                    sample.update({"snapshot_sha256": result_hash, "phase_metrics": {"phases": {}}})
                entry = {"sequence": sequence, "plan": item, "sample": sample, "child_sample_sha256": "smoke"}
                entry["entry_sha256"] = __import__("hashlib").sha256(__import__("json").dumps(entry, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                entries.append(entry)
            ledger = {"version": "PERF-WINDOWS-R7-REAL-LEDGER-3", "header": header, "plan": plan, "entries": entries}
            ledger["ledger_sha256"] = _ledger_digest(ledger)
            _finalize_real_ledger(SimpleNamespace(result_dir=output_dir, warmups=3, runs=7), ledger, output_dir / "ledger.json")
            artifact = __import__("json").loads((output_dir / "manual_bench_windows_real_r7.json").read_text(encoding="utf-8"))
            self.assertLess(len(__import__("json").dumps(ledger).encode()), 1_000_000)
            self.assertNotIn("candidate_payload_ordered", __import__("json").dumps(ledger))
        self.assertEqual([entry["sequence"] for entry in artifact["run_ledger"]], list(range(1, 25)))
        self.assertTrue(artifact["assertions"]["run_counts"]["pass"])

    def test_semantic_sidecar_missing_or_tampered_fails_closed(self):
        sample = {"snapshot": {"a": 1}, "result_snapshot": {"candidate_count": 0, "pair_count": 0, "total_pairs": 0}, "snapshot_sha256": __import__("hashlib").sha256(b'{"a":1}').hexdigest(), "result_snapshot_sha256": __import__("hashlib").sha256(b'{"candidate_count":0,"pair_count":0,"total_pairs":0}').hexdigest(), "provenance": {"source_label": "baseline"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = _write_semantic_sidecar(root / "semantic.json", sample)
            self.assertEqual(_load_semantic_sidecar({"ledger_root": str(root)}, summary)["snapshot"], {"a": 1})
            (root / "semantic.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "tampered"):
                _load_semantic_sidecar({"ledger_root": str(root)}, summary)

    def test_canonical_semantic_digest_changes_for_field_or_order_change(self):
        original = [{"id": 1, "payload": "a"}, {"id": 2, "payload": "b"}]
        self.assertNotEqual(_canonical_digest(original), _canonical_digest(list(reversed(original))))
        self.assertNotEqual(_canonical_digest(original), _canonical_digest([{**original[0], "payload": "changed"}, original[1]]))


if __name__ == "__main__":
    unittest.main()
