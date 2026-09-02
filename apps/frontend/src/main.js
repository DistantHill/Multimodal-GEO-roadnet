import './style.css';

import OlMap from 'ol/Map.js';
import View from 'ol/View.js';
import GeoJSON from 'ol/format/GeoJSON.js';
import DragBox from 'ol/interaction/DragBox.js';
import Select from 'ol/interaction/Select.js';
import { defaults as defaultInteractions } from 'ol/interaction/defaults.js';
import TileLayer from 'ol/layer/Tile.js';
import VectorLayer from 'ol/layer/Vector.js';
import { defaults as defaultControls } from 'ol/control/defaults.js';
import OSM from 'ol/source/OSM.js';
import VectorSource from 'ol/source/Vector.js';
import CircleStyle from 'ol/style/Circle.js';
import Fill from 'ol/style/Fill.js';
import Stroke from 'ol/style/Stroke.js';
import Style from 'ol/style/Style.js';
import { createEmpty, extend, isEmpty as isEmptyExtent } from 'ol/extent.js';
import { fromLonLat, getPointResolution, toLonLat, transformExtent } from 'ol/proj.js';
import {
  listMapLayers,
  loadMapLayer,
  requestClip,
  requestReplay,
} from './map-api.js';

const icon = (name, size = 18) => {
  const paths = {
    box: '<rect x="4" y="4" width="16" height="16" rx="1" stroke-dasharray="3 2"/>',
    chevron: '<path d="m8 10 4 4 4-4"/>',
    clear: '<path d="m7 7 10 10M17 7 7 17"/><circle cx="12" cy="12" r="9"/>',
    close: '<path d="m7 7 10 10M17 7 7 17"/>',
    collapse: '<path d="m8 14 4-4 4 4"/>',
    database: '<ellipse cx="12" cy="5" rx="7" ry="3"/><path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
    fit: '<path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/><path d="m4 9 5-5M20 9l-5-5M4 15l5 5M20 15l-5 5"/>',
    layers: '<path d="m12 3-9 5 9 5 9-5-9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
    minus: '<path d="M5 12h14"/>',
    pan: '<path d="M8 11V6.5a1.5 1.5 0 0 1 3 0V10 4.5a1.5 1.5 0 0 1 3 0V10 6a1.5 1.5 0 0 1 3 0v5-2a1.5 1.5 0 0 1 3 0v5c0 4-3 7-7 7h-1c-2 0-3.4-.8-4.6-2.3L4.3 15a1.7 1.7 0 0 1 2.4-2.4L9 14"/>',
    play: '<path d="m9 7 8 5-8 5V7Z"/><circle cx="12" cy="12" r="9"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    pointer: '<path d="m6 3 12 8-6 2-3 6L6 3Z"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M18.5 9A7 7 0 0 0 6.7 6.7L4 9m16 6-2.7 2.3A7 7 0 0 1 5.5 15"/>',
    route: '<path d="M6 19a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM18 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/><path d="M8.5 14.5 16 9.5"/>',
    scissors: '<circle cx="6" cy="7" r="3"/><circle cx="6" cy="17" r="3"/><path d="m8.5 8.5 10 7M8.5 15.5l10-7"/>',
    search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  };
  return `<svg aria-hidden="true" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name] || ''}</svg>`;
};

const app = document.querySelector('#app');

