"""Disk scheduling preserves global geometry semantics without retaining points."""
from __future__ import annotations

import gc
import hashlib
import json
import math
import random
import weakref
from dataclasses import replace
from unittest.mock import patch

import pytest

from track.v2.candidate_extraction import extract_cross_track_candidates, extract_disk_cross_track_candidates
from track.v2.detect import (
    IntersectConfig, SpatialWindow, _build_topology_point_index,
    _build_topologies_from_tracks, _detect_intersections_db,
    _extract_disk_entry_exit_observations, _extract_entry_exit_observations_batch,
)
from track.v2.detect_workspace import DetectWorkspace
from track.v2.models import HotspotCluster, PointRef, RefinedPointRef, SpatialContext, TrajectoryPoint
from track.v2.projection import CoordinateTransformer


CONTEXT = SpatialContext(
    4326, 32650,
    {"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": .01},
)
TRANSFORMER = CoordinateTransformer(CONTEXT)


def track(vehicle, subtrack, coordinates):
    points = []
    for index, (dx, dy, heading) in enumerate(coordinates):
        x, y = 500000.0 + dx, 4320000.0 + dy
        lon, lat = TRANSFORMER.inverse(x, y)
        points.append(TrajectoryPoint(
            PointRef("fixture", f"{vehicle}:{subtrack}:{index}", True),
            vehicle, float(index), lon, lat, heading, 20.0, x=x, y=y,
            refined_point_ref=RefinedPointRef("kept", subtrack * 10000 + index, subtrack, index),
        ))
    return points


def crossing_tracks():
    # Same vehicle, distinct subtracks; long segments exceed every grid cap.
    result = {
        "z\x001": track("z", 1, [(-10000, 0, 90), (10000, 0, 90)]),
        "a\x003": track("a", 3, [(0, -10000, 0), (0, 10000, 0)]),
        "a\x001": track("a", 1, [(-1000, 5, 90), (1000, 5, 90)]),
        "b\x001": track("b", 1, [(10000, -5, 0), (10000, 0, 0), (10000, 5, 0)]),
        "c\x001": track("c", 1, [(0.00001, -20, 0), (0.00001, 20, 0)]),
    }
    # Injection provenance must survive disk serialization and canonical order.
    point = result["a\x003"][0]
    result["a\x003"][0] = replace(point,
        point_ref=PointRef("injected", "generated-1", False, from_ref="p1", to_ref="p2"),
        refined_point_ref=replace(point.refined_point_ref, point_kind="injected"),
    )
    return result


@pytest.mark.parametrize("batch_size", [1, 3, 65536])
def test_cross_candidates_match_grid_with_long_boundary_and_cross_patch_segments(tmp_path, batch_size):
    cfg = IntersectConfig(cross_track_pair_batch_size=batch_size,
                          cross_track_grid_cell_m=10, cross_track_max_grid_cells_per_segment=1)
    tracks = crossing_tracks()
    expected, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
    assert expected
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        # Deliberately different from canonical order and from the expected dict.
        for key in reversed(list(tracks)):
            workspace[key] = tracks[key]
        actual = extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg)
        assert actual == expected
        assert workspace.max_pair_rows <= min(batch_size, 8192)
        assert workspace.max_loaded_segments <= 2 * min(batch_size, 8192)
        assert len({(c.from_segment_start_point_ref, c.to_segment_start_point_ref) for c in actual}) == len(actual)
    assert not list(tmp_path.iterdir())


def test_random_cross_candidates_have_exact_global_order_and_numbering(tmp_path):
    rng = random.Random(9921)
    tracks = {}
    for n in range(32):
        vehicle = f"v{n % 11}"
        coordinates = []
        x, y = rng.uniform(-100, 100), rng.uniform(-100, 100)
        heading = 0 if n % 2 else 90
        for index in range(6):
            coordinates.append((x + index * (0 if n % 2 else 50),
                                y + index * (50 if n % 2 else 0), heading))
        tracks[f"{vehicle}\x00{n}"] = track(vehicle, n, coordinates)
    cfg = IntersectConfig(cross_track_pair_batch_size=7)
    expected, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        for key, points in tracks.items():
            workspace[key] = points
        actual = extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg)
    assert actual == expected
    assert len(actual) > 20


