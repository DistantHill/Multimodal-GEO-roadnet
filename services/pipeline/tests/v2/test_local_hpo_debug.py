"""Complete streamed local debug artifacts retain identities and rejection evidence."""
from contextlib import contextmanager
import copy
from dataclasses import dataclass, replace
from types import SimpleNamespace as NS

import numpy as np
import pytest
from shapely.geometry import box, mapping

from track.v2.experiments.intersection_hpo import local_debug as debug
from track.v2.models import HotspotCluster, IntersectionTopology, IntersectReport, MemberBatch
from track.v2.plugins.execution import CandidateResult
from .test_detect_algorithm_regressions import _events
from .detect_columns_helpers import CONTEXT


class Reader:
    def __init__(self, *_args, **_kwargs):
        self.names = ("point_id", "point_kind", "subtrack_index", "refined_sequence_in_subtrack",
                      "after_sequence_in_subtrack", "lon", "lat", "original_subtrack_index",
                      "original_refined_sequence_in_subtrack")
        self.rows = [(19, "injected", 0, 0, -1, 0., 0., 8, 5),
                     (20, "kept", 0, 1, None, 0., .1, 8, 6)]
    def _require_open(self):
        return self
    def execute(self, query):
        assert query == debug.INPUT_SQL
        self.description = [(name,) for name in self.names]
        self.position = 0
        return self
    def fetchmany(self, size):
        rows = self.rows[self.position:self.position + size]
        self.position += len(rows)
        return rows
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass


class Connection:
    def __init__(self):
        self.rows = {name: [] for name in debug.TABLES}
        self.runs, self.copy_sizes, self.noise = {}, [], set()
        self.circles = []
        self.analyzed = []
    @contextmanager
    def transaction(self):
        previous = copy.deepcopy((self.rows, self.runs, self.noise))
        try:
            yield self
        except BaseException:
            self.rows, self.runs, self.noise = previous
            raise
    def execute(self, query, args=None):
        answer = 0
        if query == debug.RUN_INSERT_SQL:
            self.runs[args[0]] = {"snapshot": args[1].obj, "status": "running"}
        elif query == debug.RUN_FINISH_SQL:
            self.runs[args[1]].update(counts=args[0].obj, status="completed")
        elif query == debug.ASSIGN_CLUSTERS_SQL:
            assert self.analyzed == [debug.ANALYZE_CANDIDATES_SQL, debug.ANALYZE_MEMBERS_SQL]
            for index, candidate in enumerate(self.rows["candidate_points"]):
                for member in self.rows["cluster_members"]:
                    if candidate[2] == member[2] and candidate[4] == member[4]:
                        replacement = list(candidate)
                        replacement[3] = member[3]
                        self.rows["candidate_points"][index] = tuple(replacement)
        elif query == debug.ASSIGN_NOISE_SQL:
            self.noise = {row[1] for row in self.rows["candidate_points"] if row[3] is None}
        elif query == debug.MEMBER_GEOMETRY_SQL:
            for index, member in enumerate(self.rows["cluster_members"]):
                for candidate in self.rows["candidate_points"]:
                    if candidate[2] == member[2] and candidate[4] == member[4]:
                        replacement = list(member)
                        replacement[6] = candidate[6]
                        self.rows["cluster_members"][index] = tuple(replacement)
        elif query == debug.MISSING_MEMBERS_SQL:
            answer = sum(not any(c[2] == m[2] and c[4] == m[4] for c in self.rows["candidate_points"])
                         for m in self.rows["cluster_members"])
        elif query == debug.NOISE_COUNT_SQL:
            answer = len(self.noise)
        elif query == debug.ROI_FACE_COUNT_SQL:
            answer = sum(row[5]["in_evaluation_roi"] for row in self.rows["final_faces"])
        elif query in (debug.ANALYZE_CANDIDATES_SQL, debug.ANALYZE_MEMBERS_SQL):
            self.analyzed.append(query)
            assert len(self.rows["candidate_points"]) == sum(row[5]["candidate_count"] for row in self.rows["candidate_instances"])
            assert len(self.rows["final_faces"]) == sum(row[5]["is_complete"] for row in self.rows["topologies"])
        else:
            assert query.startswith("CREATE")
        return NS(fetchone=lambda: (answer,))
    @contextmanager
    def cursor(self):
        connection = self
        class Cursor:
            @contextmanager
            def copy(self, query):
                table = query.split()[1].split(".")[1]
                assert table in debug.TABLES
                count = 0
                class Copier:
                    def write_row(self, row):
                        nonlocal count
                        values = list(row)
                        values[5] = values[5].obj
                        connection.rows[table].append(tuple(values))
                        count += 1
                yield Copier()
                connection.copy_sizes.append(count)
            def execute(self, query, args):
                assert query == debug.FINAL_FACE_SQL
                connection.circles.append(args)
            def fetchone(self):
                return (mapping(box(-1, 1, 1, 3)),)
        yield Cursor()


