from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from track.v2.cli.run import main
from track.v2.config import PostgresConfig


class RunCliReporterIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PostgresConfig(
            host="localhost", port=5432, database="track", user="track", password="",
        )

    def test_main_injects_reporter_and_closes_it_on_success(self) -> None:
        reporter = MagicMock()
        with (
            patch("sys.argv", ["track.v2.cli.run", "--clean-id", "11111111-1111-1111-1111-111111111111"]),
            patch("track.v2.cli.run.default_postgres_config", return_value=self.config),
            patch("track.v2.cli.run.init_logger"),
            patch("track.v2.cli.run.shutdown_logger"),
            patch("track.v2.cli.run.PipelineReporter", return_value=reporter),
            patch("track.v2.cli.run.run_pipeline", return_value={}) as run_pipeline,
            patch("track.v2.cli.run._print_write_summary"),
        ):
            self.assertEqual(main(), 0)

        run_pipeline.assert_called_once_with(
            self.config,
            csv_path=None,
            pg_source_table=None,
            clean_id="11111111-1111-1111-1111-111111111111",
            table="trajectory_points",
            config_path=None,
            emit=reporter,
        )
        reporter.close.assert_called_once_with()

    def test_main_closes_reporter_when_pipeline_fails(self) -> None:
        reporter = MagicMock()
        with (
            patch("sys.argv", ["track.v2.cli.run", "--clean-id", "11111111-1111-1111-1111-111111111111"]),
            patch("track.v2.cli.run.default_postgres_config", return_value=self.config),
            patch("track.v2.cli.run.init_logger"),
            patch("track.v2.cli.run.shutdown_logger"),
            patch("track.v2.cli.run.PipelineReporter", return_value=reporter),
            patch("track.v2.cli.run.run_pipeline", side_effect=RuntimeError("failed")),
        ):
            self.assertEqual(main(), 2)

        reporter.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
