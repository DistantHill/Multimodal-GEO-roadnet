import json
from zipfile import ZipFile

from PIL import Image
import pytest

from scripts.cv_human_check.export_coco import circle_bbox, export_coco


def test_bbox_clips_partial_circle_without_creating_a_mask():
    assert circle_bbox({"cx": 2, "cy": -1, "r": 3}, 10, 10) == [0, 0, 5, 2]
    assert circle_bbox({"cx": -10, "cy": 5, "r": 2}, 10, 10) is None


def test_coco_preserves_neighbor_and_empty_image_without_duplicating_variants(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    samples = []
    for image_id in (1, 2):
        filename = f"intersection-{image_id}.png"
        Image.new("L", (10, 10), 32).save(source / filename)
        samples.append({"intersection_id": image_id, "width": 10, "height": 10,
                        "images": {"sqrt": filename}, "geotransform": [0, 2, 0, 20, 0, -2],
                        "nearby_rule_circles": []})
    samples[0]["nearby_rule_circles"] = [
        {"intersection_id": 1, "cx": 5, "cy": 5, "r": 2},
        {"intersection_id": 8, "cx": 2, "cy": -1, "r": 3},
    ]
    (source / "manifest.json").write_text(json.dumps({"detect_id": "run", "processing_srid": 32649, "samples": samples}))
    (source / "profile.json").write_text("{}")
    output = tmp_path / "coco"
    report = export_coco(source, output)
    coco = json.loads((output / "annotations/instances_default.json").read_text())
    assert report["images"] == 2 and report["annotations"] == 2
    assert {image["id"] for image in coco["images"]} == {1, 2}
    assert {annotation["image_id"] for annotation in coco["annotations"]} == {1}
    assert coco["annotations"][1]["bbox"] == [0, 0, 5, 2]
    assert all(annotation["segmentation"] == [] for annotation in coco["annotations"])
    assert coco["annotations"][1]["attributes"]["source_intersection_id"] == "8"
    circles = json.loads((output / "metadata/circles.json").read_text())
    assert circles[1]["circle_pixels"] == {"cx": 2, "cy": -1, "r": 3}
    with ZipFile(output / "images.zip") as archive:
        assert set(archive.namelist()) == {"intersection-1.png", "intersection-2.png"}
        assert archive.testzip() is None
    with ZipFile(output / "coco.zip") as archive:
        assert "annotations/instances_default.json" in archive.namelist()
        assert "images/default/intersection-1.png" in archive.namelist()
        assert archive.testzip() is None
    assert not (output / "INCOMPLETE").exists()
    with pytest.raises(FileExistsError):
        export_coco(source, output)
