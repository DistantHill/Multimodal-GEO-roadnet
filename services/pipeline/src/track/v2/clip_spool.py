"""Run-local, repeatable disk sequences for temporary working data.

Pickles are only read from the private temporary database created here; no
external serialized input is accepted. Closing the sequence removes its files.
"""
from __future__ import annotations

import pickle
import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Generic, TypeVar

T = TypeVar("T")


class DiskSequence(Sequence[T], Generic[T]):
    def __init__(self, *, directory: str | Path | None = None) -> None:
        self._directory = TemporaryDirectory(prefix="roadnet-spool-", dir=directory)
        self._db = sqlite3.connect(f"{self._directory.name}/clips.sqlite")
        self._db.execute("PRAGMA cache_size=-4096")
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA synchronous=OFF")
        self._db.execute(
            "CREATE TABLE items (ordinal INTEGER PRIMARY KEY, item_key TEXT, "
            "pair_a INTEGER, pair_b INTEGER, payload BLOB)"
        )
        self._db.execute("CREATE INDEX items_pair ON items(pair_a, pair_b, item_key)")
        self._db.execute("CREATE UNIQUE INDEX items_key ON items(item_key)")
        self._count = 0
        self._closed = False

    def append(self, value: T, *, key: str, pair: tuple[int, int] | None = None) -> None:
        a, b = pair if pair is not None else (None, None)
        self._db.execute(
            "INSERT INTO items VALUES (?, ?, ?, ?, ?)",
            (self._count, key, a, b, pickle.dumps(value, protocol=5)),
        )
        self._count += 1
        if self._count % 1000 == 0:
            self._db.commit()

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(self._count)))
        if index < 0:
            index += self._count
        row = self._db.execute("SELECT payload FROM items WHERE ordinal=?", (index,)).fetchone()
        if row is None:
            raise IndexError(index)
        return pickle.loads(row[0])

    def __iter__(self) -> Iterator[T]:
        cursor = self._db.execute("SELECT payload FROM items ORDER BY ordinal")
        try:
            for row in cursor:
                yield pickle.loads(row[0])
        finally:
            cursor.close()

    def get_by_clip_id(self, key: str) -> T:
        row = self._db.execute("SELECT payload FROM items WHERE item_key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return pickle.loads(row[0])

    def iter_pair(self, pair: tuple[int, int]) -> Iterator[T]:
        cursor = self._db.execute(
            "SELECT payload FROM items WHERE pair_a=? AND pair_b=? ORDER BY item_key",
            pair,
        )
        try:
            for row in cursor:
                yield pickle.loads(row[0])
        finally:
            cursor.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._db.close()
            self._directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __del__(self):
        if hasattr(self, "_closed"):
            self.close()
