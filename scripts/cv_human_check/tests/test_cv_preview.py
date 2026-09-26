from contextlib import nullcontext
from dataclasses import replace
import json
from types import SimpleNamespace

import numpy as np
import pytest

from scripts.cv_human_check import cv_preview
from scripts.cv_human_check.preview import (
    METHODS, count_quantile, fit_profile, normalize, raster_tile, split_tiles, write_report,
)
from track.v2.config import PostgresConfig


@pytest.mark.parametrize("database, expected", [(None, "configured"), ("requested", "requested")])
def test_main_loads_pipeline_config_and_optionally_overrides_database(
        monkeypatch, tmp_path, database, expected):
    postgres = PostgresConfig(
        host="postgres", port=5432, database="configured", user="roadnet", password="secret",
        connect_timeout=10, sslmode=None, keepalives=True, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=3, tcp_user_timeout=30_000,
    )
    connected = []
    connection = SimpleNamespace(execute=lambda _statement: None)
    argv = ["cv-preview", "--detect-id", "00000000-0000-0000-0000-000000000001",
            "--output", str(tmp_path / "out")]
    if database:
        argv.extend(("--database", database))

    monkeypatch.setattr(cv_preview, "load_startup_config",
                        lambda: SimpleNamespace(postgres=postgres))
    monkeypatch.setattr(cv_preview, "connect",
                        lambda config: connected.append(config) or nullcontext(connection))
    monkeypatch.setattr(cv_preview, "fetch_detect_row",
                        lambda conn, value: (_ for _ in ()).throw(RuntimeError("stop after connect")))
    monkeypatch.setattr("sys.argv", argv)

    with pytest.raises(RuntimeError, match="stop after connect"):
        cv_preview.main()

    assert len(connected) == 1
    assert connected[0] == replace(postgres, database=expected)


def test_weighted_quantiles_match_expanded_pixel_population():
    values = np.array([1, 2, 7, 1200])
    frequencies = np.array([500, 200, 30, 1])
    expanded = np.repeat(values, frequencies)
    for percentile in (0, 50, 99, 99.5, 99.9, 100):
        assert count_quantile(values, frequencies, percentile) == pytest.approx(
            np.percentile(expanded, percentile))


@pytest.mark.parametrize("method", METHODS)
def test_frozen_mapping_preserves_zero_monotonicity_and_is_independent_of_tile(method):
    profile = fit_profile(np.array([1, 1, 2, 10, 100]))
    before = json.dumps(profile)
    small = normalize(np.array([0, 1, 2, 10, 100]), profile, method)
    with_hotspot = normalize(np.array([0, 1, 2, 10, 100, 10**9]), profile, method)
    np.testing.assert_array_equal(small, with_hotspot[:-1])
    assert small[0] == 0
    assert np.all(np.diff(small) >= 0)
    assert np.all((with_hotspot >= 0) & (with_hotspot <= 1))
    assert json.dumps(profile) == before


def test_cdf_ties_unseen_counts_and_no_percentile_clipping():
    profile = fit_profile(np.array([1, 1, 3, 5]))
    counts = np.array([0, 1, 2, 3, 4, 5, 100])
    expected = [0, .5, .5, .75, .75, 1, 1]
    for p in (99.0, 99.5, 99.9):
        np.testing.assert_allclose(normalize(counts, profile, "cdf", p), expected)


def test_uint8_log_does_not_overflow_and_counts_are_not_mutated():
    profile = fit_profile(np.array([255]))
    counts = np.array([0, 255], dtype=np.uint8)
    np.testing.assert_array_equal(normalize(counts, profile, "log1p"), [0, 1])
    np.testing.assert_array_equal(counts, [0, 255])


@pytest.mark.parametrize("counts", [[], [0], [-1], [np.nan], [1.5]])
def test_invalid_or_empty_training_population_is_rejected(counts):
    with pytest.raises(ValueError):
        fit_profile(np.array(counts))


def test_half_open_tiles_north_up_negative_coordinates_and_point_conservation():
    pixels = np.array([[-1, -1, 7], [0, 0, 3], [0, 1, 2], [1, 0, 5], [2, 0, 11]])
    np.testing.assert_array_equal(raster_tile(pixels, (0, 0), 2), [[2, 0], [3, 5]])
    assert raster_tile(pixels, (-1, -1), 2)[0, 1] == 7
    assert sum(raster_tile(pixels, t, 2).sum() for t in [(-1, -1), (0, 0), (1, 0)]) == 28


def test_holdout_and_buffer_never_enter_fitted_statistics():
    tile_x = np.array([-3, -2, -1, 0, 1, 2])
    roles = split_tiles(tile_x, 0)
    np.testing.assert_array_equal(roles, ["train", "train", "buffer", "buffer", "preview", "preview"])
    profile = fit_profile(np.array([1, 3, 10**6, 10**6, 10**9, 10**9])[roles == "train"])
    assert profile["quantiles"]["100.0"] == 3


def test_report_contains_five_methods_and_counts_but_no_external_assets(tmp_path):
    pixels = np.array([[-40, 0, 2], [-39, 1, 3], [20, 0, 1000], [21, 1, 8]])
    profile = fit_profile(np.array([2, 3]))
    metadata = {"tile_size": 16, "pixel_size_m": 2, "split_tile_x": 0}
    report = write_report(tmp_path / "report", pixels, profile, metadata, [])
    page = report.read_text()
    assert "data:image/png;base64," in page
    assert "https://" not in page
    for method in METHODS:
        assert f"<figcaption>{method}</figcaption>" in page
    saved = json.loads((report.parent / "profile.json").read_text())
    assert saved == profile
    for item in metadata["selected_tiles"]:
        if item["split"] == "preview":
            assert item["mapping_stats"]["linear-p99.5"]["pixels_above_clip_count"] > 0
    with pytest.raises(FileExistsError):
        write_report(report.parent, pixels, profile, metadata, [])


def test_cli_rejects_invalid_configuration_before_database_access(monkeypatch, tmp_path):
    from scripts.cv_human_check.cv_preview import main
    for flag, value in [("--pixel-size", "nan"), ("--tile-size", "0"), ("--log-scale", "0")]:
        monkeypatch.setattr("sys.argv", ["cv-preview", "--detect-id", "00000000-0000-0000-0000-000000000001",
                                       "--output", str(tmp_path / "out"), flag, value])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2
