// OpenLayers stops extent traversal when the callback returns a truthy value.
export function selectFeaturesInExtent(source, extent, collection) {
  source.forEachFeatureIntersectingExtent(extent, (feature) => {
    collection.push(feature);
  });
}

export function replayFocusFeatures(features) {
  return features.filter((feature) => feature.get('replay_role') !== 'context');
}
