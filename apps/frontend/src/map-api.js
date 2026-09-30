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

export async function listDatabases({ signal } = {}) {
  return requestJson('/map/databases', { signal });
}

export async function listRuns({ databaseName, signal } = {}) {
  return requestJson(`/map/runs${buildQuery({ database_name: databaseName })}`, { signal });
}

export async function listMapLayers({ signal, databaseName } = {}) {
  const payload = await requestJson(`/map/layers${buildQuery({ database_name: databaseName })}`, { signal });
  const candidates = firstDefined(payload?.layers, payload?.items, payload?.data, payload);
  const descriptors = Array.isArray(candidates) ? candidates : [];

  return {
    layers: descriptors.map(normalizeDescriptor).filter((layer) => layer.id !== 'tmp'),
    metadata: normalizeMetadata(payload),
  };
}

export async function loadMapLayer(
  layerId,
  { bbox, viewBbox, tileKey, detectId, fitId, refineId, databaseName, limit, maxFeatures, onPage, signal } = {},
) {
  const features = [];
  const seenFeatureIds = new Set();
  let pageOffset = 0;
  let loadedPageCount = 0;
  let resolvedDetectId = detectId;
  let resolvedFitId = fitId;
  let resolvedRefineId = refineId;
  let metadata = {};

  while (true) {
    const query = buildQuery({
      database_name: databaseName,
      refine_id: resolvedRefineId,
      bbox,
      view_bbox: viewBbox,
      tile_key: tileKey,
      detect_id: resolvedDetectId,
      fit_id: resolvedFitId,
      limit,
      offset: pageOffset,
    });
    const requestedContext = {
      detect_id: resolvedDetectId,
      fit_id: resolvedFitId,
      refine_id: resolvedRefineId,
    };
    const payload = await requestJson(`/map/layers/${encodeURIComponent(layerId)}${query}`, { signal });
    const page = normalizeFeatureCollection(payload);
    const pageMetadata = normalizeMetadata(payload);
    for (const [key, requestedId] of Object.entries(requestedContext)) {
      const returnedId = firstDefined(pageMetadata[key], pageMetadata.context?.[key]);
      if (requestedId !== undefined && requestedId !== null
        && returnedId !== undefined && returnedId !== null
        && String(returnedId) !== String(requestedId)) {
        throw new MapApiError(`地图图层 ${layerId} 分页期间的 ${key} 上下文发生变化。`);
      }
    }
    for (const feature of page.features) {
      if (feature.id === undefined || feature.id === null) continue;
      const featureId = String(feature.id);
      if (seenFeatureIds.has(featureId)) {
        throw new MapApiError(`地图图层 ${layerId} 分页返回了重复要素 ID ${featureId}。`);
      }
      seenFeatureIds.add(featureId);
    }
    if (maxFeatures !== undefined && features.length + page.features.length > maxFeatures) {
      throw new MapApiError(`当前区域超过 ${maxFeatures.toLocaleString('zh-CN')} 个要素的内存上限；请放大地图后重试。`);
    }
    // Reserve shared in-flight capacity before retaining this page or requesting another.
    if (signal?.aborted) {
      const error = new Error('地图区域请求已取消。');
      error.name = 'AbortError';
      throw error;
    }
    onPage?.(page.features.length);
    features.push(...page.features);
    loadedPageCount += 1;

    resolvedDetectId = firstDefined(resolvedDetectId, pageMetadata.detect_id, pageMetadata.context?.detect_id);
    resolvedFitId = firstDefined(resolvedFitId, pageMetadata.fit_id, pageMetadata.context?.fit_id);
    resolvedRefineId = firstDefined(resolvedRefineId, pageMetadata.refine_id, pageMetadata.context?.refine_id);
    metadata = pageMetadata;

    const hasMore = firstDefined(pageMetadata.has_more, pageMetadata.truncated, false);
    if (typeof hasMore !== 'boolean') {
      throw new MapApiError(`地图图层 ${layerId} 返回了无效的分页状态。`);
    }
    if (!hasMore) break;

    const nextCursor = pageMetadata.next_cursor;
    if (!Number.isSafeInteger(nextCursor)
      || nextCursor !== pageOffset + page.features.length
      || page.features.length === 0) {
      throw new MapApiError(`地图图层 ${layerId} 声明仍有后续页，但没有有效的分页游标。`);
    }
    pageOffset = nextCursor;
  }

  return {
    featureCollection: { type: 'FeatureCollection', features },
    metadata: {
      ...metadata,
      detect_id: resolvedDetectId ?? metadata.detect_id,
      fit_id: resolvedFitId ?? metadata.fit_id,
      refine_id: resolvedRefineId ?? metadata.refine_id,
      context: {
        ...(metadata.context || {}),
        ...(resolvedDetectId === undefined ? {} : { detect_id: resolvedDetectId }),
        ...(resolvedFitId === undefined ? {} : { fit_id: resolvedFitId }),
        ...(resolvedRefineId === undefined ? {} : { refine_id: resolvedRefineId }),
      },
      count: features.length,
      has_more: false,
      truncated: false,
      next_cursor: null,
      loaded_page_count: loadedPageCount,
    },
  };
}

