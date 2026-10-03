import numpy as np
import pytest
from types import SimpleNamespace

from track.v2.candidates.grid_segments import GridSegmentBudgetError, GridSegmentRelation, build_grid_segments, open_grid_segments


def _point(point_id, sequence, x, *, kind=0):
    return {
        "point_id": point_id, "point_kind": kind,
        "refined_sequence_in_subtrack": sequence,
        "source_id": "public.injected_points" if kind else "source_points",
        "source_row_key": None if kind else str(point_id),
        "generated_reason": "test" if kind else None,
        "timestamp": float(sequence), "lon": 110.0, "lat": 20.0,
        "heading": 90.0, "speed": 20.0, "x": x, "y": 0.0,
    }


def _row(vehicle, point, prev=None, next_=None, *, subtrack=1):
    result = {"vehicle_id": vehicle, "subtrack_index": subtrack, **point}
    for prefix, neighbor in (("prev_", prev), ("next_", next_)):
        for key in point:
            result[prefix + key] = None if neighbor is None else neighbor[key]
    return result


def _batch(*rows):
    return {name: np.asarray([row[name] for row in rows], dtype=object) for name in rows[0]}


def test_build_grid_segments_deduplicates_both_hit_endpoints_and_sorts():
    first = _point(10, 0, 0.0)
    injected = _point(20, 1, 1.0, kind=1)
    third = _point(11, 2, 2.0)
    other_first = _point(30, 0, 0.0)
    other_next = _point(31, 1, 1.0)
    batches = [
        _batch(_row("z", other_first, next_=other_next, subtrack=2),
               _row("a", first, next_=injected)),
        _batch(_row("a", injected, prev=first, next_=third)),
    ]

    result = build_grid_segments(batches, max_segments=3, max_bytes=100_000)

    assert result["vehicle_id"].tolist() == ["a", "a", "z"]
    np.testing.assert_array_equal(result["segment_identity"], [[1, 0, 10, 1, 20], [1, 1, 20, 0, 11], [2, 0, 30, 0, 31]])
    np.testing.assert_array_equal(result["x_start"], [0.0, 1.0, 0.0])
    np.testing.assert_array_equal(result["x_end"], [1.0, 2.0, 1.0])
    assert result["start_point_kind"].tolist() == ["kept", "injected", "kept"]
    assert result["start_source_id"][1] == "public.injected_points"
    assert result["start_generated_reason"][1] == "test"
    assert result["start_source_row_key"].tolist() == [10, 0, 30]
    assert result["start_source_row_key"].dtype == np.int64


@pytest.mark.parametrize("spill", [False, True])
def test_open_grid_assigns_each_segment_once_with_closed_boundary(tmp_path, spill):
    outside_left = _point(10, 0, -1.0)
    on_left = _point(11, 1, 0.0)
    on_right = _point(12, 2, 1.0)
    outside_right = _point(13, 3, 2.0)
    batches = (
        _batch(_row("car", on_left, prev=outside_left, next_=on_right)),
        _batch(_row("car", on_right, prev=on_left, next_=outside_right)),
    )
    runtime = SimpleNamespace(duckdb_memory_limit="16MB", duckdb_threads=1,
                              duckdb_max_temp_directory_size="64MB")
    with open_grid_segments(batches, max_segments=1 if spill else 3,
                            max_bytes=100_000, runtime=runtime, data_root=tmp_path,
                            bounds=(0.0, -1.0, 1.0, 1.0)) as segments:
        if spill:
            assert isinstance(segments, GridSegmentRelation)
            assert segments.count == 3
            segments = segments.block(0, 3)
        assert segments["start_point_id"].tolist() == [10, 11, 12]
        assert segments["end_point_id"].tolist() == [11, 12, 13]


def test_build_grid_segments_budget_fails_without_retry():
    first = _point(10, 0, 0.0)
    second = _point(11, 1, 1.0)
    third = _point(12, 2, 2.0)
    batch = _batch(_row("a", second, prev=first, next_=third))
    with pytest.raises(GridSegmentBudgetError, match="max_segments"):
        build_grid_segments([batch], max_segments=1, max_bytes=100_000)
    with pytest.raises(GridSegmentBudgetError, match="max_bytes"):
        build_grid_segments([batch], max_segments=2, max_bytes=100)


