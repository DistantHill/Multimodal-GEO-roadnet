from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from track.v2.telemetry.console_logging import _resolve_log_path


class LoggerPathTest(unittest.TestCase):
    def test_missing_configured_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "log_path must be provided"):
            _resolve_log_path()

    def test_log_directory_keeps_timestamped_filename(self) -> None:
        with (
            patch("track.v2.telemetry.console_logging.datetime") as mocked_datetime,
            patch("track.v2.telemetry.console_logging.os.getpid", return_value=4242),
        ):
            mocked_datetime.now.return_value = datetime(2026, 8, 31, 9, 8, 7, 654321)
            path = _resolve_log_path("/data/logs")

        self.assertEqual(
            path,
            Path("/data/logs/20260831_090807_654321_pid-4242_log.txt"),
        )

    def test_same_timestamp_uses_pid_to_separate_processes(self) -> None:
        with (
            patch("track.v2.telemetry.console_logging.datetime") as mocked_datetime,
            patch("track.v2.telemetry.console_logging.os.getpid", side_effect=[101, 202]),
        ):
            mocked_datetime.now.return_value = datetime(2026, 8, 31, 9, 8, 7, 654321)
            first = _resolve_log_path("/data/logs")
            second = _resolve_log_path("/data/logs")

        self.assertNotEqual(first, second)
        self.assertIn("pid-101", first.name)
        self.assertIn("pid-202", second.name)

    def test_explicit_file_path_is_preserved(self) -> None:
        explicit = _resolve_log_path("custom/run.log")
        self.assertEqual(explicit, Path("custom/run.log"))


if __name__ == "__main__":
    unittest.main()
