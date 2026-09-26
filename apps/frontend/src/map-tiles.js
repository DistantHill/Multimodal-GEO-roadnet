import { loadMapLayer } from './map-api.js';

const WEB_MERCATOR_HALF_WORLD = 20_037_508.342789244;
const WEB_MERCATOR_EARTH_RADIUS = 6_378_137;
const WEB_MERCATOR_MAX_LAT = 85.0511287798066;
const MIN_TILE_ZOOM = 12;
const MAX_TILE_ZOOM = 30;
const MAX_VISIBLE_TILES = 64;
const MAX_CONCURRENT_REQUESTS = 4;
const MAX_CACHED_TILES = 64;
const MAX_FEATURES = 100_000;

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function mercatorToLonLat(x, y) {
  const lon = (x / WEB_MERCATOR_HALF_WORLD) * 180;
  const lat = (Math.atan(Math.sinh(y / WEB_MERCATOR_EARTH_RADIUS)) * 180) / Math.PI;
  return [lon, clamp(lat, -WEB_MERCATOR_MAX_LAT, WEB_MERCATOR_MAX_LAT)];
}

function abortError() {
  const error = new Error('视口瓦片加载已取消。');
  error.name = 'AbortError';
  return error;
}

function throwIfAborted(signal) {
  if (signal?.aborted) throw signal.reason instanceof Error ? signal.reason : abortError();
}

function stableFeatureId(feature) {
  const id = feature?.id ?? feature?.properties?.resolved_point_id ?? feature?.properties?.id;
  return id === undefined || id === null ? null : String(id);
}

function cleanPageMetadata(metadata) {
  const { tile_key: _tileKey, view_bbox: _viewBbox, ...rest } = metadata;
  return rest;
}

function contextCacheKey(context, tile) {
  return JSON.stringify([
    context.layerId,
    context.databaseName ?? null,
    context.refineId ?? null,
    context.detectId ?? null,
    context.fitId ?? null,
    tile.z,
    tile.x,
    tile.y,
  ]);
}

function tileReferenceBudgetError(maxFeatures = MAX_FEATURES) {
  const error = new Error(
    `当前视口瓦片累计返回超过 ${maxFeatures.toLocaleString('zh-CN')} 个要素引用（含跨瓦片重复）；请放大地图后重试。`,
  );
  error.name = 'MapTileBudgetError';
  return error;
}

function resultMetadata(plan, counts, extra = {}) {
  return {
    ...extra,
    tile_count: plan.tiles.length,
    loaded_tile_count: counts.loadedTileCount,
    loaded_page_count: counts.loadedPageCount,
    count: counts.featureCount,
    scope: 'viewport tiles',
    view_bbox: plan.viewBbox4326,
    tile_zoom: plan.z,
    has_more: false,
    truncated: false,
    next_cursor: null,
  };
}

function snapshotResult(plan, featureById, counts, extra = {}) {
  return {
    featureCollection: {
      type: 'FeatureCollection',
      features: [...featureById.values()],
    },
    metadata: resultMetadata(plan, {
      ...counts,
      featureCount: featureById.size,
    }, extra),
  };
}

