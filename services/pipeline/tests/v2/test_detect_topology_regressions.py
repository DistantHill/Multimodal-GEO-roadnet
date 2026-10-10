"""Complete-subtrack routing, circle boundaries, and cursor lifetime contracts."""
from unittest.mock import Mock, patch
import numpy as np
import pytest
from .config_factory import intersect_run_config
from .detect_columns_helpers import subtrack, TRANSFORMER
from track.v2.models import HotspotCluster, IntersectionTopology
from track.v2.trajectories.results.read_parquet import TopologyTrackSelection
from track.v2.intersections.extract_topology import (
    _build_topologies_from_reader, _extract_entry_exit_observations_batch,
    _cluster_angles_greedy, _nearest_angle_id,
)


def cluster(index=0):
    return HotspotCluster(cluster_calculate_index=index, centroid_x=0., centroid_y=0.,
                          centroid_lon=0., centroid_lat=0., radius_m=1., source="turn")


def cfg():
    return intersect_run_config(min_intersection_radius_m=50., topology_radius_padding_m=0., bearing_window=1)


def _reader(tracks):
    reader = Mock()
    reader.iter_topology_tracks.return_value = iter(
        TopologyTrackSelection((0,), str(track.subtrack_index), track) for track in tracks
    )
    return reader


@pytest.mark.parametrize("points,indices", [
    ([[-250.,0.],[0.,0.],[250.,0.]],[0,1]),
    ([[-250.,0.],[250.,0.]],[0,0]),
])
def test_segments_with_outer_endpoints_still_produce_entry_and_exit(points,indices):
    track=subtrack(points,subtrack_index=123)
    topologies,annotated,observations=_build_topologies_from_reader([cluster()],cfg(),TRANSFORMER,refine_reader=_reader([track]))
    crossings=annotated[0].crossing_points
    assert crossings.is_entry.tolist()==[True,False]
    assert crossings.point_index.tolist()==indices
    assert crossings.calculation_index.tolist()==[0,1]
    assert sum(len(batch) for batch in observations)==1
    assert observations[0].entry_crossing_calculate_index.tolist()==[0]
    assert observations[0].exit_crossing_calculate_index.tolist()==[1]


def test_same_vehicle_subtracks_do_not_form_a_synthetic_segment():
    tracks={17:subtrack([[-90.,0.],[-80.,0.]],subtrack_index=17),
            42:subtrack([[80.,0.],[90.,0.]],subtrack_index=42)}
    _,annotated,observations=_build_topologies_from_reader([cluster()],cfg(),TRANSFORMER,refine_reader=_reader(tracks.values()))
    assert len(annotated[0].crossing_points)==0
    assert sum(len(batch) for batch in observations)==0


def test_reader_topology_closes_selection_iterator_on_geometry_failure():
    closed=[]
    def selections():
        try:
            yield TopologyTrackSelection((0,),"123",subtrack([[-100.,0.],[100.,0.]],subtrack_index=123))
        finally:
            closed.append(True)
    reader=Mock()
    reader.iter_topology_tracks.return_value=selections()
    with patch("track.v2.intersections.extract_topology._extract_entry_exit_observations_batch",side_effect=KeyboardInterrupt),pytest.raises(KeyboardInterrupt):
        _build_topologies_from_reader([cluster()],cfg(),TRANSFORMER,refine_reader=reader)
    assert closed==[True]


def test_reader_topology_matches_index_for_duplicate_windows_and_track_order():
    a=subtrack([[-100.,0.],[100.,0.]],subtrack_index=99)
    b=subtrack([[0.,-100.],[0.,100.]],subtrack_index=2)
    reader=Mock()
    reader.iter_topology_tracks.return_value=iter([TopologyTrackSelection((0,1),"99",a),TopologyTrackSelection((0,1),"2",b)])
    _,annotated,observations=_build_topologies_from_reader([cluster(),cluster(1)],cfg(),TRANSFORMER,refine_reader=reader)
    for result in annotated:
        assert result.crossing_points.calculation_index.tolist()==[0,1,2,3]
        assert result.crossing_points.subtrack_index.tolist()==[99,99,2,2]
    assert sum(len(batch) for batch in observations)==4
    reader.iter_tracks.assert_not_called()


@pytest.mark.parametrize("points,entries", [
    ([[-100.,50.],[100.,50.]],[]),
    ([[-100.,0.],[-50.,0.],[-100.,0.]],[]),
    ([[-100.,0.],[-50.,0.],[0.,0.],[50.,0.],[100.,0.]],[True,False]),
    ([[0.,0.],[100.,0.]],[False]),
    ([[-100.,0.],[0.,0.]],[True]),
])
def test_circle_tangent_shared_vertex_touch_and_open_observations(points,entries):
    track=subtrack(points)
    observations,crossings=_extract_entry_exit_observations_batch({track.subtrack_index:track},cluster=cluster(),transformer=TRANSFORMER,radius_m=50.)
    assert crossings.is_entry.tolist()==entries
    assert len(observations)==(1 if entries==[True,False] else 0)
    if len(crossings)==2:
        assert crossings.segment_fraction.tolist()==[0.,0.]


def test_branch_clustering():
    centers=_cluster_angles_greedy([0.,90.,180.,270.,5.,95.,175.,265.],35.)
    assert len(centers)==4


def test_branch_center_weights_every_member_equally():
    assert _cluster_angles_greedy([0., 20., 40.], 35.) == pytest.approx([20.])


def test_branch_center_does_not_drift_into_a_separate_branch():
    # Pairwise averaging follows the chain and absorbs the 70/80 degree branch.
    angles = [0., 10., 20., 30., 40., 50., 70., 80.]
    centers = _cluster_angles_greedy(angles, 35.)
    assert centers == pytest.approx([25., 75.])
    assert _cluster_angles_greedy(list(reversed(angles)), 35.) == centers


def test_branch_center_preserves_member_weights_across_zero_degrees():
    centers = _cluster_angles_greedy([1., 2., 359.], 35.)
    expected = np.degrees(np.arctan2(
        np.sin(np.radians([1., 2., 359.])).sum(),
        np.cos(np.radians([1., 2., 359.])).sum(),
    )) % 360.
    assert centers == pytest.approx([expected])


def test_nearest_angle_id():
    assert [_nearest_angle_id(angle,[0.,90.,180.,270.]) for angle in [359.,89.,181.,269.]]==[0,1,2,3]


def test_transition_matrix():
    topology=IntersectionTopology(0,[0.,90.,180.,270.],{(0,1):5,(1,2):3,(2,3):2,(0,2):4},14,True,"complete",50.)
    assert sum(topology.transition_counts.values())==topology.observation_count
    assert topology.is_complete
