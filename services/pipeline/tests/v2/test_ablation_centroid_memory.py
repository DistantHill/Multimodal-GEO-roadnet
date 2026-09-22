from .config_factory import (clip_config, fitting_config, intersect_config, network_config, postgres_config, projection_config, refine_config, runtime_config)
"""Source ablation keeps its full-cluster semantics without retaining payloads."""
from dataclasses import replace
import gc
import weakref
from unittest.mock import patch

import pytest

from track.v2.detect import (
    IntersectConfig,
    _annotate_source_ablation,
    _build_candidate_clusters,
    _cluster_sources,
    _is_cluster_rediscovered,
    _with_source_provenance,
)
from track.v2.models import CrossTrackCandidate, HotspotCluster, PointRef, TurnEvent
from .test_detect import _TEST_TRANSFORMER


def _old_annotation(clusters, turns, crossings, config):
    """The previous implementation retained complete reclustered objects."""
    reclustered = {
        source: _build_candidate_clusters(
            turns, crossings, config, _TEST_TRANSFORMER,
            disabled_sources={source},
        )
        for source in sorted({source for cluster in clusters for source in _cluster_sources(cluster)})
    }
    return [
        _with_source_provenance(
            cluster,
            critical_sources=tuple(
                source for source in _cluster_sources(cluster)
                if not _is_cluster_rediscovered(
                    cluster, reclustered[source],
                    distance_m=config.merge_centroid_distance_m,
                )
            ),
        )
        for cluster in clusters
    ]


def _event(index, x, y):
    lon, lat = _TEST_TRANSFORMER.inverse(x, y)
    return TurnEvent(index, f"v{index}", float(index), lon, lat, x, y, 0, 90, 90, 90)


def _crossing(index, x, y):
    lon, lat = _TEST_TRANSFORMER.inverse(x, y)
    return CrossTrackCandidate(
        point_ref=PointRef("s", str(index), stable=True),
        from_segment_start_point_ref=PointRef("s", f"a{index}", stable=True),
        to_segment_start_point_ref=PointRef("s", f"b{index}", stable=True),
        vehicle_id=f"cross{index}", timestamp=float(index), lon=lon, lat=lat,
        x=x, y=y, heading=0., speed=10.,
    )


@pytest.mark.parametrize("separation", [0., 10., 1000.])
def test_compact_ablation_matches_full_reclustering_reference(separation):
    x, y = _TEST_TRANSFORMER.forward(116., 39.)
    turns = [_event(i, x + i, y + i) for i in range(4)]
    crossings = [_crossing(i, x + separation + i, y + i) for i in range(4)]
    config = intersect_config(
        dbscan_eps_m_turn=20., dbscan_min_samples_turn=2,
        cross_track_dbscan_eps_m=20., cross_track_dbscan_min_samples=2,
        merge_centroid_distance_m=25.,
    )
    clusters = _build_candidate_clusters(turns, crossings, config, _TEST_TRANSFORMER)
    expected = _old_annotation(clusters, turns, crossings, config)
    assert _annotate_source_ablation(clusters, turns, crossings, config, _TEST_TRANSFORMER) == expected


def test_full_reclustered_objects_are_released_before_next_source():
    base = HotspotCluster(
        cluster_calculate_index=0, centroid_x=0., centroid_y=0.,
        centroid_lon=116., centroid_lat=39., radius_m=25.,
        algorithm_sources=("orthogonal_cross_track", "turn"),
    )
    refs = []
    calls = []

    def build(*_args, disabled_sources, **_kwargs):
        gc.collect()
        assert all(reference() is None for reference in refs)
        calls.append(disabled_sources)
        event = _event(len(calls), 0., 0.)
        point_ref = PointRef("only-in-reclustered-result", str(len(calls)), stable=True)
        rebuilt = replace(
            base, events=(event,), source_point_refs={"turn": (point_ref,)},
        )
        refs.extend((weakref.ref(rebuilt), weakref.ref(event), weakref.ref(point_ref)))
        return [rebuilt]

    with patch("track.v2.detect._build_candidate_clusters", side_effect=build):
        actual = _annotate_source_ablation([base], [], [], intersect_config(), _TEST_TRANSFORMER)
    gc.collect()
    assert all(reference() is None for reference in refs)
    assert calls == [{"orthogonal_cross_track"}, {"turn"}]
    assert actual[0].critical_sources == ()


def test_rediscovery_still_accepts_full_clusters_and_includes_distance_boundary():
    cluster = HotspotCluster(0, 0., 0., 116., 39., 1.)
    other = replace(cluster, centroid_x=3., centroid_y=4.)
    assert _is_cluster_rediscovered(cluster, [other], distance_m=5.)
    assert not _is_cluster_rediscovered(cluster, [other], distance_m=4.999)
    assert not _is_cluster_rediscovered(cluster, [], distance_m=5.)
