from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, pipeline_config, postgres_config, projection_config, refine_config, runtime_config)

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import track.v2 as v2
from track.v2.trajectories.refine_trajectories import refine as _refine
from track.v2.errors import DataSourceCorrupted


_SPATIAL_CONTEXT = v2.create_spatial_context(
    projection_config(processing_srid=32650)
)


def _get_database(source, **kwargs):
    config = pipeline_config()
    field_path = kwargs.pop("field_config", None)
    if field_path is not None:
        from track.v2.config.field_mapping import load_field_config
        kwargs["field_config"] = load_field_config(field_path)
    else:
        kwargs["field_config"] = config.parser.fields
    kwargs.setdefault("runtime", config.runtime)
    return v2.get_database(source, **kwargs)


def refine(*args, **kwargs):
    kwargs.setdefault("config", refine_config())
    return _refine(*args, **kwargs)


def _make_csv(tmpdir: str, content: str) -> Path:
    path = Path(tmpdir) / "points.csv"
    path.write_text(content, encoding="utf-8")
    return path


class PublicApiTest(unittest.TestCase):
    def test_track_v2_exports_parser_and_refine_entrypoints(self) -> None:
        self.assertIs(
            v2.get_database,
            __import__(
                "track.v2.trajectories.read_source", fromlist=["get_database"],
            ).get_database,
        )
        self.assertTrue(callable(refine))
        self.assertTrue(hasattr(v2, "CsvSource"))
        self.assertTrue(hasattr(v2, "SourceBatch"))


class ParserQualityBehaviorTest(unittest.TestCase):
    def test_standard_time_string_is_unix_seconds_not_scaled_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td,
                "vid,lng,lat,included_angle,gps_speed,standard_time\n"
                "v1,116,39,90,36,2023-03-01 12:46:15\n",
            )
            handle = _get_database(v2.CsvSource(csv_path))
            try:
                point = list(handle.iter_vehicle_batches())[0]
            finally:
                handle.close()
            self.assertGreater(point.timestamp[0], 1_600_000_000)
            self.assertLess(point.timestamp[0], 1_800_000_000)

    def test_bad_optional_heading_and_speed_warn_but_keep_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td,
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116,39,bad,-5,1700000000000\n",
            )
            handle = _get_database(v2.CsvSource(csv_path))
            try:
                points = list(handle.iter_vehicle_batches())
                report = handle.get_report()
            finally:
                handle.close()
            self.assertEqual(len(points), 1)
            self.assertEqual(points[0].heading[0], 0.0)
            self.assertEqual(points[0].speed[0], 0.0)
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
                (Path(__file__).resolve().parents[2] / "config" / "field_specs.toml").read_text(
                    encoding="utf-8"
                ).replace("max_error_ratio = 1.0", "max_error_ratio = 0.4"),
                encoding="utf-8",
            )
            handle = _get_database(v2.CsvSource(csv_path), field_config=config_path)
            try:
                with self.assertRaises(DataSourceCorrupted):
                    list(handle.iter_vehicle_batches())
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
        handle = _get_database(source)
        try:
            points = list(handle.iter_vehicle_batches())
        finally:
            handle.close()
        self.assertEqual(len(points), 1)
        self.assertEqual([fraction for conn in connections for fraction in conn.declared_fractions], ["0.37"])
        self.assertTrue(all(conn.fraction == "0.37" for conn in connections))
        self.assertFalse(points[0].source_stable[0])
        self.assertTrue(all(not cursor.fetchall_called for conn in connections for cursor in conn.cursors))


class RefineContractTest(unittest.TestCase):
    def test_gap_split_rewrites_vehicle_id_to_subtrack_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            csv_path = _make_csv(
                td,
                "vid,lng,lat,included_angle,gps_speed,gps_time_a\n"
                "v1,116,39,90,36,1700000000000\n"
                "v1,116.001,39,90,36,1700000200000\n",
            )
            handle = _get_database(v2.CsvSource(csv_path))
            try:
                result = refine(
                    handle,
                    config=refine_config(max_gap_s=120.0),
                    spatial_context=_SPATIAL_CONTEXT,
                )
                vehicle_ids = [track.vehicle_id for track in result.subtracks]
            finally:
                handle.close()
            self.assertEqual(vehicle_ids, ["v1", "v1"])


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
        self.fraction = "0.37"
        self.declared_fractions = []

    def cursor(self, **_kwargs):
        cursor = _FakeCursor(self.columns, self.rows)
        cursor.connection = self
        cursor.name = _kwargs.get("name")
        self.cursors.append(cursor)
        return cursor

    @contextmanager
    def transaction(self):
        previous = self.fraction
        try:
            yield
        except BaseException:
            self.fraction = previous
            raise

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, columns: list[str], rows: list[tuple[str, ...]]) -> None:
        self.description = [(column,) for column in columns]
        self.rows = rows
        self.fetchall_called = False
        self.closed = False

    def execute(self, query: str, params=()) -> None:
        if query.startswith("SET LOCAL cursor_tuple_fraction"):
            self.connection.fraction = "1"
        elif "set_config" in query:
            self.connection.fraction = params[0]
        elif self.name:
            self.connection.declared_fractions.append(self.connection.fraction)

    def fetchone(self):
        return (self.connection.fraction,)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __iter__(self):
        return iter(self.rows)

    def fetchall(self):
        self.fetchall_called = True
        raise AssertionError("fetchall() is forbidden in Parser")

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
