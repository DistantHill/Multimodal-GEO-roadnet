import assert from 'node:assert/strict';
import { test } from 'node:test';
import { selectFeaturesInExtent, replayFocusFeatures, replayEventSelection } from '../src/selection.js';

test('Replay auto-fit excludes grey context segments and preserves legacy results', () => {
  const high = { get: () => 'highlight' };
  const grey = { get: () => 'context' };
  const legacy = { get: () => undefined };
  assert.deepEqual(replayFocusFeatures([grey, high]), [high]);
  assert.deepEqual(replayFocusFeatures([grey]), []);
  assert.deepEqual(replayFocusFeatures([legacy]), [legacy]);
});

test('box selection visits every intersecting feature without early termination', () => {
  const features = [{ id: 1, x: 1 }, { id: 2, x: 2 }, { id: 3, x: 3 }, { id: 4, x: 9 }];
  const source = {
    forEachFeatureIntersectingExtent(extent, callback) {
      for (const feature of features) {
        if (feature.x >= extent[0] && feature.x <= extent[2] && callback(feature)) break;
      }
    },
  };
  const selected = [];
  selectFeaturesInExtent(source, [0, 0, 4, 4], selected);
  assert.deepEqual(selected.map((feature) => feature.id), [1, 2, 3]);
});

test('event selections preserve entity identity instead of using source keys', () => {
  const feature = (layerId, id) => ({ get: (key) => ({ _layerId: layerId, id })[key], getId: () => id });
  assert.deepEqual(replayEventSelection([feature('tracks_cross', 7)]), { tracksCrossIds: [7] });
  assert.deepEqual(replayEventSelection([feature('turn_event_geom', 8)]), { turnEventIds: [8] });
  assert.equal(replayEventSelection([feature('tracks_cross', 7), feature('turn_event_geom', 8)]), null);
  assert.equal(replayEventSelection([feature('tracks_cross', -1)]), null);
});
