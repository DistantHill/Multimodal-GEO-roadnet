from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from track.v2.config import PostgresConfig
from track.v2.config.pipeline_config import PipelineConfig
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
_PROJECTION_CONFIG = ProjectionConfig(processing_srid=3857)
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

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def get_refine_run(self, refine_id: str) -> RefineRunRecord:
        return self.refine_record

    def start_detect_run(self, **kwargs: object) -> str:
        return "detect-1"

    def persist_detect(self, *args: object, **kwargs: object) -> DetectPersistenceResult:
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
        data_root: object, runtime: object, config: object,
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
        db_config = PostgresConfig(
            host="localhost", port=5432, database="track", user="track", password="",
        )
        clip_config = ClipConfig(include_boundary_clips=False)
        fitting_config = FittingConfig(point_spacing_m=17.0)
        network_config = NetworkConfig(include_isolated_nodes=False)
        pipeline_config = PipelineConfig(
            projection=_PROJECTION_CONFIG,
            refine=RefineConfig(),
            detect=IntersectConfig(),
            clip=clip_config,
            fitting=fitting_config,
            network=network_config,
            runtime=RuntimeConfig(),
        )
        store = _ConfigRecordingStore()
        intersections = _empty_intersect_result()
        reader = MagicMock()
        reader.__enter__.return_value = reader

        with (
            patch("track.v2.pipeline.load_pipeline_config", return_value=pipeline_config),
            patch("track.v2.pipeline.pg_connect", return_value=_Connection()),
            patch("track.v2.pipeline.PostgresPipelineStore", return_value=store),
            patch("track.v2.pipeline.require_schema_v4"),
            patch("track.v2.pipeline.verify_refine_source_fingerprint"),
            patch("track.v2.pipeline.validate_source_table", return_value=MagicMock()),
            patch(
                "track.v2.pipeline.calculate_source_fingerprint",
                return_value={"version": 1, "row_count": 0, "sha256": "0"},
            ),
            patch("track.v2.pipeline.find_refine_artifact", return_value=MagicMock()),
            patch("track.v2.pipeline.RefineDatasetReader", return_value=reader),
            patch("track.v2.pipeline.detect_intersections", return_value=intersections),
            patch("track.v2.pipeline.create_staging", return_value=MagicMock(stage="detect")),
            patch("track.v2.pipeline.export_detect_parquet", return_value=MagicMock()),
            patch("track.v2.pipeline.publish"),
            patch("track.v2.pipeline.fit_centerline_variants", return_value=store.fit_bundle) as fit,
            patch("track.v2.pipeline.build_network", return_value=MagicMock()) as network,
            patch("track.v2.pipeline._add_clip_stats"),
            patch("track.v2.pipeline._add_fit_stats"),
            patch("track.v2.pipeline._add_network_stats"),
            patch("track.v2.pipeline._parquet_root", return_value=Path("/tmp/parquet")),
            patch("track.v2.pipeline._data_root", return_value=Path("/tmp/data")),
        ):
            run_pipeline(PipelineInput(db_config, refine_id=REFINE_ID))

        self.assertIs(store.loaded_clip_config, clip_config)
        self.assertEqual(store.loaded_clip_publication_root, Path("/tmp/parquet"))
        self.assertEqual(store.loaded_clip_data_root, Path("/tmp/data"))
        self.assertIs(store.loaded_clip_runtime, pipeline_config.runtime)
        self.assertIs(store.fit_clip_config, clip_config)
        self.assertIs(store.fit_fitting_config, fitting_config)
        fit.assert_called_once_with(store.clip_result, config=fitting_config)
        network.assert_called_once_with(
            store.fit_bundle.combined,
            store.clip_result.intersections,
            config=network_config,
        )


if __name__ == "__main__":
    unittest.main()
