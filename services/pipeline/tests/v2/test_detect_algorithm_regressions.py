"""Independent numeric, identity, clustering, and progress regressions."""
import math
import unittest
from dataclasses import fields, replace
from unittest.mock import patch
import numpy as np
import pytest
from .config_factory import intersect_config
from .detect_columns_helpers import TRANSFORMER
from track.v2.candidates.detect_turn_events import empty_turn_batch
from track.v2.candidates.detect_cross_track_pairs import empty_cross_batch
from track.v2.models import HotspotCluster, MemberBatch
from track.v2.intersections.performance_timing import PerfTracker
from track.v2.intersections.detect_intersections import (
    _run_dbscan, _build_dbscan_grid, _compute_getis_ord_g_star,
    _select_hotspot_events, _cluster_dbscan, _merge_clusters,
    _cluster_orthogonal_cross_track_candidates, _annotate_source_ablation,
)


def _events(xy, *, indices=None, cross=False, turns=None):
    xy=np.asarray(xy,dtype=np.float64).reshape(-1,2)
    size=len(xy)
    empty=empty_cross_batch() if cross else empty_turn_batch()
    columns={item.name: np.zeros((size,2) if item.name in {"xy","lonlat"} else size,
                                dtype=getattr(empty,item.name).dtype) for item in fields(empty)}
    for name, column in columns.items():
        if column.dtype.kind in "OUS":
            column[:] = "p" if name.endswith("point_kind") else "" if name.endswith("generated_reason") else "fixture"
    columns["xy"]=xy
    columns["lonlat"]=np.zeros((size,2),dtype=np.float64)
    columns["calculation_index"]=np.arange(size,dtype=np.int64) if indices is None else np.asarray(indices,dtype=np.int64)
    if not cross:
        columns["turn_deg"]=np.full(size,90.) if turns is None else np.asarray(turns,dtype=np.float64)
        columns["score"]=np.abs(columns["turn_deg"])
    return replace(empty,**columns)


