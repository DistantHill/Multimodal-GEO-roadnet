"""Ablation retains centroid columns, releasing rebuilt cluster payloads."""
import gc
import weakref
from dataclasses import replace
from unittest.mock import patch
import numpy as np
import pytest
from .config_factory import intersect_config
from .detect_columns_helpers import TRANSFORMER
from track.v2.models import HotspotCluster, MemberBatch
from track.v2.candidates.detect_turn_events import empty_turn_batch
from track.v2.candidates.detect_cross_track_pairs import empty_cross_batch
from track.v2.intersections.detect_intersections import _annotate_source_ablation


def test_rebuilt_clusters_are_released_between_source_passes():
    cluster=HotspotCluster(0,0.,0.,0.,0.,1.,algorithm_sources=("orthogonal_cross_track","turn"))
    references=[]; sources=[]
    def build(*args,disabled_sources,**kwargs):
        gc.collect(); assert all(value() is None for value in references)
        sources.append(disabled_sources)
        members=MemberBatch(np.asarray(["turn"]),np.asarray([0],dtype=np.int64),np.asarray([0],dtype=np.int64),np.asarray(["source"]),np.asarray([True]))
        rebuilt=replace(cluster,members=members)
        references.extend((weakref.ref(rebuilt),weakref.ref(members.calculation_index),weakref.ref(members.kind)))
        return [rebuilt]
    with patch("track.v2.intersections.detect_intersections._build_candidate_clusters",side_effect=build):
        output=_annotate_source_ablation([cluster],(("turn",empty_turn_batch()),("orthogonal_cross_track",empty_cross_batch())),intersect_config(),TRANSFORMER)
    assert sources==[{"orthogonal_cross_track"},{"turn"}]
    assert output[0].critical_sources==()
    gc.collect(); assert all(value() is None for value in references)


@pytest.mark.parametrize("separation,critical",[(0.,()),(10.,()),(1000.,("orthogonal_cross_track","turn"))])
def test_compact_ablation_preserves_independent_expected_source_criticality(separation,critical):
    from .test_detect_algorithm_regressions import _events
    from track.v2.intersections.detect_intersections import _build_candidate_clusters
    turns=_events([[i,i] for i in range(4)])
    cross=_events([[separation+i,i] for i in range(4)],cross=True)
    config=intersect_config(candidate_dbscan_eps_m=20.,candidate_dbscan_min_samples=2,merge_centroid_distance_m=25.)
    sources=(("turn",turns),("orthogonal_cross_track",cross))
    clusters=_build_candidate_clusters(sources,config,TRANSFORMER)
    annotated=_annotate_source_ablation(clusters,sources,config,TRANSFORMER)
    assert tuple(sorted(source for cluster in annotated for source in cluster.critical_sources))==critical


@pytest.mark.parametrize("distance,critical",[(5.,()),(4.999,("turn",))])
def test_rediscovery_includes_distance_boundary(distance,critical):
    cluster=HotspotCluster(0,0.,0.,0.,0.,1.,algorithm_sources=("turn",))
    other=replace(cluster,centroid_x=3.,centroid_y=4.)
    with patch("track.v2.intersections.detect_intersections._build_candidate_clusters",return_value=[other]):
        result=_annotate_source_ablation([cluster],(("turn",empty_turn_batch()),),intersect_config(merge_centroid_distance_m=distance),TRANSFORMER)
    assert result[0].critical_sources==critical
