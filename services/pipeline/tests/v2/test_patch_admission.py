from copy import copy
from dataclasses import FrozenInstanceError, replace
import random

import pytest

from track.v2.patch_admission import (
    ReservationBook,
    ReservationToken,
    ResourceUnavailable,
)


INVALID_BYTES = (-1, True, False, 1.0, "1", None)


@pytest.mark.parametrize("field", ["memory_capacity_bytes", "spool_limit_bytes", "initial_spool_bytes"])
@pytest.mark.parametrize("value", INVALID_BYTES)
def test_constructor_rejects_invalid_bytes(field, value):
    options = dict(memory_capacity_bytes=100, spool_limit_bytes=100, initial_spool_bytes=0)
    options[field] = value
    with pytest.raises(ValueError, match=field):
        ReservationBook(**options)


def test_initial_usage_is_retained_and_cannot_exceed_limit():
    with pytest.raises(ValueError, match="exceeds"):
        ReservationBook(100, 9, 10)
    snapshot = ReservationBook(100, 10, 10).snapshot()
    assert snapshot.retained_spool_bytes == 10
    assert snapshot.available_spool_bytes == 0
    assert snapshot.available_memory_bytes == 100
    assert snapshot.active_count == 0


def test_concurrent_reservations_share_exact_capacity_and_settle_separately():
    book = ReservationBook(100, 200, 20)
    first = book.reserve("first", memory_bytes=40, spool_bytes=80)
    second = book.reserve("second", memory_bytes=60, spool_bytes=100)
    full = book.snapshot()
    assert (full.available_memory_bytes, full.available_spool_bytes) == (0, 0)
    assert (full.reserved_memory_bytes, full.reserved_spool_bytes) == (100, 180)
    assert full.active_count == 2
    book.settle(first, retained_spool_bytes=30)
    partial = book.snapshot()
    assert partial.reserved_memory_bytes == 60
    assert partial.reserved_spool_bytes == 100
    assert partial.retained_spool_bytes == 50
    assert partial.available_memory_bytes == 40
    assert partial.available_spool_bytes == 50
    assert partial.active_count == 1
    book.settle(second, retained_spool_bytes=100)
    assert book.snapshot().retained_spool_bytes == 150
    assert book.snapshot().reserved_spool_bytes == 0
    assert book.snapshot().available_memory_bytes == 100


def test_token_and_snapshot_are_immutable():
    book = ReservationBook(10, 10)
    token = book.reserve("attempt", memory_bytes=2, spool_bytes=3)
    snapshot = book.snapshot()
    with pytest.raises(FrozenInstanceError):
        token.spool_bytes = 0
    with pytest.raises(FrozenInstanceError):
        snapshot.reserved_memory_bytes = 0
    book.settle(token, retained_spool_bytes=0)
    assert snapshot.active_count == 1
    assert book.snapshot().active_count == 0


@pytest.mark.parametrize("key", [None, 1, True, (), "", "a" * 1025])
def test_invalid_key_does_not_change_accounting(key):
    book = ReservationBook(10, 10)
    before = book.snapshot()
    with pytest.raises(ValueError, match="attempt_key"):
        book.reserve(key, memory_bytes=1, spool_bytes=1)
    assert book.snapshot() == before


@pytest.mark.parametrize("field", ["memory_bytes", "spool_bytes"])
@pytest.mark.parametrize("value", INVALID_BYTES)
def test_invalid_reservation_does_not_change_accounting(field, value):
    book = ReservationBook(10, 10)
    before = book.snapshot()
    values = dict(memory_bytes=1, spool_bytes=1)
    values[field] = value
    with pytest.raises(ValueError, match=field):
        book.reserve("attempt", **values)
    assert book.snapshot() == before


@pytest.mark.parametrize(
    "memory,spool,resource,requested,available",
    [(7, 0, "memory", 7, 6), (0, 7, "spool", 7, 6), (100, 100, "memory", 100, 6)],
)
def test_quota_failure_is_atomic_and_reports_safe_dimensions(memory, spool, resource, requested, available):
    book = ReservationBook(10, 12, 2)
    token = book.reserve("first", memory_bytes=4, spool_bytes=4)
    before = book.snapshot()
    with pytest.raises(ResourceUnavailable) as error:
        book.reserve("rejected-secret-key", memory_bytes=memory, spool_bytes=spool)
    assert error.value.resource == resource
    assert error.value.requested_bytes == requested
    assert error.value.available_bytes == available
    assert "secret" not in str(error.value)
    assert book.snapshot() == before
    book.settle(token, retained_spool_bytes=0)
    book.reserve("rejected-secret-key", memory_bytes=1, spool_bytes=1)


def test_duplicate_active_key_rejected_without_change():
    book = ReservationBook(10, 10)
    book.reserve("same", memory_bytes=1, spool_bytes=1)
    before = book.snapshot()
    with pytest.raises(ValueError, match="already"):
        book.reserve("same", memory_bytes=1, spool_bytes=1)
    assert book.snapshot() == before


def test_old_token_cannot_settle_reused_key():
    book = ReservationBook(10, 10)
    old = book.reserve("key", memory_bytes=1, spool_bytes=1)
    book.settle(old, retained_spool_bytes=1)
    current = book.reserve("key", memory_bytes=2, spool_bytes=2)
    before = book.snapshot()
    with pytest.raises(ValueError, match="stale"):
        book.settle(old, retained_spool_bytes=0)
    assert book.snapshot() == before
    book.settle(current, retained_spool_bytes=2)
    assert book.snapshot().retained_spool_bytes == 3


