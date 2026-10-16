"""Read every polygon feature from labelled GeoPackages without deduplication."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from pyproj import CRS, Transformer
from shapely import from_wkb
from shapely.ops import transform


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def read_polygon_inputs(paths: list[Path], label_override: str | None = None) -> tuple[list[dict], list[dict]]:
    samples, sources = [], []
    for source_index, supplied in enumerate(paths):
        path = supplied.resolve(strict=True)
        label = label_override or path.name.split('_', 1)[0]
        if label not in {'TP', 'FP'}:
            raise ValueError(f"polygon filename must start with TP_ or FP_: {path.name}")
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as conn:
            layers = conn.execute("SELECT table_name, column_name, srs_id FROM gpkg_geometry_columns").fetchall()
            for table, geometry_column, srs_id in layers:
                crs_row = conn.execute(
                    "SELECT organization, organization_coordsys_id, definition FROM gpkg_spatial_ref_sys WHERE srs_id = ?",
                    (srs_id,),
                ).fetchone()
                crs = CRS.from_epsg(crs_row[1]) if crs_row[0].upper() == 'EPSG' else CRS.from_wkt(crs_row[2])
                project = Transformer.from_crs(crs, 4326, always_xy=True).transform
                columns = conn.execute(f"PRAGMA table_info({_identifier(table)})").fetchall()
                keys = [column[1] for column in columns if column[5]]
                if len(keys) != 1:
                    raise ValueError(f"GeoPackage layer needs one feature ID: {table}")
                rows = conn.execute(f"SELECT * FROM {_identifier(table)}")
                names = [description[0] for description in rows.description]
                for row in rows:
                    properties = dict(zip(names, row))
                    blob = properties.pop(geometry_column)
                    fid = properties[keys[0]]
                    if not blob or blob[:2] != b'GP' or blob[2] != 0 or blob[3] & 0x30:
                        raise ValueError(f"unsupported or empty GeoPackage geometry: {table}/{fid}")
                    envelope = (blob[3] >> 1) & 7
                    if envelope > 4:
                        raise ValueError('invalid GeoPackage envelope')
                    offset = 8 + (0, 32, 48, 48, 64)[envelope]
                    geometry = transform(project, from_wkb(blob[offset:]))
                    if geometry.geom_type not in {'Polygon', 'MultiPolygon'} or geometry.is_empty or not geometry.is_valid:
                        raise ValueError(f"expected a valid nonempty polygon: {table}/{fid}")
                    samples.append({'sample_id': f'{label}_{source_index:02d}_{len(samples):05d}',
                                    'label': label, 'source_file': path.name, 'layer': table,
                                    'fid': fid, 'properties': properties, 'geometry': geometry})
        sources.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'label': label})
    if not samples:
        raise ValueError('polygon inputs contain no features')
    return samples, sources
