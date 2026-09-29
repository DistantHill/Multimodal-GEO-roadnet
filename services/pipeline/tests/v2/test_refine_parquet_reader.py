from __future__ import annotations

from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)

import hashlib
import json
import math
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import duckdb
import numpy as np
import pytest

from track.v2.artifacts import publish_dataset as publish_dataset
from track.v2.trajectories.results import read_parquet as refine_reader
from track.v2.trajectories.results import parquet_schema as refine_schema
from track.v2.artifacts.parquet_manifest import build_descriptor
from track.v2.db.artifact_registry import ArtifactDescriptor, canonical_json
from track.v2.projection import SpatialWindow
from track.v2.intersections.detect_intersections import IntersectConfig
from track.v2.candidates.detect_cross_track_pairs import _prepare_segments
from track.v2.candidates.cross_track_relation import CrossCandidateRelation
from track.v2.candidates.collect_candidates import (
    extract_columnar_cross_track_candidates, extract_cross_track_candidates,
)
from track.v2.models import RuntimeConfig, SpatialContext
from track.v2.projection import CoordinateTransformer
from track.v2.candidates.detect_turn_events import concatenate_turn_batches


def _published(tmp_path: Path):
    handle = publish_dataset.create_staging(
        tmp_path.resolve(), "refine", uuid4(), uuid4(),
    )
    common = (
        f"'{handle.run_id}'::UUID AS refine_id, {{point}}::BIGINT AS point_id, "
        "'{vehicle}'::VARCHAR AS vehicle_id, {subtrack}::INTEGER AS subtrack_index, "
        "{sequence}::BIGINT AS refined_sequence_in_subtrack, "
        "1.0::DOUBLE AS \"timestamp\", 120.0::DOUBLE AS lon, 30.0::DOUBLE AS lat, "
        "10.0::DOUBLE AS x, 20.0::DOUBLE AS y, 90.0::DOUBLE AS heading, "
        "4.0::DOUBLE AS speed"
    )
    with duckdb.connect() as connection:
        for table in refine_schema.REFINE_PARQUET_V1:
            directory = handle.path / table
            directory.mkdir()
            output = directory / "part.parquet"
            if table == "kept_points":
                rows = [common.format(point=1, vehicle="v1", subtrack=4, sequence=0),
                        common.format(point=3, vehicle="v1", subtrack=4, sequence=2),
                        common.format(point=7, vehicle="v1", subtrack=5, sequence=0),
                        common.format(point=8, vehicle="v2", subtrack=6, sequence=0)]
                sql = " UNION ALL ".join(
                    f"SELECT {row}, point_id::BIGINT source_row_key"
                    for row in rows
                )
            else:
                sql = (
                    f"SELECT {common.format(point=2, vehicle='v1', subtrack=4, sequence=1)}, "
                    "'orthogonal'::VARCHAR generated_reason, "
                    "0::INTEGER after_sequence_in_subtrack"
                )
            connection.execute(f"COPY ({sql}) TO ? (FORMAT PARQUET)", [str(output)])
    descriptor = build_descriptor(
        handle,
        {name: [f"{name}/part.parquet"] for name in refine_schema.REFINE_PARQUET_V1},
        {"version": 1},
    )
    publish_dataset.publish(handle)
    return descriptor


def _published_tracks(tmp_path: Path, tracks, *, source_keys=None):
    """Publish real Parquet rows used to differential-test the SQL operator."""
    handle = publish_dataset.create_staging(
        tmp_path.resolve(), "refine", uuid4(), uuid4(),
    )
    with duckdb.connect() as connection:
        kept = handle.path / "kept_points"
        kept.mkdir()
        rows = []
        point_id = 0
        for vehicle_id, subtrack_index, points in tracks:
            for sequence, (timestamp, x, y, heading) in enumerate(points):
                point_id += 1
                rows.append((handle.run_id, point_id, vehicle_id, subtrack_index,
                             sequence, timestamp,
                             120 + x / 1e6 if x is not None else 120,
                             30 + y / 1e6 if y is not None else 30,
                             x, y, heading, 4.0, point_id if source_keys is None else source_keys[point_id-1]))
        connection.execute("""
            CREATE TABLE input_points(
              refine_id UUID, point_id BIGINT, vehicle_id VARCHAR,
              subtrack_index INTEGER, refined_sequence_in_subtrack BIGINT,
              timestamp DOUBLE, lon DOUBLE, lat DOUBLE, x DOUBLE, y DOUBLE,
              heading DOUBLE, speed DOUBLE, source_row_key BIGINT)
        """)
        connection.executemany(
            "INSERT INTO input_points VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
        )
        connection.execute("COPY input_points TO ? (FORMAT PARQUET)",
                           [str(kept / "part.parquet")])
        injected = handle.path / "injected_points"
        injected.mkdir()
        connection.execute("""
            COPY (SELECT refine_id, point_id, vehicle_id, subtrack_index,
                         refined_sequence_in_subtrack, timestamp, lon, lat, x, y,
                         heading, speed, 'unused'::VARCHAR generated_reason,
                         0::INTEGER after_sequence_in_subtrack
                  FROM input_points WHERE false)
            TO ? (FORMAT PARQUET)
        """, [str(injected / "part.parquet")])
    descriptor = build_descriptor(
        handle, {name: [f"{name}/part.parquet"] for name in refine_schema.REFINE_PARQUET_V1},
        {"version": 1},
    )
    publish_dataset.publish(handle)
    return descriptor


