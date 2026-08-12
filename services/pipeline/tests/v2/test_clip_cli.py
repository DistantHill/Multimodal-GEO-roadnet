from __future__ import annotations

import unittest
from unittest.mock import patch

from track.v2.cli.run import main
from track.v2.config import PostgresConfig


class ClipCliTest(unittest.TestCase):
    def test_replay_clips_is_a_standalone_data_source_mode(self) -> None:
        config = PostgresConfig(
            host="localhost",
            port=5432,
            database="track",
            user="track",
            password="",
        )
        sentinel_result = object()

        with (
            patch("sys.argv", ["track.v2.cli.run", "--replay-clips", "run-123"]),
            patch("track.v2.cli.run.default_postgres_config", return_value=config),
            patch("track.v2.cli.run.init_logger"),
            patch("track.v2.cli.run.shutdown_logger"),
            patch("track.v2.cli.run.replay_clips", return_value=sentinel_result) as replay,
            patch("track.v2.cli.run._print_clip_report") as print_report,
            patch("track.v2.cli.run.run_pipeline") as run_pipeline,
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        replay.assert_called_once_with(config, "run-123")
        print_report.assert_called_once_with(sentinel_result)
        run_pipeline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
