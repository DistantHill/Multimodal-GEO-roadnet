from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from unittest.mock import MagicMock, patch

import track.v2 as v2
from track.v2.cli.run import run_pipeline as cli_run_pipeline
from track.v2.config import PostgresConfig
from track.v2.pipeline import PipelineInput, PipelineResult


class PipelineApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PostgresConfig(
            host="localhost", port=5432, database="track", user="track", password="",
        )

    def test_public_types_are_frozen_dataclasses(self) -> None:
        pipeline_input = PipelineInput(self.config, pg_source_table="raw_points")
        result = PipelineResult(stats={"detect_id": "detect-1"})

        self.assertTrue(is_dataclass(PipelineInput))
        self.assertTrue(is_dataclass(PipelineResult))
        self.assertIs(v2.PipelineInput, PipelineInput)
        self.assertIs(v2.PipelineResult, PipelineResult)
        with self.assertRaises(FrozenInstanceError):
            pipeline_input.table = "other"
        with self.assertRaises(FrozenInstanceError):
            result.stats = {}

    def test_cli_adapter_keeps_historical_signature_and_result_shape(self) -> None:
        pipeline_result = PipelineResult(stats={"detect_id": "detect-1"})
        reporter = MagicMock()
        with (
            patch("track.v2.cli.run.PipelineReporter", return_value=reporter),
            patch("track.v2.cli.run._run_pipeline", return_value=pipeline_result) as run,
        ):
            stats = cli_run_pipeline(
                self.config,
                pg_source_table="raw_points",
                table="target_points",
            )

        self.assertEqual(stats, pipeline_result.stats)
        run.assert_called_once_with(
            PipelineInput(
                db_config=self.config,
                pg_source_table="raw_points",
                table="target_points",
            ),
            emit=reporter,
        )
        reporter.close.assert_called_once_with()

    def test_cli_adapter_closes_reporter_when_core_fails(self) -> None:
        reporter = MagicMock()
        with (
            patch("track.v2.cli.run.PipelineReporter", return_value=reporter),
            patch(
                "track.v2.cli.run._run_pipeline",
                side_effect=RuntimeError("core failed"),
            ) as run,
            self.assertRaisesRegex(RuntimeError, "core failed"),
        ):
            cli_run_pipeline(self.config, pg_source_table="raw_points")

        run.assert_called_once_with(
            PipelineInput(db_config=self.config, pg_source_table="raw_points"),
            emit=reporter,
        )
        reporter.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