@pytest.mark.parametrize("batch_size", [1, 7])
def test_cross_queries_cover_metadata_and_preserve_complete_boundary_candidate_digest(tmp_path, batch_size):
    cfg = IntersectConfig(cross_track_pair_batch_size=batch_size,
                          cross_track_max_grid_cells_per_segment=1)
    # Match the DB reader's floating-point headings so the JSON digest also
    # compares representation (90 versus 90.0), not just numeric equality.
    tracks = {key: [replace(point, heading=float(point.heading)) for point in points]
              for key, points in crossing_tracks().items()}
    expected, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
    assert expected
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        for key in reversed(list(tracks)):
            workspace[key] = tracks[key]
        queries = []
        workspace.db.set_trace_callback(queries.append)
        actual = extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg)
        workspace.db.set_trace_callback(None)

        # Explain the actual executed SQL, including numeric batch reads. A
        # named index alone does not prove that SQLite avoids the payload table.
        cross_query, = [q for q in queries if q.lstrip().startswith("SELECT a.id,b.id")]
        plan = [row[3] for row in workspace.db.execute("EXPLAIN QUERY PLAN " + cross_query)]
        assert not any("TEMP" in step for step in plan), plan
        assert not any("Sort" in row[1] for row in workspace.db.execute("EXPLAIN " + cross_query))
        for alias, index_name in (("a", "segments_numeric"), ("b", "segments_numeric"),
                                  ("ta", "tracks_id_vehicle"), ("tb", "tracks_id_vehicle")):
            assert any(f"{alias} USING COVERING INDEX {index_name}" in step for step in plan), plan
        numeric_queries = [q for q in queries if q.startswith("SELECT id,x1,y1,x2,y2,heading ")]
        assert numeric_queries
        for query in numeric_queries:
            numeric_plan = [row[3] for row in workspace.db.execute("EXPLAIN QUERY PLAN " + query)]
            assert any("USING COVERING INDEX segments_numeric" in step for step in numeric_plan), numeric_plan
        # Exact hits still retrieve the original point payload and provenance.
        assert any(q.startswith("SELECT id,track_id,segment_index,x1,y1,x2,y2,heading,payload ")
                   for q in queries)

    def digest(candidates):
        return hashlib.sha256(json.dumps([c.as_dict() for c in candidates], sort_keys=True).encode()).hexdigest()

    # Includes endpoint contacts, long segments, same-vehicle subtracks,
    # injection provenance and global candidate numbering in reversed input order.
    assert digest(actual) == digest(expected)
    assert actual == expected


def scan_progress_tracks():
    return {
        "zero_head": track("z", 1, [(1000, 1000, 0.), (1000, 1010, 0.)]),
        "a": track("a", 2, [(-10, 0, 90.), (10, 0, 90.)]),
        "b": track("b", 3, [(0, -10, 0.), (0, 10, 0.)]),
        "c": track("c", 4, [(1, -10, 0.), (1, 10, 0.)]),
        "d": track("d", 5, [(-10, 1, 90.), (10, 1, 90.)]),
        "e": track("e", 6, [(2, -10, 0.), (2, 10, 0.)]),
        "zero_tail": track("tail", 7, [(2000, 2000, 0.), (2000, 2010, 90.)]),
    }


@pytest.mark.parametrize("reverse_unordered", [False, True])
def test_source_scan_prefix_advances_only_after_consumption_and_exhaustion(tmp_path, reverse_unordered):
    with DetectWorkspace(IntersectConfig(), directory=tmp_path) as workspace:
        for key, points in scan_progress_tracks().items():
            workspace[key] = points
        workspace.db.execute(f"PRAGMA reverse_unordered_selects={int(reverse_unordered)}")
        iterator = workspace.iter_cross_patches(1)
        sources = []
        for batch in iterator:
            source = batch.segment_ids[int(batch.pair_a[0])]
            target = batch.segment_ids[int(batch.pair_b[0])]
            assert workspace.scanned_source_segments == (sources[-1] - 1 if sources else 0)
            assert workspace.scanned_source_segments < source
            if source == 2:
                assert target >= 3  # Targets ahead of the source do not advance it.
            sources.append(source)
        assert sources == sorted(sources)
        assert sources.count(2) == 3  # One source spans three consumed patches.
        assert sources[-1] < workspace.segment_count  # Unreturned zero-hit tail.
        assert workspace.scanned_source_segments == workspace.segment_count == 7


