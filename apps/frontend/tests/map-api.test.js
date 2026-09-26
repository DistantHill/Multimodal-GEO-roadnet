import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { planViewTiles } from '../src/map-tiles.js';

import {
  listMapLayers,
  listRuns,
  loadMapLayer,
  MapApiError,
  requestClip,
  requestReplay,
} from '../src/map-api.js';

const originalFetch = globalThis.fetch;

test('Web Mercator half-world northing converts to the known latitude limit', () => {
  const halfWorld = 20_037_508.342789244;
  const north = planViewTiles([0, halfWorld - 1, 1, halfWorld], 4);
  const south = planViewTiles([0, -halfWorld, 1, -halfWorld + 1], 4);
  assert.ok(Math.abs(north.viewBbox4326[3] - 85.0511287798066) < 1e-10);
  assert.ok(Math.abs(south.viewBbox4326[1] + 85.0511287798066) < 1e-10);
  assert.equal(north.tiles.length, 0);
});

test('sample XYZ tile and current view AABB use the correct Mercator latitude', () => {
  const halfWorld = 20_037_508.342789244;
  const width = (halfWorld * 2) / (2 ** 14);
  const west = 13178 * width - halfWorld;
  const north = halfWorld - 6493 * width;
  const extent = [west, north - width, west + width, north];
  const expected = [109.5556640625, 34.92197103616376, 109.57763671875, 34.93998515156042];
  const plan = planViewTiles(extent, 14);
  const tile = plan.tiles.find((item) => item.key === '14/13178/6493');
  assert.ok(tile);
  for (let index = 0; index < 4; index += 1) {
    assert.ok(Math.abs(tile.bbox4326[index] - expected[index]) < 1e-9);
    assert.ok(Math.abs(plan.viewBbox4326[index] - expected[index]) < 1e-9);
  }
});

test('Replay exposes database resource exhaustion message', async () => {
  const message = 'PostgreSQL 共享内存空间不足（SQLSTATE 53100）';
  globalThis.fetch = async () => jsonResponse({ detail: {
    error: 'database_resource_exhausted', sqlstate: '53100', message,
  } }, 503);
  await assert.rejects(requestReplay({ sourceRowKeys: ['1'] }),
    (error) => error.message === message && error.details.detail.sqlstate === '53100');
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

test('database selection propagates to catalog, layers and analysis', async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), body: options.body && JSON.parse(options.body) });
    return jsonResponse({ type: 'FeatureCollection', features: [], layers: [] });
  };
  await listMapLayers({ databaseName: 'test' });
  await loadMapLayer('intersections', { databaseName: 'test', refineId: 'refine-1', detectId: 'detect-1', fitId: 'fit-1' });
  await requestReplay({ databaseName: 'test', sourceRowKeys: ['1'] });
  await requestClip({ databaseName: 'test', detectId: 'run', intersectionIds: [1, 2] });
  assert.match(calls[0].url, /database_name=test/);
  assert.match(calls[1].url, /database_name=test/);
  assert.match(calls[1].url, /refine_id=refine-1/);
  assert.match(calls[1].url, /detect_id=detect-1/);
  assert.match(calls[1].url, /fit_id=fit-1/);
  assert.equal(calls[2].body.database_name, 'test');
  assert.equal(calls[3].body.database_name, 'test');
});

test('layer catalog keeps the backend default context', async () => {
  globalThis.fetch = async () => jsonResponse({
    default_context: { detect_id: 41, fit_id: 9 },
    layers: [{ id: 'resolved_subtrack_points', title: '轨迹点', geometry_type: 'Point' }],
  });

  const catalog = await listMapLayers();

  assert.equal(catalog.layers[0].id, 'resolved_subtrack_points');
  assert.equal(catalog.layers[0].role, 'trajectory');
  assert.deepEqual(catalog.metadata.context, { detect_id: 41, fit_id: 9 });
  assert.equal(catalog.metadata.detect_id, 41);
});

test('layer request uses the configured backend page size by default', async () => {
  let requestedUrl;
  globalThis.fetch = async (url) => {
    requestedUrl = new URL(url, 'http://frontend.test');
    return jsonResponse({ type: 'FeatureCollection', features: [] });
  };

  await loadMapLayer('trajectory points', { detectId: 8, fitId: 3 });

  assert.equal(requestedUrl.pathname, '/api/v1/map/layers/trajectory%20points');
  assert.equal(requestedUrl.searchParams.has('limit'), false);
  assert.equal(requestedUrl.searchParams.get('detect_id'), '8');
  assert.equal(requestedUrl.searchParams.get('fit_id'), '3');
});

test('replay separates source keys from resolved point IDs', async () => {
  const bodies = [];
  globalThis.fetch = async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return jsonResponse({ data: { type: 'FeatureCollection', features: [] }, meta: { detect_id: 7 } });
  };

  await requestReplay({ sourceRowKeys: ['external-101', 102] });
  await requestReplay({ detectId: 7, resolvedPointIds: [201, 202] });
  await requestReplay({ intersectionId: 12 });
  await assert.rejects(requestReplay({ bbox: [120.1, 30.2, 120.3, 30.4] }), /不支持 bbox/);

  assert.deepEqual(bodies[0], { source_row_keys: ['external-101', '102'] });
  assert.deepEqual(bodies[1], { detect_id: 7, resolved_point_ids: [201, 202] });
  assert.deepEqual(bodies[2], { intersection_id: 12 });
});

