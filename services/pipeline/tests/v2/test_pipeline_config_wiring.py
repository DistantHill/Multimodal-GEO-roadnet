from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from track.v2.config import PostgresConfig
from track.v2.config.pipeline_config import PipelineConfig
from track.v2.db.pipeline_store import DetectPersistenceResult
from track.v2.db_writer import CleanRunRecord
from track.v2.intersect2 import IntersectConfig, IntersectResult
from track.v2.models import (
    CleanerConfig,
    ClipConfig,
    FittingConfig,
    IntersectReport,
    NetworkConfig,
    RuntimeConfig,
)
from track.v2.pipeline import PipelineInput, run_pipeline


CLEAN_ID = "11111111-1111-1111-1111-111111111111"


def _empty_intersect_result() -> IntersectResult:
    return IntersectResult(
        turn_events=[],
        hotspot_events=[],
        orthogonal_candidates=[],
        clusters=[],
        topologies=[],
        observations=[],
        cross_track_candidates=[],
        report=IntersectReport(
            track_count=0,
            turn_event_count=0,
            hotspot_event_count=0,
            orthogonal_candidate_count=0,
            cross_track_candidate_count=0,
            cluster_count=0,
            cluster_count_turn=0,
            cluster_count_ortho=0,
            cluster_count_cross_track=0,
            complete_intersection_count=0,
        ),
    )


class _Connection:
    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class _ConfigRecordingStore:
    def __init__(self) -> None:
        self.connection = object()
        self.clean_record = CleanRunRecord(
            id=CLEAN_ID,
            source_id="raw_points",
            source_kind="postgres_table",
            parameters={"target_table": "raw_points"},
            status="completed",
        )
        self.clip_result = MagicMock()
        self.clip_result.report.as_dict.return_value = {}
        self.fit_bundle = MagicMock()
        for variant in ("combined", "a_to_b", "b_to_a"):
            getattr(self.fit_bundle, variant).report.as_dict.return_value = {}
            getattr(self.fit_bundle, variant).issues = ()
        self.loaded_clip_config: object | None = None
        self.fit_clip_config: object | None = None
        self.fit_fitting_config: object | None = None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def get_clean_run(self, clean_id: str) -> CleanRunRecord:
        return self.clean_record

    def start_detect_run(self, **kwargs: object) -> str:
        return "detect-1"

    def persist_detect(self, *args: object, **kwargs: object) -> DetectPersistenceResult:
        return DetectPersistenceResult(
            tracks_cross_count=0,
            candidate_intersection_ids={},
            candidate_entry_exit_ids={},
            intersection_ids={},
            turn_event_count=0,
            candidate_intersection_count=0,
            candidate_entry_exit_point_count=0,
            intersection_count=0,
            entry_exit_point_count=0,
        )

    def finish_detect(self, detect_id: str, *, status: str) -> None:
        return None

    def load_clip(self, detect_id: str, *, config: object) -> object:
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
            cleaner=CleanerConfig(),
            detect=IntersectConfig(),
            clip=clip_config,
            fitting=fitting_config,
            network=network_config,
            runtime=RuntimeConfig(),
        )
        store = _ConfigRecordingStore()
        intersections = _empty_intersect_result()

        with (
            patch("track.v2.pipeline.load_pipeline_config", return_value=pipeline_config),
            patch("track.v2.pipeline.pg_connect", return_value=_Connection()),
            patch("track.v2.pipeline.PostgresPipelineStore", return_value=store),
            patch("track.v2.pipeline.detect_intersections", return_value=intersections),
            patch("track.v2.pipeline.fit_centerline_variants", return_value=store.fit_bundle) as fit,
            patch("track.v2.pipeline.build_network", return_value=MagicMock()) as network,
            patch("track.v2.pipeline._add_clip_stats"),
            patch("track.v2.pipeline._add_fit_stats"),
            patch("track.v2.pipeline._add_network_stats"),
        ):
            run_pipeline(PipelineInput(db_config, clean_id=CLEAN_ID))

        self.assertIs(store.loaded_clip_config, clip_config)
        self.assertIs(store.fit_clip_config, clip_config)
        self.assertIs(store.fit_fitting_config, fitting_config)
        fit.assert_called_once_with(store.clip_result, config=fitting_config)
        network.assert_called_once_with(
            store.fit_bundle.combined,
            intersections,
            config=network_config,
        )


if __name__ == "__main__":
    unittest.main()
