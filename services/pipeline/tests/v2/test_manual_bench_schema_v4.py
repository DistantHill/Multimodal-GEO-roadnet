from __future__ import annotations

import json
from dataclasses import replace

import pytest

from track.v2.manual_thin_benchmark.manual_bench_schema_v4 import (
    PIPELINE_TIMING_KEYS,
    _clip_digest,
    _find_regressions,
    _load_pipeline_timings,
    _rows_digest,
)
from track.v2.models import (
    ClipEndpoint,
    ClipPoint,
    ClipReport,
    ClipResult,
    SpatialContext,
    TrackClip,
)


def _report(*, bytes_per_row: float, query_s: float) -> dict[str, object]:
    return {
        "tables": {
            "resolved_subtrack_points": {
                "table_bytes_per_row": bytes_per_row,
                "total_bytes_per_row": bytes_per_row,
            }
        },
        "pipeline_timings": {key: 1.0 for key in PIPELINE_TIMING_KEYS},
        "clip_benchmark": {
            "query_median_s": query_s,
            "assembly_median_s": 1.0,
        },
        "behavior": {"clip_report": {"total_clips": 1}},
    }


def test_a65_gate_only_rejects_order_of_magnitude_or_behavior_changes() -> None:
    baseline = _report(bytes_per_row=100.0, query_s=1.0)
    acceptable = _report(bytes_per_row=999.0, query_s=10.0)
    assert _find_regressions(acceptable, baseline) == []

    regressed = _report(bytes_per_row=1001.0, query_s=10.01)
    messages = _find_regressions(regressed, baseline)
    assert any("bytes_per_row" in message for message in messages)
    assert any("query_median_s" in message for message in messages)

    changed = _report(bytes_per_row=100.0, query_s=1.0)
    changed["behavior"] = {"clip_report": {"total_clips": 2}}
    assert _find_regressions(changed, baseline) == [
        "behavior output differs from the supplied baseline"
    ]


def test_pipeline_stats_requires_all_a65_timings(tmp_path) -> None:
    complete = {key: index + 0.5 for index, key in enumerate(PIPELINE_TIMING_KEYS)}
    path = tmp_path / "stats.json"
    path.write_text(json.dumps(complete), encoding="utf-8")
    assert _load_pipeline_timings(path) == complete

    path.write_text(json.dumps({"detect_compute_s": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing A65 timing keys"):
        _load_pipeline_timings(path)


def test_clip_behavior_digest_uses_stable_ids_not_float_distance() -> None:
    context = SpatialContext(
        source_srid=4326,
        processing_srid=32650,
        projection_config={"coordinate_semantics_version": 2},
    )
    clip = TrackClip(
        clip_id="volatile-label",
        vehicle_id="v1",
        trip_id="trip",
        clip_type="between_intersection",
        start=ClipEndpoint("exit", 116.0, 39.0, 1, 10),
        end=ClipEndpoint("entry", 116.1, 39.1, 2, 20),
        points=(ClipPoint(99, 116.05, 39.05),),
        distance_m=100.0,
        point_count=3,
        subtrack_index=7,
    )
    report = ClipReport(1, 1, 1, 1, 0, 0, 0, 3)
    first = ClipResult((clip,), report, context)
    changed_distance = ClipResult(
        (replace(clip, distance_m=101.0),),
        report,
        context,
    )
    assert _clip_digest(first) == _clip_digest(changed_distance)


def test_persisted_row_digest_is_order_and_value_sensitive() -> None:
    first = _rows_digest([(1, "entry", 116.0), (2, "exit", 116.1)])
    assert first == _rows_digest([(1, "entry", 116.0), (2, "exit", 116.1)])
    assert first != _rows_digest([(2, "exit", 116.1), (1, "entry", 116.0)])
    assert first != _rows_digest([(1, "entry", 116.0), (2, "exit", 116.2)])