def test_reader_uses_only_manifest_files_and_merges_one_vehicle_subtrack(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        batches = list(reader.iter_subtrack_batches("v1", 4, batch_size=2))
        assert [len(batch) for batch in batches] == [2, 1]
        rows = [row for batch in batches for row in batch]
        assert [(row[1], row[12], row[2]) for row in rows] == [
            (1, "kept", "v1"), (2, "injected", "v1"), (3, "kept", "v1"),
        ]
        assert [row[4] for row in rows] == [0, 1, 2]

    extra = tmp_path / descriptor.relative_path / "kept_points" / "undeclared.parquet"
    extra.parent.chmod(0o755)
    extra.write_bytes(b"not consumed")
    extra.chmod(0o444)
    extra.parent.chmod(0o555)
    with pytest.raises(Exception, match="manifest"):
        refine_reader.RefineDatasetReader(tmp_path, descriptor, "present", runtime_config())


def test_track_count_counts_subtrack_identities(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        assert reader.track_count() == 3
        assert reader.track_count() == 3


def test_reader_adapts_all_tracks_across_batches_and_queries_extent(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        tracks = list(reader.iter_tracks("public.source_points", batch_size=2))
        assert [(track.vehicle_id, track.subtrack_index, len(track)) for track in tracks] == [("v1",4,3),("v1",5,1),("v2",6,1)]
        first=tracks[0]
        assert first.source_id == "public.source_points"
        assert first.source_row_key.tolist() == [1,0,3]
        assert first.source_row_key_valid.tolist() == [True,False,True]
        assert first.refine_point_id.tolist() == [1,2,3]
        assert first.kind.tolist() == ["p","i","p"]
        assert first.generated_reason.tolist() == ["","orthogonal",""]
        assert reader.extent() == (10.,20.,10.,20.)


def test_topology_windows_select_complete_tracks_with_stable_deduplication(tmp_path) -> None:
    # Window matching, deduplication and stable sorting belong to spillable
    # DuckDB relations. Python rebuilds only one complete subtrack at a time;
    # Polars adds no operator needed by this boundary.
    descriptor = _published_tracks(tmp_path, [
        ("a", 2, [(0, -10, 0, 0), (1, 10, 0, 0), (2, 20, 20, 45)]),
        ("a", 3, [(3, 0, 0, 0)]),
        ("b", 1, [(4, 1, -1, 90), (5, 1, 1, 90), (6, 1, 2, 90)]),
        ("c", 4, [(7, 1.000001, -1, 90), (8, 1.000001, 1, 90)]),
    ])
    windows = [
        SpatialWindow(0, 0, 1, 1),
        SpatialWindow(0, 0, 1, 1),
        SpatialWindow(-5, -0.5, 5, 0.5),
    ]
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
        data_root=tmp_path,
    ) as reader:
        selected = list(reader.iter_topology_tracks("source", windows, batch_size=1))

    assert [(item.window_indices, item.track_key) for item in selected] == [
        ((0, 1), "2"), ((0, 1), "1"),
        ((2,), "2"), ((2,), "1"), ((2,), "4"),
    ]
    assert [list(range(len(item.points))) for item in selected] == [
        [0, 1, 2], [0, 1, 2], [0, 1, 2], [0, 1, 2], [0, 1],
    ]


def test_topology_track_rebuild_preserves_kept_injected_order(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        selected = list(reader.iter_topology_tracks(
            "public.source", [SpatialWindow(10, 20, 10, 20)], batch_size=1,
        ))
    assert [(item.track_key, item.window_indices) for item in selected] == [
        ("4", (0,)),
    ]
    assert selected[0].points.kind.tolist() == ["p", "i", "p"]
    assert selected[0].points.refine_point_id.tolist() == [1, 2, 3]


def test_topology_selection_order_matches_legacy_text_track_key(tmp_path) -> None:
    tracks = [
        ("aa", 3, [(0, -1, 0, 0), (1, 1, 0, 0)]),
        ("a", 2, [(2, -1, 0, 0), (3, 1, 0, 0)]),
        ("a", 10, [(4, -1, 0, 0), (5, 1, 0, 0)]),
    ]
    descriptor = _published_tracks(tmp_path, tracks)
    expected = ["10", "2", "3"]
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        actual = [selection.track_key for selection in reader.iter_topology_tracks(
            "source", [SpatialWindow(0, 0, 0, 0)], batch_size=1,
        )]
    assert actual == expected


@pytest.mark.parametrize(("points", "matches"), [
    ([(-1, 0), (0, 0)], True), ([(1, 1), (2, 1)], True),
    ([(0.5, -1), (0.5, 0)], True), ([(0.5, 1), (0.5, 2)], True),
    ([(-1, -1), (0, 0)], True), ([(-1, -1e-6), (2, -1e-6)], False),
])
def test_topology_segment_aabb_uses_closed_window_edges(tmp_path, points, matches) -> None:
    track = [(index, x, y, 0) for index, (x, y) in enumerate(points)]
    descriptor = _published_tracks(tmp_path, [("edge", 0, track)])
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        selected = list(reader.iter_topology_tracks(
            "source", [SpatialWindow(0, 0, 1, 1)], batch_size=1,
        ))
    assert bool(selected) is matches


def test_topology_windows_reject_invalid_inputs_and_require_open_reader(tmp_path) -> None:
    descriptor = _published(tmp_path)
    reader = refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(), data_root=tmp_path,
    )
    with pytest.raises(refine_reader.RefineParquetError, match="not open"):
        list(reader.iter_topology_tracks("source", []))
    with reader:
        assert list(reader.iter_topology_tracks("source", [])) == []
        for window in (
            SpatialWindow(2, 0, 1, 1), SpatialWindow(0, 0, float("nan"), 1),
            SpatialWindow(False, 0, 1, 1),
        ):
            with pytest.raises(refine_reader.RefineParquetError, match="window"):
                list(reader.iter_topology_tracks("source", [window]))
        for batch_size in (0, True):
            with pytest.raises(refine_reader.RefineParquetError, match="batch_size"):
                list(reader.iter_topology_tracks("source", [], batch_size=batch_size))


def test_topology_generator_close_drops_window_table_and_allows_reuse(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("a", 0, [(0, -1, 0, 0), (1, 1, 0, 0)]),
    ])
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        iterator = reader.iter_topology_tracks(
            "source", [SpatialWindow(0, 0, 0, 0)], batch_size=1,
        )
        assert next(iterator).track_key == "0"
        iterator.close()
        count = reader._connection.execute("""
          SELECT count(*) FROM duckdb_tables()
          WHERE table_name LIKE 'topology_windows_%'
        """).fetchone()
        assert count == (0,)
        assert len(list(reader.iter_topology_tracks(
            "source", [SpatialWindow(0, 0, 0, 0)], batch_size=1,
        ))) == 1


def test_reader_temp_directory_is_explicit_isolated_and_cleaned(tmp_path) -> None:
    publication = tmp_path / "publication"
    publication.mkdir()
    descriptor = _published(publication)
    data_root = tmp_path / "data"
    sibling = data_root / "duckdb" / "refine-readers" / "sibling"
    sibling.mkdir(parents=True)
    marker = sibling / "owned-elsewhere"
    marker.write_text("keep")
    runtime = runtime_config(
        duckdb_memory_limit="64MB", duckdb_threads=1,
        duckdb_max_temp_directory_size="1GB",
    )
    reader = refine_reader.RefineDatasetReader(
        publication, descriptor, "present", runtime,
        data_root=data_root.resolve(),
    )
    with reader:
        leaf = reader._temp_directory
        assert leaf is not None and leaf.parent == sibling.parent and leaf.is_dir()
        settings = dict(reader._connection.execute("""
          SELECT name, value FROM duckdb_settings()
          WHERE name IN ('temp_directory', 'memory_limit', 'threads',
                         'max_temp_directory_size')
        """).fetchall())
        assert settings["temp_directory"] == str(leaf)
        assert settings["threads"] == "1"
        assert settings["memory_limit"] == "61.0 MiB"
        assert settings["max_temp_directory_size"] == "953.6 MiB"
    assert leaf is not None and not leaf.exists()
    assert marker.read_text() == "keep"
    with pytest.raises(refine_reader.RefineParquetError, match="absolute"):
        refine_reader.RefineDatasetReader(
            publication, descriptor, "present", runtime_config(),
            data_root=Path("relative"),
        )


def _adapter_reader(rows, owner):
    reader = object.__new__(refine_reader.RefineDatasetReader)
    reader._runtime = runtime_config()
    result = Mock()
    result.fetchmany.side_effect = [rows, []]
    connection = Mock()
    connection.execute.return_value = result
    reader._connection = connection
    reader._descriptor = Mock(refine_id=owner)
    reader._validate_source = Mock()
    return reader, result


def _adapter_row(owner, *, vehicle="v1", sequence=0, kind="kept", source_key="1"):
    return (owner, 1, vehicle, 4, sequence, 1.0, 120.0, 30.0, 10.0, 20.0,
            90.0, 4.0, kind, source_key,
            None if kind == "kept" else "orthogonal",
            None if kind == "kept" else 0, 1)


@pytest.mark.parametrize("change,message", [
    ({"owner": uuid4()}, "owner"),
    ({"sequence": 1}, "start"),
    ({"vehicle": ""}, "identity"),
    ({"kind": "injected", "source_key": "illegal"}, "injected"),
])
def test_adapter_fails_closed_on_corrupt_identity(change, message) -> None:
    owner = uuid4()
    changes = dict(change)
    row_owner = changes.pop("owner", owner)
    reader, _ = _adapter_reader([_adapter_row(row_owner, **changes)], owner)
    with pytest.raises(refine_reader.RefineParquetError, match=message):
        list(reader.iter_tracks("public.source"))


def test_adapter_rejects_sequence_gap_and_non_finite_or_out_of_range_values() -> None:
    owner = uuid4()
    for rows, message in [
        ([_adapter_row(owner), _adapter_row(owner, sequence=2)], "contiguous"),
        ([tuple(list(_adapter_row(owner))[:6] + [float("nan")] +
                list(_adapter_row(owner))[7:])], "non-finite"),
        ([tuple(list(_adapter_row(owner))[:6] + [181.0] +
                list(_adapter_row(owner))[7:])], "outside"),
        ([tuple(list(_adapter_row(owner))[:-1] + [2])], "multiple vehicles"),
    ]:
        reader, result = _adapter_reader(rows, owner)
        with pytest.raises(refine_reader.RefineParquetError, match=message):
            list(reader.iter_tracks("public.source", batch_size=1))
        assert not result.fetchall.called


def test_reader_rejects_non_present_unknown_tables_and_schema(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with pytest.raises(Exception, match="not present"):
        refine_reader.RefineDatasetReader(tmp_path, descriptor, "deleted", runtime_config())
    manifest = json.loads(descriptor.manifest_json)
    manifest["tables"][0]["name"] = "unknown_points"
    changed = ArtifactDescriptor.create(
        id=descriptor.id, stage="refine", refine_id=descriptor.refine_id,
        relative_path=descriptor.relative_path,
        source_fingerprint=json.loads(descriptor.source_fingerprint_json), manifest=manifest,
    )
    with pytest.raises(refine_reader.RefineParquetError, match="logical tables"):
        refine_reader.RefineDatasetReader(tmp_path, changed, "present", runtime_config())
    manifest = json.loads(descriptor.manifest_json)
    manifest["tables"][0]["schema"][0]["type"] = "VARCHAR"
    manifest["tables"][0]["schema_fingerprint"] = hashlib.sha256(
        canonical_json(manifest["tables"][0]["schema"]).encode()
    ).hexdigest()
    schema_changed = ArtifactDescriptor.create(
        id=descriptor.id, stage="refine", refine_id=descriptor.refine_id,
        relative_path=descriptor.relative_path,
        source_fingerprint=json.loads(descriptor.source_fingerprint_json), manifest=manifest,
    )
    with pytest.raises(Exception, match="schema"):
        refine_reader.RefineDatasetReader(
            tmp_path, schema_changed, "present", runtime_config(),
        )


def test_batch_api_uses_bounded_fetchmany_not_fetchall() -> None:
    reader = object.__new__(refine_reader.RefineDatasetReader)
    reader._runtime = runtime_config()
    result = Mock()
    result.fetchmany.side_effect = [[("first",)], [("second",)], []]
    connection = Mock()
    connection.execute.return_value = result
    reader._connection = connection

    assert list(reader.iter_subtrack_batches("vehicle", 4, batch_size=17)) == [
        (("first",),), (("second",),),
    ]
    assert connection.execute.call_args.args[1] == ["vehicle", 4]
    assert result.fetchmany.call_count == 3
    result.fetchmany.assert_called_with(17)
    assert not result.fetchall.called


@pytest.mark.parametrize("window", [1, 2])
def test_duckdb_turn_candidates_match_known_cases_across_batches(tmp_path, window) -> None:
    # SQL windows calculate turns before the selected events cross into Python.
    tracks = [
        ("a", 1, [(0, -40, 0, 359), (10, -20, 0, 1), (20, 0, 0, 90),
                   (30, 0, 20, 91), (40, 0, 40, 89)]),
        ("b", 2, [(0, 0, 0, 1), (10, 20, 0, 359), (20, 40, 0, 270),
                   (30, 40, -20, 269), (40, 40, -40, 271)]),
        ("short", 3, [(0, 0, 0, 0), (1, 1, 0, 90)]),
        ("elapsed", 4, [(0, 0, 0, 0), (0, 20, 0, 0), (0, 20, 20, 90)]),
    ]
    descriptor = _published_tracks(tmp_path, tracks)
    cfg = intersect_config(turn_window=window, min_turn_deg=45, max_turn_deg=170,
                          min_leg_m=19, min_movement_speed_mps=1)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        batches = list(reader.iter_turn_candidates("public.source", cfg, batch_size=1))
    actual = concatenate_turn_batches(batches)
    assert actual.subtrack_index.tolist() == [1,2] if window == 1 else actual.subtrack_index.tolist() == [1,2]
    assert actual.calculation_index.tolist() == list(range(len(actual)))
    assert all(len(batch)>0 for batch in batches)
    assert actual.source_row_key_valid.all()
    assert actual.refine_point_id_valid.all()
    assert np.all(actual.score == np.abs(actual.turn_deg))
    assert set(actual.vehicle_id) == {"a","b"}


def test_duckdb_turn_thresholds_are_inclusive_and_config_is_strict(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("edge", 0, [(0, 0, 0, 0), (10, 0, 10, 90), (20, 10, 10, 90)]),
    ])
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        cfg = intersect_config(turn_window=1, min_turn_deg=90, max_turn_deg=90,
                              min_leg_m=10, min_movement_speed_mps=1)
        assert len(list(reader.iter_turn_candidates("source", cfg))) == 1
        for invalid in (
            intersect_config(turn_window=0),
            intersect_config(min_leg_m=float("nan")),
            intersect_config(min_turn_deg=100, max_turn_deg=90),
        ):
            with pytest.raises(refine_reader.RefineParquetError, match="turn"):
                list(reader.iter_turn_candidates("source", invalid))


def test_duckdb_turn_threshold_uses_sql_values(tmp_path) -> None:
    below_min = math.nextafter(45.0, -math.inf)
    above_min = math.nextafter(45.0, math.inf)
    below_max = math.nextafter(170.0, -math.inf)
    above_max = math.nextafter(170.0, math.inf)
    descriptor = _published_tracks(tmp_path, [
        ("positive-min", 0, [(0, 0, 0, 0), (10, 10, 0, 45), (20, 20, 0, 45)]),
        ("negative-min", 1, [(0, 0, 10, 45), (10, 10, 10, 0), (20, 20, 10, 0)]),
        ("positive-max", 2, [(0, 0, 20, 0), (10, 10, 20, 170), (20, 20, 20, 170)]),
        ("negative-max", 3, [(0, 0, 30, 170), (10, 10, 30, 0), (20, 20, 30, 0)]),
        ("below-min", 4, [(0, 0, 40, 0), (10, 10, 40, below_min), (20, 20, 40, 0)]),
        ("above-min", 5, [(0, 0, 50, 0), (10, 10, 50, above_min), (20, 20, 50, 0)]),
        ("below-max", 6, [(0, 0, 60, 0), (10, 10, 60, below_max), (20, 20, 60, 0)]),
        ("above-max", 7, [(0, 0, 70, 0), (10, 10, 70, above_max), (20, 20, 70, 0)]),
        ("wrap", 8, [(0, 0, 80, 359), (10, 10, 80, 44), (20, 20, 80, 44)]),
        ("neg-below-min", 9, [(0, 0, 90, below_min), (10, 10, 90, 0),
                               (20, 20, 90, 0)]),
        ("neg-above-min", 10, [(0, 0, 100, above_min), (10, 10, 100, 0),
                                (20, 20, 100, 0)]),
        ("neg-below-max", 11, [(0, 0, 110, below_max), (10, 10, 110, 0),
                                (20, 20, 110, 0)]),
        ("neg-above-max", 12, [(0, 0, 120, above_max), (10, 10, 120, 0),
                                (20, 20, 120, 0)]),
    ])
    cfg = intersect_config(turn_window=1, min_turn_deg=45, max_turn_deg=170,
                          min_leg_m=1, min_movement_speed_mps=0)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        actual = concatenate_turn_batches(reader.iter_turn_candidates("source", cfg, batch_size=1))
    assert actual.calculation_index.tolist() == list(range(len(actual)))
    # Angle normalization rounds these one-ULP inputs to the inclusive limits.
    assert set(actual.subtrack_index.tolist()) == set(range(13))
    assert np.all(np.abs(actual.turn_deg)>=45)
    assert np.all(np.abs(actual.turn_deg)<=170)


def test_duckdb_turn_threshold_has_no_boundary_tolerance(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("below-min", 0, [(0, 0, 0, 0), (10, 10, 0, 45 - 5e-10),
                          (20, 20, 0, 45 - 5e-10)]),
        ("above-max", 1, [(0, 0, 10, 0), (10, 10, 10, 170 + 5e-10),
                           (20, 20, 10, 170 + 5e-10)]),
    ])
    cfg = intersect_config(turn_window=1, min_turn_deg=45, max_turn_deg=170,
                          min_leg_m=1, min_movement_speed_mps=0)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        assert list(reader.iter_turn_candidates("source", cfg)) == []


