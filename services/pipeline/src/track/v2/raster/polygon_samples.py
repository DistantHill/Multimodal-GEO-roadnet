"""Publish one labelled raster sample per polygon, plus an inspectable gallery."""
from __future__ import annotations

from collections import Counter
import csv
import html
import math
import os
from pathlib import Path
import tempfile
import uuid

import numpy as np
from PIL import Image, ImageDraw
from pyproj import CRS, Transformer
from shapely.geometry import mapping
from shapely.ops import transform
from rasterio.features import rasterize
from rasterio.transform import from_origin

from ..time_selection import time_bounds
from ..db.source_table import validate_source_table
from .aggregate_point_counts import accumulate_raw_counts, allocate_counts, validate_count_conservation
from .artifacts import verify_geotiff, write_geotiff, write_json
from .grid import build_grid_spec
from .models import RasterConfig
from .polygon_inputs import read_polygon_inputs
from .polygon_query import iter_polygon_points, polygon_points_sql
from .read_refine_points import load_refine_spatial_context
from .render_counts_to_uint8 import fit_q95_profile, render_sqrt_uint8


def polygon_pixel_geometry(geometry, grid):
    return transform(lambda x, y, z=None: ((np.asarray(x) - grid.west) / grid.resolution_m,
                                         (grid.north - np.asarray(y)) / grid.resolution_m), geometry)


def write_sample(directory: Path, sample: dict, projected, grid, batches,
                 config: RasterConfig) -> dict:
    directory.mkdir(parents=True)
    counts = allocate_counts(grid, max_cells=config.max_cells,
                             memory_budget_bytes=config.memory_budget_bytes,
                             tile_size_px=config.tile_size_px)
    kinds = Counter({'kept': 0, 'injected': 0})
    for batch in batches:
        accepted, excluded = accumulate_raw_counts(counts, grid, np.asarray(
            [(row[0], row[1]) for row in batch], dtype=np.float64))
        if excluded or accepted != len(batch):
            raise ValueError('selected point outside polygon raster grid or nonfinite coordinate')
        kinds.update(row[2] for row in batch)
    total = sum(kinds.values())
    validate_count_conservation(counts, total)
    profile = fit_q95_profile(counts)
    gray = render_sqrt_uint8(counts, profile['q95'])
    for name, array in [('raw_count.tif', counts), ('gray_uint8.tif', gray)]:
        write_geotiff(directory / name, array, grid, block_size_px=config.block_size_px)
        verify_geotiff(directory / name, array, grid, block_size_px=config.block_size_px)
    Image.fromarray(gray).save(directory / 'gray.png')
    mask = rasterize([(mapping(projected), 255)], out_shape=gray.shape,
                     transform=from_origin(grid.west, grid.north, grid.resolution_m, grid.resolution_m),
                     fill=0, all_touched=False, dtype='uint8')
    Image.fromarray(mask).save(directory / 'polygon_mask.png')
    pixel_geometry = polygon_pixel_geometry(projected, grid)
    annotation = {key: value for key, value in sample.items() if key != 'geometry'}
    annotation.update({'grid': grid.as_dict(), 'point_counts': dict(kinds),
                       'total_points': total, 'status': profile['status'],
                       'normalization': {**profile, 'scope': 'per_polygon_nonzero_cells'},
                       'geometry_wgs84': mapping(sample['geometry']),
                       'geometry_pixel': mapping(pixel_geometry),
                       'pixel_coordinates': 'pixel edges; origin upper left; x right; y down',
                       'image': 'gray.png'})
    write_json(directory / 'annotation.json', annotation)
    _write_preview(directory / 'annotated.png', gray, pixel_geometry, annotation)
    return {'sample_id': sample['sample_id'], 'label': sample['label'],
            'source_file': sample['source_file'], 'layer': sample['layer'], 'fid': sample['fid'],
            'width_px': grid.width_px, 'height_px': grid.height_px,
            'kept_points': kinds['kept'], 'injected_points': kinds['injected'],
            'total_points': total, 'q95': profile['q95'], 'status': profile['status'],
            'directory': f"samples/{sample['sample_id']}"}


