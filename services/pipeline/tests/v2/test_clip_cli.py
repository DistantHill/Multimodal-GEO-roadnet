from __future__ import annotations

from .config_factory import pipeline_config

import unittest
from unittest.mock import patch

from track.v2.cli.run import main
from track.v2.config import PostgresConfig


class RunCliRefineReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = pipeline_config()

    def test_refine_id_is_a_standalone_source_mode(self) -> None:
        refine_id = "11111111-1111-1111-1111-111111111111"
        with (
            patch("sys.argv", ["track.v2.cli.run", "--refine-id", refine_id]),
            patch("track.v2.cli.run.load_startup_config", return_value=self.config),
            patch("track.v2.cli.run.init_logger"),
            patch("track.v2.cli.run.shutdown_logger"),
            patch("track.v2.cli.run.run_pipeline", return_value={}) as run_pipeline,
            patch("track.v2.cli.run._print_write_summary"),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        run_pipeline.assert_called_once_with(
            self.config,
            source_table=None,
            refine_id=refine_id,
        )

    def test_refine_id_rejects_table_override(self) -> None:
        refine_id = "11111111-1111-1111-1111-111111111111"
        with (
            patch("sys.argv", ["track.v2.cli.run", "--refine-id", refine_id, "--table", "other"]),
            patch("track.v2.cli.run.load_startup_config", return_value=self.config),
            self.assertRaises(SystemExit) as raised,
        ):
            main()

        self.assertEqual(raised.exception.code, 2)

    def test_old_ambiguous_flags_are_not_accepted(self) -> None:
        for flag in ("--skip-refine", "--run-id", "--replay-clips"):
            with self.subTest(flag=flag):
                with (
                    patch("sys.argv", ["track.v2.cli.run", flag, "value"]),
                    patch("track.v2.cli.run.load_startup_config", return_value=self.config),
                    self.assertRaises(SystemExit) as raised,
                ):
                    main()
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
