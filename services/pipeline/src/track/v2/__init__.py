"""track.v2 — Step-based trajectory pipeline.

Architecture (linear):
    Parser 0  →  Cleaner 1  →  Intersect2  →  Clip  →  Fitting  →  Network

Each stage has a single public entry point and produces frozen intermediate
artifacts. No stage mutates another stage's output.

Intersect2 uses two complementary methods:
  1. Turn events: bearing-change detection → Getis-Ord G* → DBSCAN
  3. Cross-track candidates: cross-vehicle segment intersections → DBSCAN
Both feed into the same topology verification (entry/exit → branch clustering).
"""

from .cleaner1 import clean
from .clip import extract_clips, extract_clips_for_detect
from .fitting import fit_centerlines, fit_centerline_variants
from .intersect2 import IntersectConfig, IntersectResult, detect_intersections
from .network import build_network
from .models import (
    Centerline,
    CenterlineFitBundle,
    CleanerConfig,
    ClipConfig,
    ClipEndpoint,
    ClipInput,
    ClipIntersection,
    ClipPoint,
    ClipReport,
    ClipResult,
    ClipTrack,
    CsvSource,
    FitIssue,
    FitReport,
    FitResult,
    FittingConfig,
    NetworkConfig,
    NetworkEdge,
    NetworkIssue,
    NetworkNode,
    NetworkReport,
    NetworkResult,
    PointRef,
    PostgresSource,
    ProjectionConfig,
    ResolvedTrackPoint,
    SpatialContext,
    TrackClip,
    TrajectoryPoint,
)
from .parser0 import get_database
from .projection import (
    ProjectionError,
    QueryWindow,
    QueryWindowRequest,
    build_query_window,
    create_spatial_context,
)
from .replay import ReplayService, ReplayTarget, replay_centerline_inputs
from .pipeline import PipelineInput, PipelineResult