app.innerHTML = `
  <div class="app-shell">
    <header class="app-header">
      <div class="brand-mark" aria-hidden="true">R</div>
      <div class="brand-copy">
        <strong>Roadnet Web GIS</strong>
        <span>Multimodal GEO workspace</span>
      </div>
      <div class="header-context" aria-label="当前数据上下文">
        <span class="context-chip"><i class="context-dot context-dot--detect"></i>Detect <b id="detect-id">—</b></span>
        <span class="context-chip"><i class="context-dot context-dot--fit"></i>Fit <b id="fit-id">—</b></span>
      </div>
      <div class="service-state" id="service-state">
        <span class="service-state__dot"></span>
        <span id="service-state-text">正在连接地图服务</span>
      </div>
    </header>

    <nav class="menu-bar" aria-label="主菜单">
      <button type="button">工程</button>
      <button type="button">编辑</button>
      <button type="button">视图</button>
      <button type="button">图层</button>
      <button type="button">分析</button>
      <button type="button">帮助</button>
    </nav>

    <div class="tool-bar" role="toolbar" aria-label="地图工具">
      <div class="tool-group">
        <button class="tool-button is-active" type="button" data-tool="pan" title="平移地图 (V)" aria-label="平移地图" aria-pressed="true">${icon('pan')}</button>
        <button class="tool-button" type="button" data-tool="point" title="点选要素 (S，Shift 多选)" aria-label="点选要素" aria-pressed="false">${icon('pointer')}</button>
        <button class="tool-button" type="button" data-tool="box" title="框选要素 (B)" aria-label="框选要素" aria-pressed="false">${icon('box')}</button>
      </div>
      <div class="tool-separator" aria-hidden="true"></div>
      <div class="tool-group">
        <button class="tool-button" type="button" data-command="zoom-in" title="放大 (+)" aria-label="放大">${icon('plus')}</button>
        <button class="tool-button" type="button" data-command="zoom-out" title="缩小 (-)" aria-label="缩小">${icon('minus')}</button>
        <button class="tool-button" type="button" data-command="fit" title="缩放至图层" aria-label="缩放至图层">${icon('fit')}</button>
      </div>
      <div class="tool-separator" aria-hidden="true"></div>
      <div class="tool-group">
        <button class="tool-button" type="button" data-command="clear-selection" title="清除选择 (Esc)" aria-label="清除选择">${icon('clear')}</button>
        <button class="tool-button" type="button" data-command="refresh" title="重新加载图层" aria-label="重新加载图层">${icon('refresh')}</button>
      </div>
      <div class="toolbar-caption" id="toolbar-caption">平移地图</div>
    </div>

    <div class="workspace">
      <aside class="dock dock--left" aria-label="图层面板">
        <section class="dock-section dock-section--fill">
          <header class="dock-heading">
            <span>${icon('layers', 16)} 图层</span>
            <span class="dock-heading__meta" id="layer-total">2</span>
          </header>
          <div class="layer-filter">
            ${icon('search', 15)}
            <input id="layer-filter" type="search" placeholder="筛选图层…" aria-label="筛选图层" autocomplete="off" />
          </div>
          <div class="layer-tree" id="layer-tree" role="tree" aria-label="地图图层"></div>
        </section>
        <section class="dock-section dock-section--context">
          <header class="dock-heading dock-heading--small">
            <span>${icon('database', 15)} 数据上下文</span>
          </header>
          <dl class="context-list">
            <div><dt>源坐标系</dt><dd>EPSG:4326</dd></div>
            <div><dt>地图坐标系</dt><dd>EPSG:3857</dd></div>
            <div><dt>加载要素</dt><dd id="feature-total">0</dd></div>
          </dl>
        </section>
      </aside>

      <section class="map-panel" aria-label="地图窗口">
        <div id="map" class="map-canvas"></div>
        <div class="map-tool-hint" id="map-tool-hint">
          ${icon('pan', 16)}
          <span>拖动平移，滚轮缩放</span>
        </div>
        <div class="map-loading" id="map-loading" role="status">
          <span class="spinner"></span>
          <span>正在读取地图图层…</span>
        </div>
        <div class="map-notice" id="map-notice" hidden>
          <strong>尚无业务图层</strong>
          <span>底图仍可浏览；请启动 Backend 或检查地图数据上下文。</span>
          <button type="button" data-command="refresh">重新连接</button>
        </div>
        <div class="map-scale-card" aria-hidden="true">
          <span class="map-scale-line"></span>
          <span id="map-scale-card-text">1 km</span>
        </div>
      </section>

      <aside class="dock dock--right" aria-label="选择与分析面板">
        <section class="dock-section selection-section">
          <header class="dock-heading">
            <span>${icon('pointer', 15)} 已选择</span>
            <button class="text-button" id="clear-selection-inline" type="button" disabled>清除</button>
          </header>
          <div class="selection-summary">
            <strong id="selection-count">0</strong>
            <span id="selection-summary-text">请选择图层中的要素</span>
          </div>
          <div class="selection-list" id="selection-list">
            <div class="empty-selection">
              <span class="empty-selection__icon">${icon('pointer', 22)}</span>
              <span>使用点选或框选工具</span>
              <small>点选时按住 Shift 可增减选择</small>
            </div>
          </div>
        </section>

        <section class="dock-section analysis-section">
          <header class="dock-heading dock-heading--small">
            <span>${icon('route', 15)} 临时分析</span>
            <span class="memory-badge">不落盘</span>
          </header>
          <div class="analysis-actions">
            <button class="action-card action-card--replay" id="replay-action" type="button" disabled>
              <span class="action-card__icon">${icon('play', 20)}</span>
              <span class="action-card__copy">
                <strong>Replay</strong>
                <small id="replay-hint">选择轨迹点或框选范围</small>
              </span>
              <span class="action-card__arrow">→</span>
            </button>
            <button class="action-card action-card--clip" id="clip-action" type="button" disabled>
              <span class="action-card__icon">${icon('scissors', 20)}</span>
              <span class="action-card__copy">
                <strong>Clip</strong>
                <small id="clip-hint">需要恰好两个交叉口</small>
              </span>
              <span class="action-card__arrow">→</span>
            </button>
          </div>
          <div class="result-card" id="result-card" hidden>
            <span class="result-card__swatch"></span>
            <span><strong id="result-title">tmp</strong><small id="result-detail">0 个要素</small></span>
            <button type="button" id="clear-result" aria-label="清除临时结果" title="清除临时结果">${icon('close', 15)}</button>
          </div>
          <p class="analysis-footnote" id="analysis-footnote">结果仅绘制到 <b>tmp</b> 图层，不写入数据库。</p>
        </section>
      </aside>
    </div>

    <footer class="status-bar">
      <div class="status-message"><span class="status-indicator"></span><span id="status-message">地图就绪</span></div>
      <div class="status-spacer"></div>
      <div class="status-item"><span>坐标</span><output id="pointer-coordinate">—, —</output></div>
      <div class="status-item"><span>比例尺</span><output id="scale-value">—</output></div>
      <div class="status-item"><span>缩放</span><output id="zoom-value">4.0</output></div>
      <div class="crs-badge" title="地图显示坐标系">EPSG:3857</div>
    </footer>
  </div>
  <div class="toast-region" id="toast-region" aria-live="polite"></div>
`;

