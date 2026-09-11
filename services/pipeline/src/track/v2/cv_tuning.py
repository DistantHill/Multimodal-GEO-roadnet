"""Three-family parameter sweeps using saved raw counts, without querying a DB."""

from __future__ import annotations

import base64
import csv
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .cv_preview import PERCENTILES, normalize


TRAINING_METHODS = ("sqrt", "log1p", "asinh")
SCALE_CANDIDATES = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)


def render_comparison(config: dict) -> str:
    """Embed data safely in the portable comparison template."""
    template = Path(__file__).with_name("image_compare.html").read_text(encoding="utf-8")
    payload = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
    return template.replace("__COMPARISON_DATA__", payload)


def png_data(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def write_tuning_report(source: Path, output: Path) -> Path:
    """Preserve the original split/profile; sweep parameters only, never refit."""
    profile = json.loads((source / "profile.json").read_text())
    manifest = json.loads((source / "manifest.json").read_text())
    tiles = manifest["selected_tiles"]
    if not tiles:
        raise ValueError("source preview has no selected tiles")
    counts_list = []
    for index in range(len(tiles)):
        with np.load(source / f"tile-{index}-counts.npz", allow_pickle=False) as saved:
            counts = saved["counts"]
        if counts.ndim != 2 or not np.issubdtype(counts.dtype, np.integer) or np.any(counts < 0):
            raise ValueError("saved rasters must be nonnegative integer matrices")
        counts_list.append(counts)
    output.mkdir(parents=True, exist_ok=False)
    samples = []
    metrics = []
    for index, (info, counts) in enumerate(zip(tiles, counts_list, strict=True)):
        np.savez_compressed(output / f"tile-{index}-counts.npz", counts=counts)
        sample = {"id": f"tile-{index}", "title": f'{info["split"]} / {info["reason"]} · tile {info["tile"]}',
                  "subtitle": f'{info["points"]:,} 原始点 · {info["occupied_pixels"]:,} 非零像素 · 峰值 {info["peak_count"]}',
                  "width": counts.shape[1], "height": counts.shape[0], "circles": [], "variants": []}
        tx, ty = info["tile"]
        size, res = manifest["tile_size"], manifest["pixel_size_m"]
        for circle in manifest.get("rule_circles", []):
            cx, cy = circle["x"] / res - tx * size, size - (circle["y"] / res - ty * size)
            r = circle["radius_m"] / res
            if cx + r >= 0 and cx - r <= size and cy + r >= 0 and cy - r <= size:
                sample["circles"].append({"cx": cx, "cy": cy, "r": r})
        for method in TRAINING_METHODS:
            for percentile in PERCENTILES:
                for scale in (SCALE_CANDIDATES if method != "sqrt" else (None,)):
                    candidate = dict(profile)
                    if scale is not None:
                        candidate["log_scale" if method == "log1p" else "asinh_scale"] = scale
                    normalized = normalize(counts, candidate, method, percentile)
                    grey = np.rint(255 * normalized).astype(np.uint8)
                    reference = normalize(np.array([1, 2, 3, 10]), candidate, method, percentile)
                    image = Image.fromarray(grey)
                    suffix = "" if scale is None else f"-s{scale:g}"
                    image.save(output / f"tile-{index}-{method}-p{percentile}{suffix}.png")
                    q = candidate["quantiles"][str(percentile)]
                    lost = int(np.count_nonzero((counts > 0) & (grey == 0)))
                    clipped = int(np.count_nonzero(counts > q))
                    when = {"percentile": str(percentile)}
                    if scale is not None:
                        when["log_scale" if method == "log1p" else "asinh_scale"] = f"{scale:g}"
                    note = (f'Q={q:g}' + (f' · s={scale:g}' if scale is not None else '')
                            + '\n灰度 [1,2,3,10 点] = ' + ', '.join(f'{v:.3f}' for v in reference)
                            + f'\n量化丢点像素 {lost} · 截断像素 {clipped}')
                    sample["variants"].append({"column": method, "when": when, "image": png_data(image), "note": note})
                    metrics.append({"tile": index, "split": info["split"], "method": method,
                                    "percentile": percentile, "q": q, "scale": scale,
                                    "gray_1": float(reference[0]), "gray_2": float(reference[1]),
                                    "gray_3": float(reference[2]), "gray_10": float(reference[3]),
                                    "quantized_to_zero": lost, "clipped_pixels": clipped})
        samples.append(sample)
    search = {
        "methods": list(TRAINING_METHODS), "percentiles": list(PERCENTILES),
        "log_scales": list(SCALE_CANDIDATES), "asinh_scales": list(SCALE_CANDIDATES),
        "priority_scales": [0.5, 1.0, 2.0], "initial_percentile": 99.5,
        "status": "candidate preview, not tuned; no models trained",
        "default_display": {"percentile": "99.5", "log_scale": "1", "asinh_scale": "1"},
        "final_parameters": None,
    }
    config = {
        "title": "轨迹灰度 · sqrt / log1p / asinh 调参对比",
        "description": [
            "仅使用保存的原始计数，不重新扫描数据库、不重新拟合标尺。规则圆只作参考叠加，不进入输入图片。",
            "切换参数即时比较同一窗口。sqrt 是固定曲线基线；log1p/asinh 的 s 越小，低计数越亮。",
            "先固定 P99.5，重点比较 s=0.5、1、2；0.1、0.25、5、10 用于观察两端。显示默认值不是已验证的最佳值。",
            "这是调参预览，不是完整训练数据集。正式选择要看人工复核区域的召回/FP；本次看过的 preview 区域不能再当最终测试集。",
        ],
        "summary": [f'Q(P99.5) = {profile["quantiles"]["99.5"]:g} 点/像素',
                    f'{len(samples)} 个窗口', '3 个候选家族', '每窗 45 张参数图片'],
        "axes": [
            {"id": "percentile", "label": "截断分位数", "values": [str(p) for p in PERCENTILES], "default": "99.5"},
            {"id": "log_scale", "label": "log1p · s", "values": [f"{s:g}" for s in SCALE_CANDIDATES], "default": "1"},
            {"id": "asinh_scale", "label": "asinh · s", "values": [f"{s:g}" for s in SCALE_CANDIDATES], "default": "1"},
        ],
        "columns": [{"id": method, "title": method} for method in TRAINING_METHODS],
        "overlay_label": "规则圆（伪标签）", "samples": samples,
        "metadata": {"source_preview": str(source), "source_manifest": manifest,
                     "frozen_profile": profile, "search": search},
    }
    (output / "index.html").write_text(render_comparison(config), encoding="utf-8")
    (output / "comparison.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    (output / "training-candidates.json").write_text(json.dumps(search, indent=2), encoding="utf-8")
    (output / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with (output / "sweep-metrics.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(metrics[0]))
        writer.writeheader()
        writer.writerows(metrics)
    return output / "index.html"
