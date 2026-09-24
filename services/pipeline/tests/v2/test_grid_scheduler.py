from collections import Counter
from contextlib import nullcontext
from threading import get_ident
from types import SimpleNamespace
from uuid import uuid4

import pytest

from track.v2 import grid_cross_track_candidates
from track.v2.models import CrossTrackCandidate, PointRef, RefinedPointRef


class _WorkerConnection:
    def __init__(self):
        self.closed = False

    def transaction(self):
        return nullcontext()

    def close(self):
        self.closed = True


def _candidate():
    first = PointRef("public.source_points", "1", True)
    second = PointRef("public.source_points", "2", True)
    return CrossTrackCandidate(
        point_ref=first, from_segment_start_point_ref=first,
        to_segment_start_point_ref=second, vehicle_id="a|b",
        timestamp=1.0, lon=110.0, lat=20.0, x=1.0, y=2.0,
        heading=90.0, speed=5.0,
        from_segment_start=RefinedPointRef("kept", 1, 1, 0),
        to_segment_start=RefinedPointRef("kept", 2, 2, 0),
    )


def test_grid_scheduler_retries_then_deduplicates_and_writes_on_main_thread(monkeypatch):
    main_thread = get_ident()
    opened = []
    attempts = Counter()
    grids = grid_cross_track_candidates.create_grid((0.0, 0.0, 10.0, 1.0), 5.0)
    monkeypatch.setattr(grid_cross_track_candidates, "create_grid", lambda *_args: grids)
    logs = []
    monkeypatch.setattr(grid_cross_track_candidates, "log_info", logs.append)
    real_wait = grid_cross_track_candidates.wait
    wait_calls = 0
    def delayed_wait(futures, **kwargs):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            return set(), set(futures)
        return real_wait(futures, **kwargs)
    monkeypatch.setattr(grid_cross_track_candidates, "wait", delayed_wait)
    ticks = iter((0.0,))
    monkeypatch.setattr(
        grid_cross_track_candidates, "time",
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

    monkeypatch.setattr(grid_cross_track_candidates, "fetch_points_in_grid", fetch)
    monkeypatch.setattr(grid_cross_track_candidates, "build_grid_segments", lambda batches, **_kw:
                        {"grid": batches[0], "segment_identity": (1, 2)})
    monkeypatch.setattr(grid_cross_track_candidates, "iter_grid_crossings", lambda segments, *_a, **_kw:
                        [segments["grid"]])
    pair = (("a", 1, 0, 1, 0, 2), ("b", 2, 0, 2, 0, 3))
    monkeypatch.setattr(grid_cross_track_candidates, "iter_grid_candidates", lambda *_args:
                        [(pair, _candidate())])
    updates = []
    status = SimpleNamespace(update=lambda **kwargs: updates.append(kwargs))
    store = SimpleNamespace(items=[], sealed=False)
    def extend(items):
        assert get_ident() == main_thread
        store.items.extend(items)
    store.extend = extend
    store.seal = lambda: setattr(store, "sealed", True)
    result = grid_cross_track_candidates.run_grid_cross_track_task(
        uuid4(), (0.0, 0.0, 10.0, 1.0), None, None, None,
        connect_worker=connect_worker,
        grid_cell_size_m=5.0, max_segments=10, max_segment_bytes=100_000,
        max_grid_memberships=100, max_overflow_pair_space=100,
        max_candidates=10, max_result_bytes=100_000,
        thread_count=2, candidate_store=store, live_status=status,
    )

    assert result is store and store.sealed
    assert len(store.items) == 1
    assert store.items[0].cross_track_calculate_index == 0
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
    monkeypatch.setattr(grid_cross_track_candidates, "log_info", lambda _message: None)
    attempts = []
    def over_budget(_conn, grid_index, *_args):
        attempts.append(grid_index)
        raise grid_cross_track_candidates.GridSegmentBudgetError("grid budget exceeded")
    monkeypatch.setattr(grid_cross_track_candidates, "fetch_points_in_grid", over_budget)
    with pytest.raises(grid_cross_track_candidates.GridSegmentBudgetError):
        grid_cross_track_candidates.run_grid_cross_track_task(
            uuid4(), (0.0, 0.0, 1.0, 1.0), None, None, None,
            connect_worker=_WorkerConnection,
            grid_cell_size_m=5.0, max_segments=1, max_segment_bytes=1,
            max_grid_memberships=1, max_overflow_pair_space=1,
            max_candidates=1, max_result_bytes=1, thread_count=1,
        )
    assert attempts == [(0, 0)]


def test_grid_scheduler_caps_worker_candidate_results_without_retry(monkeypatch):
    monkeypatch.setattr(grid_cross_track_candidates, "log_info", lambda _message: None)
    attempts = []
    def fetch(_conn, grid_index, *_args):
        attempts.append(grid_index)
        return [grid_index]
    monkeypatch.setattr(grid_cross_track_candidates, "fetch_points_in_grid", fetch)
    monkeypatch.setattr(grid_cross_track_candidates, "build_grid_segments", lambda _batches, **_kw:
                        {"segment_identity": (1, 2)})
    monkeypatch.setattr(grid_cross_track_candidates, "iter_grid_crossings", lambda *_args, **_kw:
                        [object()])
    pair = (("a", 1, 0, 1, 0, 2), ("b", 2, 0, 2, 0, 3))
    monkeypatch.setattr(grid_cross_track_candidates, "iter_grid_candidates", lambda *_args:
                        [(pair, _candidate())])
    with pytest.raises(grid_cross_track_candidates.GridSegmentBudgetError, match="candidate result"):
        grid_cross_track_candidates.run_grid_cross_track_task(
            uuid4(), (0.0, 0.0, 1.0, 1.0), None, None, None,
            connect_worker=_WorkerConnection,
            grid_cell_size_m=5.0, max_segments=2, max_segment_bytes=100_000,
            max_grid_memberships=2, max_overflow_pair_space=2,
            max_candidates=0, max_result_bytes=100_000, thread_count=1,
        )
    assert attempts == [(0, 0)]
