from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from track.v2.cli.replay import _target_from_args
from track.v2.replay import (
    REFINE_REPLAY_KINDS,
    DETECT_REPLAY_KINDS,
    PostgresReplayStore,
    ReplayService,
    ReplayTarget,
    _replay_fit_endpoint,
)


class _Store:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, object]] = {}
        self.links: dict[tuple[str, str], list[tuple[str, ReplayTarget]]] = {}

    def get_entity(self, target: ReplayTarget) -> dict[str, object] | None:
        return self.rows.get((target.kind, str(target.id)))

    def parent_refs(
        self,
        target: ReplayTarget,
        _row: dict[str, object],
    ) -> list[tuple[str, ReplayTarget]]:
        return self.links.get((target.kind, str(target.id)), [])


def test_replay_walks_v3_ids_and_stops_cycles() -> None:
    store = _Store()
    intersection = ReplayTarget("intersection", 3, detect_id="detect")
    candidate = ReplayTarget("candidate_intersection", 2, detect_id="detect")
    turn = ReplayTarget("turn_event", 1, detect_id="detect")
    for target in (intersection, candidate, turn):
        store.rows[(target.kind, str(target.id))] = {"id": target.id}
    store.links[("intersection", "3")] = [("candidate", candidate)]
    store.links[("candidate_intersection", "2")] = [("member_turn", turn)]
    store.links[("turn_event", "1")] = [("candidate", candidate)]

    trace = ReplayService(store).replay(intersection)

    assert trace.status == "ok"
    assert [(node.kind, node.id) for node in trace.nodes] == [
        ("intersection", 3),
        ("candidate_intersection", 2),
        ("turn_event", 1),
    ]
    assert len(trace.edges) == 3


def test_source_point_supports_non_integer_external_key() -> None:
    store = _Store()
    target = ReplayTarget("source_point", "92b9f2b4-key", refine_id="refine")
    store.rows[(target.kind, str(target.id))] = {"id": "92b9f2b4-key"}

    trace = ReplayService(store).replay(target)

    assert trace.status == "ok"
    assert trace.nodes[0].raw_row == {"id": "92b9f2b4-key"}


def test_missing_target_is_reported_without_legacy_fallback() -> None:
    trace = ReplayService(_Store()).replay(
        ReplayTarget("resolved_point", 99, detect_id="detect")
    )
    assert trace.status == "not_found"
    assert trace.errors[0].code == "target_not_found"


def test_fit_endpoint_is_a_persisted_crossing_not_an_interpolation() -> None:
    point = SimpleNamespace(
        resolved_point_id=10,
        crossing_id=20,
        intersection_id=30,
        crossing_type=0,
        lon=116.1,
        lat=39.1,
    )

    endpoint = _replay_fit_endpoint(point, expected_type=0)

    assert endpoint.resolved_point_id == 10
    assert endpoint.crossing_id == 20
    assert endpoint.kind == "exit"
    assert endpoint.lon == 116.1
    assert not hasattr(endpoint, "segment_fraction")


def test_fit_endpoint_rejects_wrong_crossing_type() -> None:
    point = SimpleNamespace(
        resolved_point_id=10,
        crossing_id=20,
        intersection_id=30,
        crossing_type=1,
        lon=116.1,
        lat=39.1,
    )
    with pytest.raises(ValueError, match="resolved exit crossing"):
        _replay_fit_endpoint(point, expected_type=0)


class _NoQueryConnection:
    def cursor(self) -> object:
        raise AssertionError("this relationship should not query the database")


def test_turn_event_lineage_uses_resolved_point_and_candidate_ids() -> None:
    store = PostgresReplayStore(_NoQueryConnection())
    target = ReplayTarget("turn_event", 7, detect_id="detect")
    links = store.parent_refs(
        target,
        {"resolved_point_id": 11, "candidate_intersection_id": 13},
    )
    assert links == [
        ("observed_point", ReplayTarget("resolved_point", 11, detect_id="detect")),
        (
            "candidate",
            ReplayTarget("candidate_intersection", 13, detect_id="detect"),
        ),
    ]


def _parse_target(**values: object) -> ReplayTarget:
    parser = argparse.ArgumentParser()
    namespace = SimpleNamespace(
        kind=values.get("kind"),
        id=values.get("id"),
        refine_id=values.get("refine_id"),
        detect_id=values.get("detect_id"),
    )
    return _target_from_args(parser, namespace)


def test_cli_scopes_external_and_internal_ids_explicitly() -> None:
    assert "source_point" in REFINE_REPLAY_KINDS
    assert "resolved_point" in DETECT_REPLAY_KINDS
    assert _parse_target(
        kind="source_point", id="uuid-or-text", refine_id="refine"
    ) == ReplayTarget("source_point", "uuid-or-text", refine_id="refine")
    assert _parse_target(
        kind="resolved_point", id="42", detect_id="detect"
    ) == ReplayTarget("resolved_point", 42, detect_id="detect")
