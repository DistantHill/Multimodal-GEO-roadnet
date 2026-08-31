const API_ROOT = '/api/v1';

export class MapApiError extends Error {
  constructor(message, { status = 0, details = null } = {}) {
    super(message);
    this.name = 'MapApiError';
    this.status = status;
    this.details = details;
  }
}

async function requestJson(path, { method = 'GET', body, signal } = {}) {
  let response;

  try {
    response = await fetch(`${API_ROOT}${path}`, {
      method,
      body: body === undefined ? undefined : JSON.stringify(body),
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      signal,
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new MapApiError('无法连接地图服务，请检查 Backend 是否已启动。', { details: error });
  }

  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json')
    ? await response.json().catch(() => null)
    : await response.text().catch(() => '');

  if (!response.ok) {
    const serverMessage =
      payload?.detail?.message || payload?.detail || payload?.message || payload?.error;
    throw new MapApiError(
      typeof serverMessage === 'string' ? serverMessage : `地图服务返回 HTTP ${response.status}`,
      { status: response.status, details: payload },
    );
  }

  return payload;
}

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null);
}

function normalizeMetadata(payload) {
  const defaultContext = payload?.default_context || {};
  const explicit = payload?.metadata || payload?.meta || payload?.context || {};
  return {
    ...defaultContext,
    ...explicit,
    context: {
      ...defaultContext,
      ...(explicit.context || {}),
    },
  };
}

function normalizeFeatureCollection(payload) {
  const candidates = [payload, payload?.data, payload?.result, payload?.geojson];
  const collection = candidates.find((candidate) => candidate?.type === 'FeatureCollection');

  if (collection) {
    return {
      type: 'FeatureCollection',
      features: Array.isArray(collection.features) ? collection.features : [],
    };
  }

  const features = firstDefined(payload?.features, payload?.data?.features, payload?.result?.features);
  if (Array.isArray(features)) return { type: 'FeatureCollection', features };

  return { type: 'FeatureCollection', features: [] };
}

function inferLayerRole(descriptor) {
  const explicit = firstDefined(descriptor.role, descriptor.kind, descriptor.layer_type);
  if (explicit) return String(explicit).toLowerCase();

  const hint = `${descriptor.id || ''} ${descriptor.name || ''}`.toLowerCase();
  if (/intersection|junction|crossing|交叉|路口/.test(hint)) return 'intersection';
  if (/trajectory|track|trace|轨迹/.test(hint)) return 'trajectory';
  if (/point|sample|detection|检测点|采样点/.test(hint)) return 'point';
  if (/road|edge|network|道路|路网/.test(hint)) return 'road';
  if (/polygon|area|区域/.test(hint)) return 'area';
  return 'generic';
}

function normalizeDescriptor(descriptor, index) {
  const id = String(
    firstDefined(descriptor.id, descriptor.layer_id, descriptor.slug, descriptor.key, `layer-${index + 1}`),
  );
  const geometryType = firstDefined(
    descriptor.geometry_type,
    descriptor.geometryType,
    descriptor.geom_type,
    null,
  );

  return {
    ...descriptor,
    id,
    name: String(firstDefined(descriptor.title, descriptor.name, descriptor.label, id)),
    role: inferLayerRole({ ...descriptor, id }),
    geometryType,
    selectable: descriptor.selectable !== false && descriptor.queryable !== false,
    visible: descriptor.visible !== false && descriptor.default_visible !== false,
  };
}

function buildQuery(parameters) {
  const query = new URLSearchParams();
  Object.entries(parameters).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    query.set(key, Array.isArray(value) ? value.join(',') : String(value));
  });
  const serialized = query.toString();
  return serialized ? `?${serialized}` : '';
}

function normalizeAnalysisResult(payload) {
  return {
    featureCollection: normalizeFeatureCollection(payload),
    metadata: normalizeMetadata(payload),
  };
}

export async function listMapLayers({ signal } = {}) {
  const payload = await requestJson('/map/layers', { signal });
  const candidates = firstDefined(payload?.layers, payload?.items, payload?.data, payload);
  const descriptors = Array.isArray(candidates) ? candidates : [];

  return {
    layers: descriptors.map(normalizeDescriptor).filter((layer) => layer.id !== 'tmp'),
    metadata: normalizeMetadata(payload),
  };
}

export async function loadMapLayer(
  layerId,
  { bbox, detectId, fitId, limit = 5_000, signal } = {},
) {
  const query = buildQuery({
    bbox,
    detect_id: detectId,
    fit_id: fitId,
    limit,
  });
  const payload = await requestJson(`/map/layers/${encodeURIComponent(layerId)}${query}`, { signal });

  return {
    featureCollection: normalizeFeatureCollection(payload),
    metadata: normalizeMetadata(payload),
  };
}

export async function requestReplay(
  { detectId, pointIds, bbox, maxVehicles, maxPoints },
  { signal } = {},
) {
  const body = {};
  if (detectId !== undefined && detectId !== null) body.detect_id = detectId;
  if (pointIds?.length) body.point_ids = pointIds;
  if (bbox?.length === 4) body.bbox = bbox.map(Number);
  if (maxVehicles !== undefined) body.max_vehicles = maxVehicles;
  if (maxPoints !== undefined) body.max_points = maxPoints;

  if (!body.point_ids && !body.bbox) {
    throw new MapApiError('Replay 需要点主键或框选范围。');
  }

  const payload = await requestJson('/map/replay', { method: 'POST', body, signal });
  return normalizeAnalysisResult(payload);
}

export async function requestClip(
  { detectId, intersectionIds, maxVehicles, maxPoints },
  { signal } = {},
) {
  if (detectId === undefined || detectId === null || detectId === '') {
    throw new MapApiError('Clip 缺少 detect_id 上下文。');
  }
  if (!Array.isArray(intersectionIds) || intersectionIds.length !== 2) {
    throw new MapApiError('Clip 必须且只能选择两个交叉口。');
  }

  const body = {
    detect_id: detectId,
    intersection_ids: intersectionIds,
  };
  if (maxVehicles !== undefined) body.max_vehicles = maxVehicles;
  if (maxPoints !== undefined) body.max_points = maxPoints;

  const payload = await requestJson('/map/clip', { method: 'POST', body, signal });
  return normalizeAnalysisResult(payload);
}
