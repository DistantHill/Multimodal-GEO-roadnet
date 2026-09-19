"""Pipeline TOML config loader tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from track.v2.config.pipeline_config import load_pipeline_config
from track.v2.errors import ConfigError
from track.v2.detect import IntersectConfig
from track.v2.models import (
    RefineConfig,
    ClipConfig,
    FittingConfig,
    NetworkConfig,
    ProjectionConfig,
    RuntimeConfig,
)
from track.v2.pipeline import _should_flush_refine_batch


class DefaultPipelineConfigTest(unittest.TestCase):
    def test_builtin_toml_matches_dataclass_defaults(self):
        """The checked-in TOML must reproduce every dataclass default exactly."""
        pc = load_pipeline_config()
        self.assertEqual(pc.projection, ProjectionConfig())
        self.assertEqual(pc.refine, RefineConfig())
        self.assertEqual(pc.detect, IntersectConfig())
        self.assertEqual(pc.clip, ClipConfig())
        self.assertEqual(pc.fitting, FittingConfig())
        self.assertEqual(pc.network, NetworkConfig())
        self.assertEqual(pc.runtime, RuntimeConfig())

    def test_columnar_runtime_defaults(self):
        runtime = load_pipeline_config().runtime
        self.assertEqual(runtime.duckdb_memory_limit, "1GB")
        self.assertEqual(runtime.duckdb_threads, 4)
        self.assertEqual(runtime.duckdb_max_temp_directory_size, "50GB")
        self.assertEqual(runtime.polars_threads, 4)
        self.assertEqual(runtime.parquet_target_file_size_bytes, 0)
        self.assertEqual(runtime.parquet_row_group_size_rows, 0)
        self.assertEqual(runtime.artifact_gc_grace_days, 7)


class PipelineConfigOverrideTest(unittest.TestCase):
    def test_section_key_override_keeps_other_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.toml"
            path.write_text("[refine]\nmax_gap_s = 999.0\n", encoding="utf-8")
            pc = load_pipeline_config(path)
            self.assertEqual(pc.refine.max_gap_s, 999.0)
            self.assertEqual(pc.refine.min_speed_mps, RefineConfig().min_speed_mps)
            self.assertEqual(pc.detect, IntersectConfig())

    def test_array_field_coerced_to_tuple(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.toml"
            path.write_text("[detect]\nspatial_window_size = [1.0, 2.0]\n", encoding="utf-8")
            pc = load_pipeline_config(path)
            self.assertEqual(pc.detect.spatial_window_size, (1.0, 2.0))

    def test_fallback_encodings_coerced_to_tuple(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.toml"
            path.write_text('[runtime]\nfallback_encodings = ["utf-8"]\n', encoding="utf-8")
            pc = load_pipeline_config(path)
            self.assertEqual(pc.runtime.fallback_encodings, ("utf-8",))

    def test_processing_srid_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.toml"
            path.write_text("[projection]\nprocessing_srid = 32650\n", encoding="utf-8")
            pc = load_pipeline_config(path)
            self.assertEqual(pc.projection.processing_srid, 32650)

    def test_refine_point_threshold_flushes_before_vehicle_limit(self):
        runtime = RuntimeConfig(
            mark_flush_every_n=2000,
            excluded_flush_threshold=2000,
            refine_flush_point_threshold=10,
        )

        self.assertFalse(_should_flush_refine_batch(
            pending_excluded_count=0,
            pending_point_count=9,
            vehicle_count=1,
            runtime=runtime,
        ))
        self.assertTrue(_should_flush_refine_batch(
            pending_excluded_count=0,
            pending_point_count=10,
            vehicle_count=1,
            runtime=runtime,
        ))


class PipelineConfigErrorTest(unittest.TestCase):
    def _write(self, content: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "pipeline.toml"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_unknown_section_raises(self):
        path = self._write("[bogus]\nx = 1\n")
        with self.assertRaises(ConfigError):
            load_pipeline_config(path)

    def test_unknown_key_raises(self):
        path = self._write("[refine]\nnot_a_field = 1\n")
        with self.assertRaises(ConfigError):
            load_pipeline_config(path)

    def test_removed_method_2_keys_report_migration(self):
        path = self._write(
            "[detect]\n"
            "use_orthogonal_candidates = true\n"
            "dbscan_eps_m_ortho = 100.0\n"
        )
        with self.assertRaisesRegex(
            ConfigError,
            "rename use_orthogonal_candidates to use_cross_track_candidates",
        ) as raised:
            load_pipeline_config(path)
        self.assertIn("remove dbscan_eps_m_ortho", str(raised.exception))

    def test_wrong_type_raises(self):
        path = self._write("[runtime]\ndb_insert_batch = \"not-an-int\"\n")
        with self.assertRaises(ConfigError):
            load_pipeline_config(path)

    def test_new_runtime_integer_limits_are_strict(self):
        invalid_values = (
            "duckdb_threads = true",
            "duckdb_threads = 5",
            "polars_threads = 0",
            "parquet_target_file_size_bytes = -1",
            "parquet_row_group_size_rows = -1",
            "artifact_gc_grace_days = 0",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                path = self._write(f"[runtime]\n{value}\n")
                with self.assertRaises(ConfigError):
                    load_pipeline_config(path)

    def test_duckdb_capacity_limits_reject_invalid_values(self):
        for value in ('"-1GB"', '"nanGB"', '"12 elephants"', '1'):
            with self.subTest(value=value):
                path = self._write(
                    f"[runtime]\nduckdb_max_temp_directory_size = {value}\n"
                )
                with self.assertRaises(ConfigError):
                    load_pipeline_config(path)

    def test_processing_srid_wrong_type_raises(self):
        path = self._write('[projection]\nprocessing_srid = "32650"\n')
        with self.assertRaises(ConfigError):
            load_pipeline_config(path)

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_pipeline_config(Path("/definitely/missing/pipeline.toml"))


if __name__ == "__main__":
    unittest.main()
