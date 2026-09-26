from contextlib import nullcontext
import json
from types import SimpleNamespace
import uuid

import numpy as np
from PIL import Image
import pytest

from scripts.cv_human_check import export_intersections
from scripts.cv_human_check.export_intersections import crop_grid, rasterize, unique_pixels, write_dataset
from scripts.cv_human_check.preview import normalize
from track.v2.config import PostgresConfig


def test_main_loads_pipeline_config_and_overrides_database(monkeypatch, tmp_path):
    postgres = PostgresConfig(
        host="postgres", port=5432, database="configured", user="roadnet", password="secret",
        connect_timeout=10, sslmode=None, keepalives=True, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=3, tcp_user_timeout=30_000,
    )
    connected = []
    connection = SimpleNamespace(execute=lambda _statement: None)
    detect_id = uuid.uuid4()

    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(export_intersections, "load_startup_config",
                        lambda: SimpleNamespace(postgres=postgres))
    monkeypatch.setattr(export_intersections, "connect",
                        lambda config: connected.append(config) or nullcontext(connection))
    monkeypatch.setattr(export_intersections, "fetch_detect_row",
                        lambda conn, value: (_ for _ in ()).throw(RuntimeError("stop after connect")))
    monkeypatch.setattr(
        "sys.argv",
        ["export_intersections", "--database", "requested", "--detect-id", str(detect_id),
         "--bbox", "116", "39", "117", "40", "--output", str(tmp_path / "export")],
    )

    with pytest.raises(RuntimeError, match="stop after connect"):
        export_intersections.main()

    assert len(connected) == 1
    assert connected[0] == PostgresConfig(
        host="postgres", port=5432, database="requested", user="roadnet", password="secret",
        connect_timeout=10, sslmode=None, keepalives=True, keepalives_idle=30,
        keepalives_interval=10, keepalives_count=3, tcp_user_timeout=30_000,
    )


def test_circle_crop_preserves_full_extent_on_negative_global_grid():
    grid = crop_grid(-1, 1, 2, 2)
    assert grid == (-2, -1, 1, 2)
    pixels = np.array([[-2, -1, 5], [0, 1, 7], [-1, 0, 2]])
    counts = rasterize(pixels, grid)
    np.testing.assert_array_equal(counts, [[0, 0, 7], [0, 2, 0], [5, 0, 0]])
    assert counts.sum() == 14
    with pytest.raises(ValueError, match="outside crop"):
        rasterize(np.array([[1, 1, 3]]), grid)


def test_overlap_is_deduplicated_without_changing_pixel_counts():
    first = {"pixels": np.array([[0, 0, 1], [0, 1, 10]])}
    second = {"pixels": np.array([[0, 1, 10], [1, 1, 2]])}
    np.testing.assert_array_equal(unique_pixels([first, second]), [[0, 0, 1], [0, 1, 10], [1, 1, 2]])
    with pytest.raises(ValueError, match="disagree"):
        unique_pixels([first, {"pixels": np.array([[0, 1, 11]])}])


def test_export_keeps_empty_intersections_and_generates_reproducible_images(tmp_path):
    crops = [{"intersection_id": 12, "radius_m": 2, "grid_bounds": [0, 0, 2, 2],
              "circle_pixels": {"cx": 1, "cy": 1, "r": 1},
              "pixels": np.array([[0, 0, 1], [1, 1, 300]])},
             {"intersection_id": 15, "radius_m": 2, "grid_bounds": [4, 4, 6, 6],
              "circle_pixels": {"cx": 1, "cy": 1, "r": 1},
              "pixels": np.empty((0, 3), dtype=np.int64)}]
    output = tmp_path / "export"
    manifest = write_dataset(output, crops, {"detect_id": "example"}, 2)
    assert manifest["intersection_count"] == 2
    assert manifest["image_count"] == 6
    assert manifest["empty_intersection_ids"] == [15]
    assert manifest["unique_raw_points"] == 301
    assert not (output / "INCOMPLETE").exists()
    profile = json.loads((output / "profile.json").read_text())
    for sample in manifest["samples"]:
        with np.load(output / sample["counts"], allow_pickle=False) as data:
            counts = data["counts"]
        assert counts.dtype == np.int64
        for method, path in sample["images"].items():
            with Image.open(output / path) as image:
                assert image.mode == "L"
                expected = np.rint(normalize(counts, profile, method, 99.5) * 255).astype(np.uint8)
                np.testing.assert_array_equal(np.asarray(image), expected)
    page = (output / "index.html").read_text()
    assert "__COMPARISON_DATA__" not in page
    assert "data:image/png;base64," in page
    before = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        write_dataset(output, crops, {}, 2)
    assert (output / "manifest.json").read_bytes() == before
