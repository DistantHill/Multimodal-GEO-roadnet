"""Detect Run/CRS boundaries and direct column materialization regressions."""
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from .config_factory import intersect_config, intersect_run_config, detect_context, runtime_config
from .detect_columns_helpers import subtrack, sql_turns_from_subtrack
from track.v2.models import SpatialContext, RefinementSubtrack
from track.v2.exceptions import V2Error
from track.v2.projection import CoordinateTransformer, SpatialWindow, QueryWindowRequest, build_query_window
from track.v2.db.source_table import SourceTableContract
from track.v2.candidates.query_source_windows import _query_active_points_in_window
from track.v2.trajectories.query_refine_points import _stream_vehicle_tracks_from_db
from track.v2.candidates.collect_candidates import extract_columnar_cross_track_candidates
from track.v2.candidates.detect_turn_events import empty_turn_batch
from track.v2.intersections.detect_intersections import detect_intersections, _detect_intersections_db, _run_columnar_db_detect

_TEST_SPATIAL_CONTEXT=SpatialContext(4326,32650,{"coordinate_semantics_version":2,"enforce_area_of_use":True,"max_scale_error":.01})
_TEST_TRANSFORMER=CoordinateTransformer(_TEST_SPATIAL_CONTEXT)


class ProjectionContextContractTest(unittest.TestCase):
    class _Handle:
        def __init__(self, tracks=(), spatial_context=None):
            self.subtracks=tuple(tracks)
            self.spatial_context=spatial_context

    def test_detect_requires_explicit_run_spatial_context(self):
        with self.assertRaisesRegex(V2Error, "requires.*SpatialContext"):
            detect_intersections(
                self._Handle(), config=intersect_config(), context=detect_context(),
                runtime=runtime_config(),
            )


    def test_empty_result_carries_parent_run_spatial_context(self):
        result = detect_intersections(
            self._Handle(), config=intersect_config(), context=detect_context(),
            runtime=runtime_config(), spatial_context=_TEST_SPATIAL_CONTEXT,
        )

        self.assertEqual(result.spatial_context, _TEST_SPATIAL_CONTEXT)


    def test_refine_handle_context_must_match_detect_context(self):
        with self.assertRaisesRegex(V2Error, "does not match"):
            detect_intersections(
                self._Handle(spatial_context=object()),
                config=intersect_config(), context=detect_context(),
                runtime=runtime_config(), spatial_context=_TEST_SPATIAL_CONTEXT,
            )


    def test_formal_db_detect_requires_refine_before_any_data_query(self):
        with self.assertRaisesRegex(V2Error, "requires refine_id"):
            detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True),
                runtime=runtime_config(),
            )


    def test_formal_db_detect_requires_runtime_and_data_root(self):
        refine = SimpleNamespace(
            refine_id="refine-1", source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint={"version": 1},
        )
        reader = SimpleNamespace(descriptor=SimpleNamespace(
            refine_id="refine-1", source_fingerprint_json='{"version":1}',
        ))
        with (
            patch("track.v2.intersections.detect_intersections.fetch_refine_run_row", return_value=refine),
            self.assertRaisesRegex(V2Error, "runtime and data_root"),
        ):
            detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                runtime=runtime_config(), refine_reader=reader,
            )


    def test_formal_db_detect_requires_columnar_reader(self):
        refine = SimpleNamespace(
            refine_id="refine-1", source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint={"version": 1},
        )
        with (
            patch("track.v2.intersections.detect_intersections.fetch_refine_run_row", return_value=refine),
            patch("track.v2.db.source_table.verify_refine_source_fingerprint") as fingerprint,
            self.assertRaisesRegex(V2Error, "RefineDatasetReader"),
        ):
            detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                runtime=runtime_config(), data_root=Path(tempfile.mkdtemp()),
            )
        fingerprint.assert_not_called()


    def test_formal_parquet_detect_rejects_fingerprint_mismatch_without_pg_scan(self):
        refine = SimpleNamespace(
            refine_id="11111111-1111-1111-1111-111111111111",
            source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT,
            source_fingerprint={"version": 1},
        )
        reader = SimpleNamespace(descriptor=SimpleNamespace(
            refine_id=refine.refine_id,
            source_fingerprint_json='{"version":2}',
        ))
        with (
            patch("track.v2.intersections.detect_intersections.fetch_refine_run_row", return_value=refine),
            patch("track.v2.intersections.detect_intersections._detect_intersections_db") as compute,
            self.assertRaisesRegex(V2Error, "fingerprint"),
        ):
            detect_intersections(
                conn=object(),
                config=intersect_config(),
                context=detect_context(query_active_from_db=True, refine_id=refine.refine_id),
                runtime=runtime_config(), refine_reader=reader,
            )
        compute.assert_not_called()


    def test_formal_parquet_empty_extent_avoids_pg_point_queries(self):
        reader = MagicMock()
        reader.extent.return_value = None
        result = _detect_intersections_db(
            object(), intersect_config(), _TEST_SPATIAL_CONTEXT,
            runtime=runtime_config(), data_root=Path(tempfile.mkdtemp()),
            refine_reader=reader,
        )
        reader.iter_tracks.assert_not_called()
        self.assertEqual(result.report.track_count, 0)


    def test_formal_parquet_cross_track_uses_columnar_pairs_not_disk_index(self):
        reader = MagicMock()
        reader.iter_cross_track_pair_batches.return_value = iter(())
        with (
            patch("track.v2.candidates.collect_candidates._prepare_segments") as prepare,
            patch("track.v2.candidates.collect_candidates.build_cross_track_index") as build_index,
        ):
            actual = extract_columnar_cross_track_candidates(
                reader,
                "source",
                _TEST_TRANSFORMER,
                intersect_run_config(source_table="source"),
            )
        self.assertEqual(len(actual), 0)
        prepare.assert_not_called()
        build_index.assert_not_called()


    def test_disabled_formal_parquet_cross_track_does_not_read_pair_planner(self):
        reader = MagicMock()
        cfg = intersect_run_config(use_cross_track_candidates=False)
        reader.extent.return_value = (0.0, 0.0, 1.0, 1.0)
        reader.iter_turn_candidates.return_value = iter(())
        reader.track_count.return_value = 0
        with (
            patch("track.v2.intersections.detect_intersections.run_grid_cross_track_task") as columnar,
            patch("track.v2.intersections.detect_intersections._cluster_pipeline", return_value=(empty_turn_batch(), [])),
            patch("track.v2.intersections.detect_intersections._build_topologies_from_tracks", return_value=([], [], [])),
        ):
            _run_columnar_db_detect(
                cfg,
                _TEST_SPATIAL_CONTEXT,
                reader,
                runtime=runtime_config(),
                data_root=Path(tempfile.mkdtemp()),
            )
        columnar.assert_not_called()
        reader.iter_cross_track_pair_batches.assert_not_called()


    def test_formal_db_detect_rejects_assertion_and_source_mismatch(self):
        refine = SimpleNamespace(
            refine_id="refine-1", source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint={"version": 1},
        )
        different = SpatialContext(
            4326, 32649,
            {"coordinate_semantics_version": 2, "enforce_area_of_use": True, "max_scale_error": .01},
        )
        with patch("track.v2.intersections.detect_intersections.fetch_refine_run_row", return_value=refine):
            with self.assertRaisesRegex(V2Error, "does not match"):
                detect_intersections(
                    conn=object(), config=intersect_config(),
                    context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                    runtime=runtime_config(), spatial_context=different,
                )
            with self.assertRaisesRegex(V2Error, "source_table"):
                detect_intersections(
                    conn=object(),
                    config=intersect_config(),
                    context=detect_context(
                        query_active_from_db=True, refine_id="refine-1", source_table="wrong",
                    ),
                    runtime=runtime_config(),
                )


    def test_formal_db_detect_rejects_unready_refine_and_table_overrides_before_reads(self):
        for status, fingerprint in (("running", {"version": 1}), ("completed", None)):
            refine = SimpleNamespace(
                refine_id="refine-1", source_table="raw_points", status=status,
                spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint=fingerprint,
            )
            with (
                self.subTest(status=status, fingerprint=fingerprint),
                patch("track.v2.intersections.detect_intersections.fetch_refine_run_row", return_value=refine),
            ):
                with self.assertRaisesRegex(V2Error, "completed immutable-source"):
                    detect_intersections(
                        conn=object(),
                        config=intersect_config(),
                        context=detect_context(query_active_from_db=True, refine_id="refine-1"),
                        runtime=runtime_config(),
                    )

        refine = SimpleNamespace(
            refine_id="refine-1", source_table="raw_points", status="completed",
            spatial_context=_TEST_SPATIAL_CONTEXT, source_fingerprint={"version": 1},
        )
        for override in (
            {"db_table": "other"},
            {"kept_table": "other_members"},
        ):
            with (
                self.subTest(override=override),
                patch("track.v2.intersections.detect_intersections.fetch_refine_run_row", return_value=refine),
            ):
                with self.assertRaisesRegex(V2Error, "does not belong|must use"):
                    detect_intersections(
                        conn=object(),
                        config=intersect_config(),
                        context=detect_context(
                            query_active_from_db=True, refine_id="refine-1", **override,
                        ),
                        runtime=runtime_config(),
                    )