class DBSCANArrayContractTest(unittest.TestCase):
    def test_seed_neighbor_telemetry_is_block_level(self):
        tracker = PerfTracker("dbscan-block-test")
        count = 260

        labels = _run_dbscan(
            [0.0] * count,
            [0.0] * count,
            eps_m=1.0,
            min_samples=2,
            perf=tracker,
        )

        self.assertEqual(labels, [0] * count)
        node = tracker._root.children["dbscan_points"].children["dbscan_seed_neighbors"]
        self.assertLessEqual(node.call_count, count)


    def test_labels_obey_boundary_contract(self):
        """The spatial index preserves labels, IDs, order, and <= eps semantics."""
        cases = (
            ([], [], 1.0, 1, []),
            ([0.0], [0.0], 1.0, 1, [0]),
            ([0.0, 3.0, 6.0], [0.0, 0.0, 0.0], 1.0, 2, [-1, -1, -1]),
            ([0.0, 0.1, 0.2, 10.0, 10.1, 10.2], [0.0] * 6, 0.5, 2, [0, 0, 0, 1, 1, 1]),
            ([0.0, 0.9, 1.8, 2.7, 3.6], [0.0] * 5, 1.0, 2, [0, 0, 0, 0, 0]),
            ([-1.0, -1.0, -1.0, -4.0, -4.0], [-1.0, -1.0, -1.0, 3.0, 3.0], 0.1, 2, [0, 0, 0, 1, 1]),
            ([0.0, 1.0, math.nextafter(1.0, math.inf)], [0.0] * 3, 1.0, 1, [0, 0, 0]),
            ([0.0, 0.0], [0.0, 0.0], 0.0, 2, [0, 0]),
            ([0.0, 0.5], [0.0, 0.0], -1.0, 2, [0, 0]),
            ([math.nan], [0.0], 1.0, 1, [-1]),
            ([0.0, 1.0], [0.0, 0.0], math.nan, 1, [-1, -1]),
            ([0.0, 1.0], [0.0, 0.0], math.inf, 1, [0, 0]),
            ([0.0, math.inf], [0.0, 0.0], 1.0, 1, [0, -1]),
            ([0.0], [0.0], 1.0, 0, [0]),
            ([0.0], [0.0], 1.0, -1, [0]),
        )
        for xs, ys, eps_m, min_samples, expected in cases:
            with self.subTest(xs=xs, eps_m=eps_m, min_samples=min_samples):
                self.assertEqual(
                    _run_dbscan(xs, ys, eps_m=eps_m, min_samples=min_samples),
                    expected,
                )
        with self.assertRaises(IndexError):
            _run_dbscan([0.0, 1.0], [0.0], eps_m=1.0, min_samples=1)


    def test_grid_keeps_neighbors_across_zero_exact_boundary(self):
        """A one-ULP/cross-zero cell boundary cannot hide an exact neighbour."""
        xs = [-1e-300, 1.0, math.nextafter(1.0, math.inf), math.nextafter(-1e-300, -math.inf)]
        ys = [0.0] * len(xs)
        expected = [0, 0, 0, 0]
        self.assertEqual(_run_dbscan(xs, ys, eps_m=1.0, min_samples=1), expected)


    def test_many_disconnected_clusters_do_not_require_cluster_sized_resets(self):
        """Regression shape for per-cluster queue clearing: 400 singleton clusters."""
        xs = [float(index * 10) for index in range(400)]
        self.assertEqual(_run_dbscan(xs, [0.0] * len(xs), eps_m=0.1, min_samples=1), list(range(400)))


    def test_dense_grid_enqueues_each_point_at_most_once(self):
        """Dense clusters do not re-materialise N neighbours for every core."""
        from track.v2.intersections.detect_intersections import _take_grid_frontier_neighbors

        taken = 0

        def observe(*args):
            nonlocal taken
            result = _take_grid_frontier_neighbors(*args)
            taken += len(result)
            return result

        count = 2_000
        with patch(
            "track.v2.intersections.detect_intersections._take_grid_frontier_neighbors",
            side_effect=observe,
        ):
            labels = _run_dbscan(
                [0.0] * count,
                [0.0] * count,
                eps_m=1.0,
                min_samples=3,
            )

        self.assertEqual(labels, [0] * count)
        self.assertLessEqual(taken, count)



@pytest.mark.parametrize('xs,ys,eps,min_samples,expected', [
    ([-1e-300, 1., math.nextafter(1., math.inf)] + [1000.+i*10 for i in range(254)], [0.]*257, 1., 1, [0,0,0]+list(range(1,255))),
    ([0., .9, 1.7, 1.5, 1.5]+[100.+i*10 for i in range(252)], [0.,0.,0.,.6,-.6]+[100.]*252, 1., 4, [0]*5+[-1]*252),
    ([0.,.9,1.7,1.5,1.5,-.9,-1.7,-1.5,-1.5]+[100.+i*10 for i in range(248)], [0.,0.,0.,.6,-.6,0.,0.,.6,-.6]+[100.]*248, 1., 4, [0]*5+[1]*4+[-1]*248),
    ([1e308, math.nextafter(1e308,-math.inf)]+[float(i*10) for i in range(255)], [0.]*257, 1., 1, list(range(257))),
])
def test_dbscan_large_frontier_border_and_finite_fallback(xs,ys,eps,min_samples,expected):
    assert _run_dbscan(xs,ys,eps_m=eps,min_samples=min_samples)==expected


def test_dbscan_seeded_dense_components_have_explicit_labels():
    for seed in range(6):
        rng=np.random.default_rng(seed)
        xy=np.concatenate((rng.uniform(-.1,.1,(150,2)),rng.uniform(9.9,10.1,(150,2))))
        assert _run_dbscan(xy[:,0],xy[:,1],eps_m=.5,min_samples=5)==[0]*150+[1]*150


@pytest.mark.parametrize('xy, radius, signs', [
    ([[0,0],[5,5],[10,10],[-5,-5],[5000,5000]],200.,[1,1,1,1,-1]),
    ([[i*10000,i*10000] for i in range(5)],180.,[0]*5),
    ([[0,0]],180.,[0]),
])
def test_gstar_concentration_sparse_and_single(xy,radius,signs):
    scores=_compute_getis_ord_g_star(_events(xy),radius)
    assert [int(np.sign(scores[i])) for i in range(len(xy))]==signs