const dom = {
  analysisFootnote: document.querySelector('#analysis-footnote'),
  clearResult: document.querySelector('#clear-result'),
  clearSelectionInline: document.querySelector('#clear-selection-inline'),
  clipAction: document.querySelector('#clip-action'),
  clipHint: document.querySelector('#clip-hint'),
  detectId: document.querySelector('#detect-id'),
  featureTotal: document.querySelector('#feature-total'),
  fitId: document.querySelector('#fit-id'),
  layerFilter: document.querySelector('#layer-filter'),
  layerTotal: document.querySelector('#layer-total'),
  layerTree: document.querySelector('#layer-tree'),
  mapLoading: document.querySelector('#map-loading'),
  mapNotice: document.querySelector('#map-notice'),
  mapScaleCardText: document.querySelector('#map-scale-card-text'),
  mapToolHint: document.querySelector('#map-tool-hint'),
  pointerCoordinate: document.querySelector('#pointer-coordinate'),
  replayAction: document.querySelector('#replay-action'),
  replayHint: document.querySelector('#replay-hint'),
  resultCard: document.querySelector('#result-card'),
  resultDetail: document.querySelector('#result-detail'),
  resultTitle: document.querySelector('#result-title'),
  scaleValue: document.querySelector('#scale-value'),
  selectionCount: document.querySelector('#selection-count'),
  selectionList: document.querySelector('#selection-list'),
  selectionSummaryText: document.querySelector('#selection-summary-text'),
  serviceState: document.querySelector('#service-state'),
  serviceStateText: document.querySelector('#service-state-text'),
  statusMessage: document.querySelector('#status-message'),
  toastRegion: document.querySelector('#toast-region'),
  toolbarCaption: document.querySelector('#toolbar-caption'),
  zoomValue: document.querySelector('#zoom-value'),
};

const geoJson = new GeoJSON();
const view = new View({
  center: fromLonLat([104, 35]),
  zoom: 4.3,
  minZoom: 2,
  maxZoom: 21,
  constrainResolution: false,
});

const baseLayer = new TileLayer({
  source: new OSM({ crossOrigin: 'anonymous' }),
  visible: true,
  properties: {
    id: 'osm',
    name: 'OpenStreetMap',
    role: 'base',
    selectable: false,
  },
});

const tmpSource = new VectorSource();
const tmpLayer = new VectorLayer({
  source: tmpSource,
  style: createLayerStyle('tmp'),
  zIndex: 900,
  properties: {
    id: 'tmp',
    name: 'tmp',
    role: 'tmp',
    selectable: false,
  },
});

const map = new OlMap({
  target: 'map',
  layers: [baseLayer, tmpLayer],
  view,
  controls: defaultControls({
    zoom: false,
    rotate: false,
    attributionOptions: { collapsible: true },
  }),
  interactions: defaultInteractions({
    altShiftDragRotate: false,
    pinchRotate: false,
  }),
});

const state = {
  activeLayerId: null,
  analysisAbortController: null,
  busy: false,
  context: { detectId: null, fitId: null },
  layerAbortController: null,
  layerRecords: new Map(),
  lastResult: null,
  selectionBbox: null,
  selectionOrigin: null,
  tool: 'pan',
};

state.layerRecords.set('osm', {
  descriptor: { id: 'osm', name: 'OpenStreetMap', role: 'base', selectable: false },
  featureCount: null,
  layer: baseLayer,
  source: baseLayer.getSource(),
  status: 'ready',
});
state.layerRecords.set('tmp', {
  descriptor: { id: 'tmp', name: 'tmp', role: 'tmp', selectable: false },
  featureCount: 0,
  layer: tmpLayer,
  source: tmpSource,
  status: 'ready',
});

const selectedFeatures = new Select({
  hitTolerance: 8,
  layers: (layer) =>
    layer.get('id') === state.activeLayerId &&
    layer.get('selectable') === true &&
    layer.getVisible(),
  multi: true,
  style: createSelectionStyle,
});
selectedFeatures.setActive(false);
map.addInteraction(selectedFeatures);

const dragBox = new DragBox({
  className: 'roadnet-drag-box',
  condition: () => true,
});
dragBox.setActive(false);
map.addInteraction(dragBox);

selectedFeatures.on('select', () => {
  state.selectionOrigin = 'point';
  state.selectionBbox = null;
  renderSelection();
});

dragBox.on('boxstart', () => {
  selectedFeatures.getFeatures().clear();
});

dragBox.on('boxend', () => {
  const extent = dragBox.getGeometry().getExtent();
  const record = state.layerRecords.get(state.activeLayerId);
  const collection = selectedFeatures.getFeatures();

  if (record?.descriptor.selectable && record.source instanceof VectorSource) {
    record.source.forEachFeatureIntersectingExtent(extent, (feature) => collection.push(feature));
  }

  state.selectionOrigin = 'box';
  state.selectionBbox = transformExtent(extent, 'EPSG:3857', 'EPSG:4326').map(roundCoordinate);
  renderSelection();
});

function createLayerStyle(role) {
  const styles = {
    area: { color: '#7969c8', fill: 'rgba(121, 105, 200, 0.14)', width: 1.5, radius: 4 },
    generic: { color: '#4979a8', fill: 'rgba(73, 121, 168, 0.16)', width: 1.6, radius: 4 },
    intersection: { color: '#d76b2b', fill: '#fff8ee', width: 2, radius: 5.5 },
    point: { color: '#466b9d', fill: '#ecf4ff', width: 1.8, radius: 4 },
    road: { color: '#697987', fill: 'rgba(105, 121, 135, 0.12)', width: 2, radius: 3 },
    tmp: { color: '#e33f8e', fill: 'rgba(227, 63, 142, 0.14)', width: 3, radius: 5 },
    trajectory: { color: '#1c9c96', fill: '#e3faf8', width: 2.2, radius: 3.5 },
  };
  const definition = styles[role] || styles.generic;
  return new Style({
    fill: new Fill({ color: definition.fill }),
    stroke: new Stroke({ color: definition.color, width: definition.width }),
    image: new CircleStyle({
      radius: definition.radius,
      fill: new Fill({ color: definition.fill }),
      stroke: new Stroke({ color: definition.color, width: definition.width }),
    }),
  });
}

