"""Bounded scheduling tests, including actual spawned process reuse."""

from concurrent.futures import FIRST_COMPLETED, Future
import json
import os
from pathlib import Path
import sys
import time
import weakref

import pytest

from track.v2.patch_pool import (
    DispatchPause, PatchExecutionError, PatchTask, PoolShutdownUnconfirmed,
    RemotePatchFailure, run_bounded_patches,
)


# A child imports this module only after its initializer configured thread counts.
_IMPORT_THREAD_COUNT = os.environ.get("OPENBLAS_NUM_THREADS")
_NUMPY_BEFORE_KERNEL_IMPORT = "numpy" in sys.modules


def test_admission_pause_drains_work_without_rebuilding_the_pool():
    pool = FakePool()
    consumed = []

    def tasks():
        yield task(1)
        yield DispatchPause()
        assert consumed == ["1"]
        yield task(2)

    assert run(tasks(), pool=pool, max_workers=2, max_inflight=2,
               consume=lambda task, result: consumed.append(task.task_id)) == 2
    assert consumed == ["1", "2"]
    assert len(pool.factory_calls) == 1


def test_admission_pause_cannot_wait_on_an_idle_pool():
    pool = FakePool()
    with pytest.raises(RuntimeError, match="without in-flight"):
        run([DispatchPause()], pool=pool)
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(), OSError("shutdown")])
def test_shutdown_interruption_is_not_reported_as_a_stopped_pool(error):
    pool = FakePool()

    def shutdown(**kwargs):
        raise error

    pool.shutdown = shutdown
    with pytest.raises(PoolShutdownUnconfirmed) as raised:
        run([task(1)], pool=pool)
    assert raised.value.executor is pool
    assert raised.value.__cause__ is error


def spawn_kernel(task):
    descriptor = json.loads(task.descriptor_json)
    if descriptor.get("unsafe_error"):
        class UnsafeError(Exception):
            def __str__(self):
                raise AssertionError("must not format arbitrary kernel exceptions")

        error = UnsafeError("secret:" + "x" * 1_000_000)
        error.unpickleable = lambda: None
        raise error
    if descriptor.get("memory_error"):
        raise MemoryError("simulated only")
    if descriptor.get("exit"):
        raise SystemExit("must not escape as a pickled BaseException")
    if descriptor.get("fail"):
        raise ValueError("kernel failed")
    if descriptor.get("oversize"):
        return json.dumps({"large": "x" * 1_000})
    if "barrier_dir" in descriptor:
        directory = Path(descriptor["barrier_dir"])
        (directory / str(os.getpid())).touch()
        deadline = time.monotonic() + 15
        while len(list(directory.iterdir())) < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError("second calculation process did not arrive")
            time.sleep(0.01)
    if descriptor.get("import_numpy"):
        import numpy
        assert numpy is not None
    return json.dumps({
        "task_id": task.task_id,
        "pid": os.getpid(),
        "import_threads": _IMPORT_THREAD_COUNT,
        "numpy_before_kernel_import": _NUMPY_BEFORE_KERNEL_IMPORT,
        "threads": {name: os.environ.get(name) for name in (
            "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
            "NUMBA_NUM_THREADS", "OMP_DYNAMIC", "MKL_DYNAMIC",
        )},
    })


class TrackedFuture(Future):
    def __init__(self):
        super().__init__()
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        return super().cancel()