@dataclass(frozen=True)
class Params:
    min_turn_deg: float = 45.


def graph():
    events = _events([[0., 0.], [1., 0.], [50., 0.]])
    selected = events.take(np.array([0, 1]))
    member = MemberBatch(np.array(["turn_threshold:default"], dtype=object),
                         np.array([10], dtype=np.int64), np.array([0], dtype=np.int64),
                         np.array(["trajectory_points"], dtype=object), np.array([True]))
    cluster = HotspotCluster(0, 0., 0., 0., 0., 1., members=member, source="turn_threshold:default")
    topology = IntersectionTopology(0, [0., 90., 180.], {(0, 1): 2}, 0, True, "complete", 90.)
    item = CandidateResult("turn_threshold", "default", "turn", selected, events, Params(), 10)
    return NS(candidate_results=(item,), clusters=[cluster], topologies=[topology], observations=[],
              spatial_context=CONTEXT, report=IntersectReport(1, 3, 2, 0, 1, 1, 0, 1))


def snapshot():
    region = mapping(box(0, 0, 10, 10))
    return {"local_point_count": 2, "evaluation_region": region, "computation_region": region,
            "truth": {"type": "FeatureCollection", "features": [{"type": "Feature", "id": "label",
                       "geometry": mapping(box(-1, 1, 1, 3)), "properties": {"type": "unclassified"}}]}}


def test_all_artifacts_retain_zero_subtrack_negative_anchor_noise_and_roi_boundary():
    connection = Connection()
    with connection.transaction():
        result = debug.persist_local_debug(connection, Reader(), graph(), snapshot(), batch_size=2,
            expected_stats={"source_point_count": 2, "turn_event_count": 3, "candidate_point_count": 2,
                            "intersection_count": 1})
    assert result["counts"]["input_points"] == 2
    assert result["counts"]["noise_points"] == 1
    assert result["counts"]["candidate_points"] == 2
    assert result["counts"]["candidate_intersections"] == 1
    assert result["counts"]["evaluation_roi_final_faces"] == 1
    head = connection.rows["input_points"][0][5]
    assert head["subtrack_index"] == 0
    assert head["after_sequence_in_subtrack"] == -1
    assert head["original_subtrack_index"] == 8
    assert head["original_refined_sequence_in_subtrack"] == 5
    assert connection.rows["cluster_members"][0][6] is not None
    assert connection.rows["candidate_points"][0][2] == 10
    assert connection.rows["candidate_points"][1][2] == 11
    assert connection.rows["candidate_intersections"][0][5]["radius_m"] == 1.
    assert connection.rows["topologies"][0][5]["clip_radius_m"] == 90.
    assert connection.rows["topologies"][0][5]["transition_counts"] == {"0,1": 2}
    assert connection.rows["topologies"][0][6] == "SRID=4326;POINT(0.0 0.0)"
    assert connection.rows["final_faces"][0][5]["geometry"] == debug._native(mapping(box(-1, 1, 1, 3)))
    assert len(connection.rows["truth_regions"]) == len(connection.rows["evaluation_regions"]) == 1
    assert max(connection.copy_sizes) <= 2
    assert connection.runs[result["debug_run_id"]]["status"] == "completed"


def test_incomplete_topology_reason_and_all_candidate_faces_are_retained():
    result = graph()
    result.topologies = [replace(result.topologies[0], is_complete=False,
                                 reason="branches=1, observations=0, directed_transitions=0")]
    result.report = replace(result.report, complete_intersection_count=0)
    connection = Connection()
    stored = debug.persist_local_debug(connection, Reader(), result, snapshot(), batch_size=1)
    assert stored["counts"]["final_faces"] == 0
    assert stored["counts"]["candidate_intersections"] == 1
    assert connection.rows["topologies"][0][5]["is_complete"] is False
    assert connection.rows["topologies"][0][5]["reason"].startswith("branches=1")