function createSelectionStyle(feature) {
  const geometryType = feature.getGeometry()?.getType() || '';
  return new Style({
    fill: new Fill({ color: 'rgba(255, 205, 60, 0.24)' }),
    stroke: new Stroke({ color: '#ffbb18', width: geometryType.includes('Line') ? 5 : 3 }),
    image: new CircleStyle({
      radius: geometryType.includes('Point') ? 8 : 6,
      fill: new Fill({ color: '#fff6cc' }),
      stroke: new Stroke({ color: '#e99d00', width: 3 }),
    }),
    zIndex: 1000,
  });
}

function inferRole(descriptor) {
  const role = String(descriptor.role || '').toLowerCase();
  if (role.includes('intersection') || role.includes('junction')) return 'intersection';
  if (role.includes('trajectory') || role.includes('track')) return 'trajectory';
  if (role.includes('road') || role.includes('network') || role.includes('edge')) return 'road';
  if (role.includes('point')) return 'point';
  if (role.includes('polygon') || role.includes('area')) return 'area';
  return 'generic';
}

function updateContext(metadata = {}) {
  const source = metadata.context || metadata;
  const detectId = firstValue(source.detect_id, source.detectId, metadata.resolved_detect_id);
  const fitId = firstValue(source.fit_id, source.fitId, metadata.resolved_fit_id);
  if (detectId !== undefined) state.context.detectId = detectId;
  if (fitId !== undefined) state.context.fitId = fitId;
  dom.detectId.textContent = state.context.detectId ?? '—';
  dom.fitId.textContent = state.context.fitId ?? '—';
  dom.detectId.title = state.context.detectId === null ? '未解析' : String(state.context.detectId);
  dom.fitId.title = state.context.fitId === null ? '未解析' : String(state.context.fitId);
}

function firstValue(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== '');
}

function roundCoordinate(value) {
  return Number(Number(value).toFixed(7));
}

function decorateFeature(feature, descriptor, metadata) {
  const properties = feature.getProperties();
  const role = inferRole(descriptor);
  const replayablePointLayer = role === 'trajectory' || descriptor.id === 'trajectory_points';
  const detectId = firstValue(
    properties.detect_id,
    properties.detectId,
    metadata.detect_id,
    metadata.resolved_detect_id,
    metadata.context?.detect_id,
  );
  const fitId = firstValue(
    properties.fit_id,
    properties.fitId,
    metadata.fit_id,
    metadata.resolved_fit_id,
    metadata.context?.fit_id,
  );
  const rawPointId = firstValue(
    properties.point_id,
    properties.raw_point_id,
    properties.trajectory_point_id,
    properties.raw_id,
    replayablePointLayer ? properties.database_id : undefined,
    replayablePointLayer ? properties.id : undefined,
    replayablePointLayer ? feature.getId() : undefined,
  );
  const intersectionId = firstValue(
    properties.intersection_id,
    role === 'intersection' ? properties.id : undefined,
    role === 'intersection' ? feature.getId() : undefined,
  );

  feature.set('_layerId', descriptor.id, true);
  feature.set('_layerName', descriptor.name, true);
  feature.set('_layerRole', role, true);
  feature.set('_detectId', detectId, true);
  feature.set('_fitId', fitId, true);
  feature.set('_rawPointId', rawPointId, true);
  feature.set('_intersectionId', intersectionId, true);
}

function createLayerRecord(descriptor) {
  const role = inferRole(descriptor);
  const source = new VectorSource();
  const layer = new VectorLayer({
    source,
    style: createLayerStyle(role),
    visible: descriptor.visible,
    zIndex: role === 'intersection' || role === 'point' ? 300 : role === 'trajectory' ? 200 : 100,
    properties: {
      id: descriptor.id,
      name: descriptor.name,
      role,
      selectable: descriptor.selectable,
    },
  });
  const record = { descriptor: { ...descriptor, role }, featureCount: 0, layer, source, status: 'loading' };
  state.layerRecords.set(descriptor.id, record);
  map.getLayers().insertAt(Math.max(1, map.getLayers().getLength() - 1), layer);
  return record;
}