class FakePool:
    def __init__(self, *, all_ready=False, fail_task=None, submit_failure=None):
        self.all_ready = all_ready
        self.fail_task = fail_task
        self.submit_failure = submit_failure
        self.submissions = []
        self.shutdown_calls = []
        self.factory_calls = []
        self.wait_sizes = []

    def factory(self, **kwargs):
        self.factory_calls.append(kwargs)
        return self

    def submit(self, function, entrypoint, task, limit):
        if task.task_id == self.submit_failure:
            raise RuntimeError("submit failed")
        future = TrackedFuture()
        self.submissions.append((future, task))
        return future

    def wait(self, futures, *, return_when):
        assert return_when == FIRST_COMPLETED
        self.wait_sizes.append(len(futures))
        selected = list(futures) if self.all_ready else [next(iter(futures))]
        for future in selected:
            task = next(task for candidate, task in self.submissions if candidate is future)
            if task.task_id == self.fail_task:
                future.set_exception(ValueError("kernel failed"))
            else:
                future.set_result((True, json.dumps({"task_id": task.task_id})))
        return set(selected), set(futures) - set(selected)

    def shutdown(self, **kwargs):
        self.shutdown_calls.append(kwargs)


def task(index):
    return PatchTask(str(index), "{}")


def run(tasks, *, pool=None, consume=lambda task, result: None, **kwargs):
    pool = pool or FakePool()
    options = dict(
        compute_entrypoint="v2.test_patch_pool:spawn_kernel", consume=consume,
        max_workers=2, max_inflight=3, executor_factory=pool.factory,
        waiter=pool.wait,
    )
    options.update(kwargs)
    return run_bounded_patches(tasks, **options)


@pytest.mark.parametrize("name", [
    "max_workers", "max_inflight", "native_threads_per_process", "max_descriptor_bytes",
])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2", None])
def test_invalid_limits_rejected_without_reading_or_constructing_pool(name, value):
    pool = FakePool()

    def source():
        raise AssertionError("input was touched")
        yield

    with pytest.raises(ValueError, match=name):
        run(source(), pool=pool, **{name: value})
    assert pool.factory_calls == []


@pytest.mark.parametrize("entrypoint", [None, "", "foo", "foo::bar", "foo:",
                                      ":bar", "foo/bar:baz", "foo..bar:baz", "foo:1",
                                      "m" * 513 + ":function"])
def test_invalid_kernel_entrypoint_rejected(entrypoint):
    with pytest.raises(ValueError, match="compute_entrypoint"):
        run([], compute_entrypoint=entrypoint)


def test_empty_source_does_not_submit_or_wait():
    pool = FakePool()
    assert run([], pool=pool) == 0
    assert pool.submissions == []
    assert pool.wait_sizes == []
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]


@pytest.mark.parametrize("all_ready", [False, True])
@pytest.mark.parametrize("limit", [1, 2, 5])
def test_backpressure_includes_completed_results_until_central_consumer_returns(all_ready, limit):
    pool = FakePool(all_ready=all_ready)
    pulled = []
    accepted = []

    def source():
        for index in range(19):
            assert len(pulled) - len(accepted) < limit
            pulled.append(str(index))
            yield task(index)

    def consume(item, result):
        assert 0 < len(pulled) - len(accepted) <= limit
        assert json.loads(result)["task_id"] == item.task_id
        # All completed-but-not-yet-accepted tasks still consume slots.
        assert len(pool.submissions) == len(pulled)
        accepted.append(item.task_id)

    assert run(source(), pool=pool, consume=consume, max_inflight=limit) == 19
    assert sorted(accepted, key=int) == pulled
    assert max(pool.wait_sizes) <= limit
    assert len(pool.factory_calls) == 1
    assert pool.factory_calls[0]["mp_context"].get_start_method() == "spawn"
    assert pool.factory_calls[0]["max_workers"] == 2
    assert pool.factory_calls[0]["initargs"] == (1,)


@pytest.mark.parametrize("failure", [RuntimeError("commit failed"), KeyboardInterrupt()])
def test_consumer_failure_stops_pulling_and_cancels_remaining_tasks(failure):
    pool = FakePool()
    pulled = []

    def source():
        for index in range(100):
            pulled.append(index)
            yield task(index)

    def consume(item, result):
        raise failure

    with pytest.raises(type(failure)):
        run(source(), pool=pool, consume=consume)
    assert pulled == [0, 1, 2]
    assert all(future.cancel_calls == 1 for future, _ in pool.submissions)
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]


