"""Pipeline TOML config loader tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from track.v2.config.pipeline_config import load_pipeline_config
from track.v2.errors import ConfigError
from track.v2.intersect2 import IntersectConfig
from track.v2.models import (
    CleanerConfig,
    ClipConfig,
    FittingConfig,
    NetworkConfig,
    RuntimeConfig,
)
from track.v2.pipeline import _should_flush_clean_batch


class DefaultPipelineConfigTest(unittest.TestCase):
    def test_builtin_toml_matches_dataclass_defaults(self):
        """The checked-in TOML must reproduce every dataclass default exactly."""
        pc = load_pipeline_config()
        self.assertEqual(pc.cleaner, CleanerConfig())
        self.assertEqual(pc.detect, IntersectConfig())
        self.assertEqual(pc.clip, ClipConfig())
        self.assertEqual(pc.fitting, FittingConfig())
        self.assertEqual(pc.network, NetworkConfig())
        self.assertEqual(pc.runtime, RuntimeConfig())


class PipelineConfigOverrideTest(unittest.TestCase):
    def test_section_key_override_keeps_other_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.toml"
            path.write_text("[cleaner]\nmax_gap_s = 999.0\n", encoding="utf-8")
            pc = load_pipeline_config(path)
            self.assertEqual(pc.cleaner.max_gap_s, 999.0)
            self.assertEqual(pc.cleaner.min_speed_mps, CleanerConfig().min_speed_mps)
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

    def test_cleaner_point_threshold_flushes_before_vehicle_limit(self):
        runtime = RuntimeConfig(
            mark_flush_every_n=2000,
            excluded_flush_threshold=2000,
            cleaner_flush_point_threshold=10,
        )

        self.assertFalse(_should_flush_clean_batch(
            pending_excluded_count=0,
            pending_point_count=9,
            vehicle_count=1,
            runtime=runtime,
        ))
        self.assertTrue(_should_flush_clean_batch(
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
        path = self._write("[cleaner]\nnot_a_field = 1\n")
        with self.assertRaises(ConfigError):
            load_pipeline_config(path)

    def test_wrong_type_raises(self):
        path = self._write("[runtime]\ndb_insert_batch = \"not-an-int\"\n")
        with self.assertRaises(ConfigError):
            load_pipeline_config(path)

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_pipeline_config(Path("/definitely/missing/pipeline.toml"))


if __name__ == "__main__":
    unittest.main()
