import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import {
  listMapLayers,
  loadMapLayer,
  MapApiError,
  requestClip,
  requestReplay,
} from '../src/map-api.js';

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

test('layer catalog keeps the backend default context', async () => {
  globalThis.fetch = async () => jsonResponse({
    default_context: { detect_id: 41, fit_id: 9 },
    layers: [{ id: 'trajectory_points', title: '轨迹点', geometry_type: 'Point' }],
  });

  const catalog = await listMapLayers();

  assert.equal(catalog.layers[0].id, 'trajectory_points');
  assert.equal(catalog.layers[0].role, 'trajectory');
  assert.deepEqual(catalog.metadata.context, { detect_id: 41, fit_id: 9 });
  assert.equal(catalog.metadata.detect_id, 41);
});

test('layer request defaults to the backend-safe 5000 feature limit', async () => {
  let requestedUrl;
  globalThis.fetch = async (url) => {
    requestedUrl = new URL(url, 'http://frontend.test');
    return jsonResponse({ type: 'FeatureCollection', features: [] });
  };

  await loadMapLayer('trajectory points', { detectId: 8, fitId: 3 });

  assert.equal(requestedUrl.pathname, '/api/v1/map/layers/trajectory%20points');
  assert.equal(requestedUrl.searchParams.get('limit'), '5000');
  assert.equal(requestedUrl.searchParams.get('detect_id'), '8');
  assert.equal(requestedUrl.searchParams.get('fit_id'), '3');
});

test('replay emits either raw point ids or a WGS84 bbox', async () => {
  const bodies = [];
  globalThis.fetch = async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    return jsonResponse({ data: { type: 'FeatureCollection', features: [] }, meta: { detect_id: 7 } });
  };

  await requestReplay({ detectId: 7, pointIds: [101, 102] });
  await requestReplay({ bbox: [120.1, 30.2, 120.3, 30.4] });

  assert.deepEqual(bodies[0], { detect_id: 7, point_ids: [101, 102] });
  assert.deepEqual(bodies[1], { bbox: [120.1, 30.2, 120.3, 30.4] });
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
