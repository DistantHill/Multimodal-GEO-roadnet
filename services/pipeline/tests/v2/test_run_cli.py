from __future__ import annotations

import unittest
from unittest.mock import patch

from track.v2.cli.run import main
from track.v2.config import PostgresConfig


class RunCliCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PostgresConfig(
            host="localhost", port=5432, database="track", user="track", password="",
        )

    def test_main_preserves_historical_wrapper_call_shape(self) -> None:
        with (
            patch("sys.argv", ["track.v2.cli.run", "--clean-id", "11111111-1111-1111-1111-111111111111"]),
            patch("track.v2.cli.run.default_postgres_config", return_value=self.config),
            patch("track.v2.cli.run.init_logger"),
            patch("track.v2.cli.run.shutdown_logger") as shutdown_logger,
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
        )
        shutdown_logger.assert_called_once_with()

    def test_main_preserves_exit_code_and_cleanup_on_wrapper_failure(self) -> None:
        with (
            patch("sys.argv", ["track.v2.cli.run", "--clean-id", "11111111-1111-1111-1111-111111111111"]),
            patch("track.v2.cli.run.default_postgres_config", return_value=self.config),
            patch("track.v2.cli.run.init_logger"),
            patch("track.v2.cli.run.shutdown_logger") as shutdown_logger,
            patch("track.v2.cli.run.run_pipeline", side_effect=RuntimeError("failed")),
        ):
            self.assertEqual(main(), 2)

        shutdown_logger.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