/** Convert a Web Mercator viewport into bounded, integer XYZ tile requests. */
export function planViewTiles(extent, viewZoom, { maxTiles = MAX_VISIBLE_TILES } = {}) {
  if (!Array.isArray(extent) || extent.length !== 4
    || !extent.every(Number.isFinite)
    || extent[0] > extent[2] || extent[1] > extent[3]) {
    throw new TypeError('extent must be a finite [minX, minY, maxX, maxY] Web Mercator extent.');
  }
  if (!Number.isFinite(viewZoom)) throw new TypeError('viewZoom must be finite.');
  if (!Number.isInteger(maxTiles) || maxTiles < 1) {
    throw new RangeError('maxTiles must be a positive integer.');
  }

  const [minX, minY, maxX, maxY] = extent.map((value) =>
    clamp(value, -WEB_MERCATOR_HALF_WORLD, WEB_MERCATOR_HALF_WORLD));
  const [viewWest, viewSouth] = mercatorToLonLat(minX, minY);
  const [viewEast, viewNorth] = mercatorToLonLat(maxX, maxY);
  const viewBbox4326 = [viewWest, viewSouth, viewEast, viewNorth];
  const z = clamp(Math.max(MIN_TILE_ZOOM, Math.floor(viewZoom)), MIN_TILE_ZOOM, MAX_TILE_ZOOM);
  if (minX >= maxX || minY >= maxY) {
    return {
      z,
      viewBbox4326,
      tiles: [],
      visibleTileCount: 0,
      reason: '当前视口没有覆盖有效的 Web Mercator 范围。',
    };
  }
  if (viewZoom < MIN_TILE_ZOOM) {
    return {
      z,
      viewBbox4326,
      tiles: [],
      visibleTileCount: 0,
      reason: `请放大到缩放级别 ${MIN_TILE_ZOOM} 或更高后加载业务图层。`,
    };
  }
  const tileCountPerAxis = 2 ** z;
  const worldWidth = WEB_MERCATOR_HALF_WORLD * 2;
  const tileWidth = worldWidth / tileCountPerAxis;
  const minTileX = clamp(Math.floor((minX + WEB_MERCATOR_HALF_WORLD) / tileWidth), 0, tileCountPerAxis - 1);
  const maxTileX = clamp(Math.ceil((maxX + WEB_MERCATOR_HALF_WORLD) / tileWidth) - 1, 0, tileCountPerAxis - 1);
  const minTileY = clamp(Math.floor((WEB_MERCATOR_HALF_WORLD - maxY) / tileWidth), 0, tileCountPerAxis - 1);
  const maxTileY = clamp(Math.ceil((WEB_MERCATOR_HALF_WORLD - minY) / tileWidth) - 1, 0, tileCountPerAxis - 1);

  if (maxTileX < minTileX || maxTileY < minTileY) {
    return {
      z,
      viewBbox4326,
      tiles: [],
      visibleTileCount: 0,
      reason: '当前视口没有覆盖有效的 Web Mercator 瓦片。',
    };
  }

  const visibleTileCount = (maxTileX - minTileX + 1) * (maxTileY - minTileY + 1);
  if (visibleTileCount > maxTiles) {
    return {
      z,
      viewBbox4326,
      tiles: [],
      visibleTileCount,
      reason: `当前视口覆盖 ${visibleTileCount} 个业务瓦片（上限 ${maxTiles}），请放大地图后重试。`,
    };
  }

  const tiles = [];
  for (let y = minTileY; y <= maxTileY; y += 1) {
    for (let x = minTileX; x <= maxTileX; x += 1) {
      const tileMinX = x * tileWidth - WEB_MERCATOR_HALF_WORLD;
      const tileMaxX = tileMinX + tileWidth;
      const tileMaxY = WEB_MERCATOR_HALF_WORLD - y * tileWidth;
      const tileMinY = tileMaxY - tileWidth;
      const [west, south] = mercatorToLonLat(tileMinX, tileMinY);
      const [east, north] = mercatorToLonLat(tileMaxX, tileMaxY);
      tiles.push({
        key: `${z}/${x}/${y}`,
        z,
        x,
        y,
        bbox4326: [west, south, east, north],
      });
    }
  }

  return { z, viewBbox4326, tiles, visibleTileCount, reason: null };
}

/** Bounded viewport loader with one concurrency pool and a context-keyed LRU. */
export class TileLayerLoader {
  constructor({
    layerLoader = loadMapLayer,
    maxConcurrent = MAX_CONCURRENT_REQUESTS,
    maxCachedTiles = MAX_CACHED_TILES,
    maxFeatures = MAX_FEATURES,
  } = {}) {
    if (!Number.isInteger(maxConcurrent) || maxConcurrent < 1) {
      throw new RangeError('maxConcurrent must be a positive integer.');
    }
    if (!Number.isInteger(maxCachedTiles) || maxCachedTiles < 1) {
      throw new RangeError('maxCachedTiles must be a positive integer.');
    }
    if (!Number.isInteger(maxFeatures) || maxFeatures < 1) {
      throw new RangeError('maxFeatures must be a positive integer.');
    }
    this.layerLoader = layerLoader;
    this.maxConcurrent = maxConcurrent;
    this.maxCachedTiles = maxCachedTiles;
    this.maxFeatures = maxFeatures;
    this.activeRequests = 0;
    this.waiters = [];
    this.cache = new Map();
    this.cacheFeatureCount = 0;
    this.activePayloadCounts = new Map();
    this.activeTileCounts = new Map();
    this.generation = 0;
  }

