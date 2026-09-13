"""Raw-point raster statistics and frozen grayscale mappings for CV previews."""

from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image


METHODS = ("linear", "sqrt", "log1p", "asinh", "cdf")
PERCENTILES = (99.0, 99.5, 99.9)


def count_quantile(values: np.ndarray, frequencies: np.ndarray, percentile: float) -> float:
    """NumPy-style linear percentile without expanding a count histogram."""
    if not 0 <= percentile <= 100 or not len(values):
        raise ValueError("a nonempty histogram and percentile in [0, 100] are required")
    cumulative = np.cumsum(frequencies, dtype=np.int64)
    rank = (int(cumulative[-1]) - 1) * percentile / 100
    lower, upper = int(np.floor(rank)), int(np.ceil(rank))
    lo = float(values[np.searchsorted(cumulative, lower, side="right")])
    hi = float(values[np.searchsorted(cumulative, upper, side="right")])
    return lo + (hi - lo) * (rank - lower)


def fit_profile(training_counts: np.ndarray, *, log_scale: float = 1.0,
                asinh_scale: float = 10.0) -> dict:
    """Fit on occupied training pixels only, never on preview/validation pixels."""
    counts = np.asarray(training_counts)
    if counts.size == 0 or not np.all(np.isfinite(counts)) or np.any(counts <= 0):
        raise ValueError("training counts must be finite positive integers")
    if np.any(counts != np.floor(counts)):
        raise ValueError("training counts must be integers")
    if not all(np.isfinite(s) and s > 0 for s in (log_scale, asinh_scale)):
        raise ValueError("curve scales must be finite and positive")
    values, frequencies = np.unique(counts, return_counts=True)
    return {
        "version": 1,
        "fit_scope": "occupied pixels in provisional training region only",
        "quantile_method": "linear over occupied pixel counts",
        "cdf_definition": "zero -> 0; positive c -> training P(C <= c | C > 0)",
        "cdf_values": values.tolist(),
        "cdf_frequencies": frequencies.tolist(),
        "log_scale": float(log_scale),
        "asinh_scale": float(asinh_scale),
        "quantiles": {str(p): count_quantile(values, frequencies, p)
                      for p in (50.0, 90.0, 95.0, *PERCENTILES, 100.0)},
        "occupied_pixels": int(counts.size),
        "raw_points": int(counts.sum()),
    }


def normalize(counts: np.ndarray, profile: dict, method: str,
              percentile: float = 99.5) -> np.ndarray:
    """Apply a frozen mapping, retaining zero background and float32 precision."""
    c = np.asarray(counts, dtype=np.float64)
    if not np.all(np.isfinite(c)) or np.any(c < 0):
        raise ValueError("counts must be finite and nonnegative")
    if method == "cdf":
        values = np.asarray(profile["cdf_values"])
        frequencies = np.asarray(profile["cdf_frequencies"], dtype=np.float64)
        cdf = np.concatenate(([0.0], np.cumsum(frequencies) / frequencies.sum()))
        result = cdf[np.searchsorted(values, c, side="right")]
        result = np.where(c == 0, 0.0, result)
    else:
        q = float(profile["quantiles"][str(float(percentile))])
        x = np.minimum(c, q)
        if method == "linear":
            result = x / q
        elif method == "sqrt":
            result = np.sqrt(x / q)
        elif method == "log1p":
            s = profile["log_scale"]
            result = np.log1p(x / s) / np.log1p(q / s)
        elif method == "asinh":
            s = profile["asinh_scale"]
            result = np.arcsinh(x / s) / np.arcsinh(q / s)
        else:
            raise ValueError(f"unknown mapping: {method}")
    return result.astype(np.float32)


def split_tiles(tile_x: np.ndarray, split_tile: int) -> np.ndarray:
    """Whole tiles only: west=train, two central columns=buffer, east=preview."""
    return np.where(tile_x < split_tile - 1, "train",
                    np.where(tile_x >= split_tile + 1, "preview", "buffer"))