def test_build_grid_segments_rejects_nonadjacent_neighbours():
    first = _point(10, 0, 0.0)
    third = _point(12, 2, 2.0)
    with pytest.raises(ValueError, match="not adjacent"):
        build_grid_segments(
            [_batch(_row("a", third, prev=first))],
            max_segments=2, max_bytes=100_000,
        )


def test_spilled_grid_switches_before_large_concat_and_keeps_cross_batch_segments(tmp_path):
    from types import SimpleNamespace
    first = _point(10, 0, 0.0)
    middle = _point(11, 1, 1.0)
    last = _point(12, 2, 2.0)
    batches = iter((_batch(_row("a", first, next_=middle)),
                    _batch(_row("a", middle, prev=first, next_=last))))
    runtime = SimpleNamespace(duckdb_memory_limit="16MB", duckdb_threads=1,
                              duckdb_max_temp_directory_size="64MB")
    with open_grid_segments(batches, max_segments=1, max_bytes=100_000,
                            runtime=runtime, data_root=tmp_path) as result:
        assert isinstance(result, GridSegmentRelation)
        assert result.count == 2
        first_block = result.block(0, 1)
        second_block = result.block(1, 1)
        assert first_block["start_point_id"].tolist() == [10]
        assert second_block["start_point_id"].tolist() == [11]
        assert not first_block["start_point_id"].flags.writeable
    assert not list((tmp_path / "duckdb").iterdir())


def test_grid_owner_uses_dedicated_memory_limit(tmp_path):
    from .config_factory import runtime_config

    runtime = runtime_config(duckdb_memory_limit="64MB", duckdb_grid_memory_limit="128MB")
    owner = GridSegmentRelation(runtime, tmp_path)
    try:
        assert owner._connection.execute("SELECT current_setting('memory_limit')").fetchone()[0] == "122.0 MiB"
    finally:
        owner.close()


def test_spilled_grid_compares_distinct_blocks_with_exact_kernel(tmp_path):
    from types import SimpleNamespace
    from track.v2.candidates.detect_grid_crossings import iter_spilled_grid_candidates
    from .config_factory import intersect_config
    from .detect_columns_helpers import TRANSFORMER

    horizontal_a = _point(10, 0, -10.0)
    horizontal_b = _point(11, 1, 10.0)
    vertical_a = _point(20, 0, 0.0)
    vertical_b = _point(21, 1, 0.0)
    vertical_a["y"] = -10.0
    vertical_b["y"] = 10.0
    vertical_a["heading"] = vertical_b["heading"] = 0.0
    batches = (_batch(_row("same-car", horizontal_a, next_=horizontal_b, subtrack=1)),
               _batch(_row("same-car", vertical_a, next_=vertical_b, subtrack=2)))
    runtime = SimpleNamespace(duckdb_memory_limit="16MB", duckdb_threads=1,
                              duckdb_max_temp_directory_size="64MB")
    with open_grid_segments(batches, max_segments=1, max_bytes=100_000,
                            runtime=runtime, data_root=tmp_path) as relation:
        assert isinstance(relation, GridSegmentRelation)
        found = list(iter_spilled_grid_candidates(relation, intersect_config(), TRANSFORMER,
                                                  block_size=1))
    assert sum(map(len, found)) == 1
    assert found[0].from_subtrack_index.tolist() == [1]
    assert found[0].to_subtrack_index.tolist() == [2]