  clear() {
    this.generation += 1;
    this.cache.clear();
    this.cacheFeatureCount = 0;
    this.activePayloadCounts.clear();
  }

  async load(plan, context, { signal, onProgress } = {}) {
    const layerId = context?.layerId;
    if (!layerId) throw new TypeError('context.layerId is required.');
    const counts = { loadedTileCount: 0, loadedPageCount: 0 };
    const featureById = new Map();
    const cacheKeys = plan.tiles.map((tile) => contextCacheKey(context, tile));
    const currentKeys = new Set(cacheKeys);
    const generation = this.generation;

    if (!plan.tiles.length) {
      const result = snapshotResult(plan, featureById, counts, { reason: plan.reason || null });
      onProgress?.(result);
      return result;
    }

    throwIfAborted(signal);
    const controller = new AbortController();
    const relayAbort = () => controller.abort(signal.reason instanceof Error ? signal.reason : abortError());
    signal?.addEventListener('abort', relayAbort, { once: true });
    if (signal?.aborted) relayAbort();

    const requestId = Symbol(layerId);
    let requestPayloadCount = 0;
    this.activePayloadCounts.set(requestId, requestPayloadCount);
    for (const key of currentKeys) this.activeTileCounts.set(key, (this.activeTileCounts.get(key) || 0) + 1);
    let featureBudgetReleased = false;
    let tileProtectionReleased = false;
    const releaseFeatureBudget = () => {
      if (featureBudgetReleased) return;
      featureBudgetReleased = true;
      this.activePayloadCounts.delete(requestId);
    };
    const releaseTileProtection = () => {
      if (tileProtectionReleased) return;
      tileProtectionReleased = true;
      for (const key of currentKeys) {
        const remaining = (this.activeTileCounts.get(key) || 1) - 1;
        if (remaining > 0) this.activeTileCounts.set(key, remaining);
        else this.activeTileCounts.delete(key);
      }
    };
    const releaseActive = () => {
      releaseFeatureBudget();
      releaseTileProtection();
    };
    controller.signal.addEventListener('abort', releaseActive, { once: true });

    const stagedTiles = [];
    let firstMetadata = {};
    let failure = null;
    let pendingPageCount = 0;

    const reservePayload = (count) => {
      if (generation !== this.generation) throw abortError();
      if (count <= 0) return;
      if (this.#activePayloadCount() + count > this.maxFeatures) {
        failure = tileReferenceBudgetError(this.maxFeatures);
        controller.abort(failure);
        throw failure;
      }
      requestPayloadCount += count;
      this.activePayloadCounts.set(requestId, requestPayloadCount);
    };

    const publishProgress = (tile, cached) => {
      const partial = snapshotResult(plan, featureById, counts, {
        ...firstMetadata,
        tile_key: tile.key,
        cached,
        pending_page_count: pendingPageCount,
      });
      onProgress?.(partial);
    };

    const publishPageProgress = (tile, tilePageCount) => {
      onProgress?.(snapshotResult(plan, featureById, counts, {
        ...firstMetadata,
        tile_key: tile.key,
        pending_page_count: pendingPageCount,
        pending_tile_page_count: tilePageCount,
      }));
    };

    const loadTile = async (tile, cacheKey) => {
      throwIfAborted(controller.signal);
      if (generation !== this.generation) throw abortError();
      let entry = this.#readCache(cacheKey);
      const cached = Boolean(entry);
      let tilePageCount = 0;
      if (!entry) {
        let pageReserved = 0;
        const loaded = await this.#withConcurrency(
          () => this.layerLoader(layerId, {
            bbox: tile.bbox4326,
            viewBbox: plan.viewBbox4326,
            tileKey: tile.key,
            databaseName: context.databaseName,
            detectId: context.detectId,
            fitId: context.fitId,
            refineId: context.refineId,
            limit: context.limit,
            maxFeatures: this.maxFeatures,
            onPage: (pageFeatureCount) => {
              throwIfAborted(controller.signal);
              reservePayload(pageFeatureCount);
              pageReserved += pageFeatureCount;
              tilePageCount += 1;
              pendingPageCount += 1;
              publishPageProgress(tile, tilePageCount);
            },
            signal: controller.signal,
          }),
          controller.signal,
        );
        throwIfAborted(controller.signal);
        if (generation !== this.generation) throw abortError();
        entry = {
          featureCollection: loaded.featureCollection,
          metadata: loaded.metadata || {},
          featureCount: loaded.featureCollection.features.length,
        };
        reservePayload(entry.featureCount - pageReserved);
        stagedTiles.push({ cacheKey, layerId, entry });
      } else {
        reservePayload(entry.featureCount);
      }

      if (!Object.keys(firstMetadata).length) firstMetadata = cleanPageMetadata(entry.metadata);
      for (const feature of entry.featureCollection.features) {
        const featureId = stableFeatureId(feature);
        if (featureId === null) {
          throw new Error(`图层 ${layerId} 的瓦片 ${tile.key} 返回了没有稳定 ID 的要素。`);
        }
        if (featureById.has(featureId)) continue;
        featureById.set(featureId, feature);
      }

      counts.loadedTileCount += 1;
      const completedPageCount = entry.metadata.loaded_page_count || tilePageCount;
      counts.loadedPageCount += completedPageCount;
      pendingPageCount = Math.max(0, pendingPageCount - tilePageCount);
      publishProgress(tile, cached);
    };