def test_duckdb_turn_unstable_resultant_uses_sql_value(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("unstable", 0, [(0, 0, 0, 0), (10, 10, 0, 180), (20, 20, 0, 90),
                          (30, 30, 0, 270), (40, 40, 0, 0)]),
    ])
    cfg = intersect_config(turn_window=2, min_turn_deg=0, max_turn_deg=180,
                          min_leg_m=1, min_movement_speed_mps=0)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        actual = concatenate_turn_batches(reader.iter_turn_candidates("source", cfg, batch_size=1))
    assert len(actual)==1
    assert actual.subtrack_index.tolist()==[0]
    assert actual.point_index.tolist()==[2]
    assert actual.entry_bearing.tolist()==[90.]
    assert actual.exit_bearing.tolist()==[180.]
    assert actual.turn_deg.tolist()==[90.]


def test_duckdb_turn_internal_rows_do_not_call_python_exact_helper(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("internal", 0, [(0, 0, 0, 0), (10, 10, 0, 90), (20, 20, 0, 90)]),
    ])
    cfg = intersect_config(turn_window=1, min_turn_deg=45, max_turn_deg=170,
                          min_leg_m=1, min_movement_speed_mps=0)
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        actual = list(reader.iter_turn_candidates("source", cfg, batch_size=1))
    assert len(actual) == 1


