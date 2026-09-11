import assert from 'node:assert/strict';
import { test } from 'node:test';
import { selectFeaturesInExtent, replayFocusFeatures } from '../src/selection.js';

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
