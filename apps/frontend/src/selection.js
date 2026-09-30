// OpenLayers stops extent traversal when the callback returns a truthy value.
export function selectFeaturesInExtent(source, extent, collection) {
  source.forEachFeatureIntersectingExtent(extent, (feature) => {
    collection.push(feature);
  });
}

export function replayFocusFeatures(features) {
  return features.filter((feature) => feature.get('replay_role') !== 'context');
}

// Event IDs belong to event tables, never to the trajectory source-key domain.
export function replayEventSelection(features) {
  if (!features.length) return null;
  const layerId = features[0].get('_layerId');
  const field = { tracks_cross: 'tracksCrossIds', turn_event_geom: 'turnEventIds' }[layerId];
  if (!field || features.some((feature) => feature.get('_layerId') !== layerId)) return null;
  const ids = features.map((feature) => Number(feature.get('id') ?? feature.getId()));
  if (ids.length > 1000 || ids.some((id) => !Number.isSafeInteger(id) || id < 1)) return null;
  return { [field]: [...new Set(ids)] };
}
