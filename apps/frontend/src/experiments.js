import Map from 'ol/Map.js';
import View from 'ol/View.js';
import TileLayer from 'ol/layer/Tile.js';
import VectorLayer from 'ol/layer/Vector.js';
import OSM from 'ol/source/OSM.js';
import VectorSource from 'ol/source/Vector.js';
import GeoJSON from 'ol/format/GeoJSON.js';
import Style from 'ol/style/Style.js';
import Fill from 'ol/style/Fill.js';
import Stroke from 'ol/style/Stroke.js';
import Circle from 'ol/style/Circle.js';
import { fromLonLat, transformExtent } from 'ol/proj.js';
import { experimentRequest, parameterDifferences, comparisonWarnings, formatMetric, caseBounds, initialEvaluationId, featureInspection, runtimeStatRows, candidateEfficiency, escapeHtml as e } from './experiment-api.js';

const app = document.querySelector('#app');
app.innerHTML = `<div class="experiment-shell">
<header class="app-header"><div class="brand-mark">R</div><div class="brand-copy"><strong>交叉口实验评价</strong><span>案例 · 参数 · 指标 · 地图</span></div><a class="experiment-back" href="#gis">返回 Web GIS</a></header>
<form id="experiment-filters" class="experiment-filters">
<label>案例<input name="case_name" placeholder="案例名称"></label><label>方案<input name="scheme_name" placeholder="参数方案"></label><label>Commit<input name="commit" placeholder="代码版本"></label>
<label>状态<select name="status"><option value="">全部</option><option value="completed">成功</option><option value="failed">失败</option><option value="running">运行中</option><option value="pending_archive">待存档</option></select></label>
<label>运行时间起<input type="datetime-local" name="started_after"></label><label>运行时间止<input type="datetime-local" name="started_before"></label><button type="submit">查询</button></form>
<div id="experiment-status" role="status" class="experiment-status">读取历史实验…</div>
<div class="experiment-body"><aside class="experiment-list"><div class="experiment-list-heading"><strong>历史实验</strong><span id="experiment-count"></span></div><p class="experiment-help">选择左侧结果，再添加另一条结果对比。启动实验请使用本地 TOML 与命令行。</p><div id="experiment-rows"></div><div class="experiment-pagination"><button id="experiment-prev">上一页</button><span id="experiment-page"></span><button id="experiment-next">下一页</button></div></aside>
<section class="experiment-results"><div class="experiment-comparison" id="experiment-comparison" hidden></div><div class="experiment-map-grid"><article class="experiment-slot"><div id="experiment-detail-left" class="experiment-detail">选择一个实验查看参数与结果</div><div id="experiment-map-left" class="experiment-map"></div><div id="experiment-map-note-left" class="experiment-map-note"></div><div id="experiment-feature-left" class="experiment-feature" hidden></div></article><article class="experiment-slot" id="experiment-right-slot" hidden><div id="experiment-detail-right" class="experiment-detail"></div><div id="experiment-map-right" class="experiment-map"></div><div id="experiment-map-note-right" class="experiment-map-note"></div><div id="experiment-feature-right" class="experiment-feature" hidden></div></article></div><div class="experiment-legend"><span class="legend-tp">● TP 正确检出</span><span class="legend-fp">● FP 误报</span><span class="legend-fn">● FN 漏检</span><span>◇ 灰色：未评价 / 真值　虚线：案例区域</span></div></section></div></div>`;
const view = new View({ center: fromLonLat([116.4, 39.9]), zoom: 11 });
const colors = { TP: '#218c62', FP: '#d86a37', FN: '#ba4392' };
const maps = {};
const records = {};
let offset = 0;
let total = 0;
let query = {};
let listController;
let mapTimer;
let detailSequence = { left: 0, right: 0 };
const states = { succeeded: '成功', completed: '成功', failed: '失败', running: '运行中', pending_archive: '待存档', archived: '已存档' };
const timestamp = (value) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
const json = (value) => e(JSON.stringify(value ?? {}, null, 2));
for (const side of ['left', 'right']) {
  const source = new VectorSource();
  const layer = new VectorLayer({ source, style: (feature) => {
    const color = colors[feature.get('classification')] || '#667989';
    const region = feature.get('layer') === 'region';
    const truth = feature.get('layer') === 'truth';
    return new Style({ stroke: new Stroke({ color, width: region ? 1.5 : 2, lineDash: region || truth ? [5, 4] : undefined }), fill: new Fill({ color: `${color}22` }), image: new Circle({ radius: 5, fill: new Fill({ color }), stroke: new Stroke({ color: '#fff', width: 1 }) }) });
  } });
  const map = new Map({ target: `experiment-map-${side}`, layers: [new TileLayer({ source: new OSM() }), layer], view });
  maps[side] = { map, source, controller: null };
  map.on('singleclick', (event) => {
    const features = map.getFeaturesAtPixel(event.pixel, { hitTolerance: 5, layerFilter: (candidate) => candidate === layer });
    const feature = features.find((candidate) => candidate.get('layer') !== 'region') || features[0];
    const panel = document.querySelector(`#experiment-feature-${side}`);
    panel.hidden = !feature;
    if (!feature) return;
    const fields = featureInspection(feature.getProperties(), feature.getId());
    panel.innerHTML = `<strong>选中要素 · ${e(fields.classification || '未评价')}</strong><dl>${Object.entries(fields).map(([key, value]) => `<div><dt>${e(key)}</dt><dd>${e(value !== null && typeof value === 'object' ? JSON.stringify(value) : value)}</dd></div>`).join('')}</dl>`;
  });
  map.on('moveend', () => { clearTimeout(mapTimer); mapTimer = setTimeout(loadMaps, 180); });
}
function status(message, error = false) {
  const element = document.querySelector('#experiment-status');
  element.textContent = message;
  element.classList.toggle('is-error', error);
}
function caseDescription(record) {
  const snapshot = record.case_snapshot || {};
  return `<span>数据时间：${e(snapshot.timestamp_range ? JSON.stringify(snapshot.timestamp_range) : `${snapshot.start_time || snapshot.time_start || '—'} → ${snapshot.end_time || snapshot.time_end || '—'}`)}</span><span>来源：${e(snapshot.source_table || '—')} · 区域：${e(snapshot.bbox ? JSON.stringify(snapshot.bbox) : snapshot.region_file || snapshot.geometry_file || '多边形')}</span>`;
}
async function loadList() {
  listController?.abort();
  listController = new AbortController();
  status('读取历史实验…');
  try {
    const payload = await experimentRequest('', { ...query, limit: 25, offset }, { signal: listController.signal });
    total = payload.total;
    document.querySelector('#experiment-count').textContent = `${total} 条`;
    document.querySelector('#experiment-rows').innerHTML = payload.items.length ? payload.items.map((record) => `<article class="experiment-row"><strong>${e(record.case_name)} / ${e(record.scheme_name)}</strong><span>${e(record.algorithm || 'Detect')} · ${e(record.commit?.slice(0, 10))} · ${e(states[record.status] || record.status)}</span>${caseDescription(record)}<span>执行：${e(timestamp(record.started_at))}</span><span>F1 ${e(record.metrics ? formatMetric(record.metrics.f1) : '未评价')} · ${e(record.experiment_id)}</span><div><button data-experiment="${e(record.experiment_id)}" data-side="left">查看</button><button data-experiment="${e(record.experiment_id)}" data-side="right">加入对比</button></div></article>`).join('') : '<p class="experiment-help">没有符合条件的实验。</p>';
    document.querySelector('#experiment-page').textContent = `${Math.floor(offset / 25) + 1} / ${Math.max(1, Math.ceil(total / 25))}`;
    document.querySelector('#experiment-prev').disabled = offset === 0;
    document.querySelector('#experiment-next').disabled = offset + 25 >= total;
    status('历史由评价存档库提供；地图仅加载当前视口内最多 5,000 个结果要素。');
  } catch (error) { if (error.name !== 'AbortError') status(error.message, true); }
}
async function selectRecord(id, side) {
  const sequence = ++detailSequence[side];
  status('读取实验详情…');
  try {
    const [record, history] = await Promise.all([experimentRequest(`/${encodeURIComponent(id)}`), experimentRequest(`/${encodeURIComponent(id)}/evaluations`)]);
    if (sequence !== detailSequence[side]) return;
    maps[side].controller?.abort();
    maps[side].source.clear();
    records[side] = { record, evaluations: history.items || [], evaluationId: initialEvaluationId(record, history.items || []) };
    document.querySelector('#experiment-right-slot').hidden = !records.right;
    renderDetail(side);
    renderComparison();
    Object.values(maps).forEach(({ map }) => map.updateSize());
    if (side === 'left' || !records.left) {
      const bbox = caseBounds(record.case_snapshot);
      if (Array.isArray(bbox) && bbox.length === 4) view.fit(transformExtent(bbox, 'EPSG:4326', 'EPSG:3857'), { size: maps[side].map.getSize(), padding: [30, 30, 30, 30], maxZoom: 17 });
    }
    await loadMap(side, true);
    status('已加载实验；可切换评价版本或选择另一条实验对比。');
  } catch (error) { status(error.message, true); }
}
function selectedEvaluation(side) {
  return records[side]?.evaluations.find((item) => item.evaluation_id === records[side].evaluationId);
}
function renderDetail(side) {
  const { record, evaluations, evaluationId } = records[side];
  const evaluation = selectedEvaluation(side);
  const metrics = evaluation?.metrics;
  document.querySelector(`#experiment-detail-${side}`).innerHTML = `<div class="experiment-detail-heading"><strong>${e(record.case_name)} / ${e(record.scheme_name)}</strong>${side === 'right' ? '<button id="experiment-remove-comparison">关闭对比</button>' : ''}</div><div class="experiment-context">${caseDescription(record)}<span>执行：${e(timestamp(record.started_at))} → ${e(timestamp(record.finished_at))}</span><span>算法 ${e(record.algorithm || 'Detect')} · Commit ${e(record.commit)} · 状态 ${e(states[record.status] || record.status)} · 存档 ${e(record.archive_status || '—')}</span><span>${record.cleaned ? '中间产物已清理，轨迹回放不可用；存档结果可查看。' : '中间产物未标记清理。'}</span>${record.error ? `<span class="is-error">${e(record.error)}</span>` : ''}</div><label>评价版本 <select data-evaluation-side="${side}">${evaluations.length ? evaluations.map((item) => `<option value="${e(item.evaluation_id)}" ${item.evaluation_id === evaluationId ? 'selected' : ''}>${e(timestamp(item.created_at))} · ${e(item.truth_version || item.evaluation_id)}</option>`).join('') : '<option value="">未评价</option>'}</select></label><div class="experiment-metrics">${metrics ? ['tp', 'fp', 'fn', 'precision', 'recall', 'f1'].map((key) => `<span><small>${key.toUpperCase()}</small><strong>${e(formatMetric(metrics[key]))}</strong></span>`).join('') : '<span>未评价：暂无真值或评价记录</span>'}</div><details><summary>运行统计</summary>${candidateEfficiency(metrics, record.stats) !== null ? `<p>每万候选点正确检出：${e(formatMetric(candidateEfficiency(metrics, record.stats)))}</p>` : ''}<dl class="experiment-statistics">${runtimeStatRows(record.stats).map((row) => `<div><dt>${e(row.name)}</dt><dd>${e(row.value)}</dd></div>`).join('') || '<div>暂无运行统计</div>'}</dl></details><details><summary>完整参数与评价规则</summary><pre>${json(record.parameters)}</pre><pre>${json(evaluation?.rules)}</pre></details>`;
}
function renderComparison() {
  const container = document.querySelector('#experiment-comparison');
  container.hidden = !records.left || !records.right;
  if (container.hidden) return;
  const differences = parameterDifferences(records.left.record.parameters, records.right.record.parameters);
  const warnings = comparisonWarnings(records.left.record, records.right.record, selectedEvaluation('left'), selectedEvaluation('right'));
  container.innerHTML = `<strong>参数对比 · 地图同步平移与缩放</strong>${warnings.map((warning) => `<p class="experiment-warning">${e(warning)}</p>`).join('')}<details open><summary>${differences.length} 项参数差异</summary>${differences.length ? `<table><thead><tr><th>参数</th><th>左侧</th><th>右侧</th></tr></thead><tbody>${differences.map((row) => `<tr><td>${e(row.name)}</td><td>${e(JSON.stringify(row.left) ?? '未设置')}</td><td>${e(JSON.stringify(row.right) ?? '未设置')}</td></tr>`).join('')}</tbody></table>` : '<p>参数相同。</p>'}</details>`;
}
async function loadMap(side, fit = false) {
  const current = records[side];
  if (!current) return;
  const state = maps[side];
  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  const note = document.querySelector(`#experiment-map-note-${side}`);
  note.textContent = '加载当前视口…';
  state.source.clear();
  document.querySelector(`#experiment-feature-${side}`).hidden = true;
  try {
    const bbox = transformExtent(view.calculateExtent(state.map.getSize()), 'EPSG:3857', 'EPSG:4326').map((value, index) => Math.max(index % 2 ? -90 : -180, Math.min(index % 2 ? 90 : 180, value)));
    const collection = await experimentRequest(`/${encodeURIComponent(current.record.experiment_id)}/map`, { bbox, limit: 5000, evaluation_id: current.evaluationId }, { signal: controller.signal });
    if (controller.signal.aborted) return;
    state.source.clear();
    state.source.addFeatures(new GeoJSON().readFeatures(collection, { featureProjection: 'EPSG:3857' }));
    const metadata = collection.metadata || {};
    note.textContent = `${collection.features.length} 个存档要素${metadata.truncated || metadata.has_more ? ' · 达到 5,000 要素上限，当前结果不完整；请放大地图'  : ''}`;
    if (fit && !caseBounds(current.record.case_snapshot) && collection.features.length && !records.right) view.fit(state.source.getExtent(), { size: state.map.getSize(), padding: [30, 30, 30, 30], maxZoom: 17 });
  } catch (error) { if (error.name !== 'AbortError') note.textContent = error.message; }
}
function loadMaps() { return Promise.all(['left', 'right'].map((side) => loadMap(side))); }
app.addEventListener('click', (event) => {
  const button = event.target.closest('[data-experiment]');
  if (button) void selectRecord(button.dataset.experiment, button.dataset.side);
  if (event.target.id === 'experiment-remove-comparison') {
    detailSequence.right += 1;
    delete records.right;
    maps.right.controller?.abort();
    maps.right.source.clear();
    document.querySelector('#experiment-right-slot').hidden = true;
    renderComparison();
    maps.left.map.updateSize();
  }
});
app.addEventListener('change', (event) => {
  const side = event.target.dataset.evaluationSide;
  if (!side) return;
  records[side].evaluationId = event.target.value || null;
  renderDetail(side);
  renderComparison();
  void loadMap(side);
});
document.querySelector('#experiment-filters').addEventListener('submit', (event) => {
  event.preventDefault();
  query = Object.fromEntries(new FormData(event.target));
  for (const field of ['started_after', 'started_before']) if (query[field]) query[field] = new Date(query[field]).toISOString();
  offset = 0;
  void loadList();
});
document.querySelector('#experiment-prev').addEventListener('click', () => { offset = Math.max(0, offset - 25); void loadList(); });
document.querySelector('#experiment-next').addEventListener('click', () => { offset += 25; void loadList(); });
void loadList();