async function loadLayers() {
  state.layerAbortController?.abort();
  state.layerAbortController = new AbortController();
  const { signal } = state.layerAbortController;

  clearSelection();
  removeBusinessLayers();
  setMapLoading(true);
  setServiceState('loading', '正在连接地图服务');
  setStatus('正在读取图层目录…');
  dom.mapNotice.hidden = true;
  renderLayerTree();

  try {
    const catalog = await listMapLayers({ signal });
    updateContext(catalog.metadata);

    if (!catalog.layers.length) {
      setServiceState('warning', '服务在线 · 无可用图层');
      setStatus('地图服务未返回业务图层');
      dom.mapNotice.hidden = false;
      return;
    }

    catalog.layers.forEach(createLayerRecord);
    const preferred = catalog.layers.find((layer) => inferRole(layer) === 'intersection' && layer.selectable)
      || catalog.layers.find((layer) => layer.selectable);
    state.activeLayerId = preferred?.id || null;
    renderLayerTree();

    const results = await Promise.allSettled(
      catalog.layers.map(async (descriptor) => {
        const requiredContext = descriptor.required_context || descriptor.requiredContext || [];
        const result = await loadMapLayer(descriptor.id, {
          detectId: requiredContext.includes('detect_id') ? state.context.detectId : undefined,
          fitId: requiredContext.includes('fit_id') ? state.context.fitId : undefined,
          signal,
        });
        const record = state.layerRecords.get(descriptor.id);
        if (!record) return;

        updateContext(result.metadata);
        const features = geoJson.readFeatures(result.featureCollection, {
          dataProjection: 'EPSG:4326',
          featureProjection: 'EPSG:3857',
        });
        features.forEach((feature) => decorateFeature(feature, descriptor, result.metadata));
        record.source.addFeatures(features);
        record.featureCount = features.length;
        record.status = 'ready';
        record.metadata = result.metadata;
        renderLayerTree();
        updateFeatureTotal();
      }),
    );

    if (signal.aborted) return;
    const failed = results.filter((result) => result.status === 'rejected');
    const loadedFeatures = getBusinessFeatureCount();
    if (failed.length === results.length) throw failed[0].reason;

    const bestActiveLayer = catalog.layers.find((descriptor) => {
      const record = state.layerRecords.get(descriptor.id);
      return descriptor.selectable && record?.status === 'ready' && record.featureCount > 0;
    });
    if (bestActiveLayer && state.layerRecords.get(state.activeLayerId)?.featureCount === 0) {
      state.activeLayerId = bestActiveLayer.id;
    }

    if (failed.length) {
      catalog.layers.forEach((descriptor, index) => {
        if (results[index].status === 'rejected') {
          const record = state.layerRecords.get(descriptor.id);
          if (record) record.status = 'error';
        }
      });
      showToast(`${failed.length} 个图层加载失败，其余图层仍可使用。`, 'warning');
      setServiceState('warning', `服务在线 · ${failed.length} 个图层异常`);
    } else {
      setServiceState('online', '地图服务已连接');
    }

    renderLayerTree();
    updateFeatureTotal();
    if (loadedFeatures > 0) {
      fitToData({ animate: false });
      setStatus(`已加载 ${loadedFeatures.toLocaleString('zh-CN')} 个要素`);
    } else {
      dom.mapNotice.hidden = false;
      setStatus('图层已连接，但当前上下文没有要素');
    }
  } catch (error) {
    if (error.name === 'AbortError') return;
    setServiceState('offline', '地图服务不可用');
    setStatus(error.message || '地图数据加载失败', true);
    dom.mapNotice.hidden = false;
    showToast(error.message || '地图数据加载失败', 'error');
  } finally {
    if (!signal.aborted) setMapLoading(false);
  }
}

function removeBusinessLayers() {
  Array.from(state.layerRecords.entries()).forEach(([id, record]) => {
    if (id === 'osm' || id === 'tmp') return;
    map.removeLayer(record.layer);
    state.layerRecords.delete(id);
  });
  state.activeLayerId = null;
  updateFeatureTotal();
}

function renderLayerTree() {
  const filter = dom.layerFilter.value.trim().toLocaleLowerCase('zh-CN');
  const records = Array.from(state.layerRecords.values()).filter((record) =>
    record.descriptor.name.toLocaleLowerCase('zh-CN').includes(filter),
  );
  dom.layerTotal.textContent = state.layerRecords.size;

  dom.layerTree.innerHTML = records
    .map((record) => {
      const { descriptor, featureCount, layer, status } = record;
      const active = descriptor.id === state.activeLayerId;
      const checked = layer.getVisible();
      const role = inferRole(descriptor);
      const count = featureCount === null ? '' : featureCount.toLocaleString('zh-CN');
      const statusIcon = status === 'loading'
        ? '<span class="layer-spinner" title="加载中"></span>'
        : status === 'error'
          ? '<span class="layer-error" title="加载失败">!</span>'
          : `<span class="layer-count">${count}</span>`;

      return `
        <div class="layer-row ${active ? 'is-active' : ''} ${descriptor.selectable ? '' : 'is-static'}" data-layer-id="${escapeHtml(descriptor.id)}" role="treeitem" aria-selected="${active}">
          <label class="layer-visibility" title="显示或隐藏 ${escapeHtml(descriptor.name)}">
            <input type="checkbox" ${checked ? 'checked' : ''} data-layer-visible="${escapeHtml(descriptor.id)}" aria-label="显示 ${escapeHtml(descriptor.name)}" />
            <span></span>
          </label>
          <button class="layer-main" type="button" data-layer-activate="${escapeHtml(descriptor.id)}" ${descriptor.selectable ? '' : 'tabindex="-1"'}>
            <i class="layer-swatch layer-swatch--${role}"></i>
            <span class="layer-label"><strong>${escapeHtml(descriptor.name)}</strong><small>${layerSubtitle(record)}</small></span>
          </button>
          ${statusIcon}
        </div>`;
    })
    .join('');
}

