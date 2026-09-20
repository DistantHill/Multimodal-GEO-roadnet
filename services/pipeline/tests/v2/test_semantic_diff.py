from __future__ import annotations

import math

import pytest

from uuid import uuid4

from track.v2.validation.semantic_diff import TABLES, compare_rows, compare_runs


class _Cursor:
    def __init__(self, name: str, rows: list[tuple]) -> None:
        self.name = name
        self.rows = rows
        self.itersize = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, params: tuple) -> None:
        assert sql.lstrip().upper().startswith(("SELECT", "WITH"))
        assert len(params) == 1

    def __iter__(self):
        return iter(self.rows)


class _Result:
    def fetchone(self):
        return ("completed", "completed", "completed", True, True)


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, _params: tuple = ()):
        self.statements.append(sql)
        assert sql.strip().upper().startswith(("SET TRANSACTION READ ONLY", "SELECT"))
        return _Result()

    def cursor(self, name: str | None = None):
        if name is None:
            return _SchemaCursor()
        spec = next(spec for spec in TABLES if spec.name.replace(".", "_") in name)
        row = ("stable-key", *(0.0 if kind != "exact" else "same" for _, kind in spec.columns))
        return _Cursor(name, [row])


class _SchemaCursor:
    def __init__(self) -> None:
        self.responses = iter([("pipeline_schema_versions",), (7,)])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, _params: tuple = ()) -> None:
        assert sql.lstrip().upper().startswith("SELECT")

    def fetchone(self):
        return next(self.responses)


def test_compare_runs_is_read_only_and_uses_named_bounded_cursors() -> None:
    conn = _Connection()
    runs = {kind: uuid4() for kind in ("refine", "detect", "fit")}
    result = compare_runs(conn, runs, runs, batch_size=17)
    assert result["passed"] is True
    assert conn.statements[0] == "SET TRANSACTION READ ONLY"
    assert len(result["tables"]) == len(TABLES)


def test_surrogate_id_offsets_are_absent_from_normalized_rows() -> None:
    columns = (("source_identity", "exact"), ("lon", "degree"))
    baseline = [("track:1:0", "kept:source-7", 120.0)]
    candidate = [("track:1:0", "kept:source-7", 120.0)]
    result = compare_rows("detect.resolved_points", columns, baseline, candidate)
    assert result["passed"] is True
    assert result["not_comparable"] == ["surrogate primary keys", "run UUIDs", "timestamps"]


def test_topology_change_fails_even_when_row_counts_match() -> None:
    result = compare_rows(
        "network.topology", (("variant", "exact"),),
        [("combined:A:B", "combined")], [("combined:A:C", "combined")],
    )
    assert result["passed"] is False
    assert result["failure"].startswith("business_identity_mismatch")


@pytest.mark.parametrize(
    "kind,tolerance", [("metre", 1e-6), ("degree", 1e-9), ("float", 1e-12)],
)
def test_numeric_tolerance_boundary_and_excess(kind: str, tolerance: float) -> None:
    columns = (("value", kind),)
    assert compare_rows("probe", columns, [("key", 0.0)], [("key", tolerance)])["passed"]
    assert not compare_rows("probe", columns, [("key", 0.0)], [("key", tolerance * 1.01)])["passed"]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_values_fail_closed(value: float) -> None:
    result = compare_rows("probe", (("value", "float"),), [("key", value)], [("key", value)])
    assert result["passed"] is False
    assert result["maximum_error"]["value"] is None


def test_missing_intersection_business_evidence_fails_closed() -> None:
    result = compare_rows(
        "detect.intersections", (("observations", "exact"),),
        [("__UNCOMPARABLE_INTERSECTION__", 1)], [("__UNCOMPARABLE_INTERSECTION__", 1)],
    )
    assert result["failure"] == "unreliable_business_identity"


def test_duplicate_business_identity_fails_closed() -> None:
    rows = [("same", 1), ("same", 1)]
    assert compare_rows("probe", (("value", "exact"),), rows, rows)["failure"] == "non_unique_business_identity"