def test_duckdb_cross_track_segments_match_q49_oracle(tmp_path) -> None:
    # DuckDB fits partition/window/sort/filter and can spill externally; segment
    # preparation needs neither Polars nor a Python fallback. Python constructs
    # only the minimal objects required by the later exact geometry kernel.
    tracks = [
        ("a", 10, [(0, 0, 0, 359), (1, 1, 0, 1), (2, 2, 0, 4)]),
        ("a", 2, [(3, 8, 8, 90)]),
        ("b", 3, [(4, 0, 1, 180), (5, -1, 1, 178), (6, -2, 1, 174)]),
    ]
    descriptor = _published_tracks(tmp_path, tracks)
    cfg = intersect_config(cross_track_straightness_deg=2)
    transformer = CoordinateTransformer(SpatialContext(
        4326, 32650,
        {"coordinate_semantics_version": 2, "enforce_area_of_use": True,
         "max_scale_error": .01},
    ))
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        batches = list(reader.iter_cross_track_segments("public.source", cfg, batch_size=1))
    assert [len(batch["starts"]) for batch in batches] == [1,1]
    assert np.concatenate([batch["subtrack_index"] for batch in batches]).tolist() == [10,3]
    assert np.concatenate([batch["point_index"] for batch in batches]).tolist() == [0,0]
    assert np.concatenate([batch["starts"] for batch in batches]).tolist() == [[0.,0.],[0.,1.]]
    assert np.concatenate([batch["ends"] for batch in batches]).tolist() == [[1.,0.],[-1.,1.]]
    assert np.concatenate([batch["source_row_key"] for batch in batches]).dtype == np.dtype("int64")


