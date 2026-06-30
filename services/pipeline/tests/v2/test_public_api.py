from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import track.v2 as v2
from track.v2.errors import DataSourceCorrupted


def _make_csv(tmpdir: str, content: str) -> Path:
    path = Path(tmpdir) / "points.csv"
    path.write_text(content, encoding="utf-8")
    return path


class PublicApiTest(unittest.TestCase):
    def test_track_v2_exports_parser_and_cleaner_entrypoints(self) -> None:
        self.assertIs(v2.get_database, __import__("track.v2.sources", fromlist=["get_database"]).get_database)
        self.assertTrue(callable(v2.clean))
        self.assertTrue(hasattr(v2, "CsvSource"))
        self.assertTrue(hasattr(v2, "TrajectoryPoint"))


class ParserQualityBehaviorTest(unittest.TestCase):
    def test_standard_time_string_is_unix_seconds_not_scaled_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td,
                "vid,lng,lat,included_angle,gps_speed,standard_time\n"
                "v1,116,39,90,36,2023-03-01 12:46:15\n",
            )
            handle = v2.get_database(v2.CsvSource(csv_path))
            try:
                point = list(handle.iter_points())[0]
            finally:
                handle.close()
            self.assertGreater(point.timestamp, 1_600_000_000)
            self.assertLess(point.timestamp, 1_800_000_000)

    def test_bad_optional_heading_and_speed_warn_but_keep_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td,
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116,39,bad,-5,1700000000000\n",
            )
            handle = v2.get_database(v2.CsvSource(csv_path))
            try:
                points = list(handle.iter_points())
                report = handle.get_report()
            finally:
                handle.close()
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0].heading, 0.0)
            self.assertEqual(points[0].speed, 0.0)
            self.assertGreaterEqual(report.warn_count, 2)
            self.assertEqual(report.error_count, 0)

    def test_configured_error_ratio_threshold_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td,
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,,39,90,36,1700000000000\n"
                "v1,116,39,90,36,1700000001000\n",
            )
            config_path = Path(td) / "field_specs.toml"
            config_path.write_text(
                Path(v2.__file__).resolve().parent.joinpath("config/default_field_specs.toml").read_text(
                    encoding="utf-8"
                ).replace("max_error_ratio = 1.0", "max_error_ratio = 0.4"),
                encoding="utf-8",
            )
            handle = v2.get_database(v2.CsvSource(csv_path), field_config=config_path)
            try:
                with self.assertRaises(DataSourceCorrupted):
                    list(handle.iter_points())
            finally:
                handle.close()


class FakePostgresStreamingTest(unittest.TestCase):
    def test_fake_postgres_streams_without_fetchall_and_uses_run_local_ref_without_pk(self) -> None:
        columns = ["vid", "lng", "lat", "included_angle", "gps_speed", "gps_time_a"]
        rows = [("v1", "116", "39", "90", "36", "1700000000000")]
        connections: list[_FakeConnection] = []

        def factory(_config):
            conn = _FakeConnection(columns, rows)
            connections.append(conn)
            return conn

        source = v2.PostgresSource(
            config=_FakePgConfig(),
            query="select vid,lng,lat,included_angle,gps_speed,gps_time_a from points order by vid,gps_time_a",
            connection_factory=factory,
        )
        handle = v2.get_database(source)
        try:
            points = list(handle.iter_points())
        finally:
            handle.close()
        self.assertEqual(len(points), 1)
        self.assertFalse(points[0].point_ref.stable)
        self.assertTrue(all(not cursor.fetchall_called for conn in connections for cursor in conn.cursors))


class CleanerContractTest(unittest.TestCase):
    def test_gap_split_rewrites_vehicle_id_to_subtrack_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td,
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116,39,90,36,1700000000000\n"
                "v1,116.001,39,90,36,1700000200000\n",
            )
            handle = v2.get_database(v2.CsvSource(csv_path))
            try:
                result = v2.clean(handle, config=v2.CleanerConfig(max_gap_s=120.0))
                vehicle_ids = [point.vehicle_id for point in result.active.iter_points()]
            finally:
                handle.close()
            self.assertEqual(vehicle_ids, ["v1_seg01", "v1_seg02"])


class _FakePgConfig:
    def dsn(self) -> str:
        return "postgresql://fake"

    def safe_dsn(self) -> str:
        return "postgresql://fake"


class _FakeConnection:
    def __init__(self, columns: list[str], rows: list[tuple[str, ...]]) -> None:
        self.columns = columns
        self.rows = rows
        self.cursors: list[_FakeCursor] = []
        self.closed = False

    def cursor(self):
        cursor = _FakeCursor(self.columns, self.rows)
        self.cursors.append(cursor)
        return cursor

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, columns: list[str], rows: list[tuple[str, ...]]) -> None:
        self.description = [(column,) for column in columns]
        self.rows = rows
        self.fetchall_called = False
        self.closed = False

    def execute(self, _query: str, _params=()) -> None:
        return None

    def __iter__(self):
        return iter(self.rows)

    def fetchall(self):
        self.fetchall_called = True
        raise AssertionError("fetchall() is forbidden in Parser 0")

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