def test_spilled_grid_owner_cleans_directory_when_schema_creation_fails(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from track.v2.candidates import grid_segments
    connection = SimpleNamespace(closed=False)
    connection.execute = lambda _sql: (_ for _ in ()).throw(RuntimeError("schema failure"))
    connection.close = lambda: setattr(connection, "closed", True)
    monkeypatch.setattr(grid_segments.duckdb, "connect", lambda *_a, **_kw: connection)
    runtime = SimpleNamespace(duckdb_memory_limit="16MB", duckdb_threads=1,
                              duckdb_max_temp_directory_size="64MB")
    with pytest.raises(RuntimeError, match="schema failure"):
        GridSegmentRelation(runtime, tmp_path)
    assert connection.closed
    assert not list((tmp_path / "duckdb").iterdir())


@pytest.mark.parametrize("spill", [False, True])
def test_open_grid_releases_build_workspaces_before_worker_consumes(tmp_path, monkeypatch, spill):
    import gc
    import weakref
    from types import SimpleNamespace
    from track.v2.candidates import grid_segments

    refs = []
    original_pieces = grid_segments._pieces
    original_finish = grid_segments._finish_memory

    def tracked_pieces(batch, bounds=None):
        pieces = original_pieces(batch, bounds)
        refs.extend(weakref.ref(value) for piece in pieces for value in piece.values())
        return pieces

    def tracked_finish(result):
        refs.extend(weakref.ref(value) for value in result.values())
        return original_finish(result)

    monkeypatch.setattr(grid_segments, "_pieces", tracked_pieces)
    monkeypatch.setattr(grid_segments, "_finish_memory", tracked_finish)

    class Batch(dict):
        pass

    class SingleBatch:
        used = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.used:
                raise StopIteration
            self.used = True
            batch = Batch(_batch(_row("car", _point(10, 0, 0.0),
                                      next_=_point(11, 1, 1.0))))
            refs.append(weakref.ref(batch))
            return batch

    runtime = SimpleNamespace(duckdb_memory_limit="16MB", duckdb_threads=1,
                              duckdb_max_temp_directory_size="64MB")
    with open_grid_segments(SingleBatch(), max_segments=0 if spill else 10,
                            max_bytes=100_000, runtime=runtime,
                            data_root=tmp_path) as delivered:
        assert isinstance(delivered, GridSegmentRelation) if spill else len(delivered["subtrack_index"]) == 1
        gc.collect()
        # DuckDB may retain registered NumPy columns while its connection is
        # alive; the last cursor batch must not be retained by the generator.
        assert refs and refs[0]() is None
        if not spill:
            assert all(ref() is None for ref in refs)
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_grid_owner_native_temp_spill_and_quota_retry(tmp_path):
    from types import SimpleNamespace
    from track.v2.candidates.temp_quota import TempQuota
    runtime = SimpleNamespace(duckdb_memory_limit="16MB", duckdb_threads=1,
                              duckdb_max_temp_directory_size="64MB")
    owner = GridSegmentRelation(runtime, tmp_path)
    events = []
    try:
        owner._quota = TempQuota(owner._connection, "0B", "grid-test",
                                 limits=("0B", "64MB"), emit=events.append)
        owner._connection.execute(
            "CREATE TEMP TABLE stress_source AS SELECT i, md5(i::VARCHAR) AS value "
            "FROM range(120000) t(i)"
        )
        owner._connection.execute("SET max_temp_directory_size='0B'")
        owner._quota.run("test_native_sort", lambda: owner._connection.execute(
            "CREATE TEMP TABLE sorted_stress AS "
            "SELECT * FROM stress_source ORDER BY value DESC"
        ))
        assert owner._connection.execute("SELECT count(*) FROM sorted_stress").fetchone() == (120000,)
        assert owner._connection.execute(
            "SELECT coalesce(sum(size), 0) FROM duckdb_temporary_files()"
        ).fetchone()[0] > 0
        assert len(events) == 1 and "old=0B new=64MB" in events[0]
    finally:
        owner.close()
    assert not list((tmp_path / "duckdb").iterdir())


def test_spilled_text_columns_preserve_unicode_and_empty_reasons(tmp_path):
    first = _point(10, 0, 0.0)
    injected = _point(20, 1, 1.0, kind=1)
    injected['generated_reason'] = '正交补点：转向'
    third = _point(11, 2, 2.0)
    batch = _batch(_row('陕A-长车辆标识', first, next_=injected),
                   _row('陕A-长车辆标识', injected, prev=first, next_=third))
    expected = build_grid_segments([batch], max_segments=10, max_bytes=100_000)
    runtime = SimpleNamespace(duckdb_memory_limit='16MB', duckdb_threads=1,
                              duckdb_max_temp_directory_size='64MB')
    with open_grid_segments([batch], max_segments=0, max_bytes=100_000,
                            runtime=runtime, data_root=tmp_path) as owner:
        actual = owner.block(0, owner.count)
        for name in expected:
            np.testing.assert_array_equal(actual[name], expected[name])