export async function requestReplay(
  { detectId, fitId, refineId, sourceRowKeys, resolvedPointIds, intersectionId, intersectionIds, centerlineIds, tracksCrossIds, turnEventIds, bbox, maxVehicles, maxPoints, databaseName },
  { signal } = {},
) {
  const body = {};
  if (databaseName) body.database_name = databaseName;
  if (detectId !== undefined && detectId !== null) body.detect_id = detectId;
  if (sourceRowKeys?.length) body.source_row_keys = sourceRowKeys.map(String);
  if (resolvedPointIds?.length) body.resolved_point_ids = resolvedPointIds;
  if (bbox !== undefined) throw new MapApiError('Replay 不支持 bbox，请选择准确的点、交叉口或中心线 ID。');
  if (intersectionId !== undefined) body.intersection_id = intersectionId;
  if (intersectionIds !== undefined) {
    if (!Array.isArray(intersectionIds) || intersectionIds.length === 0
      || new Set(intersectionIds.map(Number)).size !== intersectionIds.length
      || intersectionIds.some((id) => !Number.isSafeInteger(Number(id)) || Number(id) < 1)) {
      throw new MapApiError('Replay 必须选择一个以上具有有效且互不重复 intersection_id 的交叉口。');
    }
    body.intersection_ids = intersectionIds.map(Number);
  }
  if (centerlineIds !== undefined) {
    if (!Array.isArray(centerlineIds) || centerlineIds.length === 0 || centerlineIds.length > 1_000
      || new Set(centerlineIds.map(Number)).size !== centerlineIds.length
      || centerlineIds.some((id) => !Number.isSafeInteger(Number(id)) || Number(id) < 1)) {
      throw new MapApiError('Replay 必须选择 1 到 1000 条具有有效且互不重复 centerline_id 的中心线。');
    }
    if (fitId === undefined || fitId === null || fitId === '') {
      throw new MapApiError('中心线 Replay 缺少 fit_id 上下文。');
    }
    if (detectId === undefined || detectId === null || detectId === '') {
      throw new MapApiError('中心线 Replay 缺少 detect_id 上下文。');
    }
    body.centerline_ids = centerlineIds.map(Number);
    body.fit_id = fitId;
    if (refineId !== undefined && refineId !== null && refineId !== '') body.refine_id = refineId;
  }
  for (const [field, ids] of [['tracks_cross_ids', tracksCrossIds], ['turn_event_ids', turnEventIds]]) {
    if (ids === undefined) continue;
    if (!Array.isArray(ids) || ids.length === 0 || ids.length > 1000
      || new Set(ids.map(Number)).size !== ids.length
      || ids.some((id) => !Number.isSafeInteger(Number(id)) || Number(id) < 1)) {
      throw new MapApiError('事件 Replay 需要 1 到 1000 个有效且互不重复的事件 ID。');
    }
    if (!detectId) throw new MapApiError('事件 Replay 缺少 detect_id 上下文。');
    body[field] = ids.map(Number);
  }
  if (maxVehicles !== undefined) body.max_vehicles = maxVehicles;
  if (maxPoints !== undefined) body.max_points = maxPoints;

  const selections = [body.source_row_keys, body.resolved_point_ids, body.intersection_id, body.intersection_ids, body.centerline_ids, body.tracks_cross_ids, body.turn_event_ids]
    .filter((value) => value !== undefined);
  if (selections.length !== 1) {
    throw new MapApiError('Replay 需要且只能提供来源键、resolved point ID、交叉口 ID 或中心线 ID。');
  }
  if (body.resolved_point_ids && !body.detect_id) {
    throw new MapApiError('resolved point ID Replay 缺少 detect_id 上下文。');
  }
  if (body.intersection_ids && !body.detect_id) {
    throw new MapApiError('交叉口 Replay 缺少 detect_id 上下文。');
  }

  const payload = await requestJson('/map/replay', { method: 'POST', body, signal });
  return normalizeAnalysisResult(payload);
}

export async function requestClip(
  { detectId, intersectionIds, maxVehicles, maxPoints, databaseName },
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
  if (databaseName) body.database_name = databaseName;
  if (maxVehicles !== undefined) body.max_vehicles = maxVehicles;
  if (maxPoints !== undefined) body.max_points = maxPoints;

  const payload = await requestJson('/map/clip', { method: 'POST', body, signal });
  return normalizeAnalysisResult(payload);
}