function layerSubtitle(record) {
  if (record.descriptor.id === 'osm') return 'XYZ 瓦片底图';
  if (record.descriptor.id === 'tmp') return '内存临时结果';
  if (record.status === 'loading') return '正在读取 GeoJSON…';
  if (record.status === 'error') return '图层加载失败';
  const geometry = record.descriptor.geometryType || inferRole(record.descriptor);
  return `${geometry} · EPSG:4326`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function setActiveLayer(layerId) {
  const record = state.layerRecords.get(layerId);
  if (!record?.descriptor.selectable) return;
  if (state.activeLayerId !== layerId) clearSelection();
  state.activeLayerId = layerId;
  renderLayerTree();
  setStatus(`活动图层：${record.descriptor.name}`);
}

function setLayerVisibility(layerId, visible) {
  const record = state.layerRecords.get(layerId);
  if (!record) return;
  record.layer.setVisible(visible);
  if (!visible && state.activeLayerId === layerId) clearSelection();
  renderLayerTree();
}

function setTool(tool) {
  state.tool = tool;
  selectedFeatures.setActive(tool === 'point');
  dragBox.setActive(tool === 'box');
  document.querySelectorAll('[data-tool]').forEach((button) => {
    const active = button.dataset.tool === tool;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  document.querySelector('#map').classList.toggle('is-crosshair', tool === 'box');

  const labels = {
    pan: { caption: '平移地图', icon: 'pan', hint: '拖动平移，滚轮缩放' },
    point: { caption: '点选要素', icon: 'pointer', hint: '单击选择；Shift + 单击增减选择' },
    box: { caption: '框选要素', icon: 'box', hint: '在地图上拖出矩形范围' },
  };
  const current = labels[tool];
  dom.toolbarCaption.textContent = current.caption;
  dom.mapToolHint.innerHTML = `${icon(current.icon, 16)}<span>${current.hint}</span>`;
  setStatus(current.caption);
}

function clearSelection() {
  selectedFeatures.getFeatures().clear();
  state.selectionBbox = null;
  state.selectionOrigin = null;
  renderSelection();
}

function renderSelection() {
  const features = selectedFeatures.getFeatures().getArray();
  dom.selectionCount.textContent = features.length;
  dom.clearSelectionInline.disabled = features.length === 0 && !state.selectionBbox;

  if (!features.length) {
    dom.selectionSummaryText.textContent = state.selectionBbox
      ? '框选范围内暂无已加载要素'
      : '请选择图层中的要素';
    dom.selectionList.innerHTML = state.selectionBbox
      ? `<div class="bbox-selection"><span>${icon('box', 20)}</span><strong>已定义 Replay 范围</strong><small>${formatBbox(state.selectionBbox)}</small></div>`
      : `<div class="empty-selection"><span class="empty-selection__icon">${icon('pointer', 22)}</span><span>使用点选或框选工具</span><small>点选时按住 Shift 可增减选择</small></div>`;
  } else {
    const record = state.layerRecords.get(features[0].get('_layerId'));
    dom.selectionSummaryText.textContent = `${record?.descriptor.name || '要素'} · ${selectionModeLabel()}`;
    dom.selectionList.innerHTML = features.map((feature, index) => selectionItem(feature, index)).join('');
  }

  updateActionState();
}

function selectionModeLabel() {
  if (state.selectionOrigin === 'box') return '框选';
  if (state.selectionOrigin === 'point') return '点选';
  return '选择';
}

function selectionItem(feature, index) {
  const properties = feature.getProperties();
  const displayId = firstValue(
    feature.get('_intersectionId'),
    feature.get('_rawPointId'),
    properties.name,
    properties.id,
    feature.getId(),
    index + 1,
  );
  const geometry = feature.getGeometry();
  let coordinateLabel = geometry?.getType() || 'Feature';
  if (geometry?.getType() === 'Point') {
    const [lon, lat] = toLonLat(geometry.getCoordinates());
    coordinateLabel = `${lon.toFixed(5)}, ${lat.toFixed(5)}`;
  }

  return `
    <div class="selection-item">
      <span class="selection-item__number">${index + 1}</span>
      <span class="selection-item__copy"><strong>${escapeHtml(displayId)}</strong><small>${escapeHtml(coordinateLabel)}</small></span>
      <button type="button" data-remove-selection="${index}" aria-label="移除此要素" title="移除此要素">${icon('close', 14)}</button>
    </div>`;
}

function formatBbox(bbox) {
  return `${bbox[0].toFixed(5)}, ${bbox[1].toFixed(5)} → ${bbox[2].toFixed(5)}, ${bbox[3].toFixed(5)}`;
}

function updateActionState() {
  const features = selectedFeatures.getFeatures().getArray();
  const pointIds = unique(features.map((feature) => feature.get('_rawPointId')).filter(isPresent));
  const boxReady = state.selectionOrigin === 'box' && state.selectionBbox?.length === 4;
  const replayReady = !state.busy && (boxReady || (pointIds.length > 0 && pointIds.length <= 1_000));

  dom.replayAction.disabled = !replayReady;
  if (state.busy) {
    dom.replayHint.textContent = '正在处理请求…';
  } else if (boxReady) {
    dom.replayHint.textContent = features.length
      ? `按框选范围回放 · 命中 ${features.length} 个要素`
      : '按当前框选范围回放';
  } else if (pointIds.length) {
    dom.replayHint.textContent = pointIds.length > 1_000
      ? `点选上限为 1000 · 当前 ${pointIds.length}`
      : `${pointIds.length} 个轨迹点已就绪`;
  } else if (features.length) {
    dom.replayHint.textContent = '所选要素缺少原始 point_id';
  } else {
    dom.replayHint.textContent = '选择轨迹点或框选范围';
  }

  const intersections = features.filter((feature) => feature.get('_layerRole') === 'intersection');
  const intersectionIds = intersections.map((feature) => feature.get('_intersectionId')).filter(isPresent);
  const detectIds = unique(
    intersections.map((feature) => firstValue(feature.get('_detectId'), state.context.detectId)).filter(isPresent),
  );
  const clipReady =
    !state.busy &&
    features.length === 2 &&
    intersections.length === 2 &&
    intersectionIds.length === 2 &&
    detectIds.length === 1;
  dom.clipAction.disabled = !clipReady;

  if (state.busy) dom.clipHint.textContent = '正在处理请求…';
  else if (features.length !== 2) dom.clipHint.textContent = `需要恰好两个交叉口 · 当前 ${intersections.length}`;
  else if (intersections.length !== 2) dom.clipHint.textContent = '所选要素必须都是交叉口';
  else if (intersectionIds.length !== 2) dom.clipHint.textContent = '交叉口缺少 intersection_id';
  else if (detectIds.length !== 1) dom.clipHint.textContent = '两个交叉口不属于同一 detect';
  else dom.clipHint.textContent = `Detect ${detectIds[0]} · 已就绪`;
}

function isPresent(value) {
  return value !== undefined && value !== null && value !== '';
}

function unique(values) {
  const seen = new Set();
  return values.filter((value) => {
    const key = String(value);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function runReplay() {
  if (dom.replayAction.disabled) return;
  const features = selectedFeatures.getFeatures().getArray();
  const useBbox = state.selectionOrigin === 'box' && state.selectionBbox?.length === 4;
  const pointIds = unique(features.map((feature) => feature.get('_rawPointId')).filter(isPresent));
  const detectIds = unique(
    features.map((feature) => feature.get('_detectId')).filter(isPresent),
  );
  const activeMetadata = state.layerRecords.get(state.activeLayerId)?.metadata || {};
  const detectId = detectIds.length === 1
    ? detectIds[0]
    : firstValue(activeMetadata.detect_id, activeMetadata.context?.detect_id);

  await runAnalysis('Replay', (signal) => requestReplay({
    detectId,
    pointIds: useBbox ? undefined : pointIds,
    bbox: useBbox ? state.selectionBbox : undefined,
  }, { signal }));
}

async function runClip() {
  if (dom.clipAction.disabled) return;
  const features = selectedFeatures.getFeatures().getArray();
  const intersectionIds = features.map((feature) => feature.get('_intersectionId'));
  const detectId = firstValue(features[0].get('_detectId'), state.context.detectId);

  await runAnalysis('Clip', (signal) => requestClip({ detectId, intersectionIds }, { signal }));
}

async function runAnalysis(name, request) {
  state.analysisAbortController?.abort();
  state.analysisAbortController = new AbortController();
  state.busy = true;
  updateActionState();
  setStatus(`${name} 请求处理中…`);
  dom.analysisFootnote.textContent = `${name} 正在后端内存中计算，请稍候…`;

  try {
    const result = await request(state.analysisAbortController.signal);
    updateContext(result.metadata);
    drawResult(name, result.featureCollection, result.metadata);
  } catch (error) {
    if (error.name === 'AbortError') return;
    setStatus(`${name} 失败：${error.message}`, true);
    showToast(`${name} 失败：${error.message}`, 'error');
  } finally {
    state.busy = false;
    dom.analysisFootnote.innerHTML = '结果仅绘制到 <b>tmp</b> 图层，不写入数据库。';
    updateActionState();
  }
}

function drawResult(name, featureCollection, metadata = {}) {
  const features = geoJson.readFeatures(featureCollection, {
    dataProjection: 'EPSG:4326',
    featureProjection: 'EPSG:3857',
  });
  tmpSource.clear();
  tmpSource.addFeatures(features);
  tmpLayer.setVisible(true);
  const record = state.layerRecords.get('tmp');
  record.featureCount = features.length;
  state.lastResult = { name, featureCount: features.length, metadata };
  dom.resultCard.hidden = false;
  dom.resultTitle.textContent = `tmp · ${name}`;
  dom.resultDetail.textContent = `${features.length.toLocaleString('zh-CN')} 个要素`;
  renderLayerTree();
  updateFeatureTotal();

  if (features.length) {
    const extent = tmpSource.getExtent();
    view.fit(extent, { duration: 420, maxZoom: 18, padding: [70, 70, 70, 70] });
    setStatus(`${name} 完成 · ${features.length.toLocaleString('zh-CN')} 个要素已绘制到 tmp`);
    showToast(`${name} 完成，结果已绘制到 tmp 图层。`, 'success');
  } else {
    setStatus(`${name} 完成 · 返回结果为空`);
    showToast(`${name} 已完成，但没有返回可绘制要素。`, 'warning');
  }
}

function clearResult() {
  tmpSource.clear();
  state.layerRecords.get('tmp').featureCount = 0;
  state.lastResult = null;
  dom.resultCard.hidden = true;
  renderLayerTree();
  updateFeatureTotal();
  setStatus('已清除 tmp 临时结果');
}

function getBusinessFeatureCount() {
  return Array.from(state.layerRecords.entries()).reduce((total, [id, record]) => {
    if (id === 'osm' || id === 'tmp') return total;
    return total + (record.featureCount || 0);
  }, 0);
}

function updateFeatureTotal() {
  const total = Array.from(state.layerRecords.entries()).reduce((sum, [id, record]) =>
    id === 'osm' ? sum : sum + (record.featureCount || 0), 0);
  dom.featureTotal.textContent = total.toLocaleString('zh-CN');
}

function fitToData({ animate = true } = {}) {
  const extent = createEmpty();
  state.layerRecords.forEach((record, id) => {
    if (id === 'osm' || !record.layer.getVisible() || !(record.source instanceof VectorSource)) return;
    const sourceExtent = record.source.getExtent();
    if (!isEmptyExtent(sourceExtent)) extend(extent, sourceExtent);
  });
  if (isEmptyExtent(extent)) {
    view.animate({ center: fromLonLat([104, 35]), zoom: 4.3, duration: animate ? 350 : 0 });
    showToast('当前没有可缩放至的可见要素。', 'warning');
    return;
  }
  view.fit(extent, { duration: animate ? 420 : 0, maxZoom: 18, padding: [60, 60, 60, 60] });
}

function setMapLoading(loading) {
  dom.mapLoading.classList.toggle('is-visible', loading);
}

function setServiceState(status, message) {
  dom.serviceState.dataset.status = status;
  dom.serviceStateText.textContent = message;
}

function setStatus(message, error = false) {
  dom.statusMessage.textContent = message;
  dom.statusMessage.closest('.status-message').classList.toggle('is-error', error);
}

function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast toast--${type}`;
  toast.innerHTML = `<span class="toast__mark"></span><span>${escapeHtml(message)}</span>`;
  dom.toastRegion.append(toast);
  requestAnimationFrame(() => toast.classList.add('is-visible'));
  window.setTimeout(() => {
    toast.classList.remove('is-visible');
    window.setTimeout(() => toast.remove(), 220);
  }, 4200);
}

function updateMapStatus() {
  const zoom = view.getZoom() || 0;
  const resolution = view.getResolution();
  dom.zoomValue.textContent = zoom.toFixed(1);
  if (!resolution) return;

  const metersPerPixel = getPointResolution('EPSG:3857', resolution, view.getCenter(), 'm');
  const scale = Math.round(metersPerPixel * 3779.527559);
  dom.scaleValue.textContent = `1:${formatScale(scale)}`;
  const cardDistance = niceDistance(metersPerPixel * 92);
  dom.mapScaleCardText.textContent = formatDistance(cardDistance);
  document.documentElement.style.setProperty('--scale-width', `${Math.max(30, cardDistance / metersPerPixel)}px`);
}

function niceDistance(distance) {
  if (!Number.isFinite(distance) || distance <= 0) return 1;
  const exponent = 10 ** Math.floor(Math.log10(distance));
  const fraction = distance / exponent;
  const nice = fraction >= 5 ? 5 : fraction >= 2 ? 2 : 1;
  return nice * exponent;
}

function formatDistance(meters) {
  if (meters >= 1000) return `${Number((meters / 1000).toPrecision(3))} km`;
  return `${Number(meters.toPrecision(3))} m`;
}

function formatScale(scale) {
  if (scale >= 1_000_000) return `${(scale / 1_000_000).toFixed(scale >= 10_000_000 ? 0 : 1)}M`;
  if (scale >= 1000) return `${Math.round(scale / 1000)}K`;
  return scale.toLocaleString('zh-CN');
}

function executeCommand(command) {
  if (command === 'zoom-in') view.animate({ zoom: (view.getZoom() || 0) + 1, duration: 180 });
  else if (command === 'zoom-out') view.animate({ zoom: (view.getZoom() || 0) - 1, duration: 180 });
  else if (command === 'fit') fitToData();
  else if (command === 'clear-selection') clearSelection();
  else if (command === 'refresh') loadLayers();
}

document.addEventListener('click', (event) => {
  const tool = event.target.closest('[data-tool]')?.dataset.tool;
  if (tool) setTool(tool);

  const command = event.target.closest('[data-command]')?.dataset.command;
  if (command) executeCommand(command);

  const activate = event.target.closest('[data-layer-activate]')?.dataset.layerActivate;
  if (activate) setActiveLayer(activate);

  const removeIndex = event.target.closest('[data-remove-selection]')?.dataset.removeSelection;
  if (removeIndex !== undefined) {
    selectedFeatures.getFeatures().removeAt(Number(removeIndex));
    state.selectionOrigin = 'point';
    state.selectionBbox = null;
    renderSelection();
  }
});

dom.layerTree.addEventListener('change', (event) => {
  const layerId = event.target.dataset.layerVisible;
  if (layerId) setLayerVisibility(layerId, event.target.checked);
});

dom.layerFilter.addEventListener('input', renderLayerTree);
dom.clearSelectionInline.addEventListener('click', clearSelection);
dom.replayAction.addEventListener('click', runReplay);
dom.clipAction.addEventListener('click', runClip);
dom.clearResult.addEventListener('click', clearResult);

map.on('pointermove', (event) => {
  if (event.dragging) return;
  const [lon, lat] = toLonLat(event.coordinate);
  dom.pointerCoordinate.textContent = `${lon.toFixed(6)}, ${lat.toFixed(6)}`;
});
map.getViewport().addEventListener('mouseleave', () => {
  dom.pointerCoordinate.textContent = '—, —';
});
view.on('change:resolution', updateMapStatus);
view.on('change:center', updateMapStatus);

window.addEventListener('keydown', (event) => {
  if (event.target.matches('input, textarea, select')) return;
  if (event.key === 'Escape') clearSelection();
  else if (event.key.toLowerCase() === 'v') setTool('pan');
  else if (event.key.toLowerCase() === 's') setTool('point');
  else if (event.key.toLowerCase() === 'b') setTool('box');
  else if (event.key === '+' || event.key === '=') executeCommand('zoom-in');
  else if (event.key === '-') executeCommand('zoom-out');
});

window.addEventListener('resize', () => map.updateSize());

renderLayerTree();
renderSelection();
updateMapStatus();
loadLayers();
