const ROOT = '/api/experiments';

export async function experimentRequest(path = '', parameters = {}, { signal } = {}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(parameters)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, Array.isArray(value) ? value.join(',') : String(value));
  }
  const response = await fetch(`${ROOT}${path}${query.size ? `?${query}` : ''}`, { signal });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof payload?.detail === 'string' ? payload.detail : `实验服务返回 HTTP ${response.status}`);
  return payload;
}

export function parameterDifferences(left, right, prefix = '') {
  const rows = [];
  for (const key of [...new Set([...Object.keys(left || {}), ...Object.keys(right || {})])].sort()) {
    const a = left?.[key];
    const b = right?.[key];
    const name = prefix ? `${prefix}.${key}` : key;
    if (a && b && typeof a === 'object' && typeof b === 'object' && !Array.isArray(a) && !Array.isArray(b)) {
      rows.push(...parameterDifferences(a, b, name));
    } else if (JSON.stringify(a) !== JSON.stringify(b)) rows.push({ name, left: a, right: b });
  }
  return rows;
}

export function comparisonWarnings(left, right, leftEvaluation, rightEvaluation) {
  const warnings = [];
  if (left?.case_name !== right?.case_name || JSON.stringify(left?.case_snapshot) !== JSON.stringify(right?.case_snapshot)) warnings.push('案例或输入范围不同，指标不可直接归因于算法参数。');
  if (leftEvaluation?.truth_version !== rightEvaluation?.truth_version) warnings.push('真值版本不同。');
  if (JSON.stringify(leftEvaluation?.rules) !== JSON.stringify(rightEvaluation?.rules)) warnings.push('评价规则不同。');
  return warnings;
}

export function formatMetric(value) {
  return value === null || value === undefined ? '不适用' : typeof value === 'number' ? Number(value.toFixed(4)).toString() : String(value);
}

export function escapeHtml(value) {
  return String(value ?? '—').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
}

export function caseBounds(snapshot = {}) {
  if (Array.isArray(snapshot.bbox) && snapshot.bbox.length === 4 && snapshot.bbox.every(Number.isFinite)) return snapshot.bbox;
  const points = [];
  function visit(coordinates) {
    if (!Array.isArray(coordinates)) return;
    if (coordinates.length >= 2 && Number.isFinite(coordinates[0]) && Number.isFinite(coordinates[1])) points.push(coordinates);
    else coordinates.forEach(visit);
  }
  visit(snapshot.geometry?.coordinates);
  if (!points.length) return null;
  return points.reduce((bounds, point) => [Math.min(bounds[0], point[0]), Math.min(bounds[1], point[1]), Math.max(bounds[2], point[0]), Math.max(bounds[3], point[1])], [Infinity, Infinity, -Infinity, -Infinity]);
}

export function initialEvaluationId(record, evaluations) {
  return evaluations.find((item) => item.evaluation_id === record.latest_evaluation_id)?.evaluation_id || evaluations[0]?.evaluation_id || null;
}

export function featureInspection(properties, featureId) {
  const { geometry, ...fields } = properties || {};
  return { id: fields.id ?? featureId ?? null, layer: fields.layer ?? null, classification: fields.classification ?? null, matched_id: fields.matched_id ?? null, ...fields };
}

export function runtimeStatRows(stats = {}) {
  const labels = {
    elapsed_s: '运行耗时（秒）', source_point_count: '输入轨迹点数',
    hotspot_event_count: 'turn 候选点数', cross_track_candidate_count: 'orthogonal 候选点数',
    rss_scope: '内存统计范围', tracks_cross_count: '跨轨迹候选持久化数', turn_event_count: '转向事件数', candidate_intersection_count: '候选交叉口数',
    candidate_point_count: '候选点总数', intersection_count: '检出交叉口数',
    process_peak_rss_bytes: '进程生命周期内存高水位（MiB，非单次实验峰值）',
  };
  return Object.entries(stats).map(([key, value]) => ({
    name: labels[key] || key,
    value: key === 'process_peak_rss_bytes' && typeof value === 'number' ? formatMetric(value / 1024 ** 2) : typeof value === 'object' && value !== null ? JSON.stringify(value) : formatMetric(value),
  }));
}

export function candidateEfficiency(metrics, stats) {
  const count = stats?.candidate_point_count;
  const tp = metrics?.tp;
  return Number.isFinite(count) && count > 0 && Number.isFinite(tp) && tp >= 0 ? tp / count * 10000 : null;
}
