import assert from 'node:assert/strict';
import { test } from 'node:test';
import { partitionResultLayers, resultPropertyEntries } from '../src/result-layers.js';

const metadata = { replay_layers: [
  { id: 'turn_events', geometry_type: 'Point' },
  { id: 'cross_points', geometry_type: 'Point' },
  { id: 'segments', geometry_type: 'LineString' },
  { id: 'subtracks', geometry_type: 'LineString' },
] };
const feature = (id, replayLayer, geometryType, properties = {}) => ({
  type: 'Feature', id,
  geometry: { type: geometryType, coordinates: geometryType === 'Point' ? [108, 34] : [[108, 34], [109, 35]] },
  properties: { replay_layer: replayLayer, ...properties },
});
const collection = (...features) => ({ type: 'FeatureCollection', features });

test('intersection Replay separates same-geometry event and track layers and retains source identities', () => {
  const events = [{ kind: 'turn_events', id: 7, intersection_ids: [1, 2] }];
  const turn = feature('turn_events:7', 'turn_events', 'Point', { event_id: 7, intersection_ids: [1, 2] });
  const cross = feature('cross_points:8', 'cross_points', 'Point', { event_id: 8 });
  const segment = feature('s:1', 'segments', 'LineString', { events, source_row_keys: ['17397855', '17397856'] });
  const track = feature('subtrack:2120901165585716214:127148', 'subtracks', 'LineString', {
    vehicle_id: '2120901165585716214', subtrack_index: 127148, events,
  });
  const groups = partitionResultLayers(collection(track, cross, segment, turn), metadata);
  assert.deepEqual(groups.map((group) => group.name), [
    'tmp.turn_events', 'tmp.cross_points', 'tmp.segments', 'tmp.subtracks',
  ]);
  assert.deepEqual(groups.map((group) => group.featureCollection.features), [[turn], [cross], [segment], [track]]);
  assert.equal(groups[2].featureCollection.features[0].properties.events, events);
  assert.ok(groups[3].zIndex < groups[2].zIndex);
  assert.ok(groups[2].zIndex < groups[0].zIndex);
});

test('empty intersections retain all declared layers', () => {
  const groups = partitionResultLayers(collection(), metadata);
  assert.equal(groups.length, 4);
  assert.ok(groups.every((group) => group.featureCollection.features.length === 0));
  assert.deepEqual(groups.map((group) => group.geometryType), ['Point', 'Point', 'LineString', 'LineString']);
});

test('plain Replay and Clip retain all geometries and context properties in a single result layer', () => {
  const legacy = collection(
    feature(1, undefined, 'Point'),
    feature(2, undefined, 'LineString', { replay_role: 'context' }),
  );
  const [group] = partitionResultLayers(legacy);
  assert.equal(group.id, 'tmp.result');
  assert.equal(group.featureCollection, legacy);
});

test('reject malformed layer definitions before exposing partial results', () => {
  for (const definitions of [null, [], [{}], [null], [{ id: '__proto__', geometry_type: 'Point' }],
    [metadata.replay_layers[0], metadata.replay_layers[0]],
    [{ id: 'subtracks', geometry_type: 'Point' }]]) {
    assert.throws(() => partitionResultLayers(collection(), { replay_layers: definitions }));
  }
  assert.throws(() => partitionResultLayers({ type: 'Feature', features: [] }));
});

test('reject unassigned and mismatched geometries without mutating the payload', () => {
  for (const wrong of [feature(1, undefined, 'Point'), feature(2, 'segments', 'Point')]) {
    const payload = collection(feature(3, 'turn_events', 'Point'), wrong);
    const previous = structuredClone(payload);
    assert.throws(() => partitionResultLayers(payload, metadata));
    assert.deepEqual(payload, previous);
  }
});

test('attribute inspection preserves source arrays, relations and large vehicle IDs', () => {
  assert.deepEqual(resultPropertyEntries({
    geometry: {}, _layerId: 'tmp.segments', vehicle_id: '2120901165585716214',
    resolved_point_ids: [2280518, 2280519], source_row_keys: ['17397855', '17397856'],
    events: [{ kind: 'turn_events', id: 7, intersection_ids: [1, 2] }], crossing_id: null,
  }), [
    ['vehicle_id', '2120901165585716214'], ['resolved_point_ids', '[2280518,2280519]'],
    ['source_row_keys', '["17397855","17397856"]'],
    ['events', '[{"kind":"turn_events","id":7,"intersection_ids":[1,2]}]'], ['crossing_id', 'null'],
  ]);
});
