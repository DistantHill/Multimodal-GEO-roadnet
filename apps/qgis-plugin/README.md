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

Activate a point vector layer and use QGIS' normal point or rectangle selection.
Then choose one of the toolbar/menu actions:

- **Replay selected points** reads each selected feature's `id`, falling back to
  `database_id`, and sends `point_ids`. If every point has the same non-null
  `detect_id`, it is sent too.
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

```json
{
  "detect_id": "12345678-1234-5678-1234-567812345678",
  "point_ids": [101, 102]
}
```

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