@pytest.mark.parametrize("finish", ["close", "error", "workspace_close"])
def test_source_scan_prefix_does_not_mark_unconsumed_patch_or_cancelled_scan_complete(tmp_path, finish):
    with DetectWorkspace(IntersectConfig(), directory=tmp_path) as workspace:
        for key, points in scan_progress_tracks().items():
            workspace[key] = points
        iterator = workspace.iter_cross_patches(1)
        next(iterator)
        assert workspace.scanned_source_segments == 0
        next(iterator)
        assert workspace.scanned_source_segments == 1
        if finish == "error":
            with pytest.raises(RuntimeError, match="cancelled"):
                iterator.throw(RuntimeError("cancelled"))
        else:
            if finish == "workspace_close":
                workspace.close()
            iterator.close()
        assert workspace.scanned_source_segments == 1 < workspace.segment_count


@pytest.mark.parametrize("inputs", ["empty", "zero_hits", "crossings"])
def test_disk_heartbeat_reports_source_prefix_and_refreshes_zero_hit_tail(tmp_path, inputs):
    tracks = scan_progress_tracks()
    if inputs == "empty":
        tracks = {}
    elif inputs == "zero_hits":
        tracks = {key: points for key, points in tracks.items() if key.startswith("zero_")}
    cfg = IntersectConfig(cross_track_pair_batch_size=1)
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        for key, points in tracks.items():
            workspace[key] = points
        with patch("track.v2.candidate_extraction.heartbeat_progress") as progress:
            actual = extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg)
        all_calls = progress.call_args_list
        # Extraction counts segment pairs and reports a conservative source
        # prefix. The subsequent sort/number stage counts candidates instead.
        calls = [call for call in all_calls if call.kwargs.get("unit") == "线段对"]
        sorting_calls = [call for call in all_calls if call.kwargs.get("unit") == "候选"]
        assert all_calls == calls + sorting_calls
        assert [call.args[0] for call in sorting_calls] == list(range(1, len(actual) + 1))
        assert calls[0].kwargs["detail"] == f"源线段已扫描至少 0/{len(tracks)}；候选命中 0"
        assert calls[-1].kwargs["detail"] == (
            f"源线段已扫描至少 {len(tracks)}/{len(tracks)}；候选命中 {len(actual)}"
        )
        prefixes = [int(call.kwargs["detail"].split("至少 ")[1].split("/")[0]) for call in calls]
        assert prefixes == sorted(prefixes)
        if tracks:
            assert all(prefix < len(tracks) for prefix in prefixes[:-1])
        else:
            assert prefixes == [0, 0]
        assert [call.args[0] for call in calls] == sorted(call.args[0] for call in calls)
    expected, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
    assert actual == expected


def cluster():
    lon, lat = TRANSFORMER.inverse(500000, 4320000)
    return HotspotCluster(cluster_calculate_index=0, centroid_x=500000,
                          centroid_y=4320000, centroid_lon=lon, centroid_lat=lat,
                          radius_m=10, events=(), source="turn")


def test_topology_preserves_complete_subtracks_roots_and_global_crossing_ids(tmp_path):
    tracks = crossing_tracks()
    tracks.update({
        "inside": track("inside", 50, [(0, 0, 90), (100, 0, 90)]),
        "tangent": track("tangent", 51, [(-100, 10, 90), (100, 10, 90)]),
        "vertices": track("vertices", 52, [(-100, 0, 90), (-10, 0, 90), (10, 0, 90), (100, 0, 90)]),
    })
    cfg = IntersectConfig(min_intersection_radius_m=10, topology_radius_padding_m=0)
    window = SpatialWindow(499980, 4319980, 500020, 4320020)
    index = _build_topology_point_index(tracks)
    expected_tracks = index.tracks_in(window)
    assert "z\x001" in expected_tracks  # Both endpoints outside the query window.
    kwargs = dict(cluster=cluster(), transformer=TRANSFORMER, radius_m=10, bearing_window=2)
    expected = _extract_entry_exit_observations_batch(expected_tracks, **kwargs)
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        for key, points in tracks.items():
            workspace[key] = points
        assert dict(workspace.iter_nearby_tracks(window)) == expected_tracks
        actual = _extract_disk_entry_exit_observations(workspace, window, **kwargs)
        assert actual == expected
        assert _build_topologies_from_tracks([cluster()], workspace, cfg, TRANSFORMER) == \
            _build_topologies_from_tracks([cluster()], tracks, cfg, TRANSFORMER)
    assert expected[0] and expected[1]


