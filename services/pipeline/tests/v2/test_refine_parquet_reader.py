from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import duckdb
import pytest

from track.v2 import artifact_publication, refine_parquet
from track.v2.artifact_parquet import build_descriptor
from track.v2.db.artifact_registry import ArtifactDescriptor, canonical_json
from track.v2.db.queries import SpatialWindow
from track.v2.detect import IntersectConfig
from track.v2.cross_track import _prepare_segments
from track.v2.cross_candidate_relation import CrossCandidateRelation
from track.v2.candidate_extraction import (
    extract_columnar_cross_track_candidates, extract_cross_track_candidates,
)
from track.v2.models import RuntimeConfig, SpatialContext
from track.v2.projection import CoordinateTransformer
from track.v2.turn_events import detect_turn_patch


def _published(tmp_path: Path):
    handle = artifact_publication.create_staging(
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
        for table in refine_parquet.REFINE_PARQUET_V1:
            directory = handle.path / table
            directory.mkdir()
            output = directory / "part.parquet"
            if table == "kept_points":
                rows = [common.format(point=1, vehicle="v1", subtrack=4, sequence=0),
                        common.format(point=3, vehicle="v1", subtrack=4, sequence=2),
                        common.format(point=7, vehicle="v1", subtrack=5, sequence=0),
                        common.format(point=8, vehicle="v2", subtrack=6, sequence=0)]
                sql = " UNION ALL ".join(
                    f"SELECT {row}, ('source-' || point_id)::VARCHAR source_row_key"
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
        {name: [f"{name}/part.parquet"] for name in refine_parquet.REFINE_PARQUET_V1},
        {"version": 1},
    )
    artifact_publication.publish(handle)
    return descriptor


def _published_tracks(tmp_path: Path, tracks):
    """Publish real Parquet rows used to differential-test the SQL operator."""
    handle = artifact_publication.create_staging(
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
                             x, y, heading, 4.0, f"source-{point_id}"))
        connection.execute("""
            CREATE TABLE input_points(
              refine_id UUID, point_id BIGINT, vehicle_id VARCHAR,
              subtrack_index INTEGER, refined_sequence_in_subtrack BIGINT,
              timestamp DOUBLE, lon DOUBLE, lat DOUBLE, x DOUBLE, y DOUBLE,
              heading DOUBLE, speed DOUBLE, source_row_key VARCHAR)
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
        handle, {name: [f"{name}/part.parquet"] for name in refine_parquet.REFINE_PARQUET_V1},
        {"version": 1},
    )
    artifact_publication.publish(handle)
    return descriptor


def test_reader_uses_only_manifest_files_and_merges_one_vehicle_subtrack(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
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
        refine_parquet.RefineDatasetReader(tmp_path, descriptor, "present", RuntimeConfig())


def test_track_count_validates_source_and_counts_subtrack_identities(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        assert reader.track_count() == 3
        assert reader.track_count() == 3


def test_reader_adapts_all_tracks_across_batches_and_queries_extent(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        tracks = list(reader.iter_tracks("public.source_points", batch_size=2))
        assert [(track[0].vehicle_id, track[0].refined_point_ref.subtrack_index,
                 [point.refined_point_ref.refined_sequence_in_subtrack for point in track])
                for track in tracks] == [
            ("v1", 4, [0, 1, 2]), ("v1", 5, [0]), ("v2", 6, [0]),
        ]
        first, injected, last = tracks[0]
        assert first.point_ref == refine_parquet.PointRef(
            "public.source_points", "source-1", True,
        )
        assert injected.point_ref == refine_parquet.PointRef(
            "public.injected_points", "2", False, generated_reason="orthogonal",
        )
        assert injected.extra == {} and last.refined_point_ref.point_kind == "kept"
        assert reader.extent() == (10.0, 20.0, 10.0, 20.0)


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
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
        data_root=tmp_path,
    ) as reader:
        selected = list(reader.iter_topology_tracks("source", windows, batch_size=1))

    assert [(item.window_indices, item.track_key) for item in selected] == [
        ((0, 1), "a\0" + "2"), ((0, 1), "b\0" + "1"),
        ((2,), "a\0" + "2"), ((2,), "b\0" + "1"), ((2,), "c\0" + "4"),
    ]
    assert [[point.refined_point_ref.refined_sequence_in_subtrack
             for point in item.points] for item in selected] == [
        [0, 1, 2], [0, 1, 2], [0, 1, 2], [0, 1, 2], [0, 1],
    ]


def test_topology_track_rebuild_preserves_kept_injected_order(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        selected = list(reader.iter_topology_tracks(
            "public.source", [SpatialWindow(10, 20, 10, 20)], batch_size=1,
        ))
    assert [(item.track_key, item.window_indices) for item in selected] == [
        ("v1\0" + "4", (0,)),
    ]
    assert [(point.refined_point_ref.point_kind,
             point.refined_point_ref.refined_sequence_in_subtrack)
            for point in selected[0].points] == [("kept", 0), ("injected", 1), ("kept", 2)]


def test_topology_selection_order_matches_legacy_text_track_key(tmp_path) -> None:
    tracks = [
        ("aa", 3, [(0, -1, 0, 0), (1, 1, 0, 0)]),
        ("a", 2, [(2, -1, 0, 0), (3, 1, 0, 0)]),
        ("a", 10, [(4, -1, 0, 0), (5, 1, 0, 0)]),
    ]
    descriptor = _published_tracks(tmp_path, tracks)
    expected = sorted(f"{vehicle}\0{subtrack}" for vehicle, subtrack, _ in tracks)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
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
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        selected = list(reader.iter_topology_tracks(
            "source", [SpatialWindow(0, 0, 1, 1)], batch_size=1,
        ))
    assert bool(selected) is matches


def test_topology_windows_reject_invalid_inputs_and_require_open_reader(tmp_path) -> None:
    descriptor = _published(tmp_path)
    reader = refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(), data_root=tmp_path,
    )
    with pytest.raises(refine_parquet.RefineParquetError, match="not open"):
        list(reader.iter_topology_tracks("source", []))
    with reader:
        assert list(reader.iter_topology_tracks("source", [])) == []
        for window in (
            SpatialWindow(2, 0, 1, 1), SpatialWindow(0, 0, float("nan"), 1),
            SpatialWindow(False, 0, 1, 1),
        ):
            with pytest.raises(refine_parquet.RefineParquetError, match="window"):
                list(reader.iter_topology_tracks("source", [window]))
        for batch_size in (0, True):
            with pytest.raises(refine_parquet.RefineParquetError, match="batch_size"):
                list(reader.iter_topology_tracks("source", [], batch_size=batch_size))


def test_topology_generator_close_drops_window_table_and_allows_reuse(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("a", 0, [(0, -1, 0, 0), (1, 1, 0, 0)]),
    ])
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        iterator = reader.iter_topology_tracks(
            "source", [SpatialWindow(0, 0, 0, 0)], batch_size=1,
        )
        assert next(iterator).track_key == "a\0" + "0"
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
    runtime = RuntimeConfig(
        duckdb_memory_limit="64MB", duckdb_threads=1,
        duckdb_max_temp_directory_size="1GB",
    )
    reader = refine_parquet.RefineDatasetReader(
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
    with pytest.raises(refine_parquet.RefineParquetError, match="absolute"):
        refine_parquet.RefineDatasetReader(
            publication, descriptor, "present", RuntimeConfig(),
            data_root=Path("relative"),
        )


def _adapter_reader(rows, owner):
    reader = object.__new__(refine_parquet.RefineDatasetReader)
    result = Mock()
    result.fetchmany.side_effect = [rows, []]
    connection = Mock()
    connection.execute.return_value = result
    reader._connection = connection
    reader._descriptor = Mock(refine_id=owner)
    return reader, result


def _adapter_row(owner, *, vehicle="v1", sequence=0, kind="kept", source_key="source-1"):
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
    with pytest.raises(refine_parquet.RefineParquetError, match=message):
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
        with pytest.raises(refine_parquet.RefineParquetError, match=message):
            list(reader.iter_tracks("public.source", batch_size=1))
        assert not result.fetchall.called


def test_reader_rejects_non_present_unknown_tables_and_schema(tmp_path) -> None:
    descriptor = _published(tmp_path)
    with pytest.raises(Exception, match="not present"):
        refine_parquet.RefineDatasetReader(tmp_path, descriptor, "deleted", RuntimeConfig())
    manifest = json.loads(descriptor.manifest_json)
    manifest["tables"][0]["name"] = "unknown_points"
    changed = ArtifactDescriptor.create(
        id=descriptor.id, stage="refine", refine_id=descriptor.refine_id,
        relative_path=descriptor.relative_path,
        source_fingerprint=json.loads(descriptor.source_fingerprint_json), manifest=manifest,
    )
    with pytest.raises(refine_parquet.RefineParquetError, match="logical tables"):
        refine_parquet.RefineDatasetReader(tmp_path, changed, "present", RuntimeConfig())
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
        refine_parquet.RefineDatasetReader(
            tmp_path, schema_changed, "present", RuntimeConfig(),
        )


def test_batch_api_uses_bounded_fetchmany_not_fetchall() -> None:
    reader = object.__new__(refine_parquet.RefineDatasetReader)
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
def test_duckdb_turn_candidates_match_python_oracle_across_batches(tmp_path, window) -> None:
    # DuckDB is selected because ordering, fixed windows and threshold filtering
    # are columnar and spillable; this operator needs no Polars/Python fallback.
    tracks = [
        ("a", 1, [(0, -40, 0, 359), (10, -20, 0, 1), (20, 0, 0, 90),
                   (30, 0, 20, 91), (40, 0, 40, 89)]),
        ("b", 2, [(0, 0, 0, 1), (10, 20, 0, 359), (20, 40, 0, 270),
                   (30, 40, -20, 269), (40, 40, -40, 271)]),
        ("short", 3, [(0, 0, 0, 0), (1, 1, 0, 90)]),
        ("elapsed", 4, [(0, 0, 0, 0), (0, 20, 0, 0), (0, 20, 20, 90)]),
    ]
    descriptor = _published_tracks(tmp_path, tracks)
    cfg = IntersectConfig(turn_window=window, min_turn_deg=45, max_turn_deg=170,
                          min_leg_m=19, min_movement_speed_mps=1)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        expected = detect_turn_patch(
            {f"{t[0]}\0{t[1]}": t for t in reader.iter_tracks("public.source", batch_size=2)},
            cfg,
        )
        actual = list(reader.iter_turn_candidates("public.source", cfg, batch_size=1))
    assert [(e.event_calculate_index, e.vehicle_id, e.point_ref, e.refined_point_ref)
            for e in actual] == [
        (e.event_calculate_index, e.vehicle_id, e.point_ref, e.refined_point_ref)
        for e in expected
    ]
    for left, right in zip(actual, expected, strict=True):
        assert left.source_ids == right.source_ids
        assert left.x == pytest.approx(right.x, abs=1e-6, rel=0)
        assert left.y == pytest.approx(right.y, abs=1e-6, rel=0)
        assert left.lon == pytest.approx(right.lon, abs=1e-9, rel=0)
        assert left.lat == pytest.approx(right.lat, abs=1e-9, rel=0)
        for field in ("timestamp", "entry_bearing", "exit_bearing", "turn_deg", "score"):
            assert getattr(left, field) == pytest.approx(
                getattr(right, field), abs=1e-12, rel=0,
            )


def test_duckdb_turn_thresholds_are_inclusive_and_config_is_strict(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("edge", 0, [(0, 0, 0, 0), (10, 0, 10, 90), (20, 10, 10, 90)]),
    ])
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        cfg = IntersectConfig(turn_window=1, min_turn_deg=90, max_turn_deg=90,
                              min_leg_m=10, min_movement_speed_mps=1)
        assert len(list(reader.iter_turn_candidates("source", cfg))) == 1
        for invalid in (
            IntersectConfig(turn_window=0),
            IntersectConfig(min_leg_m=float("nan")),
            IntersectConfig(min_turn_deg=100, max_turn_deg=90),
        ):
            with pytest.raises(refine_parquet.RefineParquetError, match="turn"):
                list(reader.iter_turn_candidates("source", invalid))


def test_duckdb_turn_boundary_fallback_matches_python_oracle(tmp_path) -> None:
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
    cfg = IntersectConfig(turn_window=1, min_turn_deg=45, max_turn_deg=170,
                          min_leg_m=1, min_movement_speed_mps=0)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        tracks = {f"{t[0]}\0{t[1]}": t for t in reader.iter_tracks("source")}
        expected = detect_turn_patch(tracks, cfg)
        actual = list(reader.iter_turn_candidates("source", cfg, batch_size=1))
    assert [event.vehicle_id for event in actual] == [event.vehicle_id for event in expected]
    assert [event.event_calculate_index for event in actual] == list(range(len(actual)))
    for left, right in zip(actual, expected, strict=True):
        assert left.point_ref == right.point_ref
        assert left.refined_point_ref == right.refined_point_ref
        for field in ("entry_bearing", "exit_bearing", "turn_deg", "score"):
            assert getattr(left, field) == getattr(right, field)


def test_duckdb_turn_unstable_resultant_forces_python_oracle(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("unstable", 0, [(0, 0, 0, 0), (10, 10, 0, 180), (20, 20, 0, 90),
                          (30, 30, 0, 270), (40, 40, 0, 0)]),
    ])
    cfg = IntersectConfig(turn_window=2, min_turn_deg=0, max_turn_deg=180,
                          min_leg_m=1, min_movement_speed_mps=0)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        tracks = {"unstable": next(reader.iter_tracks("source"))}
        expected = detect_turn_patch(tracks, cfg)
        with patch.object(
            refine_parquet, "_circular_mean_deg",
            wraps=refine_parquet._circular_mean_deg,
        ) as exact:
            actual = list(reader.iter_turn_candidates("source", cfg, batch_size=1))
    assert exact.call_count == 2
    assert len(actual) == len(expected) == 1
    for field in ("entry_bearing", "exit_bearing", "turn_deg", "score"):
        assert getattr(actual[0], field) == getattr(expected[0], field)


def test_duckdb_turn_internal_rows_do_not_call_python_exact_helper(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("internal", 0, [(0, 0, 0, 0), (10, 10, 0, 90), (20, 20, 0, 90)]),
    ])
    cfg = IntersectConfig(turn_window=1, min_turn_deg=45, max_turn_deg=170,
                          min_leg_m=1, min_movement_speed_mps=0)
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader, patch.object(refine_parquet, "_circular_mean_deg") as exact:
        actual = list(reader.iter_turn_candidates("source", cfg, batch_size=1))
    assert len(actual) == 1
    exact.assert_not_called()


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
    cfg = IntersectConfig(cross_track_straightness_deg=2)
    transformer = CoordinateTransformer(SpatialContext(
        4326, 32650,
        {"coordinate_semantics_version": 2, "enforce_area_of_use": True,
         "max_scale_error": .01},
    ))
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        oracle_tracks = {
            f"{track[0].vehicle_id}\0{track[0].refined_point_ref.subtrack_index}": track
            for track in reader.iter_tracks("public.source", batch_size=2)
        }
        expected, vehicle_count = _prepare_segments(oracle_tracks, transformer, cfg)
        actual = list(reader.iter_cross_track_segments("public.source", cfg, batch_size=1))
    assert vehicle_count == 2
    assert len(actual) == len(expected) == 2
    for left, right in zip(actual, expected, strict=True):
        assert (left.vehicle_id, left.vehicle_rank, left.track_rank, left.segment_index,
                left.start.point_ref, left.start.refined_point_ref) == (
            right.vehicle_id, right.vehicle_rank, right.track_rank, right.segment_index,
            right.start.point_ref, right.start.refined_point_ref,
        )
        for field in ("heading", "timestamp"):
            assert getattr(left, field) == pytest.approx(
                getattr(right, field), abs=1e-12, rel=0,
            )
        for field in ("x1", "y1", "x2", "y2"):
            assert getattr(left, field) == pytest.approx(
                getattr(right, field), abs=1e-6, rel=0,
            )
    # The old oracle ranks the decimal track key lexically: a\0 10 precedes a\0 2.
    assert [(segment.track_rank, segment.segment_index) for segment in actual] == [(0, 0), (2, 0)]


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
    cfg = IntersectConfig(cross_track_grid_cell_m=10,
                          cross_track_max_grid_cells_per_segment=3)
    transformer = CoordinateTransformer(SpatialContext(
        4326, 32650, {"coordinate_semantics_version": 2,
                      "enforce_area_of_use": True, "max_scale_error": .01},
    ))
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        oracle_tracks = {
            f"{track[0].vehicle_id}\0{track[0].refined_point_ref.subtrack_index}": track
            for track in reader.iter_tracks("public.source", batch_size=1)
        }
        segments, _ = _prepare_segments(oracle_tracks, transformer, cfg)
        expected = {
            (a, b) for a, left in enumerate(segments)
            for b, right in enumerate(segments[a + 1:], a + 1)
            if left.vehicle_rank != right.vehicle_rank
            and min(left.x1, left.x2) <= max(right.x1, right.x2)
            and min(right.x1, right.x2) <= max(left.x1, left.x2)
            and min(left.y1, left.y2) <= max(right.y1, right.y2)
            and min(right.y1, right.y2) <= max(left.y1, left.y2)
        }
        batches = list(reader.iter_cross_track_pair_batches(
            "public.source", cfg, batch_size=batch_size,
        ))
        compatibility_batches = list(reader.iter_cross_track_pair_batches(
            "public.source",
            IntersectConfig(cross_track_grid_cell_m=1,
                            cross_track_max_grid_cells_per_segment=1),
            batch_size=batch_size,
        ))
    actual = [((a.track_rank, a.segment_index), (b.track_rank, b.segment_index))
              for batch in batches for a, b in batch]
    expected_identity = {
        ((segments[a].track_rank, segments[a].segment_index),
         (segments[b].track_rank, segments[b].segment_index)) for a, b in expected
    }
    assert len(expected_identity) == 10
    assert len(actual) == 4
    assert set(actual) < expected_identity
    assert ((0, 0), (2, 0)) in actual  # interior crossing
    assert ((0, 0), (4, 0)) in actual  # endpoint crossing
    assert ((0, 0), (7, 0)) in actual  # uncertain determinant retained
    assert not any(5 in (left[0], right[0]) for left, right in actual)  # angle reject
    assert actual == [
        ((a.track_rank, a.segment_index), (b.track_rank, b.segment_index))
        for batch in compatibility_batches for a, b in batch
    ]
    assert len(actual) == len(set(actual))
    order = [
        (a.vehicle_rank, b.vehicle_rank, a.segment_index, b.segment_index,
         a.track_rank, b.track_rank)
        for batch in batches for a, b in batch
    ]
    assert order == sorted(order)
    assert all(0 < len(batch) <= batch_size for batch in batches)
    assert not any(a.vehicle_id == "f" or b.vehicle_id == "f"
                   for batch in batches for a, b in batch)


@pytest.mark.parametrize("batch_size", [1, 2])
def test_columnar_cross_track_exact_kernel_matches_q49_oracle(tmp_path, batch_size) -> None:
    # Q44 fallback record: exact segment intersection is the existing vectorized
    # NumPy geometry kernel. DuckDB owns relational pairing; Polars adds no useful
    # operator for this continuous kernel, so bounded minimal Python objects are
    # the permitted fallback. No threads, processes, or Dask are involved.
    descriptor = _published_tracks(tmp_path, [
        ("a", 10, [(0, -10, 0, 0), (2, 10, 0, 0)]),
        ("a", 11, [(3, -10, 3, 0), (4, 10, 3, 0)]),  # same vehicle excluded
        ("b", 20, [(1, 0, -10, 90), (5, 0, 10, 90)]),
        ("c", 30, [(6, -10, 5, 0), (7, 10, 5, 0)]),  # parallel
        ("d", 40, [(8, -8, -8, 45), (9, 8, 8, 45)]),  # angle boundary
        ("e", 50, [(10, -100, -4, 90), (11, -100, 4, 90)]),  # overflow pair
        ("f", 60, [(12, 5, -1e-13, 90), (13, 5, 1e-13, 90)]),  # near-degenerate
    ])
    cfg = IntersectConfig(
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
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        tracks = {
            f"{track[0].vehicle_id}\0{track[0].refined_point_ref.subtrack_index}": track
            for track in reader.iter_tracks("source", batch_size=1)
        }
        expected, _, _ = extract_cross_track_candidates(tracks, transformer, cfg)
        actual = extract_columnar_cross_track_candidates(
            reader, "source", transformer, cfg,
        )
        with CrossCandidateRelation(
            RuntimeConfig(duckdb_threads=1), tmp_path,
        ) as candidate_store:
            stored = extract_columnar_cross_track_candidates(
                reader, "source", transformer, cfg, candidate_store=candidate_store,
            )
            assert stored is candidate_store
            assert list(stored) == actual
    assert len(actual) == len(expected)
    for left, right in zip(actual, expected, strict=True):
        assert left.cross_track_calculate_index == right.cross_track_calculate_index
        assert left.point_ref == right.point_ref
        assert left.from_segment_start_point_ref == right.from_segment_start_point_ref
        assert left.to_segment_start_point_ref == right.to_segment_start_point_ref
        assert left.from_segment_start == right.from_segment_start
        assert left.to_segment_start == right.to_segment_start
        assert left.vehicle_id == right.vehicle_id
        for field, tolerance in (("x", 1e-6), ("y", 1e-6),
                                 ("lon", 1e-9), ("lat", 1e-9),
                                 ("timestamp", 1e-12), ("heading", 1e-12),
                                 ("speed", 1e-12)):
            assert getattr(left, field) == pytest.approx(
                getattr(right, field), abs=tolerance, rel=0,
            )
    assert any(candidate.vehicle_id == "a|b" for candidate in actual)


def test_columnar_cross_track_does_not_seal_caller_store_on_error() -> None:
    reader = Mock()
    reader.iter_cross_track_pair_batches.side_effect = RuntimeError("planner failed")
    store = Mock()
    with pytest.raises(RuntimeError, match="planner failed"):
        extract_columnar_cross_track_candidates(
            reader, "source", Mock(), IntersectConfig(), candidate_store=store,
        )
    store.seal.assert_not_called()
    store.close.assert_not_called()


def test_duckdb_cross_track_pairs_empty_single_vehicle_and_strict_config(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("a", 0, [(0, -1, 0, 0), (1, 1, 0, 0)]),
        ("a", 1, [(2, 0, -1, 90), (3, 0, 1, 90)]),
    ])
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        assert list(reader.iter_cross_track_pair_batches("source", IntersectConfig())) == []
        for cfg, match in (
            (IntersectConfig(cross_track_grid_cell_m=float("nan")), "cell size"),
            (IntersectConfig(cross_track_grid_cell_m=0), "cell size"),
            (IntersectConfig(cross_track_max_grid_cells_per_segment=0), "maximum grid"),
        ):
            with pytest.raises(refine_parquet.RefineParquetError, match=match):
                list(reader.iter_cross_track_pair_batches("source", cfg))
        with pytest.raises(refine_parquet.RefineParquetError, match="batch_size"):
            list(reader.iter_cross_track_pair_batches("source", IntersectConfig(), batch_size=0))


def test_cross_track_pair_api_never_fetches_all() -> None:
    reader = object.__new__(refine_parquet.RefineDatasetReader)
    reader._descriptor = Mock(refine_id=uuid4())
    validation, materialized, analyzed, result, dropped = (Mock() for _ in range(5))
    validation.fetchone.return_value = (0,)
    result.fetchmany.return_value = []
    connection = Mock()
    connection.execute.side_effect = [validation, materialized, analyzed, result, dropped]
    reader._connection = connection
    assert list(reader.iter_cross_track_pair_batches(
        "source", IntersectConfig(), batch_size=1,
    )) == []
    result.fetchmany.assert_called_once_with(1)
    assert not result.fetchall.called
    create_sql, create_parameters = connection.execute.call_args_list[1].args
    assert create_sql.startswith("CREATE TEMP TABLE cross_track_straight_")
    assert create_sql.count("FROM refine_points") == 1
    assert "end_x <> x" not in create_sql
    default = IntersectConfig()
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
    cfg = IntersectConfig()
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        count_sql = """
          SELECT count(*) FROM duckdb_tables()
          WHERE table_name LIKE 'cross_track_straight_%'
        """
        iterator = reader.iter_cross_track_pair_batches("source", cfg, batch_size=1)
        assert len(next(iterator)) == 1
        assert reader._connection.execute(count_sql).fetchone() == (1,)
        iterator.close()
        assert reader._connection.execute(count_sql).fetchone() == (0,)

        first = list(reader.iter_cross_track_pair_batches("source", cfg, batch_size=1))
        second = list(reader.iter_cross_track_pair_batches("source", cfg, batch_size=1))
        assert first == second
        assert reader._connection.execute(count_sql).fetchone() == (0,)

        with patch.object(
            reader, "_decode_cross_track_segment", side_effect=RuntimeError("decode failed"),
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

    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        proxy = ExplainProxy(reader._connection)
        reader._connection = proxy
        assert list(reader.iter_cross_track_pair_batches(
            "source", IntersectConfig(), batch_size=7,
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
    reader = refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    )
    with reader:
        iterator = reader.iter_cross_track_pair_batches(
            "source", IntersectConfig(), batch_size=1,
        )
        assert len(next(iterator)) == 1
    iterator.close()


def test_cross_track_segment_api_is_bounded_and_validates_config() -> None:
    owner = uuid4()
    reader = object.__new__(refine_parquet.RefineDatasetReader)
    validation = Mock()
    validation.fetchone.return_value = (0,)
    result = Mock()
    result.fetchmany.side_effect = [[], []]
    connection = Mock()
    connection.execute.side_effect = [validation, result]
    reader._connection = connection
    reader._descriptor = Mock(refine_id=owner)

    assert list(reader.iter_cross_track_segments(
        "source", IntersectConfig(), batch_size=1,
    )) == []
    result.fetchmany.assert_called_with(1)
    assert not result.fetchall.called
    for threshold in (float("nan"), -1, 181, True):
        with pytest.raises(refine_parquet.RefineParquetError, match="straightness"):
            list(reader.iter_cross_track_segments(
                "source", IntersectConfig(cross_track_straightness_deg=threshold),
            ))


def test_cross_track_segments_reject_global_subtrack_vehicle_mixing(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [
        ("a", 7, [(0, 0, 0, 0)]), ("b", 7, [(1, 1, 0, 0)]),
    ])
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        with pytest.raises(refine_parquet.RefineParquetError, match="corrupt"):
            list(reader.iter_cross_track_segments("source", IntersectConfig()))


def test_cross_track_segments_reject_null_on_single_point_track(tmp_path) -> None:
    descriptor = _published_tracks(tmp_path, [("a", 0, [(0, None, 0, 0)])])
    with refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(duckdb_threads=1),
    ) as reader:
        with pytest.raises(refine_parquet.RefineParquetError, match="corrupt"):
            list(reader.iter_cross_track_segments("source", IntersectConfig()))


def test_reader_closes_connection_when_view_creation_fails(tmp_path, monkeypatch) -> None:
    descriptor = _published(tmp_path)
    reader = refine_parquet.RefineDatasetReader(
        tmp_path, descriptor, "present", RuntimeConfig(), data_root=tmp_path,
    )
    connection = Mock()
    connection.execute.side_effect = duckdb.IOException("view probe")
    monkeypatch.setattr(refine_parquet.duckdb, "connect", Mock(return_value=connection))

    with pytest.raises(duckdb.IOException, match="view probe"):
        reader.__enter__()

    connection.close.assert_called_once_with()
    assert reader._connection is None
    temp_root = tmp_path / "duckdb" / "refine-readers"
    assert temp_root.is_dir() and list(temp_root.iterdir()) == []