def test_gstar_inclusive_radius_non_contiguous_ids():
    radius=180.
    events=_events([[0,0],[radius,0],[math.nextafter(radius,0.),0],
                    [math.nextafter(radius,math.inf),0],[-radius,0]],indices=[10,27,51,88,103])
    counts=np.array([4,4,4,3,2],dtype=np.float64)
    expected=(counts-counts.mean())/counts.std()
    scores=_compute_getis_ord_g_star(events,radius)
    np.testing.assert_allclose([scores[i] for i in events.calculation_index],expected)


@pytest.mark.parametrize('radius',[-180.,1e308])
def test_gstar_negative_and_squared_overflow_radius(radius):
    scores=_compute_getis_ord_g_star(_events([[i*400,-i*400] for i in range(260)]),radius)
    assert list(scores.values())==[0.]*260


def test_gstar_huge_finite_coordinate_range_uses_bounded_fallback():
    scores=_compute_getis_ord_g_star(_events([[1e308,i*3] for i in range(257)]),1.)
    assert list(scores.values())==[0.]*257


@pytest.mark.parametrize('base', [0.,-1e-300])
def test_gstar_large_seams_duplicate_members_and_threshold(base):
    radius=180.
    xy=np.tile([[base,0],[radius,0],[math.nextafter(radius,0.),0],
                [math.nextafter(radius,math.inf),0],[-radius,0]],(52,1))
    events=_events(xy,indices=np.arange(260,dtype=np.int64)*13+10000)
    counts=np.tile([4,4,4,3,2],52)*52
    expected=(counts-counts.mean())/counts.std()
    scores=_compute_getis_ord_g_star(events,radius)
    np.testing.assert_allclose([scores[i] for i in events.calculation_index],expected)
    selected=_select_hotspot_events(events,intersect_config(g_star_radius_m=radius,g_star_min_z=.5))
    assert selected.calculation_index.tolist()==events.calculation_index[expected>=.5].tolist()


def test_gstar_coincident_members_have_zero_scores():
    assert list(_compute_getis_ord_g_star(_events([[3,-4]]*260),180.).values())==[0.]*260


def test_gstar_progress_telemetry_and_repeatability():
    calls=[]
    class Live:
        def update(self,**kwargs): calls.append(kwargs)
    events=_events([[i*400,-i*400] for i in range(600)])
    perf=PerfTracker('gstar-arrays')
    first=_compute_getis_ord_g_star(events,180.,live_status=Live(),perf=perf)
    second=_compute_getis_ord_g_star(events,180.,live_status=Live(),perf=perf)
    assert first==second
    assert len(calls)>=4
    report=perf.report()
    assert 'gstar_events' in report and 'gstar_neighbors' in report
    assert perf._root.children['gstar_events'].children['gstar_neighbors'].call_count==1200


def test_cluster_summary_counts_order_and_noise():
    events=_events([[i,i] for i in range(4)]+[[300+i,300+i] for i in range(4)])
    clusters=_cluster_dbscan(events,TRANSFORMER,eps_m=100.,min_samples=3)
    assert len(clusters)==2
    assert clusters[0].algorithm_sources==('turn',)
    assert clusters[0].source_counts=={'turn':4}
    assert clusters[0].source_ratios=={'turn':1.}
    assert clusters[0].members.calculation_index.tolist()==[0,1,2,3]
    assert _cluster_dbscan(_events([[0,0],[5000,5000]]),TRANSFORMER,eps_m=100.,min_samples=3)==[]
    assert len(_cluster_dbscan(_events([[i*5,i*5] for i in range(5)]),TRANSFORMER,eps_m=200.,min_samples=3))==1