def test_original_count_mismatch_rolls_back_entire_debug_replay():
    connection = Connection()
    with pytest.raises(ValueError, match="original archived counts"):
        with connection.transaction():
            debug.persist_local_debug(connection, Reader(), graph(), snapshot(), batch_size=1,
                                       expected_stats={"source_point_count": 999})
    assert not connection.runs
    assert not any(connection.rows.values())


def test_empty_local_input_retains_regions_and_truth_and_ignores_old_ratios():
    reader, result, record = Reader(), graph(), snapshot()
    reader.rows = []
    record["local_point_count"] = 0
    item = result.candidate_results[0]
    empty = item.events.take(np.array([], dtype=np.int64))
    result.candidate_results = (replace(item, events=empty, candidates=empty),)
    result.clusters = result.topologies = []
    result.report = IntersectReport(0, 0, 0, 0, 0, 0, 0, 0)
    connection = Connection()
    stored = debug.persist_local_debug(connection, reader, result, record,
        expected_stats={"source_point_count": 0, "intersection_count": 0,
                        "complete_intersection_source_ratios": {"old": .9}})
    assert stored["counts"]["input_points"] == stored["counts"]["final_faces"] == 0
    assert len(connection.rows["truth_regions"]) == 1
    assert connection.runs[stored["debug_run_id"]]["status"] == "completed"


def test_copy_consumes_generator_in_bounded_chunks_and_closes():
    connection = Connection()
    writer = debug.DebugWriter(connection, "fixture-run", 2)
    closed = []
    def records():
        try:
            for index in range(7):
                assert index - len(connection.rows["input_points"]) < 2
                yield index, None, "kept", {"point_id": index}, None
        finally:
            closed.append(True)
    writer.write("input_points", records())
    assert connection.copy_sizes == [2, 2, 2, 1]
    assert closed == [True]


def test_runner_uses_readonly_parent_and_exact_local_detect_boundary(tmp_path, monkeypatch):
    from track.v2.db.write_refine_results import RefineRunRecord
    source, output = NS(read_only=False), Connection()
    result = graph()
    close_calls = []
    result.close = lambda: close_calls.append(True)
    config = NS(postgres=NS(database="roadnet"), detect=NS(), runtime=NS(db_insert_batch=2),
                paths=NS(data_root=tmp_path), snapshot_parameters=lambda: {"fixed_detect": True})
    workspace = NS(configs={"scheme": config}, case=NS(source_table="trajectory_points"))
    cache = {"descriptor": NS(refine_id="parent"), "point_count": 2, "fragment_count": 1}
    inputs = {"sha256": "inputs", "levels": {"L1": snapshot()}}
    caches = {"cache_key": "cache", "levels": {"L1": cache}}
    @contextmanager
    def connect(settings):
        yield output if settings.database == "roadnet_debug" else source
    called = []
    def detect(**kwargs):
        assert kwargs["conn"] is source and source.read_only
        assert isinstance(kwargs["refine_reader"], Reader)
        assert kwargs["context"].refine_id == "parent"
        called.append(True)
        return result
    monkeypatch.setattr(debug, "connect", connect)
    monkeypatch.setattr(debug, "threshold_config", lambda configured, threshold: configured)
    monkeypatch.setattr(debug, "fetch_refine_run", lambda *_: RefineRunRecord("parent", "trajectory_points", {}, "completed", CONTEXT))
    monkeypatch.setattr(debug, "LocalRefineReader", Reader)
    monkeypatch.setattr(debug, "detect_intersections", detect)
    monkeypatch.setattr(debug, "code_revision", lambda: "debug-source")
    monkeypatch.setattr(debug, "asdict", lambda value: vars(value) if isinstance(value, NS) else __import__('dataclasses').asdict(value))
    stored = debug.run_local_debug(workspace, "scheme", "parent", inputs, caches, "L1", 45.,
                                  debug_postgres=NS(database="roadnet_debug"))
    assert stored["debug_database"] == "roadnet_debug"
    assert called == close_calls == [True]
    assert output.runs[stored["debug_run_id"]]["status"] == "completed"
