# Roadnet Tools for QGIS 3.44 LTR

Roadnet Tools sends the current point-layer selection to the Roadnet Backend and
draws the returned GeoJSON without writing an intermediate file. Every successful
response replaces the plugin-owned memory output in a `tmp` group. Its primary
visible layer is named exactly `tmp`; mixed-geometry responses add secondary
`tmp — <GeometryType>` memory layers in the same group.

## Install

The installable plugin directory is `roadnet_tools/`. From the repository root:

```sh
cd apps/qgis-plugin
zip -r roadnet_tools.zip roadnet_tools \
  -x 'roadnet_tools/__pycache__/*' 'roadnet_tools/*.pyc'
```

In QGIS, open **Plugins → Manage and Install Plugins → Install from ZIP**, choose
`apps/qgis-plugin/roadnet_tools.zip`, then enable **Roadnet Tools**. This workflow
does not modify an existing plugin source checkout. QGIS 3.44 is the minimum
supported version; the plugin has no Python dependency beyond QGIS and PyQt.

Open **Vector → Roadnet Tools → Roadnet Tools settings** and set the Backend URL.
Defaults are:

- Backend URL: `http://localhost:8000`
- Replay path: `/api/v1/map/replay`
- Clip path: `/api/v1/map/clip`

The endpoint paths are configurable to support reverse proxies and future API
mounts. QGIS' shared network manager is used, so proxy and authentication settings
configured in QGIS continue to apply.

## Use

Replay/Clip requests now include `database_name`, read from the active PostgreSQL
layer's connection URI. Select a PostgreSQL layer with an explicit database name;
memory, file, and HTTP layers cannot establish this database identity and are
rejected locally. Only the database name is sent, never the connection password.
The Backend checks `pg_database` before spatial queries. If the database exists,
it opens a request-scoped, read-only connection to that database on the configured
PostgreSQL server, using Backend credentials, and verifies schema v3. Concurrent
requests and the default database are unaffected. Responses identify the database
in `metadata.database_name`. A missing database returns HTTP 404 with
`detail.error=database_not_found`; incompatible schema returns 409; connection or
SQL errors return 503. Errors appear in the QGIS native message bar and retain the
previous `tmp` output. Existing Web requests may omit `database_name` and continue
using the configured Backend database. The Backend role must have CONNECT and
read permissions on each target database; no credentials or host from QGIS are used.

Activate a point vector layer and use QGIS' normal point or rectangle selection.
Then choose one of the toolbar/menu actions:

- For intersection Replay, activate the PostgreSQL `intersections` layer (point
  or polygon), select exactly one feature, and choose **Replay selected points**.
  The plugin sends its database `id` as `intersection_id`, plus `database_name`
  and the feature's `detect_id` when available. The Backend resolves the
  intersection's candidate within that Detect Run, selects matching `turn_events`
  and `tracks_cross`, and calls Pipeline's `replay_points` to return constituent
  points into `tmp`. Crossing events include both endpoints of both cleaned
  segments; turn events include their referenced point. Points are deduplicated.
  This mode does not limit vehicles. Exceeding `max_points` (default 10,000,
  maximum 50,000) returns an error instead of silently returning partial data.
- **Replay selected points** prefers `resolved_point_id` and sends
  `resolved_point_ids` with one common `detect_id`. Otherwise it reads
  `source_row_key` (falling back to a raw layer's `id`/`database_id`), serializes
  it as text, and sends `source_row_keys`. One selection cannot mix the two
  identity domains.
- **Clip between intersections** requires exactly two selected points. It reads
  `intersection_id` (falling back to the QGIS feature ID), rejects duplicate IDs,
  and requires both features to have the same non-null `detect_id`.

Requests run asynchronously and do not freeze the QGIS UI. Only the newest reply
for each operation is applied. A validation, network, HTTP, JSON, CRS, or geometry
error is shown in the QGIS message bar and leaves the previous successful `tmp`
result untouched. A successful empty FeatureCollection clears `tmp` and leaves an
empty in-memory layer, which is useful when the database has no matching
intersections.

## API contract

Replay request:

For a selected intersection: `{"database_name": "roadnet", "intersection_id": 101}`.
The `intersection_id` selection cannot be combined with source keys, resolved
point IDs, or a bounding box. Backend images now include the Pipeline package
to reuse its point-replay helper; rebuild using the Compose configuration, which
supplies the `pipeline_source` build context. No batch Pipeline job is run in
the request handler and results are not persisted.

```json
{
  "detect_id": "12345678-1234-5678-1234-567812345678",
  "resolved_point_ids": [101, 102]
}
```

For original/Clean source rows, the alternative request is
`{"source_row_keys": ["101", "external-key"]}`; `detect_id` is optional.

Clip request:

```json
{
  "detect_id": "12345678-1234-5678-1234-567812345678",
  "intersection_ids": [101, 202]
}
```

`intersection_ids` contains PostgreSQL `intersections.id` values, not transient
Detect-stage cluster calculation indexes.

Both endpoints return an EPSG:4326 GeoJSON FeatureCollection. The plugin accepts
the FeatureCollection directly or an envelope of the following form:

```json
{
  "data": {"type": "FeatureCollection", "features": []},
  "crs": "EPSG:4326",
  "meta": {}
}
```

A missing CRS is treated as EPSG:4326, matching the backend contract. Named
GeoJSON CRS objects and common CRS84/URN forms are also recognized.

## Test without QGIS

The contract and package checks run with the Python standard library only:

```sh
python3 -m unittest discover -s apps/qgis-plugin/tests -v
python3 -m compileall -q apps/qgis-plugin/roadnet_tools
```

For a manual QGIS smoke test, load a point layer with the fields described above,
select features, run both actions, and verify that the request returns immediately
while the result appears under **tmp → tmp** with the project still unmodified on
disk.
