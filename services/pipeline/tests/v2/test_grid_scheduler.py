from collections import Counter
from contextlib import nullcontext
from threading import get_ident
from types import SimpleNamespace
from uuid import uuid4

import pytest

from track.v2.candidates import detect_grid_crossings
import numpy as np
from .detect_columns_helpers import subtrack, TRANSFORMER
from .config_factory import intersect_config
from track.v2.candidates.collect_candidates import extract_cross_track_candidates


class _WorkerConnection:
    def __init__(self):
        self.closed = False

    def transaction(self):
        return nullcontext()

    def close(self):
        self.closed = True


def _candidate():
    batch, _, _ = extract_cross_track_candidates(
        {1: subtrack([[-10, 0], [10, 0]], subtrack_index=1, headings=[90, 90]),
         2: subtrack([[0, -10], [0, 10]], subtrack_index=2, headings=[0, 0])},
        TRANSFORMER, intersect_config())
    return batch


def test_grid_scheduler_retries_then_deduplicates_and_writes_on_main_thread(monkeypatch):
    main_thread = get_ident()
    opened = []
    attempts = Counter()
    grids = detect_grid_crossings.create_grid((0.0, 0.0, 10.0, 1.0), 5.0)
    monkeypatch.setattr(detect_grid_crossings, "create_grid", lambda *_args: grids)
    logs = []
    monkeypatch.setattr(detect_grid_crossings, "log_info", logs.append)
    real_wait = detect_grid_crossings.wait
    wait_calls = 0
    def delayed_wait(futures, **kwargs):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            return set(), set(futures)
        return real_wait(futures, **kwargs)
    monkeypatch.setattr(detect_grid_crossings, "wait", delayed_wait)
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
    monkeypatch.setattr(detect_grid_crossings, "build_grid_segments", lambda batches, **_kw:
                        {"grid": batches[0], "subtrack_index": np.array([1, 2]), "segment_identity": np.array([[1, 2]])})
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_crossings", lambda segments, *_a, **_kw:
                        [segments["grid"]])
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_candidates", lambda *_args:
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
        thread_count=2, candidate_store=store, live_status=status,
    )

    assert result is store and store.sealed
    assert len(store.items) == 1
    assert store.items[0].calculation_index.tolist() == [0]
    assert attempts == {(0, 0): 1, (1, 0): 2}
    assert all(cell.status == "done" for column in grids for cell in column)
    assert opened and all(conn.closed for conn in opened)
    assert "0/2" in updates[0]["detail"]
    assert "2/2" in updates[-1]["detail"]
    assert wait_calls >= 2
    assert len(logs) == 2
    assert "0/2" in logs[1] and "运行中" in logs[1]
    assert "耗时 3.1s" in logs[1]


def test_grid_scheduler_does_not_retry_budget_error(monkeypatch):
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
            max_candidates=1, max_result_bytes=1, thread_count=1,
        )
    assert attempts == [(0, 0)]


def test_grid_scheduler_caps_worker_candidate_results_without_retry(monkeypatch):
    monkeypatch.setattr(detect_grid_crossings, "log_info", lambda _message: None)
    attempts = []
    def fetch(_conn, grid_index, *_args):
        attempts.append(grid_index)
        return [grid_index]
    monkeypatch.setattr(detect_grid_crossings, "fetch_points_in_grid", fetch)
    monkeypatch.setattr(detect_grid_crossings, "build_grid_segments", lambda _batches, **_kw:
                        {"subtrack_index": np.array([1, 2]), "segment_identity": np.array([[1, 2]])})
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_crossings", lambda *_args, **_kw:
                        [object()])
    monkeypatch.setattr(detect_grid_crossings, "iter_grid_candidates", lambda *_args:
                        [_candidate()])
    with pytest.raises(detect_grid_crossings.GridSegmentBudgetError, match="candidate result"):
        detect_grid_crossings.run_grid_cross_track_task(
            uuid4(), (0.0, 0.0, 1.0, 1.0), None, None, None,
            connect_worker=_WorkerConnection,
            grid_cell_size_m=5.0, max_segments=2, max_segment_bytes=100_000,
            max_grid_memberships=2, max_overflow_pair_space=2,
            max_candidates=0, max_result_bytes=100_000, thread_count=1,
        )
    assert attempts == [(0, 0)]
