"""Validate host-side directory settings through Compose's own resolver.

These tests only render configuration; they do not start containers.
"""

import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[3]


class ComposePathsTest(unittest.TestCase):
    def resolve_pipeline(self, mode, overrides):
        environment = dict(os.environ)
        for key in (
            "DATA_ROOT", "PARQUET_ROOT", "PIPELINE_LOG_DIR", "PIPELINE_LOG_PATH",
            "MODEL_ROOT", "TRACK_LOG_DIR", "TRACK_LOG_PATH",
        ):
            environment.pop(key, None)
        environment.update({
            "POSTGRES_PASSWORD": "compose-config-test-only",
            "MODEL_ROOT": "/host/models",
            **overrides,
        })
        result = subprocess.run(
            ["docker", "compose", "--env-file", os.devnull, "-f",
             str(ROOT / "infra/docker" / f"compose.{mode}.yml"),
             "config", "--format", "json"],
            env=environment, capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)["services"]["pipeline"]

    def resolve_config(self, mode):
        environment = {
            **os.environ,
            "POSTGRES_PASSWORD": "compose-config-test-only",
            "DATA_ROOT": "/host/runtime",
            "MODEL_ROOT": "/host/models",
        }
        result = subprocess.run(
            ["docker", "compose", "--env-file", os.devnull, "-f",
             str(ROOT / "infra/docker" / f"compose.{mode}.yml"),
             "config", "--format", "json"],
            env=environment, capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def assert_paths(self, pipeline, data, parquet, logs):
        mounts = {entry["target"]: entry for entry in pipeline["volumes"]}
        for target, expected in (("/data", data), ("/data/parquet", parquet), ("/data/logs", logs)):
            self.assertEqual(mounts[target]["type"], "bind")
            self.assertEqual(mounts[target]["source"], expected)
        self.assertEqual(pipeline["environment"]["TRACK_LOG_DIR"], "/data/logs")
        self.assertEqual(pipeline["environment"]["PARQUET_ROOT"], "/data/parquet")
        self.assertNotIn("TRACK_LOG_PATH", pipeline["environment"])

    def test_default_subdirectories_follow_host_data_root(self):
        for mode in ("dev", "prod"):
            with self.subTest(mode=mode):
                pipeline = self.resolve_pipeline(mode, {"DATA_ROOT": "/host/runtime"})
                self.assert_paths(pipeline, "/host/runtime", "/host/runtime/parquet", "/host/runtime/logs")

    def test_external_directories_do_not_change_container_paths(self):
        for mode in ("dev", "prod"):
            with self.subTest(mode=mode):
                pipeline = self.resolve_pipeline(mode, {
                    "DATA_ROOT": "/host/runtime",
                    "PARQUET_ROOT": "/host/column data",
                    "PIPELINE_LOG_DIR": "/host/pipeline logs",
                    # The removed Compose option must not override log naming.
                    "PIPELINE_LOG_PATH": "/old/fixed.log",
                })
                self.assert_paths(pipeline, "/host/runtime", "/host/column data", "/host/pipeline logs")

    def test_empty_overrides_use_data_subdirectories(self):
        for mode in ("dev", "prod"):
            with self.subTest(mode=mode):
                pipeline = self.resolve_pipeline(mode, {
                    "DATA_ROOT": "/host/runtime", "PARQUET_ROOT": "", "PIPELINE_LOG_DIR": "",
                })
                self.assert_paths(pipeline, "/host/runtime", "/host/runtime/parquet", "/host/runtime/logs")

    def test_development_defaults_without_path_settings(self):
        self.assert_paths(
            self.resolve_pipeline("dev", {}),
            "/data/tmp/road-network", "/data/tmp/road-network/parquet", "/data/tmp/road-network/logs",
        )

    def test_databases_are_external_services(self):
        for mode in ("dev", "prod"):
            with self.subTest(mode=mode):
                config = self.resolve_config(mode)
                self.assertNotIn("postgres", config["services"])
                self.assertNotIn("qdrant", config["services"])
                self.assertTrue(config["networks"]["roadnet_services"]["external"])
                self.assertEqual(config["networks"]["roadnet_services"]["name"], "roadnet-services")
                for service in ("backend", "pipeline"):
                    self.assertIn("default", config["services"][service]["networks"])
                    self.assertIn("roadnet_services", config["services"][service]["networks"])
                self.assertNotIn("roadnet_services", config["services"]["frontend"]["networks"])


if __name__ == "__main__":
    unittest.main()
