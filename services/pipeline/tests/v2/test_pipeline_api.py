from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from unittest.mock import patch

import track.v2 as v2
from track.v2.cli.run import run_pipeline as cli_run_pipeline
from track.v2.config import PostgresConfig
from track.v2.pipeline import PipelineInput, PipelineResult
from track.v2.pipeline_events import PipelineEvent


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
        with patch("track.v2.cli.run._run_pipeline", return_value=pipeline_result) as run:
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
            )
        )

    def test_cli_adapter_forwards_explicit_event_sink(self) -> None:
        pipeline_result = PipelineResult(stats={"detect_id": "detect-1"})
        events: list[PipelineEvent] = []
        with patch("track.v2.cli.run._run_pipeline", return_value=pipeline_result) as run:
            self.assertEqual(
                cli_run_pipeline(
                    self.config,
                    pg_source_table="raw_points",
                    emit=events.append,
                ),
                pipeline_result.stats,
            )

        run.assert_called_once_with(
            PipelineInput(db_config=self.config, pg_source_table="raw_points"),
            emit=events.append,
        )


if __name__ == "__main__":
    unittest.main()