def test_copy_reconstruction_and_foreign_tokens_cannot_settle():
    book = ReservationBook(10, 10)
    token = book.reserve("key", memory_bytes=1, spool_bytes=2)
    foreign = ReservationBook(10, 10).reserve("key", memory_bytes=1, spool_bytes=2)
    before = book.snapshot()
    for fake in (copy(token), replace(token), ReservationToken("key", 1, 2), foreign, None):
        with pytest.raises(ValueError, match="token"):
            book.settle(fake, retained_spool_bytes=0)
        assert book.snapshot() == before
    book.settle(token, retained_spool_bytes=0)
    with pytest.raises(ValueError, match="settled"):
        book.settle(token, retained_spool_bytes=0)


@pytest.mark.parametrize("value", [*INVALID_BYTES, 4])
def test_invalid_settlement_retains_full_active_allowance(value):
    book = ReservationBook(10, 10)
    token = book.reserve("key", memory_bytes=2, spool_bytes=3)
    before = book.snapshot()
    with pytest.raises(ValueError, match="retained_spool_bytes"):
        book.settle(token, retained_spool_bytes=value)
    assert book.snapshot() == before


def test_failure_with_unknown_files_retains_full_disk_until_confirmed_deletion():
    book = ReservationBook(10, 10)
    token = book.reserve("failed", memory_bytes=10, spool_bytes=10)
    # Caller first confirms the process has stopped; no assumption about files.
    book.settle(token, retained_spool_bytes=token.spool_bytes)
    assert book.snapshot().available_memory_bytes == 10
    assert book.snapshot().available_spool_bytes == 0
    with pytest.raises(ResourceUnavailable):
        book.reserve("retry", memory_bytes=10, spool_bytes=1)
    book.reclaim_spool(deleted_bytes=4)
    assert book.snapshot().available_spool_bytes == 4
    retry = book.reserve("retry", memory_bytes=10, spool_bytes=4)
    book.settle(retry, retained_spool_bytes=2)
    assert book.snapshot().retained_spool_bytes == 8


@pytest.mark.parametrize("value", [*INVALID_BYTES, 6])
def test_invalid_reclamation_cannot_credit_active_allowances(value):
    book = ReservationBook(10, 10, 5)
    book.reserve("running", memory_bytes=10, spool_bytes=5)
    before = book.snapshot()
    with pytest.raises(ValueError):
        book.reclaim_spool(deleted_bytes=value)
    assert book.snapshot() == before


def test_reclamation_only_changes_retained_usage():
    book = ReservationBook(10, 10, 5)
    book.reserve("running", memory_bytes=7, spool_bytes=5)
    book.reclaim_spool(deleted_bytes=5)
    snapshot = book.snapshot()
    assert snapshot.reserved_spool_bytes == 5
    assert snapshot.reserved_memory_bytes == 7
    assert snapshot.active_count == 1
    assert snapshot.retained_spool_bytes == 0
    assert snapshot.available_spool_bytes == 5


def test_zero_allowances_and_arbitrary_precision_integer_accounting():
    book = ReservationBook(0, 0)
    token = book.reserve("zero", memory_bytes=0, spool_bytes=0)
    book.settle(token, retained_spool_bytes=0)
    book.reclaim_spool(deleted_bytes=0)
    huge = 10 ** 100
    book = ReservationBook(huge, huge)
    token = book.reserve("huge", memory_bytes=huge, spool_bytes=huge)
    book.settle(token, retained_spool_bytes=huge)
    book.reclaim_spool(deleted_bytes=huge)
    assert book.snapshot().available_spool_bytes == huge


def test_million_settlements_do_not_accumulate_history():
    book = ReservationBook(1, 1)
    for index in range(1_000_000):
        token = book.reserve(str(index), memory_bytes=1, spool_bytes=1)
        book.settle(token, retained_spool_bytes=0)
    snapshot = book.snapshot()
    assert snapshot.active_count == 0
    assert snapshot.available_memory_bytes == snapshot.available_spool_bytes == 1
    # An observable object-size bound guards against a completed-token archive.
    assert book._active.__sizeof__() < 1024
    assert not hasattr(book, "__dict__")


def test_random_interleaving_preserves_model_and_boundaries():
    rng = random.Random(732)
    book = ReservationBook(100, 500, 7)
    active = {}
    retained = 7
    for index in range(2000):
        action = rng.randrange(3)
        if action == 0:
            memory, spool = rng.randrange(30), rng.randrange(50)
            fits = (
                sum(token.memory_bytes for token in active.values()) + memory <= 100
                and retained + sum(token.spool_bytes for token in active.values()) + spool <= 500
            )
            if fits:
                active[index] = book.reserve(str(index), memory_bytes=memory, spool_bytes=spool)
            else:
                before = book.snapshot()
                with pytest.raises(ResourceUnavailable):
                    book.reserve(str(index), memory_bytes=memory, spool_bytes=spool)
                assert book.snapshot() == before
        elif action == 1 and active:
            token = active.pop(rng.choice(tuple(active)))
            remaining = rng.randrange(token.spool_bytes + 1)
            book.settle(token, retained_spool_bytes=remaining)
            retained += remaining
        else:
            deleted = rng.randrange(retained + 1)
            book.reclaim_spool(deleted_bytes=deleted)
            retained -= deleted
        snapshot = book.snapshot()
        assert snapshot.active_count == len(active)
        assert snapshot.retained_spool_bytes == retained
        assert snapshot.reserved_memory_bytes == sum(token.memory_bytes for token in active.values())
        assert snapshot.reserved_spool_bytes == sum(token.spool_bytes for token in active.values())
        assert snapshot.available_memory_bytes >= 0
        assert snapshot.available_spool_bytes >= 0
