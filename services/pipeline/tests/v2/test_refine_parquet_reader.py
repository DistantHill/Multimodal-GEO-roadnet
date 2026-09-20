from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import duckdb
import pytest

from track.v2 import artifact_publication, refine_parquet
from track.v2.artifact_parquet import build_descriptor
from track.v2.db.artifact_registry import ArtifactDescriptor, canonical_json
from track.v2.detect import IntersectConfig
from track.v2.cross_track import _prepare_segments
from track.v2.candidate_extraction import (
    extract_columnar_cross_track_candidates, extract_cross_track_candidates,
)
from track.v2.candidate_patches import (
    CROSS_TRACK_OVERFLOW_PAIR_LIMIT, CrossTrackCapacityError,
    build_cross_track_index, iter_cross_track_patches,
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
        assert left.x == pytest.approx(right.x, abs=1e-6)
        assert left.y == pytest.approx(right.y, abs=1e-6)
        assert left.lon == pytest.approx(right.lon, abs=1e-9)
        assert left.lat == pytest.approx(right.lat, abs=1e-9)
        for field in ("timestamp", "entry_bearing", "exit_bearing", "turn_deg", "score"):
            assert getattr(left, field) == pytest.approx(getattr(right, field), abs=1e-12)


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
            assert getattr(left, field) == pytest.approx(getattr(right, field), abs=1e-12)
        for field in ("x1", "y1", "x2", "y2"):
            assert getattr(left, field) == pytest.approx(getattr(right, field), abs=1e-6)
    # The old oracle ranks the decimal track key lexically: a\0 10 precedes a\0 2.
    assert [(segment.track_rank, segment.segment_index) for segment in actual] == [(0, 0), (2, 0)]


@pytest.mark.parametrize("batch_size", [1, 3])
def test_duckdb_cross_track_pairs_match_grid_and_overflow_oracle(tmp_path, batch_size) -> None:
    # Q44: expansion, joins, dedupe, and overflow pairing are spillable relations;
    # Python receives only bounded metadata batches for the later exact kernel.
    # The old oracle pairs every overflow segment with every finite cross-vehicle
    # segment, even for disjoint AABBs, so SQL deliberately retains that strict set.
    tracks = [
        ("a", 10, [(0, -10, 0, 0), (1, 10, 0, 0)]),
        ("a", 2, [(2, -100, 30, 0), (3, 100, 30, 0)]),
        ("b", 20, [(4, 0, -10, 90), (5, 0, 10, 90)]),
        ("c", 30, [(6, 20, -100, 90), (7, 20, 100, 90)]),
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
        index = build_cross_track_index(segments, cell_m=10, max_cells=3)
        expected = {(int(a), int(b))
                    for ids_a, ids_b in iter_cross_track_patches(index, 1)
                    for a, b in zip(ids_a, ids_b, strict=True)}
        batches = list(reader.iter_cross_track_pair_batches(
            "public.source", cfg, batch_size=batch_size,
        ))
    actual = [((a.track_rank, a.segment_index), (b.track_rank, b.segment_index))
              for batch in batches for a, b in batch]
    expected_identity = {
        ((segments[a].track_rank, segments[a].segment_index),
         (segments[b].track_rank, segments[b].segment_index)) for a, b in expected
    }
    assert set(actual) == expected_identity
    assert len(actual) == len(set(actual))
    order = [
        (a.vehicle_rank, b.vehicle_rank, a.segment_index, b.segment_index,
         a.track_rank, b.track_rank)
        for batch in batches for a, b in batch
    ]
    assert order == sorted(order)
    assert all(0 < len(batch) <= batch_size for batch in batches)


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
            assert getattr(left, field) == pytest.approx(getattr(right, field), abs=tolerance)
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
    validation, estimate, result = Mock(), Mock(), Mock()
    validation.fetchone.return_value = (0,)
    estimate.fetchone.return_value = (0,)
    result.fetchmany.return_value = []
    connection = Mock()
    connection.execute.side_effect = [validation, estimate, result]
    reader._connection = connection
    assert list(reader.iter_cross_track_pair_batches(
        "source", IntersectConfig(), batch_size=1,
    )) == []
    result.fetchmany.assert_called_once_with(1)
    assert not result.fetchall.called


def test_cross_track_pair_api_fails_closed_before_overflow_join() -> None:
    reader = object.__new__(refine_parquet.RefineDatasetReader)
    reader._descriptor = Mock(refine_id=uuid4())
    validation, estimate = Mock(), Mock()
    validation.fetchone.return_value = (0,)
    estimate.fetchone.return_value = (CROSS_TRACK_OVERFLOW_PAIR_LIMIT + 1,)
    connection = Mock()
    connection.execute.side_effect = [validation, estimate]
    reader._connection = connection
    with pytest.raises(CrossTrackCapacityError, match="20,000,001"):
        list(reader.iter_cross_track_pair_batches("source", IntersectConfig()))
    assert connection.execute.call_count == 2


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
        tmp_path, descriptor, "present", RuntimeConfig(),
    )
    connection = Mock()
    connection.execute.side_effect = duckdb.IOException("view probe")
    monkeypatch.setattr(refine_parquet.duckdb, "connect", Mock(return_value=connection))

    with pytest.raises(duckdb.IOException, match="view probe"):
        reader.__enter__()

    connection.close.assert_called_once_with()
    assert reader._connection is None
