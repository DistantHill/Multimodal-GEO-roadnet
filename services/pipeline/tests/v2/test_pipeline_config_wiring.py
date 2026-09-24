from __future__ import annotations

from .config_factory import clip_config, fitting_config, network_config, pipeline_config, projection_config

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from track.v2.config import PostgresConfig
from track.v2.config.pipeline_config_loader import PipelineConfig
from track.v2.db.pipeline_store import DetectPersistenceResult
from track.v2.db_writer import RefineRunRecord
from track.v2.detect import IntersectConfig, IntersectResult
from track.v2.models import (
    RefineConfig,
    ClipConfig,
    FittingConfig,
    IntersectReport,
    NetworkConfig,
    ProjectionConfig,
    RuntimeConfig,
)
from track.v2.pipeline import PipelineInput, run_pipeline
from track.v2.projection import create_spatial_context


REFINE_ID = "11111111-1111-1111-1111-111111111111"
_PROJECTION_CONFIG = projection_config(processing_srid=3857)
_SPATIAL_CONTEXT = create_spatial_context(_PROJECTION_CONFIG)


def _empty_intersect_result() -> IntersectResult:
    return IntersectResult(
        turn_events=[],
        hotspot_events=[],
        clusters=[],
        topologies=[],
        observations=[],
        cross_track_candidates=[],
        report=IntersectReport(
            track_count=0,
            turn_event_count=0,
            hotspot_event_count=0,
            cross_track_candidate_count=0,
            cluster_count=0,
            cluster_count_turn=0,
            cluster_count_cross_track=0,
            complete_intersection_count=0,
        ),
        spatial_context=_SPATIAL_CONTEXT,
    )


class _Connection:
    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class _ConfigRecordingStore:
    def __init__(self) -> None:
        self.connection = MagicMock()
        self.connection.info.get_parameters.return_value = {
            "host": "postgres", "dbname": "roadnet", "user": "roadnet",
        }
        self.refine_record = RefineRunRecord(
            id=REFINE_ID,
            source_table="raw_points",
            parameters={},
            status="completed",
            spatial_context=_SPATIAL_CONTEXT,
            source_fingerprint={"version": 1, "row_count": 0, "sha256": "0"},
        )
        self.clip_result = MagicMock()
        self.clip_result.report.as_dict.return_value = {}
        self.fit_bundle = MagicMock()
        for variant in ("combined", "a_to_b", "b_to_a"):
            getattr(self.fit_bundle, variant).report.as_dict.return_value = {}
            getattr(self.fit_bundle, variant).issues = ()
        self.loaded_clip_config: object | None = None
        self.loaded_clip_publication_root: object | None = None
        self.loaded_clip_runtime: object | None = None
        self.fit_clip_config: object | None = None
        self.fit_fitting_config: object | None = None
        self.persist_detect_runtime: object | None = None
        self.persist_detect_data_root: object | None = None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def get_refine_run(self, refine_id: str) -> RefineRunRecord:
        return self.refine_record

    def start_detect_run(self, **kwargs: object) -> str:
        return "detect-1"

    def persist_detect(self, *args: object, **kwargs: object) -> DetectPersistenceResult:
        self.persist_detect_runtime = kwargs["runtime"]
        self.persist_detect_data_root = kwargs["data_root"]
        return DetectPersistenceResult(
            resolved_point_count=0,
            tracks_cross_count=0,
            turn_event_count=0,
            candidate_intersection_count=0,
            candidate_entry_exit_point_count=0,
            intersection_count=0,
            entry_exit_point_count=0,
        )

    def finish_detect(self, detect_id: str, *, status: str) -> None:
        return None

    def complete_detect_with_artifact(self, detect_id: str, descriptor: object) -> None:
        return None

    def load_clip(
        self, detect_id: str, *, publication_root: object,
        data_root: object, runtime: object, config: object, verify_sha256: bool,
    ) -> object:
        self.loaded_clip_publication_root = publication_root
        self.loaded_clip_data_root = data_root
        self.loaded_clip_runtime = runtime
        self.loaded_clip_config = config
        return self.clip_result

    def start_fit_run(
        self,
        *,
        detect_id: str,
        clip_config: object,
        fitting_config: object,
        parameters: dict[str, object],
    ) -> str:
        self.fit_clip_config = clip_config
        self.fit_fitting_config = fitting_config
        return "fit-1"

    def persist_fit_bundle(self, *args: object, **kwargs: object) -> tuple[int, ...]:
        return ()

    def finish_fit(self, fit_id: str, *, status: str, report: object = None) -> None:
        return None