def test_cluster_merge_keeps_namespace_counts_and_member_order():
    turn=_cluster_dbscan(_events([[0,0]]),TRANSFORMER,eps_m=100.,min_samples=1)[0]
    cross=_cluster_orthogonal_cross_track_candidates(_events([[20,20],[25,25]],cross=True),TRANSFORMER,eps_m=100.,min_samples=1)[0]
    merged=_merge_clusters([turn,cross],80.,TRANSFORMER)
    assert len(merged)==1
    assert merged[0].source=='merged'
    assert merged[0].algorithm_sources==('orthogonal_cross_track','turn')
    assert merged[0].source_counts=={'turn':1,'orthogonal_cross_track':2}
    assert merged[0].source_ratios['orthogonal_cross_track']==2/3
    assert sorted(zip(merged[0].members.kind,merged[0].members.calculation_index))==[('orthogonal_cross_track',0),('orthogonal_cross_track',1),('turn',0)]
    assert merged[0].members.member_order.tolist()==[0,1,2]


def test_distant_empty_cluster_summaries_do_not_merge():
    clusters=[HotspotCluster(0,0.,0.,0.,0.,50.,source='turn'),
              HotspotCluster(1,5000.,5000.,0.,0.,50.,source='orthogonal_cross_track')]
    assert len(_merge_clusters(clusters,80.,TRANSFORMER))==2


@pytest.mark.parametrize('cross',[False,True])
def test_source_ablation_reclusters_and_marks_only_source_critical(cross):
    events=_events([[i*2,i*2] for i in range(4)],cross=cross)
    cfg=intersect_config(dbscan_eps_m_turn=100.,dbscan_min_samples_turn=3,
                         cross_track_dbscan_eps_m=100.,cross_track_dbscan_min_samples=2)
    turns=empty_turn_batch() if cross else events
    candidates=events if cross else empty_cross_batch()
    clusterer=_cluster_orthogonal_cross_track_candidates if cross else _cluster_dbscan
    clusters=clusterer(events,TRANSFORMER,eps_m=100.,min_samples=2)
    annotated=_annotate_source_ablation(clusters,turns,candidates,cfg,TRANSFORMER)
    source='orthogonal_cross_track' if cross else 'turn'
    assert len(annotated)==1
    assert annotated[0].algorithm_sources==(source,)
    assert annotated[0].critical_sources==(source,)


def test_gstar_negative_grid_coordinates_and_duplicate_members():
    xy=np.tile([[-180,-180],[-180,-180],[-.1,-.1],[-360,-.1],[-540,-180]],(52,1))
    counts=np.tile([2,2,1,1,1],52)*52
    expected=(counts-counts.mean())/counts.std()
    scores=_compute_getis_ord_g_star(_events(xy),180.)
    np.testing.assert_allclose(list(scores.values()),expected)


def test_hotspot_selection_filters_small_turn_even_with_high_gstar():
    events=_events([[0,0],[1,1],[-1,-1],[2,2],[5000,5000],[5100,5100]],turns=[90,90,90,20,90,90])
    selected=_select_hotspot_events(events,intersect_config(g_star_radius_m=200.,g_star_min_z=.5,min_turn_deg=45.))
    assert selected.calculation_index.tolist()==[0,1,2]
    assert np.all(np.abs(selected.turn_deg)>=45.)


@pytest.mark.parametrize('cross',[False,True])
def test_detect_result_serializes_repeatable_relations_as_column_batches(tmp_path,cross):
    import json
    from .config_factory import runtime_config
    from .detect_columns_helpers import CONTEXT
    from track.v2.candidates.turn_event_relation import TurnEventRelation
    from track.v2.candidates.cross_track_relation import CrossCandidateRelation
    from track.v2.intersections.detect_intersections import _empty_intersect_result
    batch=_events([[0,0],[1,1]],cross=cross)
    relation=(CrossCandidateRelation if cross else TurnEventRelation)(runtime_config(),tmp_path)
    try:
        relation.append_batch(batch)
        relation.seal()
        field='cross_track_candidates' if cross else 'turn_events'
        result=replace(_empty_intersect_result(CONTEXT),**{field:relation})
        eager=replace(result,**{field:batch})
        assert result.as_dict()==eager.as_dict()
        assert json.loads(json.dumps(result.as_dict()))==eager.as_dict()
        assert result.as_dict()==eager.as_dict()
    finally:
        relation.close()
