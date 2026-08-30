from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from track.v2.logger import _resolve_log_path


class LoggerPathTest(unittest.TestCase):
    def test_default_path_uses_timestamped_filename(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("track.v2.logger.datetime") as mocked_datetime,
        ):
            mocked_datetime.now.return_value = datetime(2026, 8, 31, 9, 8, 7, 654321)
            path = _resolve_log_path()

        self.assertEqual(path, Path("generated/20260831_090807_654321_log.txt"))

    def test_log_directory_keeps_timestamped_filename(self) -> None:
        with (
            patch.dict("os.environ", {"TRACK_LOG_DIR": "/data/logs"}, clear=True),
            patch("track.v2.logger.datetime") as mocked_datetime,
        ):
            mocked_datetime.now.return_value = datetime(2026, 8, 31, 9, 8, 7, 654321)
            path = _resolve_log_path()

        self.assertEqual(path, Path("/data/logs/20260831_090807_654321_log.txt"))

    def test_explicit_path_has_priority_over_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TRACK_LOG_PATH": "/data/logs/environment.log",
                "TRACK_LOG_DIR": "/data/logs",
            },
            clear=True,
        ):
            explicit = _resolve_log_path("custom/run.log")
            configured = _resolve_log_path()

        self.assertEqual(explicit, Path("custom/run.log"))
        self.assertEqual(configured, Path("/data/logs/environment.log"))


if __name__ == "__main__":
    unittest.main()
