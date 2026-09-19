"""Package extracted crops as COCO detection preannotations for CVAT Community."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image


def circle_bbox(circle: dict, width: int, height: int) -> list[float] | None:
    cx, cy, radius = (float(circle[key]) for key in ("cx", "cy", "r"))
    if not all(math.isfinite(v) for v in (cx, cy, radius)) or radius <= 0:
        raise ValueError("invalid source circle")
    left, top = max(0.0, cx - radius), max(0.0, cy - radius)
    right, bottom = min(float(width), cx + radius), min(float(height), cy + radius)
    if left >= right or top >= bottom:
        return None
    return [left, top, right - left, bottom - top]


def export_coco(source: Path, output: Path, method: str = "sqrt") -> dict:
    source = source.resolve()
    manifest = json.loads((source / "manifest.json").read_text())
    if (source / "INCOMPLETE").exists():
        raise ValueError("source export is incomplete")
    images, annotations, circles, files = [], [], [], []
    seen_ids, seen_names = set(), set()
    for sample in manifest["samples"]:
        image_id = int(sample["intersection_id"])
        path = (source / sample["images"][method]).resolve()
        if source not in path.parents:
            raise ValueError("image path escapes source dataset")
        if image_id in seen_ids or path.name in seen_names:
            raise ValueError("duplicate image ID or filename")
        seen_ids.add(image_id)
        seen_names.add(path.name)
        with Image.open(path) as image:
            if image.size != (sample["width"], sample["height"]):
                raise ValueError("image dimensions disagree with manifest")
            image.verify()
        images.append({"id": image_id, "file_name": path.name,
                       "width": sample["width"], "height": sample["height"]})
        files.append(path)
        # Include neighboring visible objects, not only the crop's central one.
        for circle in sample["nearby_rule_circles"]:
            bbox = circle_bbox(circle, sample["width"], sample["height"])
            if bbox is None:
                continue
            annotation_id = len(annotations) + 1
            annotations.append({"id": annotation_id, "image_id": image_id, "category_id": 1,
                                "bbox": bbox, "area": bbox[2] * bbox[3],
                                "iscrowd": 0, "segmentation": [],
                                "attributes": {"source_intersection_id": str(circle["intersection_id"]),
                                               "review_status": "unreviewed"}})
            circles.append({"source_annotation_id": annotation_id, "image_id": image_id,
                            "file_name": path.name, "source_intersection_id": circle["intersection_id"],
                            "circle_pixels": {key: circle[key] for key in ("cx", "cy", "r")},
                            "geotransform": sample["geotransform"],
                            "processing_srid": manifest["processing_srid"],
                            "status": "original_rule_circle_not_updated_by_bbox_edits"})
    if not images:
        raise ValueError("source has no images")
    coco = {"info": {"description": "Unreviewed intersection detection preannotations; no segmentation masks",
                     "version": "1.0", "date_created": datetime.now(timezone.utc).isoformat()},
            "licenses": [], "images": images, "annotations": annotations,
            "categories": [{"id": 1, "name": "intersection", "supercategory": "road"}]}
    output.mkdir(parents=True, exist_ok=False)
    (output / "INCOMPLETE").write_text("COCO package creation in progress\n")
    for directory in ("images/default", "annotations", "metadata"):
        (output / directory).mkdir(parents=True)
    for path in files:
        shutil.copy2(path, output / "images/default" / path.name)
    annotation_path = output / "annotations/instances_default.json"
    annotation_path.write_text(json.dumps(coco, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "metadata/circles.json").write_text(json.dumps(circles, indent=2), encoding="utf-8")
    shutil.copy2(source / "manifest.json", output / "metadata/source_manifest.json")
    shutil.copy2(source / "profile.json", output / "metadata/profile.json")
    with ZipFile(output / "images.zip", "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(output / "images/default" / path.name, path.name)
    with ZipFile(output / "coco.zip", "w", ZIP_DEFLATED) as archive:
        for directory in ("images", "annotations"):
            for path in sorted((output / directory).rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(output).as_posix())
    report = {"status": "preannotation_package_created_not_imported_into_cvat",
              "images": len(images), "annotations": len(annotations), "normalization": method,
              "source_dataset": str(source), "source_detect_id": manifest["detect_id"],
              "segmentation_masks": 0, "reviewed_images": 0,
              "spatial_split": "unassigned; same location and all normalization variants must stay together"}
    (output / "package.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (output / "README.md").write_text(
        "# CVAT Community / COCO 标注包\n\n"
        f"{len(images)} 张 {method} 灰度图，{len(annotations)} 个规则框预标注。包含可见相邻圆的外接框。\n"
        "这是已有交叉口裁剪的复核包，尚未增加区域窗口、背景样本或完成验证/测试划分。\n\n"
        "## 导入 CVAT\n\n"
        "1. 创建图片任务，标签命名为 `intersection`。\n"
        "2. 为该标签添加 `source_intersection_id` 文本属性（默认空）与 `review_status` 选择属性，\n"
        "   可选值为 `unreviewed`、`accepted`、`rejected`、`uncertain`，默认 `unreviewed`。\n"
        "3. 创建任务时上传 `images.zip`。\n"
        "4. 在该任务选择 Upload annotations → COCO 1.1，上传 `annotations/instances_default.json`。\n"
        "5. 复核、修改、补画或删除检测框；完成后导出 COCO 1.1 到新的 reviewed 版本。\n\n"
        "`coco.zip` 是标准目录布局的完整图片与标注包，供数据集导入器使用。\n"
        "当前未部署 CVAT，也未实测 CVAT 服务中的导入/导出往返。\n\n"
        "## 格式与标签语义\n\n"
        "`bbox` 为左上角 x、y、宽、高（像素），裁到图像边界；`area` 为当前框面积。\n"
        "`segmentation: []` 表示只有检测框，未生成分割标注；规则圆不作为真实掩膜。\n"
        "COCO 没有原生圆心/半径字段。`metadata/circles.json` 保留原始圆和地理变换，\n"
        "属于配套元数据，不是标准 COCO 字段，框编辑不会自动更新这里的圆。\n"
        "标注工具可能重新分配 ID；回传时按文件名与 `source_intersection_id` 关联，\n"
        "新增目标需分配新 ID。圆标签仍需独立复核，不能从任意修改的矩形推定圆。\n\n"
        "三组灰度图几何一致，本包只选一组做标注；复核结果可按同名图片复用于另两组。\n"
        "后续掩膜作为独立标签版本保存为 COCO polygon/RLE，必要时栅格化用于语义分割。\n"
        "无目标图片保留在 images 中且无对应 annotation；仅经人工检查后才能认作负样本。\n\n"
        "参考：[CVAT Community](https://github.com/cvat-ai/cvat)、"
        "[COCO 导入导出](https://docs.cvat.ai/docs/dataset_management/formats/format-coco/)。\n",
        encoding="utf-8")
    (output / "INCOMPLETE").unlink()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--method", choices=("sqrt", "log1p", "asinh"), default="sqrt")
    args = parser.parse_args()
    output = args.output.resolve()
    repository = Path(__file__).resolve().parents[2]
    data_root = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
    if output == repository or repository in output.parents or data_root not in output.parents:
        parser.error("output must be a new directory under DATA_ROOT, outside the repository")
    if output.exists():
        parser.error("output already exists")
    print(json.dumps(export_coco(args.source, output, args.method)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
