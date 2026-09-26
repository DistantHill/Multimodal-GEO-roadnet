from __future__ import annotations

from .config_factory import pipeline_config

import unittest
from unittest.mock import patch

from track.v2.cli.run_pipeline import main
from track.v2.config import PostgresConfig


class RunCliV3Test(unittest.TestCase):
    def setUp(self) -> None:
        self.config = pipeline_config()

    def test_main_forwards_reused_refine_run(self) -> None:
        with (
            patch("sys.argv", ["track.v2.cli.run_pipeline", "--refine-id", "11111111-1111-1111-1111-111111111111"]),
            patch("track.v2.cli.run_pipeline.load_startup_config", return_value=self.config),
            patch("track.v2.cli.run_pipeline.init_logger"),
            patch("track.v2.cli.run_pipeline.shutdown_logger") as shutdown_logger,
            patch("track.v2.cli.run_pipeline.run_pipeline", return_value={}) as run_pipeline,
            patch("track.v2.cli.run_pipeline._print_write_summary"),
        ):
            self.assertEqual(main(), 0)

        run_pipeline.assert_called_once_with(
            self.config,
            source_table=None,
            refine_id="11111111-1111-1111-1111-111111111111",
        )
        shutdown_logger.assert_called_once_with()

    def test_main_rejects_processing_srid_override_for_new_run(self) -> None:
        with (
            patch(
                "sys.argv",
                ["track.v2.cli.run_pipeline", "--source-table", "raw_points", "--processing-srid", "32650"],
            ),
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(caught.exception.code, 2)

    def test_main_rejects_projection_override_for_reused_refine_run(self) -> None:
        with (
            patch(
                "sys.argv",
                [
                    "track.v2.cli.run_pipeline",
                    "--refine-id", "11111111-1111-1111-1111-111111111111",
                    "--processing-srid", "32650",
                ],
            ),
        ):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 2)

    def test_main_preserves_exit_code_and_cleanup_on_wrapper_failure(self) -> None:
        with (
            patch("sys.argv", ["track.v2.cli.run_pipeline", "--refine-id", "11111111-1111-1111-1111-111111111111"]),
            patch("track.v2.cli.run_pipeline.load_startup_config", return_value=self.config),
            patch("track.v2.cli.run_pipeline.init_logger"),
            patch("track.v2.cli.run_pipeline.shutdown_logger") as shutdown_logger,
            patch("track.v2.cli.run_pipeline.run_pipeline", side_effect=RuntimeError("failed")),
        ):
            self.assertEqual(main(), 2)

        shutdown_logger.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