def _write_preview(path: Path, gray, pixel_geometry, annotation):
    # Integer nearest-neighbour enlargement preserves the native 10 m pixels.
    scale = max(1, min(12, 320 // max(gray.shape)))
    raster = Image.fromarray(gray).convert('RGB').resize(
        (gray.shape[1] * scale, gray.shape[0] * scale), Image.Resampling.NEAREST)
    preview = Image.new('RGB', (max(360, raster.width + 24), raster.height + 100), '#171d26')
    xoff = (preview.width - raster.width) // 2
    preview.paste(raster, (xoff, 66))
    draw = ImageDraw.Draw(preview)
    color = '#52e3a1' if annotation['label'] == 'TP' else '#ff917b'
    draw.text((12, 10), f"{annotation['sample_id']} | {annotation['label']}", fill=color)
    draw.text((12, 28), f"FID {annotation['fid']} | {annotation['total_points']:,} points", fill='white')
    draw.text((12, 46), f"{gray.shape[1]} x {gray.shape[0]} px | {annotation['grid']['resolution_m']:g} m/px", fill='white')
    parts = list(pixel_geometry.geoms) if pixel_geometry.geom_type == 'MultiPolygon' else [pixel_geometry]
    for polygon in parts:
        for ring in [polygon.exterior, *polygon.interiors]:
            draw.line([(xoff + x * scale, 66 + y * scale) for x, y, *_ in ring.coords], fill=color, width=2)
    if annotation['status'] == 'empty':
        draw.text((12, preview.height - 22), 'EMPTY: no selected points', fill='#ffcf70')
    preview.save(path)


def _write_gallery(root: Path, records: list[dict], manifest: dict):
    cards = []
    for row in records:
        directory = row['directory']
        cards.append(f'<article data-label="{row["label"]}" data-empty="{row["status"] == "empty"}">'
                     f'<a href="{directory}/annotation.json"><img loading="lazy" src="{directory}/annotated.png"></a>'
                     f'<p>{html.escape(row["source_file"])} · FID {row["fid"]}</p>'
                     f'<a href="{directory}/gray.png">Gray PNG</a> · '
                     f'<a href="{directory}/gray_uint8.tif">GeoTIFF</a> · '
                     f'<a href="{directory}/annotation.json">Annotation</a></article>')
    page = '''<!doctype html><html lang="zh"><meta charset="utf-8"><title>Polygon raster samples</title>
<style>body{background:#10151c;color:#e4edf6;font:15px system-ui;margin:30px}h1{font-size:26px}
button,a{color:#77c6ff}button{background:#233040;border:0;padding:10px;margin:5px;cursor:pointer}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
article{background:#171d26;padding:12px;overflow:hidden}img{width:100%;height:350px;object-fit:contain}
p{overflow-wrap:anywhere}a{font-size:13px}</style><h1>Polygon raster · TP / FP</h1>'''
    page += f'<p>{len(records)} samples · {manifest["resolution_m"]:g} m/px · '
    page += html.escape(manifest['time_start'] + ' → ' + manifest['time_end']) + ' (end exclusive)</p>'
    page += '<p>Green: TP · Coral: FP. Boundaries appear only in previews. Gray PNGs retain native pixels.</p>'
    page += '<p>Kept + injected; buffered rectangular window; original polygon label; Q95 + sqrt per window.</p>'
    page += '<a href="samples.csv">CSV manifest</a> · <a href="run_manifest.json">Run metadata</a><p>'
    page += ''.join(f'<button onclick="filter(\'{value}\')">{value}</button>' for value in ['All', 'TP', 'FP', 'Empty'])
    page += '</p><main>' + ''.join(cards) + '</main>'
    page += '''<script>function filter(v){document.querySelectorAll('article').forEach(e=>{
e.hidden=!(v==='All'||e.dataset.label===v||(v==='Empty'&&e.dataset.empty==='True'))})}</script></html>'''
    (root / 'index.html').write_text(page, encoding='utf-8')
    for offset in range(0, len(records), 40):
        subset = records[offset:offset + 40]
        sheet = Image.new('RGB', (5 * 300, ((len(subset) + 4) // 5) * 320), '#10151c')
        for index, row in enumerate(subset):
            with Image.open(root / row['directory'] / 'annotated.png') as preview:
                preview.thumbnail((296, 316))
                sheet.paste(preview, ((index % 5) * 300, (index // 5) * 320))
        sheet.save(root / f'contact_sheet_{offset // 40 + 1:02d}.jpg', quality=92)


def run_polygon_samples(conn, *, refine_id: str, paths: list[Path], output_root: Path,
                        time_start: str, time_end: str, config: RasterConfig,
                        buffer_m: float = 300.0, label_override: str | None = None) -> Path:
    if not math.isfinite(buffer_m) or buffer_m < 0:
        raise ValueError('buffer_m must be finite and nonnegative')
    start, end = time_bounds(time_start, time_end)
    if start is None or end is None:
        raise ValueError('polygon samples require explicit time_start and time_end')
    source_table, context, fingerprint = load_refine_spatial_context(conn, refine_id)
    validate_source_table(conn, source_table)
    crs = CRS.from_epsg(context.processing_srid)
    if not crs.is_projected or any(axis.unit_conversion_factor != 1 for axis in crs.axis_info):
        raise ValueError('polygon raster requires a projected CRS with metre units')
    samples, sources = read_polygon_inputs(paths, label_override=label_override)
    project = Transformer.from_crs(4326, crs, always_xy=True).transform
    query = polygon_points_sql(source_table)
    root = output_root.resolve() / 'raster' / 'polygon-samples'
    root.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    final = root / run_id
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=root) as scratch:
        staging = Path(scratch)
        records, features = [], []
        for sample in samples:
            projected = transform(project, sample['geometry'])
            window = projected.buffer(buffer_m).envelope if buffer_m else projected.envelope
            sample = {**sample, 'buffer_m': buffer_m, 'input_bounds_m': list(window.bounds)}
            # Include east/south boundary points even on exact global grid lines.
            grid = build_grid_spec(context.processing_srid, config.resolution_m, window.bounds)
            params = {'refine_id': refine_id, 'srid': context.processing_srid,
                      'polygon': window.wkb, 'start': start.timestamp(), 'end': end.timestamp()}
            record = write_sample(staging / 'samples' / sample['sample_id'], sample, projected, grid,
                                  iter_polygon_points(conn, query, params, config.stream_batch_size), config)
            records.append(record)
            features.append({'type': 'Feature', 'id': sample['sample_id'],
                             'geometry': mapping(sample['geometry']), 'properties': record})
        with (staging / 'samples.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
        manifest = {'status': 'complete', 'run_id': run_id, 'refine_id': refine_id,
                    'source_table': source_table, 'source_fingerprint': fingerprint,
                    'processing_srid': context.processing_srid, 'sources': sources,
                    'time_start': start.isoformat(), 'time_end': end.isoformat(),
                    'time_interval': '[start,end)', 'point_input': 'kept UNION ALL injected; unsorted',
                    'buffer_m': buffer_m,
                    'spatial_selection': 'UTM ST_Covers(buffered polygon envelope, point); rectangle corners retained',
                    'mask_geometry': 'original unbuffered polygon; pixel-center coverage; holes excluded',
                    'resolution_m': config.resolution_m, 'sample_count': len(records),
                    'label_counts': dict(Counter(row['label'] for row in records)),
                    'empty_samples': sum(row['status'] == 'empty' for row in records),
                    'normalization_scope': 'per_polygon_nonzero_cells',
                    'semantic_config': config.semantic_dict(), 'execution_config': config.execution_dict(),
                    'overlap_policy': 'each feature retained; points may occur in multiple samples'}
        write_json(staging / 'annotations.geojson', {'type': 'FeatureCollection', 'features': features})
        write_json(staging / 'run_manifest.json', manifest)
        _write_gallery(staging, records, manifest)
        staging.chmod(0o755)
        os.rename(staging, final)
    return final
