from __future__ import annotations

import unittest
from unittest.mock import patch

from track.v2.cli.run import main
from track.v2.config import PostgresConfig


class RunCliV3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PostgresConfig(
            host="localhost", port=5432, database="track", user="track", password="",
        )

    def test_main_forwards_reused_clean_run(self) -> None:
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
            source_table=None,
            clean_id="11111111-1111-1111-1111-111111111111",
            config_path=None,
            processing_srid=None,
        )
        shutdown_logger.assert_called_once_with()

    def test_main_forwards_processing_srid_for_new_run(self) -> None:
        with (
            patch(
                "sys.argv",
                ["track.v2.cli.run", "--source-table", "raw_points", "--processing-srid", "32650"],
            ),
            patch("track.v2.cli.run.default_postgres_config", return_value=self.config),
            patch("track.v2.cli.run.init_logger"),
            patch("track.v2.cli.run.shutdown_logger"),
            patch("track.v2.cli.run.run_pipeline", return_value={}) as run_pipeline,
            patch("track.v2.cli.run._print_write_summary"),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(run_pipeline.call_args.kwargs["processing_srid"], 32650)

    def test_main_rejects_projection_override_for_reused_clean_run(self) -> None:
        with (
            patch(
                "sys.argv",
                [
                    "track.v2.cli.run",
                    "--clean-id", "11111111-1111-1111-1111-111111111111",
                    "--processing-srid", "32650",
                ],
            ),
            patch("track.v2.cli.run.default_postgres_config", return_value=self.config),
        ):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)

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
