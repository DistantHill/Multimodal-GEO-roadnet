import json

import numpy as np
import pytest

from track.v2.cv_preview import fit_profile
from track.v2.cv_tuning import render_comparison, write_tuning_report


def test_three_family_sweep_reuses_frozen_training_profile_and_saved_counts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    profile = fit_profile(np.array([1, 1, 3, 25]))
    (source / "profile.json").write_text(json.dumps(profile))
    metadata = {"tile_size": 2, "pixel_size_m": 2, "rule_circles": [], "selected_tiles": [
        {"split": "preview", "reason": "hotspot", "tile": [2, 3], "points": 1003,
         "occupied_pixels": 3, "peak_count": 1000}]}
    (source / "manifest.json").write_text(json.dumps(metadata))
    counts = np.array([[0, 1], [2, 1000]])
    np.savez_compressed(source / "tile-0-counts.npz", counts=counts)
    before = (source / "profile.json").read_bytes()
    result = write_tuning_report(source, tmp_path / "result")
    config = json.loads((result.parent / "comparison.json").read_text())
    assert [c["id"] for c in config["columns"]] == ["sqrt", "log1p", "asinh"]
    assert len(config["samples"][0]["variants"]) == 45
    assert json.loads((result.parent / "profile.json").read_text()) == profile
    assert (source / "profile.json").read_bytes() == before
    with np.load(result.parent / "tile-0-counts.npz") as saved:
        np.testing.assert_array_equal(saved["counts"], counts)
    search = json.loads((result.parent / "training-candidates.json").read_text())
    assert search["final_parameters"] is None
    # Each model's scale is independent: sqrt does not depend on either scale.
    variants = config["samples"][0]["variants"]
    for variant in variants:
        assert variant["image"].startswith("data:image/png;base64,")
        if variant["column"] == "sqrt":
            assert set(variant["when"]) == {"percentile"}
        if variant["column"] == "log1p":
            assert set(variant["when"]) == {"percentile", "log_scale"}
    with pytest.raises(FileExistsError):
        write_tuning_report(source, result.parent)


def test_embedded_configuration_cannot_end_script_block():
    text = '</script><script>alert("injected")</script>'
    result = render_comparison({"title": text, "axes": [], "columns": [], "samples": []})
    embedded = result.split('<script id="comparison-data" type="application/json">', 1)[1].split('</script>', 1)[0]
    assert '<script>' not in embedded
    assert json.loads(embedded)["title"] == text


def test_cli_does_not_write_runtime_files_into_repository(monkeypatch):
    from pathlib import Path
    from track.v2.cli.cv_tune import main
    package_root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr("sys.argv", ["cv-tune", "--preview", "/tmp/unused", "--output", str(package_root / "unsafe")])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