    try {
      await Promise.all(plan.tiles.map((tile, index) => loadTile(tile, cacheKeys[index]).catch((error) => {
        if (!failure) failure = error;
        if (!controller.signal.aborted) controller.abort(error);
        throw error;
      })));
      throwIfAborted(controller.signal);
      if (generation !== this.generation) throw abortError();
      if (generation === this.generation && !signal?.aborted) {
        this.#storeTiles(stagedTiles);
      }

      return snapshotResult(plan, featureById, counts, firstMetadata);
    } catch (error) {
      throw failure || error;
    } finally {
      signal?.removeEventListener('abort', relayAbort);
      controller.signal.removeEventListener('abort', releaseActive);
      releaseTileProtection();
      releaseFeatureBudget();
    }
  }

  #activePayloadCount() {
    let count = 0;
    for (const payloadCount of this.activePayloadCounts.values()) count += payloadCount;
    return count;
  }

  #readCache(key) {
    const entry = this.cache.get(key);
    if (!entry) return null;
    this.cache.delete(key);
    this.cache.set(key, entry);
    return entry;
  }

  #storeTiles(stagedTiles) {
    for (const item of stagedTiles) {
      if (this.cache.has(item.cacheKey)) continue;
      if (item.entry.featureCount > this.maxFeatures) continue;

      while (this.cache.size >= this.maxCachedTiles
        || this.cacheFeatureCount + item.entry.featureCount > this.maxFeatures) {
        const candidate = [...this.cache.keys()].find((key) => !this.activeTileCounts.has(key));
        if (candidate === undefined) break;
        const removed = this.cache.get(candidate);
        this.cache.delete(candidate);
        this.cacheFeatureCount -= removed.featureCount;
      }
      if (this.cache.size >= this.maxCachedTiles
        || this.cacheFeatureCount + item.entry.featureCount > this.maxFeatures) continue;

      this.cache.set(item.cacheKey, item.entry);
      this.cacheFeatureCount += item.entry.featureCount;
    }
  }

  async #withConcurrency(callback, signal) {
    const release = await this.#acquire(signal);
    try {
      return await callback();
    } finally {
      release();
    }
  }

  #acquire(signal) {
    throwIfAborted(signal);
    if (this.activeRequests < this.maxConcurrent) {
      this.activeRequests += 1;
      return Promise.resolve(this.#releaseHandle());
    }

    return new Promise((resolve, reject) => {
      const waiter = {
        resolve,
        reject,
        signal,
        onAbort: null,
      };
      waiter.onAbort = () => {
        this.waiters = this.waiters.filter((item) => item !== waiter);
        reject(signal.reason instanceof Error ? signal.reason : abortError());
      };
      signal?.addEventListener('abort', waiter.onAbort, { once: true });
      if (signal?.aborted) {
        waiter.onAbort();
        return;
      }
      this.waiters.push(waiter);
    });
  }

  #releaseHandle() {
    let released = false;
    return () => {
      if (released) return;
      released = true;
      while (this.waiters.length) {
        const waiter = this.waiters.shift();
        waiter.signal?.removeEventListener('abort', waiter.onAbort);
        if (waiter.signal?.aborted) {
          waiter.reject(waiter.signal.reason instanceof Error ? waiter.signal.reason : abortError());
          continue;
        }
        waiter.resolve(this.#releaseHandle());
        return;
      }
      this.activeRequests -= 1;
    };
  }
}
