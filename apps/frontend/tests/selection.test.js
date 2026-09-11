import assert from 'node:assert/strict';
import { test } from 'node:test';
import { selectFeaturesInExtent } from '../src/selection.js';

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