def test_bbox_rounding_is_conservative_and_exact_window_filter_matches_memory(tmp_path):
    # Float32 R-tree bounds round metres outward; sub-centimetre differences
    # at UTM magnitudes must neither drop exact hits nor change topology sets.
    tracks = {
        "hit": track("hit", 1, [(-1e-5, -1, 0), (-1e-5, 1, 0)]),
        "miss": track("miss", 2, [(1e-5, -1, 0), (1e-5, 1, 0)]),
    }
    window = SpatialWindow(499999, 4319999, 500000, 4320001)
    with DetectWorkspace(IntersectConfig(), directory=tmp_path) as workspace:
        for key, points in tracks.items():
            workspace[key] = points
        assert dict(workspace.iter_nearby_tracks(window)) == \
            _build_topology_point_index(tracks).tracks_in(window)
        assert [key for key, _ in workspace.iter_nearby_tracks(window)] == ["hit"]


def test_workspace_releases_source_points_and_loads_one_track_at_a_time(tmp_path):
    with DetectWorkspace(IntersectConfig(), directory=tmp_path) as workspace:
        references = []
        for n in range(120):
            points = track(f"v{n}", n, [(i, n, 90) for i in range(30)])
            references.append(weakref.ref(points[0]))
            workspace[str(n)] = points
        del points
        gc.collect()
        assert all(ref() is None for ref in references)
        iterator = workspace.iter_nearby_tracks(SpatialWindow(499999, 4319999, 500100, 4321000))
        _, first = next(iterator)
        previous = weakref.ref(first[0])
        del first
        next(iterator)
        gc.collect()
        assert previous() is None
        iterator.close()
        assert workspace.db.execute("PRAGMA cache_size").fetchone()[0] == -32768
        assert workspace.db.execute("PRAGMA mmap_size").fetchone()[0] == 0


def test_db_orchestrator_cleans_disk_workspace_on_reader_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    with patch("track.v2.detect._query_spatial_extent", return_value=(0, 0, 1, 1)), \
         patch("track.v2.detect._load_tracks_and_turn_candidates", side_effect=RuntimeError("reader failed")):
        with pytest.raises(RuntimeError, match="reader failed"):
            _detect_intersections_db(object(), IntersectConfig(), CONTEXT)
    assert not list(tmp_path.iterdir())


def test_nan_end_heading_keeps_historical_straight_segment_semantics(tmp_path):
    tracks = {
        "a": track("a", 1, [(-10, 0, 90), (10, 0, math.nan)]),
        "b": track("b", 2, [(0, -10, 0), (0, 10, 0)]),
    }
    cfg = IntersectConfig()
    expected, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
    assert len(expected) == 1
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        for key, points in tracks.items():
            workspace[key] = points
        assert extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg) == expected


def test_reader_heartbeat_keeps_elapsed_and_row_count_through_disk_and_turn_patches(tmp_path):
    from threading import Event
    from track.v2.detect import _load_tracks_and_turn_candidates
    from track.v2.heartbeat import heartbeat_progress, observed_operation, operation_heartbeat

    clock = [100.0]
    tick = Event()
    messages = []
    samples = []
    cfg = IntersectConfig(stream_batch_size=1)
    tracks = [track("a", 1, [(-1, 0, 90), (1, 0, 90)]),
              track("b", 2, [(0, -1, 0), (0, 1, 0)])]

    def emit(message):
        messages.append(message)
        tick.set()

    def sample():
        clock[0] += 5.0
        tick.clear()
        assert tick.wait(2), "missing heartbeat while processing a yielded track"
        samples.append(messages[-1])

    @observed_operation("读取轨迹与逐车处理")
    def source(*_args, **_kwargs):
        for index, points in enumerate(tracks, 1):
            heartbeat_progress(index * 10000)
            yield points
            # Source stays suspended during disk storage and turn extraction.
            # Sampling after yield catches a track count overwriting row count.
            sample()

    original_set = DetectWorkspace.__setitem__

    def save(workspace, key, points):
        sample()
        original_set(workspace, key, points)

    def detect_patch(*_args, **_kwargs):
        sample()
        return []

    with DetectWorkspace(cfg, directory=tmp_path) as workspace, \
         patch("track.v2.heartbeat.time.monotonic", side_effect=lambda: clock[0]), \
         patch("track.v2.heartbeat.log_info"), \
         patch("track.v2.detect._stream_vehicle_tracks_from_db", source), \
         patch.object(DetectWorkspace, "__setitem__", save), \
         patch("track.v2.candidate_extraction.detect_turn_patch", detect_patch):
        with operation_heartbeat("test", emit=emit, interval=.01):
            events, _ = _load_tracks_and_turn_candidates(
                object(), cfg, CONTEXT, TRANSFORMER, workspace=workspace,
            )
        assert not events
    assert len(samples) == 6
    for index, message in enumerate(samples, 1):
        expected_rows = 10000 if index <= 3 else 20000
        assert "读取轨迹与逐车处理" in message
        assert f"已用 {index * 5:.1f} 秒" in message
        assert f"{expected_rows:,} 行" in message


