import test from 'node:test';
import assert from 'node:assert/strict';
import { experimentRequest, parameterDifferences, comparisonWarnings, formatMetric, escapeHtml, caseBounds, initialEvaluationId, featureInspection, runtimeStatRows, candidateEfficiency } from '../src/experiment-api.js';

test('parameter comparisons show nested edits, removed values and arrays', () => {
  assert.deepEqual(parameterDifferences({ dbscan: { eps: 100 }, sources: ['turn'], old: true }, { dbscan: { eps: 80 }, sources: ['turn', 'orthogonal'] }), [
    { name: 'dbscan.eps', left: 100, right: 80 }, { name: 'old', left: true, right: undefined }, { name: 'sources', left: ['turn'], right: ['turn', 'orthogonal'] },
  ]);
});
test('comparison flags different inputs and truth rather than silently comparing metrics', () => {
  const warnings = comparisonWarnings({ case_name: 'a', case_snapshot: { bbox: [1, 2, 3, 4] } }, { case_name: 'a', case_snapshot: { bbox: [1, 2, 5, 6] } }, { truth_version: 'v1', rules: { mode: 'iou' } }, { truth_version: 'v2', rules: { mode: 'distance' } });
  assert.equal(warnings.length, 3);
});
test('undefined metrics remain unavailable and record text is safely escaped', () => {
  assert.equal(formatMetric(null), '不适用');
  assert.equal(formatMetric(0), '0');
  assert.equal(escapeHtml('<img src=x onerror="x">'), '&lt;img src=x onerror=&quot;x&quot;&gt;');
});
test('API sends bounded viewport and evaluation version; surfaces backend errors', async (t) => {
  let url;
  t.mock.method(globalThis, 'fetch', async (value) => {
    url = value;
    return { ok: true, json: async () => ({ features: [] }) };
  });
  await experimentRequest('/abc/map', { bbox: [1, 2, 3, 4], limit: 5000, evaluation_id: 'v1' });
  const query = new URL(url, 'http://localhost').searchParams;
  assert.equal(query.get('limit'), '5000');
  assert.equal(query.get('bbox'), '1,2,3,4');
  assert.equal(query.get('evaluation_id'), 'v1');
  globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ detail: '评价库不可用' }) });
  await assert.rejects(experimentRequest(), /评价库不可用/);
});

test('map fit derives bounds from archived polygon geometry without external file', () => {
  assert.deepEqual(caseBounds({ geometry: { type: 'MultiPolygon', coordinates: [[[[10, 20], [12, 21], [10, 20]]], [[[15, 25], [17, 28], [15, 25]]]] } }), [10, 20, 17, 28]);
  assert.equal(caseBounds({}), null);
});

test('historical evaluation selection never retains an ID absent from fetched history', () => {
  const history = [{ evaluation_id: 'old', metrics: { f1: .5 } }];
  assert.equal(initialEvaluationId({ latest_evaluation_id: 'stale' }, history), 'old');
  assert.equal(initialEvaluationId({ latest_evaluation_id: 'stale' }, []), null);
  assert.equal(initialEvaluationId({ latest_evaluation_id: 'new' }, [...history, { evaluation_id: 'new' }]), 'new');
});
test('feature inspection exposes match IDs and excludes OpenLayers geometry', () => {
  assert.deepEqual(featureInspection({ geometry: { huge: true }, layer: 'prediction', classification: 'TP', matched_id: 'truth-7' }, 'pred-3'), { id: 'pred-3', layer: 'prediction', classification: 'TP', matched_id: 'truth-7' });
  assert.equal(featureInspection({ id: 'explicit' }, 'fallback').id, 'explicit');
});

test('runtime statistics label process memory correctly and never fabricate missing counts', () => {
  const rows = runtimeStatRows({ elapsed_s: 10.25, process_peak_rss_bytes: 2 * 1024 ** 2 });
  assert.equal(rows[0].name, '运行耗时（秒）');
  assert.match(rows[1].name, /进程生命周期.*非单次实验/);
  assert.equal(rows[1].value, '2');
  assert.equal(rows.length, 2);
  assert.deepEqual(runtimeStatRows(), []);
});

test('candidate efficiency uses archived actual denominator and selected evaluation TP', () => {
  assert.equal(candidateEfficiency({ tp: 4 }, { candidate_point_count: 20000 }), 2);
  assert.equal(candidateEfficiency({ tp: 0 }, { candidate_point_count: 20000 }), 0);
  assert.equal(candidateEfficiency({ tp: 4 }, {}), null);
  assert.equal(candidateEfficiency({ tp: 4 }, { candidate_point_count: 0 }), null);
  assert.equal(candidateEfficiency(null, { candidate_point_count: 100 }), null);
});
