import json

import numpy as np
from PIL import Image
import pytest

from scripts.cv_human_check.export_intersections import crop_grid, rasterize, unique_pixels, write_dataset
from scripts.cv_human_check.preview import normalize


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