def test_numeric_misses_do_not_load_source_payloads(tmp_path):
    cfg = IntersectConfig(cross_track_pair_batch_size=2)
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        for index in range(12):
            # Unknown coarse buckets pass every bbox pair; the original angle
            # kernel still rejects these equal (out-of-range) headings.
            workspace[str(index)] = track(str(index), index, [(-10, -10, 450), (10, 10, 450)])
        queries = []
        workspace.db.set_trace_callback(queries.append)
        with patch("track.v2.detect_workspace.pickle.loads", side_effect=AssertionError("decoded a miss")):
            assert extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg) == []
        assert workspace.max_pair_rows == 2
        assert workspace.max_numeric_segments > 0
        assert workspace.max_loaded_segments == 0
        assert workspace.loaded_source_points == 0
        assert not any("payload" in sql and "FROM segments" in sql for sql in queries)


def test_source_payload_is_loaded_once_per_hit_segment_per_patch(tmp_path):
    tracks = crossing_tracks()
    cfg = IntersectConfig(cross_track_pair_batch_size=8192)
    expected, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        for key, points in tracks.items():
            workspace[key] = points
        actual = extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg)
        expected_sources = {
            ref for candidate in expected for ref in (
                candidate.from_segment_start_point_ref, candidate.to_segment_start_point_ref,
            )
        }
        assert actual == expected
        assert workspace.loaded_source_points == len(expected_sources)
        assert workspace.max_loaded_segments <= 2 * workspace.max_pair_rows


def test_topology_heartbeat_spans_many_short_candidates_with_cumulative_counts(tmp_path):
    from threading import Event
    from track.v2.heartbeat import operation_heartbeat

    messages = []
    pause = Event()
    cfg = IntersectConfig(min_intersection_radius_m=10, topology_radius_padding_m=0)
    clusters = [replace(cluster(), cluster_calculate_index=index) for index in range(30)]

    def short_track_calculation(*_args, **_kwargs):
        pause.wait(.008)  # Each candidate is shorter than the observer interval.
        return [], []

    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        workspace["a"] = track("a", 1, [(-100, 0, 90), (100, 0, 90)])
        with patch("track.v2.detect._extract_entry_exit_observations_batch", short_track_calculation), \
             patch("track.v2.heartbeat.log_info"):
            with operation_heartbeat("test", emit=messages.append, interval=.04):
                topologies, _, _ = _build_topologies_from_tracks(clusters, workspace, cfg, TRANSFORMER)
    assert len(topologies) == len(clusters)
    samples = [message for message in messages if "拓扑候选验证" in message]
    assert len(samples) >= 2
    assert all("/30 候选交叉口" in message for message in samples)
    assert all("子轨迹" not in message for message in samples)
    completed = [int(message.split(" 秒，")[1].split("/30")[0]) for message in samples]
    assert completed == sorted(completed)
    assert completed[-1] > completed[0]
    assert all(0 <= count < 30 for count in completed)