def test_calculation_failure_identifies_task_without_retry_or_consuming_known_failed_batch():
    pool = FakePool(all_ready=True, fail_task="1")
    accepted = []
    with pytest.raises(PatchExecutionError) as caught:
        run((task(index) for index in range(100)), pool=pool,
            consume=lambda item, result: accepted.append(item.task_id))
    assert caught.value.task == task(1)
    assert isinstance(caught.value.original_error, ValueError)
    assert caught.value.__cause__ is caught.value.original_error
    assert accepted == []
    assert [item.task_id for _, item in pool.submissions] == ["0", "1", "2"]
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]


def test_failure_finishing_during_commit_is_observed_before_refill():
    pool = FakePool()

    def consume(item, result):
        pool.submissions[1][0].set_exception(ValueError("finished during commit"))

    with pytest.raises(PatchExecutionError) as caught:
        run((task(index) for index in range(100)), pool=pool, consume=consume)
    assert caught.value.task == task(1)
    assert len(pool.submissions) == 3


def test_structured_remote_failure_finishing_during_commit_prevents_refill():
    pool = FakePool()

    def consume(item, result):
        pool.submissions[1][0].set_result((False, '{"error_type":"Failure"}'))

    with pytest.raises(PatchExecutionError) as caught:
        run((task(index) for index in range(100)), pool=pool, consume=consume)
    assert caught.value.original_error.error_type == "Failure"
    assert len(pool.submissions) == 3


def test_source_failure_cleans_up_already_submitted_work():
    pool = FakePool()

    def source():
        yield task(0)
        raise ValueError("reader failed")

    with pytest.raises(ValueError, match="reader failed"):
        run(source(), pool=pool)
    assert len(pool.submissions) == 1
    assert pool.submissions[0][0].cancelled()
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]


def test_submit_failure_stops_and_cleans_up():
    pool = FakePool(submit_failure="1")
    with pytest.raises(RuntimeError, match="submit failed"):
        run((task(index) for index in range(10)), pool=pool)
    assert len(pool.submissions) == 1
    assert pool.submissions[0][0].cancelled()
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]


@pytest.mark.parametrize("item", [
    {}, PatchTask("", "{}"), PatchTask(True, "{}"), PatchTask("id", []),
    PatchTask("id", "[]"), PatchTask("id", "broken"), PatchTask("id", '{"x":NaN}'),
    PatchTask("i" * 33, "{}"), PatchTask("i" * 32, "{}"),
    PatchTask("id", '{"x":"' + "x" * 32 + '"}'),
    PatchTask("id", '{"x":"' + "车" * 10 + '"}'),
])
def test_only_bounded_json_task_descriptions_are_submitted(item):
    pool = FakePool()
    with pytest.raises(ValueError):
        run([item], pool=pool, max_descriptor_bytes=32)
    assert pool.submissions == []
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]


def test_asynchronous_consumer_is_not_treated_as_a_completed_commit():
    pool = FakePool()

    async def consume(item, result):
        raise AssertionError("coroutine body must not run")

    with pytest.raises(TypeError, match="synchronously"):
        run((task(index) for index in range(100)), pool=pool, consume=consume)
    assert len(pool.submissions) == 3


def test_future_returning_consumer_is_not_treated_as_a_completed_commit():
    pool = FakePool()
    with pytest.raises(TypeError, match="synchronously"):
        run((task(index) for index in range(100)), pool=pool,
            consume=lambda item, result: Future())
    assert len(pool.submissions) == 3


def test_generator_consumer_is_not_treated_as_a_completed_commit():
    pool = FakePool()

    def consume(item, result):
        raise AssertionError("deferred body must not run")
        yield

    with pytest.raises(TypeError, match="synchronously"):
        run((task(index) for index in range(100)), pool=pool, consume=consume)
    assert len(pool.submissions) == 3


