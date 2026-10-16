from __future__ import annotations

import json
import os
import sqlite3
import struct

import numpy as np
from PIL import Image
import pytest
from shapely.geometry import MultiPolygon, Polygon, box

from track.v2.raster.grid import build_grid_spec
from track.v2.raster.models import RasterConfig
from track.v2.raster.polygon_inputs import read_polygon_inputs
from track.v2.raster.polygon_samples import write_sample
from track.v2.raster.polygon_query import iter_polygon_points, polygon_points_sql


def make_gpkg(path, geometries, srid=4326):
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE gpkg_geometry_columns(table_name, column_name, srs_id);
            CREATE TABLE gpkg_spatial_ref_sys(srs_id, organization, organization_coordsys_id, definition);
            CREATE TABLE polygons(fid INTEGER PRIMARY KEY, geom BLOB, type TEXT);
            INSERT INTO gpkg_geometry_columns VALUES ('polygons', 'geom', 4326);
            INSERT INTO gpkg_spatial_ref_sys VALUES (4326, 'EPSG', 4326, '');''')
        for fid, geom in enumerate(geometries, 1):
            blob = b'GP' + bytes([0, 1]) + struct.pack('<i', srid) + geom.wkb
            conn.execute('INSERT INTO polygons VALUES (?, ?, ?)', (fid, blob, 'crossing'))


def test_gpkg_retains_duplicate_multipart_holes_and_filename_class(tmp_path):
    hole = Polygon([(109, 34), (110, 34), (110, 35), (109, 35)],
                   [[(109.2, 34.2), (109.4, 34.2), (109.4, 34.4), (109.2, 34.4)]])
    geometry = MultiPolygon([hole, box(111, 34, 112, 35)])
    source = tmp_path / 'FP_example.gpkg'
    make_gpkg(source, [geometry, geometry])
    samples, sources = read_polygon_inputs([source])
    assert len(samples) == 2
    assert samples[0]['label'] == 'FP'
    assert samples[0]['geometry'].equals(geometry)
    assert samples[0]['properties']['type'] == 'crossing'
    assert samples[0]['sample_id'] != samples[1]['sample_id']
    assert [s['fid'] for s in samples] == [1, 2]
    assert len(sources[0]['sha256']) == 64


def test_gpkg_rejects_unlabelled_filename_and_invalid_geometry(tmp_path):
    source = tmp_path / 'unknown.gpkg'
    make_gpkg(source, [box(109, 34, 110, 35)])
    with pytest.raises(ValueError, match='TP_ or FP_'):
        read_polygon_inputs([source])
    invalid = tmp_path / 'TP_invalid.gpkg'
    make_gpkg(invalid, [Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])])
    with pytest.raises(ValueError, match='valid nonempty polygon'):
        read_polygon_inputs([invalid])


@pytest.mark.parametrize('empty', [False, True])
def test_sample_counts_boundary_pixels_labels_and_unchanged_gray(tmp_path, empty):
    geometry = box(0, 0, 20, 20)
    grid = build_grid_spec(32649, 10, geometry.bounds)
    # East/south boundary points remain included; input order is immaterial.
    points = [] if empty else [(20, 0, 'injected'), (0, 20, 'kept'), (0, 20, 'kept')]
    sample = {'sample_id': 'TP_00_00001', 'label': 'TP', 'source_file': 'TP_test.gpkg',
              'layer': 'test', 'fid': 1, 'properties': {}, 'geometry': geometry}
    config = RasterConfig(resolution_m=10, tile_size_px=16, overlap_px=0)
    first = write_sample(tmp_path / 'first', sample, geometry, grid, [points] if points else [], config)
    write_sample(tmp_path / 'second', sample, geometry, grid, [[p] for p in reversed(points)], config)
    assert first['total_points'] == len(points)
    assert first['injected_points'] == (0 if empty else 1)
    annotation = json.loads((tmp_path / 'first/annotation.json').read_text())
    assert annotation['grid']['resolution_m'] == 10
    assert annotation['geometry_pixel']['type'] == 'Polygon'
    with Image.open(tmp_path / 'first/gray.png') as a, Image.open(tmp_path / 'second/gray.png') as b:
        assert a.mode == 'L'
        assert a.size == (3, 3)
        np.testing.assert_array_equal(np.asarray(a), np.asarray(b))
        if empty:
            assert np.asarray(a).max() == 0
        else:
            assert np.asarray(a)[0, 0] == 255
            assert np.asarray(a)[2, 2] == 183


@pytest.mark.parametrize('arguments', [[], ['--time-start', '2023-03-01T00:00:00+08:00'],
    ['--time-start', '2023-03-01T00:00:00+08:00', '--time-end', '2023-03-07T00:00:00+08:00',
     '--bounds', '0', '0', '1', '1']])
def test_polygon_cli_rejects_missing_window_and_bounds(monkeypatch, arguments):
    import sys
    from track.v2.cli.rasterize import main
    monkeypatch.setattr(sys, 'argv', ['rasterize', '--refine-id', '6c9acb0c-f356-49ce-81d0-2dd7dad51e68',
                                    '--polygons', 'TP_test.gpkg', *arguments])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_batch_publication_and_failed_run_cleanup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from track.v2.raster import polygon_samples as module

    source = tmp_path / 'TP_test.gpkg'
    make_gpkg(source, [box(109, 34, 109.001, 34.001)])
    monkeypatch.setattr(module, 'load_refine_spatial_context', lambda *a:
                        ('source', SimpleNamespace(processing_srid=32649), None))
    monkeypatch.setattr(module, 'validate_source_table', lambda *a: None)
    monkeypatch.setattr(module, 'iter_polygon_points', lambda *a: iter([]))
    args = dict(refine_id='test', paths=[source], output_root=tmp_path,
                time_start='2023-03-01T00:00:00+08:00', time_end='2023-03-07T00:00:00+08:00',
                config=RasterConfig(resolution_m=10))
    output = module.run_polygon_samples(None, **args)
    manifest = json.loads((output / 'run_manifest.json').read_text())
    assert manifest['sample_count'] == manifest['empty_samples'] == 1
    assert output.stat().st_mode & 0o777 == 0o755
    assert (output / 'index.html').is_file()
    assert (output / 'contact_sheet_01.jpg').is_file()
    assert json.loads((output / 'annotations.geojson').read_text())['features'][0]['properties']['label'] == 'TP'

    def fail(*args):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(module, 'iter_polygon_points', fail)
    with pytest.raises(RuntimeError, match='database unavailable'):
        module.run_polygon_samples(None, **args)
    assert list(output.parent.iterdir()) == [output]


def test_buffer_window_keeps_rectangle_corners_and_original_mask(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from shapely import from_wkb
    from shapely.geometry import Point
    from track.v2.raster import polygon_samples as module

    source = tmp_path / 'TP_test.gpkg'
    make_gpkg(source, [box(109, 34, 109.001, 34.001)])
    monkeypatch.setattr(module, 'load_refine_spatial_context', lambda *a:
                        ('source', SimpleNamespace(processing_srid=32649), None))
    monkeypatch.setattr(module, 'validate_source_table', lambda *a: None)
    def points(conn, query, params, batch_size):
        window = from_wkb(params['polygon'])
        xmin, ymin, xmax, ymax = window.bounds
        assert window.equals(box(xmin, ymin, xmax, ymax))
        # This corner is inside the bbox, outside the rounded buffer and original polygon.
        yield [(xmin + 1, ymin + 1, 'kept'), (xmax - 1, ymax - 1, 'injected')]
    monkeypatch.setattr(module, 'iter_polygon_points', points)
    output = module.run_polygon_samples(None, refine_id='test', paths=[source], output_root=tmp_path,
                time_start='2023-03-01T00:00:00+08:00', time_end='2023-03-07T00:00:00+08:00',
                config=RasterConfig(resolution_m=10), buffer_m=300)
    directory = next((output / 'samples').iterdir())
    annotation = json.loads((directory / 'annotation.json').read_text())
    assert annotation['total_points'] == 2
    assert annotation['buffer_m'] == 300
    assert annotation['grid']['width_px'] > 60
    gray = np.asarray(Image.open(directory / 'gray.png'))
    mask = np.asarray(Image.open(directory / 'polygon_mask.png'))
    assert np.count_nonzero(gray) == 2
    assert mask.max() == 255
    assert mask[gray > 0].max() == 0


@pytest.mark.skipif(not os.getenv('ROADNET_TEST_DSN'), reason='ROADNET_TEST_DSN is not set')
def test_postgis_polygon_holes_time_bounds_run_isolation_and_union_all():
    import psycopg
    from pyproj import Transformer

    with psycopg.connect(os.environ['ROADNET_TEST_DSN']) as conn:
        # Transaction-local temporary tables shadow the production relations.
        conn.execute('''CREATE TEMP TABLE raster_source (id bigint PRIMARY KEY, timestamp double precision,
                        geom geometry(Point,4326));
                        CREATE TEMP TABLE kept_trajectory_points (refine_id text, source_row_key bigint);
                        CREATE TEMP TABLE injected_points (refine_id text, timestamp double precision,
                        x double precision, y double precision, geom geometry(Point,4326))''')
        geometry = Polygon([(109, 34), (109.02, 34), (109.02, 34.02), (109, 34.02)],
                           [[(109.008, 34.008), (109.012, 34.008),
                             (109.012, 34.012), (109.008, 34.012)]])
        points = [(109.002, 34.002, 10, 'selected'),  # inclusive start
                  (109.002, 34.002, 20, 'selected'),  # exclusive end
                  (109.01, 34.01, 15, 'selected'),    # hole
                  (109, 34, 15, 'selected'),          # boundary
                  (109.002, 34.002, 15, 'other'),      # other Run
                  (109.03, 34.03, 15, 'selected')]    # outside
        project = Transformer.from_crs(4326, 32649, always_xy=True)
        for index, (lon, lat, timestamp, refine) in enumerate(points):
            conn.execute('INSERT INTO raster_source VALUES (%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))',
                         (index, timestamp, lon, lat))
            conn.execute('INSERT INTO kept_trajectory_points VALUES (%s,%s)', (refine, index))
            conn.execute('INSERT INTO injected_points VALUES (%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))',
                         (refine, timestamp, *project.transform(lon, lat), lon, lat))
        from shapely.ops import transform
        geometry = transform(project.transform, geometry)
        params = {'refine_id': 'selected', 'polygon': geometry.wkb, 'srid': 32649, 'start': 10, 'end': 20}
        rows = [row for batch in iter_polygon_points(conn, polygon_points_sql('raster_source'), params, 1)
                for row in batch]
        assert len(rows) == 4
        assert sorted(row[2] for row in rows) == ['injected', 'injected', 'kept', 'kept']
        for kind in ['kept', 'injected']:
            xy = sorted(tuple(row[:2]) for row in rows if row[2] == kind)
            np.testing.assert_allclose(xy, sorted([project.transform(109.002, 34.002), project.transform(109, 34)]))
        conn.rollback()
