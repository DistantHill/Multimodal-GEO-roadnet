from threading import Event, enumerate as threads
from unittest.mock import patch

import pytest

from track.v2.heartbeat import heartbeat_progress, observed_operation, operation_heartbeat
from track.v2.logger import LiveStatus
from track.v2.pipeline_events import EventProgressAdapter, PipelineEventKind


def test_blocking_operation_emits_repeated_heartbeat_and_stops_on_error():
    messages = []
    tick = Event()

    def emit(message):
        messages.append(message)
        if len(messages) >= 3:
            tick.set()

    with patch('track.v2.heartbeat.log_info'), pytest.raises(RuntimeError, match='query failed'):
        with operation_heartbeat('提交', emit=emit, interval=.01):
            assert tick.wait(2), 'no heartbeat while caller is blocked'
            raise RuntimeError('query failed')
    assert len(messages) >= 3
    assert not any(thread.name == 'pipeline-heartbeat' for thread in threads())


def test_nested_operation_reuses_observer_and_reports_batch_counts():
    tick = Event()
    messages = []

    def emit(message):
        messages.append(message)
        if '批量查询' in message and '10/100 行' in message:
            tick.set()

    with patch('track.v2.heartbeat.log_info'):
        with operation_heartbeat('写入', emit=emit, interval=.01):
            with operation_heartbeat('批量查询'):
                heartbeat_progress(10, total=100)
                assert tick.wait(2)
                assert sum(thread.name == 'pipeline-heartbeat' for thread in threads()) == 1
    assert not any(thread.name == 'pipeline-heartbeat' for thread in threads())


def test_generator_close_stops_observer():
    @observed_operation('读取')
    def rows():
        yield 1
        yield 2

    iterator = rows()
    assert next(iterator) == 1
    iterator.close()
    assert not any(thread.name == 'pipeline-heartbeat' for thread in threads())


def test_detect_only_reaches_full_progress_after_successful_stop():
    events = []
    adapter = EventProgressAdapter('Detect', events.append, completion_pending=True)
    adapter.start()
    adapter.update(progress=1.0)
    adapter.update(detail='校验与提交')
    adapter.stop()
    assert events[1].progress == .99
    assert events[2].progress is None
    assert events[3].kind == PipelineEventKind.STAGE_FINISH
    assert events[3].progress == 1.0


def test_indeterminate_operation_clears_previous_percentage():
    status = LiveStatus('Detect')
    status.update(progress=1.0)
    status.update(detail='校验与提交')
    assert status._progress is None