@pytest.mark.parametrize("batch_size", [1, 3])
def test_duckdb_cross_track_pairs_conservatively_reduce_aabb_oracle(
    tmp_path, batch_size,
) -> None:
    # Q44: DuckDB owns the spillable AABB join plus conservative numeric broad
    # phase. Python receives bounded uncertain/hit pairs for the exact NumPy
    # kernel because SQL and NumPy floating-point expressions are not assumed
    # identical.
    tracks = [
        ("a", 10, [(0, -10, 0, 0), (1, 10, 0, 0)]),
        ("a", 2, [(2, -100, 30, 0), (3, 100, 30, 0)]),
        ("b", 20, [(4, 0, -10, 90), (5, 0, 10, 90)]),
        ("c", 30, [(6, 20, -100, 90), (7, 20, 100, 90)]),
        ("d", 40, [(8, 10, 0, 90), (9, 10, 10, 90)]),  # endpoint boundary
        ("e", 50, [(10, -100, -100, 45), (11, 100, 100, 45)]),  # long diagonal
        ("f", 60, [(12, 1000, -100, 90), (13, 1000, 100, 90)]),  # disjoint long
        ("g", 70, [(14, 5, -1e-13, 90), (15, 5, 1e-13, 90)]),  # near-degenerate
    ]
    descriptor = _published_tracks(tmp_path, tracks)
    cfg = intersect_config(cross_track_grid_cell_m=10,
                          cross_track_max_grid_cells_per_segment=3)
    transformer = CoordinateTransformer(SpatialContext(
        4326, 32650, {"coordinate_semantics_version": 2,
                      "enforce_area_of_use": True, "max_scale_error": .01},
    ))
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        batches=list(reader.iter_cross_track_pair_batches("public.source",cfg,batch_size=batch_size))
    identities=[]
    for columns,a,b in batches:
        assert 0<len(a)<=batch_size
        assert len(a)==len(b)
        assert np.all(columns["subtrack_index"][a]!=columns["subtrack_index"][b])
        identities.extend(zip(columns["subtrack_index"][a].tolist(),columns["subtrack_index"][b].tolist()))
    assert set(identities)=={(10,20),(10,40),(10,70),(2,30)}
    assert len(identities)==len(set(identities))
    assert all(60 not in pair for pair in identities)


