"""Public exports remain compatible without eager numerical initialization."""

import importlib
import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", [
    "track.v2", "track.v2.patches", "track.v2.resource_budget", "track.v2.db.source_table",
])
def test_lightweight_import_does_not_initialize_numerical_libraries(module):
    result = subprocess.run(
        [sys.executable, "-c", (
            "import importlib,json,sys; "
            f"importlib.import_module({module!r}); "
            "print(json.dumps([n for n in ('numpy','pyproj','track.v2.fitting',"
            "'track.v2.pipeline') if n in sys.modules]))"
        )],
        text=True, capture_output=True, check=True, timeout=20,
    )
    assert json.loads(result.stdout) == []


def test_every_existing_public_export_preserves_identity():
    import track.v2 as v2

    expected = {
        "refine": "refine_vehicle",
        "clip": "extract_clips extract_clips_for_detect",
        "fitting": "fit_centerlines fit_centerline_variants",
        "detect": "IntersectConfig IntersectResult detect_intersections",
        "network": "build_network",
        "models": (
            "Centerline CenterlineFitBundle RefineConfig ClipConfig ClipEndpoint "
            "ClipInput ClipIntersection ClipPoint ClipReport ClipResult ClipTrack "
            "CsvSource FitIssue FitReport FitResult FittingConfig NetworkConfig "
            "NetworkEdge NetworkIssue NetworkNode NetworkReport NetworkResult "
            "PointRef PostgresSource ProjectionConfig ResolvedTrackPoint SpatialContext "
            "TrackClip TrajectoryPoint"
        ),
        "parser": "get_database",
        "projection": (
            "ProjectionError QueryWindow QueryWindowRequest build_query_window create_spatial_context"
        ),
        "replay": "ReplayService ReplayTarget replay_centerline_inputs",
        "pipeline": "PipelineInput PipelineResult",
    }
    names = {name for values in expected.values() for name in values.split()}
    assert set(v2.__all__) == names
    assert names <= set(dir(v2))
    for module, values in expected.items():
        for name in values.split():
            assert getattr(v2, name) is getattr(importlib.import_module(f"track.v2.{module}"), name)
    with pytest.raises(AttributeError):
        getattr(v2, "not_a_public_stage")


def test_star_import_exposes_documented_names():
    namespace = {}
    exec("from track.v2 import *", namespace)
    assert callable(namespace["refine_vehicle"])
    assert callable(namespace["fit_centerlines"])
    assert "PipelineResult" in namespace
    assert "_MODULE_EXPORTS" not in namespace