def raster_tile(pixels: np.ndarray, tile: tuple[int, int], size: int) -> np.ndarray:
    """Rasterize aggregated global pixel indices; north is up, each bin is half-open."""
    tx, ty = tile
    selected = pixels[(pixels[:, 0] // size == tx) & (pixels[:, 1] // size == ty)]
    result = np.zeros((size, size), dtype=np.int64)
    np.add.at(result, (size - 1 - selected[:, 1] % size, selected[:, 0] % size),
              selected[:, 2])
    return result


def select_tiles(pixels: np.ndarray, size: int, split_tile: int,
                 circles: list[dict]) -> list[dict]:
    """Choose reproducible contrasting examples; labels are only preview context."""
    coords, inverse = np.unique(pixels[:, :2] // size, axis=0, return_inverse=True)
    occupied = np.bincount(inverse)
    totals = np.zeros(len(coords), dtype=np.int64)
    peaks = np.zeros(len(coords), dtype=np.int64)
    np.add.at(totals, inverse, pixels[:, 2])
    np.maximum.at(peaks, inverse, pixels[:, 2])
    roles = split_tiles(coords[:, 0], split_tile)
    lookup = {tuple(c): i for i, c in enumerate(coords)}
    rule_ids = {lookup[tuple(c["tile"])] for c in circles if tuple(c["tile"]) in lookup}
    result = []
    for role in ("train", "preview"):
        available = np.flatnonzero(roles == role).tolist()
        if not available:
            raise ValueError(f"no occupied tiles in {role}; choose another split")
        chosen: set[int] = set()
        for reason, metric in (("dense", totals), ("hotspot", peaks),
                               ("rule_intersection", totals), ("sparse", occupied)):
            pool = [i for i in available if i not in chosen]
            if reason == "rule_intersection":
                pool = [i for i in pool if i in rule_ids]
            if reason == "sparse":
                # Avoid choosing a completely uninformative single-point tile.
                pool = [i for i in pool if occupied[i] >= 20]
            if not pool:
                continue
            ranked = sorted(pool, key=lambda i: (int(metric[i]), i))
            index = ranked[len(ranked) // 4] if reason == "sparse" else ranked[-1]
            chosen.add(index)
            result.append({"tile": coords[index].tolist(), "split": role,
                           "reason": reason, "points": int(totals[index]),
                           "occupied_pixels": int(occupied[index]),
                           "peak_count": int(peaks[index])})
    return result


def write_report(output: Path, pixels: np.ndarray, profile: dict, metadata: dict,
                 circles: list[dict]) -> Path:
    """Write reproducible counts and a standalone HTML comparison (no CDN)."""
    size = metadata["tile_size"]
    metres = metadata["pixel_size_m"]
    selected = select_tiles(pixels, size, metadata["split_tile_x"], circles)
    output.mkdir(parents=True, exist_ok=False)
    metadata["selected_tiles"] = selected
    panels = []
    for number, tile_info in enumerate(selected):
        tx, ty = tile_info["tile"]
        counts = raster_tile(pixels, (tx, ty), size)
        np.savez_compressed(output / f"tile-{number}-counts.npz", counts=counts)
        tile_info["bounds_m"] = [tx * size * metres, ty * size * metres,
                                  (tx + 1) * size * metres, (ty + 1) * size * metres]
        overlays = []
        for circle in circles:
            cx = circle["x"] / metres - tx * size
            cy = size - (circle["y"] / metres - ty * size)
            r = circle["radius_m"] / metres
            if cx + r >= 0 and cx - r <= size and cy + r >= 0 and cy - r <= size:
                overlays.append(f'<circle cx="{cx}" cy="{cy}" r="{r}"/>')
        svg = (f'<svg class="overlay" viewBox="0 0 {size} {size}">'
               + "".join(overlays) + "</svg>")
        title = html.escape(f'{tile_info["split"]} / {tile_info["reason"]} · '
                            f'tile ({tx}, {ty}) · {tile_info["points"]:,} points')
        groups = []
        tile_info["mapping_stats"] = {}
        for p in PERCENTILES:
            cards = []
            for method in METHODS:
                normalized = normalize(counts, profile, method, p)
                grey = np.rint(255 * normalized).astype(np.uint8)
                image = Image.fromarray(grey)
                filename = f"tile-{number}-{method}-p{p}.png"
                image.save(output / filename)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                positive = counts > 0
                lost = int(np.count_nonzero(positive & (grey == 0)))
                q = profile["quantiles"][str(p)]
                clipped = int(np.count_nonzero(counts > q)) if method != "cdf" else 0
                tile_info["mapping_stats"][f"{method}-p{p}"] = {
                    "positive_pixels_quantized_to_zero": lost,
                    "pixels_above_clip_count": clipped,
                    "white_pixels": int(np.count_nonzero(grey == 255)),
                }
                cards.append(f'<figure><figcaption>{method}</figcaption><div class="raster">'
                             f'<img alt="{method}" src="data:image/png;base64,{encoded}">{svg}'
                             f'</div><small>量化丢点像素 {lost} · 截断像素 {clipped}</small></figure>')
            hidden = "" if p == 99.5 else " hidden"
            groups.append(f'<div class="methods" data-p="{p}"{hidden}>' + "".join(cards) + '</div>')
        occupancy = tile_info["occupied_pixels"] / size ** 2
        panels.append(f'<section><h2>{title}</h2><p>非零占比 {occupancy:.2%} · '
                      f'峰值 {tile_info["peak_count"]} 点/像素 · '
                      f'范围 {size * metres:g} m × {size * metres:g} m</p>'
                      + "".join(groups) + '</section>')
    summary = html.escape(json.dumps({**metadata, "profile": profile}, ensure_ascii=False, indent=2))
    quantiles = profile["quantiles"]
    cdf_at_one = float(normalize(np.array([1]), profile, "cdf")[0])
    statistics = (
        f'<p>训练区域：{profile["raw_points"]:,} 点 / {profile["occupied_pixels"]:,} 个非零像素。'
        f'P99 = {quantiles["99.0"]:g}，P99.5 = {quantiles["99.5"]:g}，'
        f'P99.9 = {quantiles["99.9"]:g}，最大值 = {quantiles["100.0"]:g} 点/像素。</p>'
        f'<p>log1p 尺度 s = {profile["log_scale"]:g}；asinh 尺度 s = {profile["asinh_scale"]:g}。'
        f'这些是预览起始参数，尚未调优。CDF 将 1 点像素映射至 {cdf_at_one:.3f}，'
        '请特别比较稀疏道路与孤立噪点的同时提亮。</p>'
    )
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>轨迹灰度归一化对比</title><style>
body{font:15px system-ui,sans-serif;background:#101418;color:#e5eaf0;margin:24px}
header{max-width:1100px}h1{font-size:26px}h2{font-size:18px}p,small{color:#b8c5cf}
nav{position:sticky;top:0;background:#19232c;padding:14px;z-index:2;display:flex;gap:22px;flex-wrap:wrap}
section{margin:30px 0} .methods{display:grid;grid-template-columns:repeat(5,minmax(var(--card-min,180px),1fr));gap:12px;overflow:auto}
[hidden]{display:none!important}figure{margin:0}figcaption{padding:8px;font-weight:600}
.raster{position:relative;background:black;line-height:0}.raster img{width:100%;image-rendering:pixelated}
.overlay{display:none;position:absolute;inset:0;width:100%;height:100%;fill:none;stroke:#ffd166;stroke-width:1}
body.show-rules .overlay{display:block}pre{white-space:pre-wrap;overflow-wrap:anywhere}
button,select{padding:5px;font:inherit}a{color:#8ecbff}
</style><header><h1>原始轨迹点 · 五组灰度归一化对比</h1>
<p>仅聚合原始落点：无连线、插值、平滑或去噪。灰度越亮，计数映射值越大。
五组共享原始计数和训练区域标尺；规则圆仅为可切换的参考叠加，不进入图片。</p>
<p>这是预览用空间划分，尚未经人工复核，也不是模型验证成绩。train 为西侧拟合区域，
preview 为东侧未参与拟合区域，中间留两列瓦片隔离带。正式训练需复核地理范围与标签。
预览为 8 位 PNG，量化丢点单独计数；浮点变换可由保存的原始计数与 profile 重建。</p>
</header><nav><label>截断分位数 <select id="percentile"><option>99.0</option>
<option selected>99.5</option><option>99.9</option></select></label>
<label><input type="checkbox" id="rules">显示规则圆（伪标签）</label>
<label>图像宽度 <select id="zoom"><option value="180px">自适应</option>
<option value="512px">512 px</option><option value="1024px">1024 px</option></select></label>
<span>CDF 使用非零训练计数的右连续经验分布，不随截断分位数变化。</span></nav>'''
    page += statistics + "".join(panels)
    page += '<details><summary>来源、参数与统计</summary><pre>' + summary + '</pre></details>'
    page += '''<script>
document.getElementById('percentile').addEventListener('change',e=>{
  document.querySelectorAll('[data-p]').forEach(x=>x.hidden=x.dataset.p!==e.target.value);
});
document.getElementById('rules').addEventListener('change',e=>{
  document.body.classList.toggle('show-rules',e.target.checked);
});
document.getElementById('zoom').addEventListener('change',e=>{
  document.documentElement.style.setProperty('--card-min',e.target.value);
});
</script></html>'''
    (output / "index.html").write_text(page, encoding="utf-8")
    (output / "profile.json").write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    (output / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    histogram = "count,training_pixels\n" + "".join(
        f"{value},{frequency}\n" for value, frequency in
        zip(profile["cdf_values"], profile["cdf_frequencies"], strict=True))
    (output / "training-histogram.csv").write_text(histogram, encoding="utf-8")
    return output / "index.html"
