from collections import Counter
from contextlib import contextmanager, nullcontext
from threading import Event, get_ident
from types import SimpleNamespace
from uuid import uuid4

import pytest

from track.v2.candidates import detect_grid_crossings
from track.v2.candidates.detect_grid_crossings import iter_grid_crossings, iter_grid_candidates
import numpy as np
from .detect_columns_helpers import subtrack, TRANSFORMER
from .test_detect_identity_routes import _grid_columns
from .config_factory import intersect_config


class _WorkerConnection:
    def __init__(self):
        self.closed = False

    def transaction(self):
        return nullcontext()

    def close(self):
        self.closed = True


_RUNTIME = SimpleNamespace(duckdb_memory_limit="16MB", duckdb_threads=1,
                           duckdb_max_temp_directory_size="64MB")


def _candidate():
    tracks = [subtrack([[-10, 0], [10, 0]], subtrack_index=1, headings=[90, 90]),
              subtrack([[0, -10], [0, 10]], subtrack_index=2, headings=[0, 0])]
    segments = _grid_columns(tracks)
    crossings = iter_grid_crossings(segments, intersect_config(),
                                    max_grid_memberships=1000, max_overflow_pair_space=1000)
    return next(iter_grid_candidates(segments, crossings, TRANSFORMER))


def test_grid_scheduler_retries_then_deduplicates_and_writes_on_main_thread(monkeypatch, tmp_path):
    main_thread = get_ident()
    opened = []
    attempts = Counter()
    grids = detect_grid_crossings.create_grid((0.0, 0.0, 10.0, 1.0), 5.0)
    monkeypatch.setattr(detect_grid_crossings, "create_grid", lambda *_args: grids)
    logs = []
    monkeypatch.setattr(detect_grid_crossings, "log_info", logs.append)
    ticks = iter((0.0,))
    monkeypatch.setattr(
        detect_grid_crossings, "time",
        SimpleNamespace(monotonic=lambda: next(ticks, 3.1)),
    )
    def connect_worker():
        assert get_ident() != main_thread
        opened.append(_WorkerConnection())
        return opened[-1]

    def fetch(_conn, grid_index, *_args):
        attempts[grid_index] += 1
        if grid_index == (1, 0) and attempts[grid_index] == 1:
            raise RuntimeError("transient read error")
        return [grid_index]

    monkeypatch.setattr(detect_grid_crossings, "fetch_points_in_grid", fetch)
    @contextmanager
    def segments(batches, **_kw):
        yield {"grid": batches[0], "subtrack_index": np.array([1, 2]), "segment_identity": np.array([[1, 2]])}
    monkeypatch.setattr(detect_grid_crossings, "open_grid_segments", segments)
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_crossings", lambda segments, *_a, **_kw:
                        [segments["grid"]])
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_candidates", lambda *_args, **_kwargs:
                        [_candidate()])
    updates = []
    status = SimpleNamespace(update=lambda **kwargs: updates.append(kwargs))
    store = SimpleNamespace(items=[], sealed=False)
    def extend(items):
        assert get_ident() == main_thread
        store.items.extend(items)
    store.extend = extend
    store.seal = lambda: setattr(store, "sealed", True)
    result = detect_grid_crossings.run_grid_cross_track_task(
        uuid4(), (0.0, 0.0, 10.0, 1.0), None, None, None,
        connect_worker=connect_worker,
        grid_cell_size_m=5.0, max_segments=10, max_segment_bytes=100_000,
        max_grid_memberships=100, max_overflow_pair_space=100,
        max_candidates=10, max_result_bytes=100_000,
        runtime=_RUNTIME, data_root=tmp_path,
        thread_count=2, candidate_store=store, live_status=status,
    )

    assert result is store and store.sealed
    assert len(store.items) == 1
    assert store.items[0].calculation_index.tolist() == [0]
    assert attempts == {(0, 0): 1, (1, 0): 2}
    assert all(cell.status == "done" for column in grids for cell in column)
    assert opened and all(conn.closed for conn in opened)
    completed = next(message for message in logs if message.startswith("cross-track grid="))
    for key in ("read_s=", "sort_s=", "index_s=", "candidate_s=", "gather_s=",
                "kernel_s=", "output_s=", "pair_count=", "pre_intersection_s="):
        assert key in completed
    assert any(message.startswith("cross-track finalize output_s=") for message in logs)
    assert "0/2" in updates[0]["detail"]
    assert "2/2" in updates[-1]["detail"]
    assert any("0/2" in message and "耗时 3.1s" in message for message in logs)


def test_grid_scheduler_shows_running_grid_segments_in_shared_status(monkeypatch, tmp_path):
    logs = []
    monkeypatch.setattr(detect_grid_crossings, "log_info", logs.append)
    ticks = iter((0.0,))
    monkeypatch.setattr(
        detect_grid_crossings, "time",
        SimpleNamespace(monotonic=lambda: next(ticks, 3.1)),
    )
    release_worker = Event()
    updates = []
    monkeypatch.setattr(detect_grid_crossings, "fetch_points_in_grid", lambda *_args: ())

    @contextmanager
    def segments(_batches, **_kwargs):
        yield {"subtrack_index": np.array([1, 2, 3]), "segment_identity": np.empty((3, 5))}

    monkeypatch.setattr(detect_grid_crossings, "open_grid_segments", segments)
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_crossings", lambda *_args, **_kwargs: ())

    def candidates(*_args, **_kwargs):
        assert release_worker.wait(timeout=5)
        yield from ()

    monkeypatch.setattr(detect_grid_crossings, "iter_grid_candidates", candidates)

    def update(**kwargs):
        updates.append(kwargs)
        if "(0, 0) 线段数：3" in kwargs.get("sub_detail", ""):
            release_worker.set()

    detect_grid_crossings.run_grid_cross_track_task(
        uuid4(), (0.0, 0.0, 1.0, 1.0), None, None, None,
        connect_worker=_WorkerConnection, grid_cell_size_m=5.0,
        max_segments=3, max_segment_bytes=100_000, max_grid_memberships=3,
        max_overflow_pair_space=3, max_candidates=0, max_result_bytes=0,
        runtime=_RUNTIME, data_root=tmp_path, thread_count=1,
        live_status=SimpleNamespace(update=update),
    )

    assert release_worker.is_set()
    assert "运行时间：" in next(item["sub_detail"] for item in updates
                            if "线段数：3" in item["sub_detail"])
    assert any("当前grid(1)" in message and "(0, 0) 线段数：3" in message
               for message in logs)
    assert updates[-1]["sub_detail"] == "当前grid(0)："