def test_accepted_futures_are_released_before_next_input_batch():
    references = []

    class ImmediatePool:
        def submit(self, *args):
            future = Future()
            future.set_result((True, "{}"))
            references.append(weakref.ref(future))
            return future

        def shutdown(self, **kwargs):
            pass

    def source():
        yield task(0)
        yield task(1)
        assert all(reference() is None for reference in references)
        yield task(2)

    assert run_bounded_patches(
        source(), compute_entrypoint="v2.test_patch_pool:spawn_kernel",
        consume=lambda item, result: None, max_workers=2, max_inflight=2,
        executor_factory=lambda **kwargs: ImmediatePool(),
    ) == 3


def test_wait_failure_cleans_up():
    pool = FakePool()

    def waiter(*args, **kwargs):
        raise RuntimeError("wait failed")

    with pytest.raises(RuntimeError, match="wait failed"):
        run([task(0)], pool=pool, waiter=waiter)
    assert pool.submissions[0][0].cancelled()
    assert pool.shutdown_calls == [{"wait": True, "cancel_futures": True}]


def test_real_spawn_uses_multiple_reused_processes_and_sets_child_only_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "37")
    descriptor = json.dumps({"barrier_dir": str(tmp_path), "import_numpy": True})
    results = []
    count = run_bounded_patches(
        (PatchTask(str(index), descriptor) for index in range(6)),
        compute_entrypoint="v2.test_patch_pool:spawn_kernel",
        consume=lambda item, result: results.append(json.loads(result)),
        max_workers=2, max_inflight=2, native_threads_per_process=2,
    )
    assert count == 6
    assert {item["task_id"] for item in results} == {str(index) for index in range(6)}
    pids = {item["pid"] for item in results}
    assert len(pids) == 2
    assert os.getpid() not in pids
    assert all(item["import_threads"] == "2" for item in results)
    assert all(item["numpy_before_kernel_import"] is False for item in results)
    for item in results:
        assert item["threads"]["OMP_DYNAMIC"] == "FALSE"
        assert item["threads"]["MKL_DYNAMIC"] == "FALSE"
        assert all(value == "2" for key, value in item["threads"].items()
                   if key not in {"OMP_DYNAMIC", "MKL_DYNAMIC"})
    assert os.environ["OPENBLAS_NUM_THREADS"] == "37"


@pytest.mark.parametrize("descriptor,expected_type", [
    ('{"fail":true}', "ValueError"), ('{"oversize":true}', "ValueError"),
    ('{"unsafe_error":true}', "UnsafeError"),
    ('{"memory_error":true}', "MemoryError"), ('{"exit":true}', "SystemExit"),
])
def test_real_spawn_kernel_errors_and_oversized_results_reach_coordinator(descriptor, expected_type):
    accepted = []
    with pytest.raises(PatchExecutionError) as caught:
        run_bounded_patches(
            [PatchTask("failed", descriptor)],
            compute_entrypoint="v2.test_patch_pool:spawn_kernel",
            consume=lambda item, result: accepted.append(result),
            max_workers=1, max_inflight=1, max_descriptor_bytes=128,
        )
    assert caught.value.task.task_id == "failed"
    assert isinstance(caught.value.original_error, RemotePatchFailure)
    assert caught.value.original_error.error_type == expected_type
    assert len(caught.value.original_error.descriptor_json.encode("utf-8")) <= 128
    assert "secret" not in caught.value.original_error.descriptor_json
    expected_category = "resource" if expected_type == "MemoryError" else "unknown"
    assert caught.value.original_error.category == expected_category
    assert accepted == []


def test_tiny_failure_description_budget_omits_metadata_but_preserves_failure():
    with pytest.raises(PatchExecutionError) as caught:
        run_bounded_patches(
            [PatchTask("x", '{"fail":true}')],
            compute_entrypoint="v2.test_patch_pool:spawn_kernel",
            consume=lambda item, result: pytest.fail("must not accept failed task"),
            max_workers=1, max_inflight=1, max_descriptor_bytes=16,
        )
    assert caught.value.original_error.descriptor_json == "{}"
    assert caught.value.original_error.error_type == "RemoteError"