@pytest.mark.parametrize("finish", ["exhaust", "generator_close", "error", "workspace_close"])
def test_cross_rank_metadata_lives_through_final_patch_then_releases(tmp_path, finish):
    from track.v2.cross_track import intersect_cross_track_patch

    class WeakText(str):
        __slots__ = ("__weakref__",)

    references = []

    def weak_text(data):
        value = WeakText(data.decode())
        references.append(weakref.ref(value))
        return value

    cfg = IntersectConfig()
    with DetectWorkspace(cfg, directory=tmp_path) as workspace:
        workspace["a"] = track("a", 1, [(-10, 0, 90), (10, 0, 90)])
        workspace["b"] = track("b", 2, [(0, -10, 0), (0, 10, 0)])
        workspace.db.text_factory = weak_text
        iterator = workspace.iter_cross_patches(8192)
        final_patch = next(iterator)
        gc.collect()
        # DISTINCT vehicle strings belonged to the temporary rank map. The
        # second SELECT supplies different string objects for per-track data.
        assert len(references) == 4
        assert all(ref() is None for ref in references[:2])
        assert all(ref() is not None for ref in references[2:])
        _, _, hit_indices = intersect_cross_track_patch(
            final_patch.starts, final_patch.ends, final_patch.headings,
            final_patch.pair_a, final_patch.pair_b, cfg,
        )
        segments = workspace.load_hit_segments(final_patch, hit_indices)
        assert len(segments) == 2  # Final yielded patch still has valid provenance.
        assert {s.vehicle_id for s in segments.values()} == {"a", "b"}
        del segments
        if finish == "exhaust":
            with pytest.raises(StopIteration):
                next(iterator)
        elif finish == "generator_close":
            iterator.close()
        elif finish == "error":
            with pytest.raises(RuntimeError, match="cancelled"):
                iterator.throw(RuntimeError("cancelled"))
        else:
            # Closing the workspace also releases metadata if its generator is
            # still suspended. Closing that generator afterwards must be safe.
            workspace.close()
            iterator.close()
        gc.collect()
        assert all(ref() is None for ref in references)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("minimum,maximum", [
    (0, 0), (0, 180), (80, 100), (90, 90), (179.999, 180),
    (170, 10), (-1, 100), (80, 181), (math.nan, 100), (0, math.inf),
])
def test_heading_coarse_ranges_conservatively_cover_exact_angles_without_duplicates(minimum, maximum):
    import numpy as np
    from track.v2.detect_workspace import _heading_bucket, _heading_ranges

    headings = [math.nan, math.inf, -math.inf, -1e300, 1e300, -1e-300, 360.0, 450.0]
    for boundary in range(0, 361, 5):
        headings.extend([math.nextafter(float(boundary), -math.inf),
                         float(boundary), math.nextafter(float(boundary), math.inf)])
    allowed = np.zeros((73, 73), dtype=np.int8)
    for source, low, high in _heading_ranges(minimum, maximum):
        allowed[source, low:high + 1] += 1
    assert np.max(allowed) == 1
    assert np.all(allowed[:, 72] == 1)
    assert np.all(allowed[72, :] == 1)
    values = np.asarray(headings)
    with np.errstate(invalid="ignore", over="ignore"):
        angles = np.abs((values[None, :] - values[:, None] + 180.0) % 360.0 - 180.0)
        exact = (minimum <= angles) & (angles <= maximum)
    buckets = np.array([_heading_bucket(h) for h in headings], dtype=int)
    covered = allowed[buckets[:, None], buckets[None, :]]
    assert np.all(covered[exact] == 1)


@pytest.mark.parametrize("angles", [(80, 100), (0, 180), (90, 90), (-1, 181)])
def test_3d_index_preserves_wrap_boundaries_unknown_candidates_and_topology(tmp_path, angles):
    import numpy as np
    headings = [0., 5., 79.99999999999999, 80., 90., 100., 100.00000000000001,
                175., 180., 270., 355., 359.99999999999994, 360., 450.,
                -1e-300, math.nan, math.inf, -math.inf, 1e300]
    tracks = {}
    for index, heading in enumerate(headings):
        direction = index * 0.37
        dx, dy = 10000 * math.cos(direction), 10000 * math.sin(direction)
        key = f"v{index % 11}\x00{index}"
        tracks[key] = track(f"v{index % 11}", index,
                            [(-dx, -dy, heading), (dx, dy, heading)])
    cfg = IntersectConfig(cross_track_min_angle_deg=angles[0], cross_track_max_angle_deg=angles[1],
                          cross_track_pair_batch_size=3, cross_track_max_grid_cells_per_segment=1)
    with np.errstate(invalid="ignore", over="ignore"):
        expected, _, _ = extract_cross_track_candidates(tracks, TRANSFORMER, cfg)
        with DetectWorkspace(cfg, directory=tmp_path) as workspace:
            for key in reversed(list(tracks)):
                workspace[key] = tracks[key]
            actual = extract_disk_cross_track_candidates(workspace, TRANSFORMER, cfg)
            assert actual == expected
            window = SpatialWindow(499980, 4319980, 500020, 4320020)
            assert [key for key, _ in workspace.iter_nearby_tracks(window)] == \
                list(_build_topology_point_index(tracks).tracks_in(window))
            assert workspace.max_pair_rows <= 3
