const replayLayers = {
  turn_events: { role: 'point', label: '转向事件', zIndex: 3 },
  cross_points: { role: 'intersection', label: '穿越事件', zIndex: 3 },
  segments: { role: 'tmp', label: '相关来源段 · 洋红色', zIndex: 2 },
  subtracks: { role: 'context', label: '完整子轨迹 · 灰色 50%', zIndex: 1 },
};

// Validate and partition before replacing any visible result.
export function partitionResultLayers(featureCollection, metadata = {}) {
  if (featureCollection?.type !== 'FeatureCollection' || !Array.isArray(featureCollection.features)) {
    throw new Error('临时结果必须是 GeoJSON FeatureCollection');
  }
  const definitions = metadata.replay_layers;
  if (definitions === undefined) {
    return [{
      id: 'tmp.result', name: 'tmp.result', role: 'tmp', label: '临时结果', zIndex: 2,
      featureCollection,
    }];
  }
  if (!Array.isArray(definitions) || !definitions.length) {
    throw new Error('Replay 图层定义为空或格式无效');
  }
  const groups = new Map();
  for (const definition of definitions) {
    const style = replayLayers[definition?.id];
    const expectedGeometry = ['turn_events', 'cross_points'].includes(definition?.id) ? 'Point' : 'LineString';
    if (!Object.hasOwn(replayLayers, definition?.id) || groups.has(definition.id)
      || definition.geometry_type !== expectedGeometry) {
      throw new Error('Replay 图层名称、几何类型或重复定义无效');
    }
    groups.set(definition.id, {
      ...style, id: `tmp.${definition.id}`, name: `tmp.${definition.id}`,
      geometryType: definition.geometry_type,
      featureCollection: { type: 'FeatureCollection', features: [] },
    });
  }
  for (const feature of featureCollection.features) {
    const group = groups.get(feature?.properties?.replay_layer);
    if (!group || feature.type !== 'Feature' || feature.geometry?.type !== group.geometryType) {
      throw new Error('Replay 要素缺少有效的图层标记或几何类型不符');
    }
    group.featureCollection.features.push(feature);
  }
  return Array.from(groups.values());
}

export function resultPropertyEntries(properties) {
  return Object.entries(properties)
    .filter(([key]) => key !== 'geometry' && !key.startsWith('_'))
    .map(([key, value]) => [key, value === null ? 'null'
      : typeof value === 'object' ? JSON.stringify(value) : String(value)]);
}
