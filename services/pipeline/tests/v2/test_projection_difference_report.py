from __future__ import annotations

from copy import deepcopy

import pytest

from track.v2.manual_thin_benchmark.projection_difference_report import (
    _write_new,
    accept_projection_report,
    build_projection_difference_report,
    verify_projection_approval,
)


def _snapshots() -> tuple[dict[str, object], dict[str, object]]:
    legacy: dict[str, object] = {
        "schema": "track.v2-projection-snapshot-v1",
        "provenance": {"source": "fixture-v1", "algorithm": "legacy-local-origin"},
        "crs": {"kind": "legacy_local_origin", "origin": [116.0, 39.0]},
        "points": [{"id": "source:a", "lon": 116.0, "lat": 39.0}],
        "candidates": [{"id": "a|b", "members": ["a:0", "b:0"]}],
        "clusters": [{"id": "cluster:0", "members": ["a|b", "c|d"]}],
        "clips": [{"id": "clip:0", "members": [1, 2, 3], "length_m": 20.0}],
        "fittings": [{"id": "fit:0", "coordinates": [[116.0, 39.0], [116.001, 39.0]]}],
    }
    fixed = deepcopy(legacy)
    fixed["provenance"] = {"source": "fixture-v1", "algorithm": "fixed-optimized"}
    fixed["crs"] = {"epsg": 32650}
    fixed["points"][0]["lon"] += 1e-7  # type: ignore[index]
    fixed["clusters"][0]["members"] = ["c|d", "a|b"]  # type: ignore[index]
    fixed["clips"][0]["length_m"] = 20.25  # type: ignore[index]
    fixed["fittings"][0]["coordinates"][1][0] += 2e-7  # type: ignore[index]
    return legacy, fixed


def test_projection_difference_reports_nonzero_and_structural_changes() -> None:
    legacy, fixed = _snapshots()

    report = build_projection_difference_report(legacy, fixed, version="test")

    assert report["review_status"] == "pending_review"
    assert report["stages"]["points"]["ellipsoidal_displacement_m"]["max"] > 0
    assert report["stages"]["clusters"]["changed_ordered_membership"] == ["cluster:0"]
    assert report["stages"]["clips"]["absolute_length_delta_m"]["max"] == .25
    assert report["stages"]["fittings"]["paired_vertex_displacement_m"]["max"] > 0


def test_missing_stages_are_not_evaluated() -> None:
    legacy, fixed = _snapshots()
    for snapshot in (legacy, fixed):
        snapshot.pop("clips")
        snapshot.pop("fittings")
    report = build_projection_difference_report(legacy, fixed, version="test")

    assert report["stages"]["clips"] == {"status": "not_evaluated"}
    assert report["stages"]["fittings"] == {"status": "not_evaluated"}


def test_approval_is_explicit_and_bound_to_report_and_inputs() -> None:
    legacy, fixed = _snapshots()
    report = build_projection_difference_report(legacy, fixed, version="test")
    approval = accept_projection_report(report, accepted_by="reviewer", reason="reviewed")

    verify_projection_approval(report, approval)
    changed_report = build_projection_difference_report(
        legacy, {**fixed, "tag": "changed"}, version="test",
    )
    with pytest.raises(ValueError, match="different report or input pair"):
        verify_projection_approval(changed_report, approval)


def test_tampered_report_cannot_be_approved() -> None:
    legacy, fixed = _snapshots()
    report = build_projection_difference_report(legacy, fixed, version="test")
    report["review_status"] = "accepted"

    with pytest.raises(ValueError, match="report hash"):
        accept_projection_report(report, accepted_by="reviewer", reason="reviewed")


def test_tampered_report_is_rejected_during_approval_verification() -> None:
    legacy, fixed = _snapshots()
    report = build_projection_difference_report(legacy, fixed, version="test")
    approval = accept_projection_report(report, accepted_by="reviewer", reason="reviewed")
    report["parameters"] = {"tampered": True}

    with pytest.raises(ValueError, match="report hash"):
        verify_projection_approval(report, approval)


def test_incomplete_or_nonfinite_stage_data_is_rejected() -> None:
    legacy, fixed = _snapshots()
    fixed["clips"][0].pop("length_m")  # type: ignore[index]
    with pytest.raises((TypeError, ValueError), match="length_m"):
        build_projection_difference_report(legacy, fixed, version="test")

    legacy, fixed = _snapshots()
    fixed["points"][0]["lon"] = float("nan")  # type: ignore[index]
    with pytest.raises(ValueError, match="finite"):
        build_projection_difference_report(legacy, fixed, version="test")


def test_artifact_writer_never_overwrites_existing_content(tmp_path) -> None:
    output = tmp_path / "report.json"
    output.write_text("keep\n", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        _write_new(output, {"replacement": True})

    assert output.read_text(encoding="utf-8") == "keep\n"