class ActivePointsWindowQueryTelemetryTest(unittest.TestCase):
    _ROWS=[('vehicle-1',123.5,116.1,39.2,90.,8.,'source-A',7,{'tag':'kept'},'',12934999.25,4748000.5,0,7,1,0)]

    class _Cursor:
        def __init__(self,rows): self.rows=rows; self.query=None; self.params=None
        def __enter__(self): return self
        def __exit__(self,*_args): return False
        def execute(self,query,params): self.query=query; self.params=params
        def __iter__(self): return iter(self.rows)

    class _Connection:
        def __init__(self,rows): self.cursor_instance=ActivePointsWindowQueryTelemetryTest._Cursor(rows)
        def cursor(self,**_kwargs): return self.cursor_instance

    def setUp(self):
        self.window=build_query_window(QueryWindowRequest(116.1,39.2,4.,6.))
        contract=SourceTableContract(source_table='source-A',quoted_table='"custom_points"',key_sql_type='bigint',has_extra=True)
        for module in ('track.v2.candidates.query_source_windows','track.v2.trajectories.query_refine_points'):
            patcher=patch(module+'.validate_source_table',return_value=contract)
            patcher.start(); self.addCleanup(patcher.stop)

    def test_query_without_telemetry_returns_columns_and_preserves_query_contract(self):
        conn=self._Connection(self._ROWS); window=self.window
        tracks=_query_active_points_in_window(conn,window,table='custom_points',source_table='source-A',spatial_context=_TEST_SPATIAL_CONTEXT,telemetry=None)
        sql=conn.cursor_instance.query
        self.assertIn('FROM "custom_points"',sql)
        self.assertNotIn('injected_points',sql)
        self.assertIn('geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)',sql)
        self.assertIn(f'ST_X(ST_Transform(geom, {window.query_srid})) >= %s',sql)
        self.assertEqual(conn.cursor_instance.params,['source-A',*window.prefilter_bounds,window.x_min,window.y_min,window.x_max,window.y_max])
        self.assertEqual(len(tracks),1)
        track=tracks[0]
        self.assertEqual(track.vehicle_id,'vehicle-1')
        self.assertEqual(track.source_id,'source-A')
        self.assertEqual(track.timestamp.tolist(),[123.5])
        np.testing.assert_array_equal(track.lonlat,[[116.1,39.2]])
        np.testing.assert_array_equal(track.xy,[[12934999.25,4748000.5]])
        self.assertEqual(track.source_row_key.tolist(),[7])
        self.assertEqual(track.source_row_key_valid.tolist(),[True])

    def test_query_kept_mode_joins_frozen_refine_members_without_active_filter(self):
        conn=self._Connection(self._ROWS); window=self.window
        tracks=_query_active_points_in_window(conn,window,table='tp',kept_table='ktp',source_table='source-A',refine_id='refine-9',spatial_context=_TEST_SPATIAL_CONTEXT)
        sql=conn.cursor_instance.query
        for text in ('FROM ktp AS ktp','JOIN "custom_points" AS tp','ON tp.id = ktp.source_row_key','ktp.refine_id = %s'):
            self.assertIn(text,sql)
        self.assertNotIn('is_active',sql)
        self.assertIn('FROM injected_points   WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)',sql)
        self.assertEqual(conn.cursor_instance.params,['source-A','refine-9',*window.prefilter_bounds,window.x_min,window.y_min,window.x_max,window.y_max,'public.injected_points',*window.prefilter_bounds,window.x_min,window.y_min,window.x_max,window.y_max,'refine-9'])
        self.assertEqual(len(tracks),1)

    def test_query_kept_mode_requires_refine_id(self):
        with self.assertRaises(ValueError):
            _query_active_points_in_window(self._Connection(self._ROWS),SpatialWindow(-1.,-2.,3.,4.),kept_table='ktp',spatial_context=_TEST_SPATIAL_CONTEXT)

    def test_window_preserves_injected_database_identity_provenance_and_xy(self):
        rows=[('vehicle-1',123.5,116.1,39.2,90.,0.,'public.injected_points',282,{},'orthogonal_heading',12946000.25,4762000.5,1,282,1,0)]
        conn=self._Connection(rows)
        tracks=_query_active_points_in_window(conn,self.window,table='tp',kept_table='ktp',source_table='source-A',refine_id='refine-9',spatial_context=_TEST_SPATIAL_CONTEXT)
        outer=conn.cursor_instance.query.partition('FROM (')[0]
        self.assertNotIn('from_source_row_key',outer); self.assertNotIn('to_source_row_key',outer)
        self.assertIn('generated_reason',outer); self.assertIn('x, y',outer)
        track=tracks[0]
        self.assertEqual(track.kind.tolist(),['i'])
        self.assertEqual(track.refine_point_id.tolist(),[282])
        self.assertEqual(track.source_row_key_valid.tolist(),[False])
        self.assertEqual(track.generated_reason.tolist(),['orthogonal_heading'])
        np.testing.assert_array_equal(track.xy,[[12946000.25,4762000.5]])

    def test_stream_uses_injected_table_and_id_as_point_identity(self):
        rows=[('vehicle-1',123.5,116.1,39.2,90.,0.,'public.injected_points',282,{},'heading_orthogonal',12946000.25,4762000.5,1,282,1,0)]
        conn=self._Connection(rows)
        tracks=list(_stream_vehicle_tracks_from_db(conn,table='tp',kept_table='ktp',refine_id='refine-9',spatial_context=_TEST_SPATIAL_CONTEXT))
        self.assertEqual(conn.cursor_instance.params,['tp','refine-9','public.injected_points','refine-9'])
        self.assertEqual(len(tracks),1)
        self.assertEqual(tracks[0].refine_point_id.tolist(),[282])
        self.assertEqual(tracks[0].source_row_key_valid.tolist(),[False])
        self.assertEqual(tracks[0].source_id,'public.injected_points')
        np.testing.assert_array_equal(tracks[0].xy,[[12946000.25,4762000.5]])

    def test_query_with_telemetry_records_disjoint_query_and_materialization_phases(self):
        telemetry={}
        with patch('track.v2.candidates.query_source_windows.time.perf_counter',side_effect=(10.,12.5,20.,20.75)):
            tracks=_query_active_points_in_window(self._Connection(self._ROWS),self.window,spatial_context=_TEST_SPATIAL_CONTEXT,telemetry=telemetry)
        self.assertEqual(len(tracks),1)
        self.assertEqual(telemetry,{'queried_rows':1,'query_seconds':2.5,'materialization_seconds':.75})

    def test_raw_multi_vehicle_has_query_local_distinct_partitions(self):
        second=('vehicle-2',*self._ROWS[0][1:])
        tracks=_query_active_points_in_window(self._Connection(self._ROWS+[second]),self.window,spatial_context=_TEST_SPATIAL_CONTEXT)
        self.assertEqual([t.vehicle_id for t in tracks],['vehicle-1','vehicle-2'])
        self.assertEqual([t.subtrack_index for t in tracks],[1,2])