class PipelineConfigWiringTest(unittest.TestCase):
    def test_non_default_stage_configs_are_used_by_loader_store_and_algorithms(self) -> None:
        configured_clip = clip_config(include_boundary_clips=False)
        configured_fitting = fitting_config(point_spacing_m=17.0)
        configured_network = network_config(include_isolated_nodes=False)
        configured_pipeline = replace(
            pipeline_config(),
            projection=_PROJECTION_CONFIG,
            clip=configured_clip,
            fitting=configured_fitting,
            network=configured_network,
        )
        store = _ConfigRecordingStore()
        intersections = _empty_intersect_result()
        reader = MagicMock()
        reader.__enter__.return_value = reader

        with (
            patch("track.v2.pipeline.pg_connect", return_value=_Connection()),
            patch("track.v2.pipeline.PostgresPipelineStore", return_value=store),
            patch("track.v2.pipeline.require_current_schema"),
            patch("track.v2.detect_stage.validate_source_table", return_value=MagicMock()),
            patch(
                "track.v2.detect_stage.calculate_source_fingerprint",
                return_value={"version": 1, "row_count": 0, "sha256": "0"},
            ),
            patch("track.v2.detect_stage.find_refine_artifact", return_value=MagicMock()),
            patch("track.v2.detect_stage.RefineDatasetReader", return_value=reader),
            patch("track.v2.detect_stage.detect_intersections", return_value=intersections) as detect,
            patch("track.v2.detect_stage.create_staging", return_value=MagicMock(stage="detect")),
            patch("track.v2.detect_stage.export_detect_parquet", return_value=MagicMock()),
            patch("track.v2.detect_stage.publish"),
            patch(
                "track.v2.fitting_stage.fit_centerline_variants",
                return_value=store.fit_bundle,
            ) as fit,
            patch("track.v2.network_stage.build_network", return_value=MagicMock()) as network,
        ):
            run_pipeline(PipelineInput(refine_id=REFINE_ID), configured_pipeline)

        with patch("psycopg.connect") as connect:
            detect.call_args.kwargs["connect_worker"]()
        connect.assert_called_once_with(
            host="postgres", dbname="roadnet", user="roadnet",
            password=configured_pipeline.postgres.password,
        )

        self.assertIs(store.loaded_clip_config, configured_clip)
        self.assertEqual(
            store.loaded_clip_publication_root,
            configured_pipeline.paths.parquet_directory,
        )
        self.assertEqual(store.loaded_clip_data_root, configured_pipeline.paths.data_root)
        self.assertIs(store.loaded_clip_runtime, configured_pipeline.runtime)
        self.assertIs(store.persist_detect_runtime, configured_pipeline.runtime)
        self.assertEqual(store.persist_detect_data_root, configured_pipeline.paths.data_root)
        self.assertIs(store.fit_clip_config, configured_clip)
        self.assertIs(store.fit_fitting_config, configured_fitting)
        fit.assert_called_once_with(store.clip_result, config=configured_fitting)
        network.assert_called_once_with(
            store.fit_bundle.combined,
            store.clip_result.intersections,
            config=configured_network,
        )


if __name__ == "__main__":
    unittest.main()