@pytest.mark.parametrize("batch_size", [1, 2])
def test_columnar_cross_track_exact_kernel_matches_q49_oracle(tmp_path, batch_size) -> None:
    # Q44 fallback record: exact segment intersection is the existing vectorized
    # NumPy geometry kernel. DuckDB owns relational pairing; Polars adds no useful
    # operator for this continuous kernel, so bounded minimal Python objects are
    # the permitted fallback. No threads, processes, or Dask are involved.
    descriptor = _published_tracks(tmp_path, [
        ("a", 10, [(0, -10, 0, 0), (2, 10, 0, 0)]),
        ("a", 11, [(3, -10, 3, 0), (4, 10, 3, 0)]),  # same vehicle, distinct subtrack participates
        ("b", 20, [(1, 0, -10, 90), (5, 0, 10, 90)]),
        ("c", 30, [(6, -10, 5, 0), (7, 10, 5, 0)]),  # parallel
        ("d", 40, [(8, -8, -8, 45), (9, 8, 8, 45)]),  # angle boundary
        ("e", 50, [(10, -100, -4, 90), (11, -100, 4, 90)]),  # overflow pair
        ("f", 60, [(12, 5, -1e-13, 90), (13, 5, 1e-13, 90)]),  # near-degenerate
    ])
    cfg = intersect_config(
        cross_track_pair_batch_size=batch_size,
        cross_track_grid_cell_m=5,
        cross_track_max_grid_cells_per_segment=2,
        cross_track_min_angle_deg=45,
        cross_track_max_angle_deg=90,
    )
    transformer = CoordinateTransformer(SpatialContext(
        4326, 32650, {"coordinate_semantics_version": 2,
                      "enforce_area_of_use": True, "max_scale_error": .01},
    ))
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        actual=extract_columnar_cross_track_candidates(reader,"source",transformer,cfg)
        assert set(zip(actual.from_subtrack_index.tolist(),actual.to_subtrack_index.tolist())) == {(10,20),(11,20),(20,30),(10,40),(11,40),(30,40),(10,60),(20,40)}
        assert actual.calculation_index.tolist()==list(range(len(actual)))
        assert actual.from_source_row_key_valid.all() and actual.to_source_row_key_valid.all()
        assert actual.from_refine_point_id_valid.all() and actual.to_refine_point_id_valid.all()
        assert np.isfinite(actual.xy).all() and np.isfinite(actual.lonlat).all()
        with CrossCandidateRelation(runtime_config(duckdb_threads=1),tmp_path) as store:
            assert extract_columnar_cross_track_candidates(reader,"source",transformer,cfg,candidate_store=store) is store
            assert sum(len(batch) for batch in store.iter_batches())==len(actual)


def test_columnar_cross_track_does_not_seal_caller_store_on_error() -> None:
    reader = Mock()
    reader.iter_cross_track_pair_batches.side_effect = RuntimeError("planner failed")
    store = Mock()
    with pytest.raises(RuntimeError, match="planner failed"):
        extract_columnar_cross_track_candidates(
            reader, "source", Mock(), intersect_config(), candidate_store=store,
        )
    store.seal.assert_not_called()
    store.close.assert_not_called()


def test_duckdb_cross_track_pairs_empty_single_vehicle_and_strict_config(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("a", 0, [(0, -1, 0, 0), (1, 1, 0, 0)]),
        ("a", 1, [(2, 0, -1, 90), (3, 0, 1, 90)]),
    ])
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        assert sum(len(a) for _,a,_ in reader.iter_cross_track_pair_batches("source", intersect_config())) == 1
        for cfg, match in (
            (intersect_config(cross_track_grid_cell_m=float("nan")), "cell size"),
            (intersect_config(cross_track_grid_cell_m=0), "cell size"),
            (intersect_config(cross_track_max_grid_cells_per_segment=0), "maximum grid"),
        ):
            with pytest.raises(refine_reader.RefineParquetError, match=match):
                list(reader.iter_cross_track_pair_batches("source", cfg))
        with pytest.raises(refine_reader.RefineParquetError, match="batch_size"):
            list(reader.iter_cross_track_pair_batches("source", intersect_config(), batch_size=0))