def test_grid_scheduler_does_not_retry_budget_error(monkeypatch, tmp_path):
    monkeypatch.setattr(detect_grid_crossings, "log_info", lambda _message: None)
    attempts = []
    def over_budget(_conn, grid_index, *_args):
        attempts.append(grid_index)
        raise detect_grid_crossings.GridSegmentBudgetError("grid budget exceeded")
    monkeypatch.setattr(detect_grid_crossings, "fetch_points_in_grid", over_budget)
    with pytest.raises(detect_grid_crossings.GridSegmentBudgetError):
        detect_grid_crossings.run_grid_cross_track_task(
            uuid4(), (0.0, 0.0, 1.0, 1.0), None, None, None,
            connect_worker=_WorkerConnection,
            grid_cell_size_m=5.0, max_segments=1, max_segment_bytes=1,
            max_grid_memberships=1, max_overflow_pair_space=1,
            max_candidates=1, max_result_bytes=1,
            runtime=_RUNTIME, data_root=tmp_path, thread_count=1,
        )
    assert attempts == [(0, 0)]


def test_grid_scheduler_streams_worker_candidate_results_past_old_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(detect_grid_crossings, "log_info", lambda _message: None)
    attempts = []
    def fetch(_conn, grid_index, *_args):
        attempts.append(grid_index)
        return [grid_index]
    monkeypatch.setattr(detect_grid_crossings, "fetch_points_in_grid", fetch)
    @contextmanager
    def segments(_batches, **_kw):
        yield {"subtrack_index": np.array([1, 2]), "segment_identity": np.array([[1, 2]])}
    monkeypatch.setattr(detect_grid_crossings, "open_grid_segments", segments)
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_crossings", lambda *_args, **_kw:
                        [object()])
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_candidates", lambda *_args, **_kwargs:
                        [_candidate()])
    result = detect_grid_crossings.run_grid_cross_track_task(
            uuid4(), (0.0, 0.0, 1.0, 1.0), None, None, None,
            connect_worker=_WorkerConnection,
            grid_cell_size_m=5.0, max_segments=2, max_segment_bytes=100_000,
            max_grid_memberships=2, max_overflow_pair_space=2,
            max_candidates=0, max_result_bytes=100_000,
            runtime=_RUNTIME, data_root=tmp_path, thread_count=1,
        )
    assert attempts == [(0, 0)]
    assert len(result) == 1


def test_grid_scheduler_discards_partial_attempt_before_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(detect_grid_crossings, "log_info", lambda _message: None)
    attempts = Counter()
    main = get_ident()
    def fetch(_conn, cell, *_args):
        attempts[cell] += 1
        return [cell]
    monkeypatch.setattr(detect_grid_crossings, "fetch_points_in_grid", fetch)
    @contextmanager
    def segments(_batches, **_kwargs):
        yield {"subtrack_index": np.array([1, 2]), "segment_identity": np.array([[1, 2]])}
    monkeypatch.setattr(detect_grid_crossings, "open_grid_segments", segments)
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_crossings", lambda *_a, **_kw: [object()])
    def candidates(_segments, _crossings, _transformer, **_kwargs):
        yield _candidate()
        if attempts[(0, 0)] == 1:
            raise RuntimeError("failed after first candidate batch")
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_candidates", candidates)

    class Staged:
        def __init__(self):
            self.rows = []
            self.finished = False
            self.discards = 0
        def append_staged(self, grid_order, local_start, batch):
            assert get_ident() == main
            self.rows.append((grid_order, local_start, batch))
        def discard_grid(self, grid_order):
            assert get_ident() == main
            self.rows = [row for row in self.rows if row[0] != grid_order]
            self.discards += 1
        def finish_grid(self, grid_order):
            assert get_ident() == main
            assert len(self.rows) == 1 and self.rows[0][:2] == (grid_order, 0)
            self.finished = True
        def finalize_staged(self):
            assert self.finished

    store = Staged()
    result = detect_grid_crossings.run_grid_cross_track_task(
        uuid4(), (0.0, 0.0, 1.0, 1.0), None, None, None,
        connect_worker=_WorkerConnection, grid_cell_size_m=5.0,
        max_segments=2, max_segment_bytes=100_000, max_grid_memberships=2,
        max_overflow_pair_space=2, max_candidates=0, max_result_bytes=0,
        runtime=_RUNTIME, data_root=tmp_path,
        thread_count=1, candidate_store=store,
    )
    assert result is store and store.discards == 1
    assert attempts[(0, 0)] == 2
    assert len(store.rows) == 1
