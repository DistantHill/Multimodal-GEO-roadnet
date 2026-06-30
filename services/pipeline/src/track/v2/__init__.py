"""track.v2 — re-export adapter for the v2 package.

The actual implementation lives in the top-level ``v2`` package.
This module provides the ``track.v2`` import path expected by tests
and linter-generated code.
"""

from v2.exceptions import (
    ConfigError,
    DataSourceCorrupted,
    FieldSelectionRequired,
    TrajectoryOrderError,
    V2Error,
)
from v2.models import (
    CleanReport,
    CleanerConfig,
    CleanerResult,
    CsvSource,
    EntryExitObservation,
    ExcludedIndex,
    ExcludedPoint,
    FieldMapping,
    FieldSpec,
    HotspotCluster,
    IntersectReport,
    IntersectionTopology,
    OrthogonalCandidate,
    ParseIssue,
    PointRef,
    PostgresSource,
    ReadReport,
    SourceInfo,
    TrackModel,
    TrajectoryPoint,
    TurnEvent,
)
from v2.parser0 import (
    CsvSource,
    PostgresSource,
    TrajectorySourceHandle,
    get_database,
)
from v2.cleaner1 import clean
from v2.intersect2 import detect_intersections, IntersectConfig, IntersectResult