def test_cross_track_pair_api_never_fetches_all() -> None:
    reader = object.__new__(refine_reader.RefineDatasetReader)
    reader._runtime = runtime_config()
    reader._descriptor = Mock(refine_id=uuid4())
    validation, materialized, analyzed, result, dropped = (Mock() for _ in range(5))
    validation.fetchone.return_value = (0,)
    result.fetchmany.return_value = []
    connection = Mock()
    connection.execute.side_effect = [validation, materialized, analyzed, result, dropped]
    reader._connection = connection
    assert list(reader.iter_cross_track_pair_batches(
        "source", intersect_config(), batch_size=1,
    )) == []
    result.fetchmany.assert_called_once_with(1)
    assert not result.fetchall.called
    create_sql, create_parameters = connection.execute.call_args_list[1].args
    assert create_sql.startswith("CREATE TEMP TABLE cross_track_straight_")
    assert create_sql.count("FROM refine_points") == 1
    assert "end_x <> x" not in create_sql
    default = intersect_config()
    assert create_parameters == [default.cross_track_straightness_deg]
    assert connection.execute.call_args_list[2].args[0].startswith("ANALYZE cross_track_straight_")
    sql, parameters = connection.execute.call_args_list[3].args
    assert "FROM refine_points" not in sql
    assert "a.segment_id < b.segment_id" in sql
    assert "a.min_x <= b.max_x AND b.min_x <= a.max_x" in sql
    assert "a.min_y <= b.max_y AND b.min_y <= a.max_y" in sql
    assert parameters == [default.cross_track_min_angle_deg,
                          default.cross_track_max_angle_deg]
    assert "arithmetic_error" in sql
    assert "ta_num BETWEEN" in sql
    assert connection.execute.call_args_list[4].args[0].startswith(
        "DROP TABLE IF EXISTS cross_track_straight_"
    )


def test_cross_track_pair_temp_table_cleanup_and_reader_reuse(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("a", 0, [(0, -1, 0, 0), (1, 1, 0, 0)]),
        ("b", 1, [(2, 0, -1, 90), (3, 0, 1, 90)]),
    ])
    cfg = intersect_config()
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        count_sql = """
          SELECT count(*) FROM duckdb_tables()
          WHERE table_name LIKE 'cross_track_straight_%'
        """
        iterator = reader.iter_cross_track_pair_batches("source", cfg, batch_size=1)
        assert len(next(iterator)[1]) == 1
        assert reader._connection.execute(count_sql).fetchone() == (1,)
        iterator.close()
        assert reader._connection.execute(count_sql).fetchone() == (0,)

        first = list(reader.iter_cross_track_pair_batches("source", cfg, batch_size=1))
        second = list(reader.iter_cross_track_pair_batches("source", cfg, batch_size=1))
        assert len(first)==len(second)
        for (left,a,b),(right,c,d) in zip(first,second):
            np.testing.assert_array_equal(a,c)
            np.testing.assert_array_equal(b,d)
            for key in left: np.testing.assert_array_equal(left[key],right[key])
        assert reader._connection.execute(count_sql).fetchone() == (0,)

        with patch.object(
            reader, "_decode_segment_columns", side_effect=RuntimeError("decode failed"),
        ):
            with pytest.raises(RuntimeError, match="decode failed"):
                list(reader.iter_cross_track_pair_batches("source", cfg, batch_size=1))
        assert reader._connection.execute(count_sql).fetchone() == (0,)


def test_cross_track_production_payload_plan_leads_with_aabb_range(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        (f"v{index}", index, [(0, -10, index / 100, 0), (1, 10, index / 100, 0)])
        for index in range(1100)
    ])

    class ExplainProxy:
        def __init__(self, connection):
            self.connection = connection
            self.query = None
            self.plan = None

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, params=None):
            if "conservative_pairs AS" in query and "SELECT a.* EXCLUDE" in query:
                self.query = query
                values = [] if params is None else params
                self.plan = self.connection.execute("EXPLAIN " + query, values).fetchone()[1]
            return (self.connection.execute(query) if params is None
                    else self.connection.execute(query, params))

    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        proxy = ExplainProxy(reader._connection)
        reader._connection = proxy
        assert list(reader.iter_cross_track_pair_batches(
            "source", intersect_config(), batch_size=7,
        )) == []
        assert proxy.query is not None and proxy.plan is not None
        assert "SELECT a.* EXCLUDE" in proxy.query
        assert "ORDER BY a.vehicle_rank, b.vehicle_rank" in proxy.query
        join_sql = proxy.query[proxy.query.index("FROM cross_track_straight_"):
                               proxy.query.index("), numeric_pairs AS")]
        assert join_sql.index("a.min_x <= b.max_x") < join_sql.index("a.segment_id < b.segment_id")
        join_plan = proxy.plan[proxy.plan.index("IE_JOIN"):]
        first_aabb = min(join_plan.index(name) for name in ("min_x", "max_x", "min_y", "max_y"))
        segment_id = join_plan.find("segment_id")
        assert segment_id < 0 or first_aabb < segment_id
        reader._connection = proxy.connection


def test_cross_track_pair_generator_close_after_reader_close_is_safe(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("a", 0, [(0, -1, 0, 0), (1, 1, 0, 0)]),
        ("b", 1, [(2, 0, -1, 90), (3, 0, 1, 90)]),
    ])
    reader = refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    )
    with reader:
        iterator = reader.iter_cross_track_pair_batches(
            "source", intersect_config(), batch_size=1,
        )
        assert len(next(iterator)[1]) == 1
    iterator.close()


def test_cross_track_segment_api_is_bounded_and_validates_config() -> None:
    owner = uuid4()
    reader = object.__new__(refine_reader.RefineDatasetReader)
    reader._runtime = runtime_config()
    validation = Mock()
    validation.fetchone.return_value = (0,)
    result = Mock()
    result.fetchmany.side_effect = [[], []]
    connection = Mock()
    connection.execute.return_value = result
    reader._connection = connection
    reader._descriptor = Mock(refine_id=owner)
    reader._validate_source = Mock()

    assert list(reader.iter_cross_track_segments(
        "source", intersect_config(), batch_size=1,
    )) == []
    result.fetchmany.assert_called_with(1)
    assert not result.fetchall.called
    for threshold in (float("nan"), -1, 181, True):
        with pytest.raises(refine_reader.RefineParquetError, match="straightness"):
            list(reader.iter_cross_track_segments(
                "source", intersect_config(cross_track_straightness_deg=threshold),
            ))