test('resolved point replay requires detect context', async () => {
  await assert.rejects(
    requestReplay({ resolvedPointIds: [201] }),
    (error) => error instanceof MapApiError && /detect_id/.test(error.message),
  );
});

test('clip requires exactly two intersections and detect context', async () => {
  await assert.rejects(
    requestClip({ detectId: 7, intersectionIds: [1] }),
    (error) => error instanceof MapApiError && /两个交叉口/.test(error.message),
  );
  await assert.rejects(
    requestClip({ intersectionIds: [1, 2] }),
    (error) => error instanceof MapApiError && /detect_id/.test(error.message),
  );

  let body;
  globalThis.fetch = async (_url, options) => {
    body = JSON.parse(options.body);
    return jsonResponse({ type: 'FeatureCollection', features: [] });
  };
  await requestClip({ detectId: 7, intersectionIds: [101, 202] });
  assert.deepEqual(body, { detect_id: 7, intersection_ids: [101, 202] });
});

test('run catalog preserves UUID relationships and empty arrays after rebuild', async () => {
  const refineId = '12345678-1234-5678-1234-567812345670';
  const detectId = '12345678-1234-5678-1234-567812345671';
  const payload = {
    refine: [{ id: refineId, started_at: '2026-09-13T00:00:00Z' }],
    detect: [{ id: detectId, refine_id: refineId, started_at: '2026-09-13T00:01:00Z' }],
    fit: [],
  };
  globalThis.fetch = async () => jsonResponse(payload);
  assert.deepEqual(await listRuns(), payload);
  globalThis.fetch = async () => jsonResponse({ refine: [], detect: [], fit: [] });
  assert.deepEqual(await listRuns(), { refine: [], detect: [], fit: [] });
});

test('layer keeps nullable point properties and completion metadata unchanged', async () => {
  const detectId = '12345678-1234-5678-1234-567812345671';
  const feature = {
    type: 'Feature', id: 41,
    geometry: { type: 'Point', coordinates: [120.1, 30.2] },
    properties: {
      detect_id: detectId, vehicle_id: 'vehicle-1', subtrack_index: 2,
      resolved_point_id: 41, source_row_key: null, point_kind: 'i',
      crossing_id: null, intersection_id: null, crossing_type: null,
    },
  };
  globalThis.fetch = async () => jsonResponse({
    type: 'FeatureCollection', features: [feature],
    metadata: { detect_id: detectId, count: 1, truncated: false, bbox: null, crs: 'EPSG:4326' },
  });
  const result = await loadMapLayer('resolved_subtrack_points', { detectId });
  assert.deepEqual(result.featureCollection.features, [feature]);
  assert.equal(result.metadata.detect_id, detectId);
  assert.equal(result.metadata.count, 1);
  assert.equal(result.metadata.truncated, false);
  assert.equal(result.metadata.bbox, null);
  assert.equal(result.metadata.crs, 'EPSG:4326');
});

test('layers preserve entry-exit and centerline feature collections unchanged', async () => {
  const entryExit = {
    type: 'Feature', id: 7, geometry: { type: 'Point', coordinates: [120.1, 30.2] },
    properties: {
      detect_id: 'detect-1', crossing_id: 7, intersection_id: 8,
      resolved_point_id: 9, source_row_key: null, subtrack_index: 10,
      crossing_type: 1, crossing_kind: 'entry',
    },
  };
  const centerline = {
    type: 'Feature', id: 81,
    geometry: { type: 'LineString', coordinates: [[120.1, 30.2], [120.2, 30.3]] },
    properties: {
      detect_id: 'detect-1', variant: 'directed', from_intersection_id: 11,
      to_intersection_id: 12, length_m: 123.5, forward_clip_count: 8,
      reverse_clip_count: 3,
    },
  };
  const payloads = [
    { type: 'FeatureCollection', features: [entryExit], metadata: { detect_id: 'detect-1', count: 1 } },
    { type: 'FeatureCollection', features: [centerline], metadata: { detect_id: 'detect-1', fit_id: 'fit-1', count: 1 } },
  ];
  globalThis.fetch = async () => jsonResponse(payloads.shift());

  const entryResult = await loadMapLayer('entry_exit_points', { detectId: 'detect-1' });
  const centerlineResult = await loadMapLayer('centerlines', { detectId: 'detect-1', fitId: 'fit-1' });

  assert.deepEqual(entryResult, {
    featureCollection: { type: 'FeatureCollection', features: [entryExit] },
    metadata: {
      detect_id: 'detect-1',
      count: 1,
      context: { detect_id: 'detect-1' },
      refine_id: undefined,
      fit_id: undefined,
      has_more: false,
      truncated: false,
      next_cursor: null,
      loaded_page_count: 1,
    },
  });
  assert.deepEqual(centerlineResult, {
    featureCollection: { type: 'FeatureCollection', features: [centerline] },
    metadata: {
      detect_id: 'detect-1',
      fit_id: 'fit-1',
      count: 1,
      context: { detect_id: 'detect-1', fit_id: 'fit-1' },
      refine_id: undefined,
      has_more: false,
      truncated: false,
      next_cursor: null,
      loaded_page_count: 1,
    },
  });
});