def test_authoritative_subtrack_xy_is_preserved_without_forward_projection():
    from .detect_columns_helpers import CONTEXT, TRANSFORMER
    track=subtrack([[12345.25,-98765.5],[12346.25,-98765.5]],headings=[90,90])
    with patch.object(TRANSFORMER.__class__,'forward_many',side_effect=AssertionError('projection must be reused')):
        result=detect_intersections({track.subtrack_index:track},config=intersect_config(),context=detect_context(),runtime=runtime_config(),spatial_context=CONTEXT)
    assert result.report.track_count==1
    np.testing.assert_array_equal(track.xy,[[12345.25,-98765.5],[12346.25,-98765.5]])


def test_half_populated_authoritative_geometry_is_rejected_at_model_boundary():
    track=subtrack([[0,0],[1,1]])
    with pytest.raises(ValueError,match=r'float64 \(N,2\)'):
        replace(track,xy=np.ones((2,1),dtype=np.float64))


def test_projection_uses_fixed_context_independent_of_input_order():
    lonlat=np.array([[115.9,38.9],[116.1,39.1]],dtype=np.float64)
    projected=_TEST_TRANSFORMER.forward_many(lonlat)
    reversed_projected=_TEST_TRANSFORMER.forward_many(lonlat[::-1])
    np.testing.assert_array_equal(projected,reversed_projected[::-1])


def test_detect_rejects_unprojected_source_columns_instead_of_legacy_point_graph():
    with pytest.raises(TypeError,match='RefinementSubtrack'):
        detect_intersections([object()],config=intersect_config(),context=detect_context(),runtime=runtime_config(),spatial_context=_TEST_SPATIAL_CONTEXT)


@pytest.mark.parametrize('heading,expected_turn',[(45.,45.),(170.,170.),(44.9,None),(170.1,None)])
def test_turn_thresholds_keep_exact_bounds_and_reject_normalized_outside(heading,expected_turn):
    track=subtrack([[0,0],[10,0],[20,0]],headings=[0,heading,heading])
    events=sql_turns_from_subtrack(track,intersect_config(turn_window=1,min_turn_deg=45.,max_turn_deg=170.,min_leg_m=1.,min_movement_speed_mps=0.))
    if expected_turn is None: assert len(events)==0
    else: assert events.turn_deg.tolist()==[expected_turn]