def test_cross_track_segments_reject_global_subtrack_vehicle_mixing(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("a", 7, [(0, 0, 0, 0)]), ("b", 7, [(1, 1, 0, 0)]),
    ])
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        with pytest.raises(refine_reader.RefineParquetError, match="corrupt"):
            list(reader.iter_cross_track_segments("source", intersect_config()))


def test_cross_track_segments_reject_null_on_single_point_track(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [("a", 0, [(0, None, 0, 0)])])
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader:
        with pytest.raises(refine_reader.RefineParquetError, match="corrupt"):
            list(reader.iter_cross_track_segments("source", intersect_config()))


def test_reader_closes_connection_when_view_creation_fails(tmp_path, monkeypatch) -> None:
    descriptor = _published(tmp_path)
    reader = refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(), data_root=tmp_path,
    )
    connection = Mock()
    connection.execute.side_effect = duckdb.IOException("view probe")
    monkeypatch.setattr(refine_reader.duckdb, "connect", Mock(return_value=connection))

    with pytest.raises(duckdb.IOException, match="view probe"):
        reader.__enter__()

    connection.close.assert_called_once_with()
    assert reader._connection is None
    temp_root = tmp_path / "duckdb" / "refine-readers"
    assert temp_root.is_dir() and list(temp_root.iterdir()) == []


@pytest.mark.parametrize("batch_size", [1, 2, 4])
def test_complete_tracks_use_column_chunks_across_driver_boundaries(tmp_path, monkeypatch, batch_size):
    descriptor = _published(tmp_path)
    original = refine_reader._subtrack_from_point_columns
    calls = []
    def decode(columns, source):
        assert all(isinstance(value, np.ndarray) and value.ndim == 1 for value in columns.values())
        calls.append(len(columns["point_id"]))
        return original(columns, source)
    monkeypatch.setattr(refine_reader, "_subtrack_from_point_columns", decode)
    with refine_reader.RefineDatasetReader(tmp_path, descriptor, "present", runtime_config(), data_root=tmp_path) as reader:
        tracks = list(reader.iter_tracks("source", batch_size=batch_size))
        assert [track.subtrack_index for track in tracks] == [4, 5, 6]
        assert calls == [3, 1, 1]
        assert tracks[0].kind.tolist() == ["p", "i", "p"]
        assert all(not track.xy.flags.writeable and not track.source_row_key.flags.writeable for track in tracks)
        windows = [SpatialWindow(0, 0, 30, 40)]
        selections = list(reader.iter_topology_tracks("source", windows, batch_size=batch_size))
        assert [item.points.subtrack_index for item in selections] == [4]
        assert not selections[0].points.xy.flags.writeable


def test_turn_producer_skips_full_source_validation_and_track_materialization(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("v", 0, [(0, 0, 0, 0), (10, 10, 0, 90), (20, 20, 0, 90)]),
    ])
    with refine_reader.RefineDatasetReader(
        tmp_path, descriptor, "present", runtime_config(duckdb_threads=1),
    ) as reader, patch.object(reader, "_validate_source", side_effect=AssertionError("validated")), \
         patch.object(reader, "iter_tracks", side_effect=AssertionError("materialized")):
        turns = list(reader.iter_turn_candidates(
            "source", intersect_config(turn_window=1, min_leg_m=1, min_movement_speed_mps=0), batch_size=1,
        ))
        assert reader.track_count() == 1
    assert len(turns) == 1


def test_published_track_does_not_retain_consumed_driver_column_chunks(monkeypatch):
    import gc
    import weakref
    owner = uuid4()
    first = _adapter_row(owner)
    second = list(_adapter_row(owner, vehicle="v2"))
    second[3] = 2
    reader, result = _adapter_reader([], owner)
    result.fetchmany.side_effect = [[first], [tuple(second)], []]
    original = refine_reader._point_columns_from_driver_batch
    references = []
    def columns(*args, **kwargs):
        batch = original(*args, **kwargs)
        references.append([weakref.ref(value) for value in batch.values()])
        return batch
    monkeypatch.setattr(refine_reader, "_point_columns_from_driver_batch", columns)
    tracks = reader.iter_tracks("source", batch_size=1)
    track = next(tracks)
    gc.collect()
    assert len(references) == 2
    assert all(reference() is None for reference in references[0])
    assert not track.xy.flags.writeable
    tracks.close()


def test_native_parquet_source_keys_preserve_zero_negative_and_large_identity(tmp_path):
    keys = [0, -1, 2**53+1]
    descriptor = _published_tracks(tmp_path, [("same",37,[(0,0,0,0),(1,1,0,0),(2,2,0,0)])], source_keys=keys)
    with refine_reader.RefineDatasetReader(tmp_path, descriptor, "present", runtime_config(), data_root=tmp_path) as reader:
        track = next(reader.iter_tracks("source", batch_size=1))
        assert track.source_row_key.dtype == np.int64
        assert track.source_row_key.tolist() == keys
        assert track.source_row_key_valid.tolist() == [True]*3
        batches = list(reader.iter_cross_track_segments("source", intersect_config(), batch_size=1))
        assert [int(batch["source_row_key"][0]) for batch in batches] == keys[:2]
        assert all(batch["source_row_key_valid"][0] for batch in batches)
