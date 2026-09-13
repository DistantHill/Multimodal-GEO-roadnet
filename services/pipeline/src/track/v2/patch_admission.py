"""Single-coordinator admission accounting for one run's owned runtime tree.

Memory quantities are estimates of incremental work, not physical enforcement.
The caller first subtracts process baselines, buffers and safety headroom from
its memory budget. Disk accounting covers retained bytes plus active maximum
allowances; it is neither a filesystem free-space check nor a cross-run quota.
Initial usage must include all existing files in the owned accounting scope.

Only the coordinator calls this module, serially. No files, futures or database
transactions are inspected here. In particular, cancelling a future does not
prove that its process stopped: keep the reservation until execution has ended.
Unknown residual files must be charged at the full reserved allowance. Lower
retained counts and reclamation require trustworthy caller-side accounting of
all remaining bytes and confirmed deletion, respectively.
"""

from __future__ import annotations

from dataclasses import dataclass


def _bytes(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


class ResourceUnavailable(RuntimeError):
    """The requested allowance does not fit; accounting remains unchanged."""

    def __init__(self, resource: str, requested_bytes: int, available_bytes: int):
        self.resource = resource
        self.requested_bytes = requested_bytes
        self.available_bytes = available_bytes
        super().__init__(f"insufficient {resource} allowance")


@dataclass(frozen=True, slots=True, eq=False)
class ReservationToken:
    """An identity-bearing capability, valid only in its issuing book.

    Tokens cannot be copied, reconstructed or sent through a process boundary
    and then redeemed. The coordinator retains the original object locally.
    An attempt key is unique among active reservations, not a durable task ID.
    """

    attempt_key: str
    memory_bytes: int
    spool_bytes: int


@dataclass(frozen=True, slots=True)
class ReservationSnapshot:
    memory_capacity_bytes: int
    spool_limit_bytes: int
    reserved_memory_bytes: int
    reserved_spool_bytes: int
    retained_spool_bytes: int
    available_memory_bytes: int
    available_spool_bytes: int
    active_count: int


class ReservationBook:
    """Account for active work and retained files with O(active attempts) state.

    Zero-byte requests and zero capacities are valid. A scheduler must separately
    bound its active task count, including zero-byte tasks, using its pool limit.
    This book has no completed-task history: the durable ledger owns that state.
    """

    __slots__ = (
        "_memory_capacity", "_spool_limit", "_memory_reserved",
        "_spool_reserved", "_spool_retained", "_active",
    )

    def __init__(
        self,
        memory_capacity_bytes: int,
        spool_limit_bytes: int,
        initial_spool_bytes: int = 0,
    ) -> None:
        _bytes(memory_capacity_bytes, "memory_capacity_bytes")
        _bytes(spool_limit_bytes, "spool_limit_bytes")
        _bytes(initial_spool_bytes, "initial_spool_bytes")
        if initial_spool_bytes > spool_limit_bytes:
            raise ValueError("initial_spool_bytes exceeds spool_limit_bytes")
        self._memory_capacity = memory_capacity_bytes
        self._spool_limit = spool_limit_bytes
        self._memory_reserved = 0
        self._spool_reserved = 0
        self._spool_retained = initial_spool_bytes
        self._active: dict[str, ReservationToken] = {}

    def snapshot(self) -> ReservationSnapshot:
        return ReservationSnapshot(
            memory_capacity_bytes=self._memory_capacity,
            spool_limit_bytes=self._spool_limit,
            reserved_memory_bytes=self._memory_reserved,
            reserved_spool_bytes=self._spool_reserved,
            retained_spool_bytes=self._spool_retained,
            available_memory_bytes=self._memory_capacity - self._memory_reserved,
            available_spool_bytes=(
                self._spool_limit - self._spool_retained - self._spool_reserved
            ),
            active_count=len(self._active),
        )

    def reserve(
        self, attempt_key: str, *, memory_bytes: int, spool_bytes: int,
    ) -> ReservationToken:
        """Reserve before dispatch; quota failure makes no partial reservation."""
        if type(attempt_key) is not str or not attempt_key or len(attempt_key) > 1024:
            raise ValueError("attempt_key must be a string of 1 to 1024 characters")
        _bytes(memory_bytes, "memory_bytes")
        _bytes(spool_bytes, "spool_bytes")
        if attempt_key in self._active:
            raise ValueError("attempt_key already has an active reservation")
        memory_available = self._memory_capacity - self._memory_reserved
        spool_available = self._spool_limit - self._spool_retained - self._spool_reserved
        if memory_bytes > memory_available:
            raise ResourceUnavailable("memory", memory_bytes, memory_available)
        if spool_bytes > spool_available:
            raise ResourceUnavailable("spool", spool_bytes, spool_available)
        token = ReservationToken(attempt_key, memory_bytes, spool_bytes)
        self._active[attempt_key] = token
        self._memory_reserved += memory_bytes
        self._spool_reserved += spool_bytes
        return token

    def settle(self, token: ReservationToken, *, retained_spool_bytes: int) -> None:
        """After execution ends, release memory and retain all remaining bytes.

        A database commit is not deletion: its artifact bytes stay charged.
        For failed attempts whose files are unknown, pass ``token.spool_bytes``.
        Reported retained bytes may not exceed the enforced attempt allowance;
        a violation raises without changing state and requires caller recovery.
        """
        if type(token) is not ReservationToken:
            raise ValueError("token must be an active reservation from this book")
        if self._active.get(token.attempt_key) is not token:
            raise ValueError("token is stale, copied, foreign or already settled")
        _bytes(retained_spool_bytes, "retained_spool_bytes")
        if retained_spool_bytes > token.spool_bytes:
            raise ValueError("retained_spool_bytes exceeds reserved spool_bytes")
        del self._active[token.attempt_key]
        self._memory_reserved -= token.memory_bytes
        self._spool_reserved -= token.spool_bytes
        self._spool_retained += retained_spool_bytes

    def reclaim_spool(self, *, deleted_bytes: int) -> None:
        """Credit already-confirmed deletion of settled/initial retained bytes.

        Never call for files merely submitted to a writer or scheduled for
        deletion, and never deduct active-attempt files through this method.
        Authentication and truthful, non-duplicated deletion accounting belong
        to the caller; aggregate accounting cannot identify individual files.
        """
        _bytes(deleted_bytes, "deleted_bytes")
        if deleted_bytes > self._spool_retained:
            raise ValueError("deleted_bytes exceeds retained spool bytes")
        self._spool_retained -= deleted_bytes
