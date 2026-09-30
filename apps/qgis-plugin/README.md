# Roadnet Tools for QGIS 3.44 LTR

Roadnet Tools sends the current trajectory, event, or intersection selection to the Roadnet Backend and
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

Choose **Browse databases** in the Roadnet Tools toolbar/menu to open a two-pane
catalog browser. Select a database on the left to list its user tables, schema
names and **Rows** on the right. Plain numbers are estimates; a trailing `*`
marks an exact COUNT result, as explained below the table. Unknown or estimated counts
below 50,000 trigger `COUNT(*)` and are labeled exact (for that query snapshot).
Counts at or above 50,000 retain PostgreSQL `reltuples` estimates. If COUNT fails
or times out, the estimate or “未知” is retained with an explanatory tooltip.
Exact partition-parent counts include children; parent estimates may not.
Click the Table name or Rows header to toggle ascending/descending sorting.
Rows sorts numerically, with unknown values first ascending and last descending.
System schemas and template
databases are excluded. Browsing does not require Roadnet the current Pipeline schema, change
the active map layer, or modify data. Connection errors appear in QGIS' message
bar. Use **刷新数据库** to reload; requests are asynchronous.

Catalog endpoints: `GET /api/v1/map/databases` and
`GET /api/v1/map/databases/tables?database_name=...`.

Replay/Clip requests now include `database_name`, read from the active PostgreSQL
layer's connection URI. Select a PostgreSQL layer with an explicit database name;
memory, file, and HTTP layers cannot establish this database identity and are
rejected locally. Only the database name is sent, never the connection password.
The Backend checks `pg_database` before spatial queries. If the database exists,
it opens a request-scoped, read-only connection to that database on the configured
PostgreSQL server, using Backend credentials, and verifies the current Pipeline schema. Concurrent
requests and the default database are unaffected. Responses identify the database
in `metadata.database_name`. A missing database returns HTTP 404 with
`detail.error=database_not_found`; incompatible schema returns 409; connection or
SQL errors return 503. Errors appear in the QGIS native message bar and retain the
previous `tmp` output. Existing Web requests may omit `database_name` and continue
using the configured Backend database. The Backend role must have CONNECT and
read permissions on each target database; no credentials or host from QGIS are used.

使用 QGIS 自带点选或框选工具选择 PostgreSQL 图层要素，然后执行 Replay 或 Clip：

- 交叉口图层 `intersections` 支持点或面几何。Replay 支持选择一个或多个交叉口，分别追溯构成事件后合并来源线段。Clip 仅接受同一 Detect 的两个交叉口，提取两口之间的片段。两者优先使用数据库 `id`，不把事件 ID 当成原始数据行键。
- 跨轨迹图层 `tracks_cross` 使用 `tracks_cross_ids`；转向图层 `turn_evnets_gemo` 或 `turn_events` 使用 `turn_event_ids`，均携带 `detect_id`。事件回放保留实际参与计算的注入点几何。
- 直接选择 `injected_points` 时，发送 `injected_point_ids` 与 `refine_id`，不需要 Detect。Backend 从注入点的前后 kept 锚点找到原始数据源 `geom`，突出生成注入点的来源线段。
- 普通轨迹点仍优先使用 `resolved_point_id` 与 `detect_id`，否则使用 `source_row_key`；原始数据图层允许以数据库 `id` 作为源行键。

点击 Replay 后自动加载带真实线几何的 `tmp` 图层，保留完整子轨迹：背景灰色、alpha=0.5，相关 segment 洋红色并绘制在背景之上。数组追溯属性保留在属性表中，无需再次执行展开操作。事件回放超过配置的车辆或点数上限时明确报错，不返回残缺子轨迹。

Requests run asynchronously and do not freeze the QGIS UI. Only the newest reply
for each operation is applied. A validation, network, HTTP, JSON, CRS, or geometry
error is shown in the QGIS message bar and leaves the previous successful `tmp`
result untouched. A successful empty FeatureCollection clears `tmp` and leaves an
empty in-memory layer, which is useful when the database has no matching
intersections.

## API contract

Replay request:

交叉口请求可使用 `{"intersection_ids": [101, 202]}`；单个 `intersection_id` 仍兼容。新增事件请求例如 `{"detect_id": "12345678-1234-5678-1234-567812345678", "tracks_cross_ids": [7]}`。注入点请求例如 `{"injected_point_ids": [70]}`，Backend 根据实体解析 Refine；QGIS 同时发送图层 `refine_id` 以核对运行归属。

每次请求只能使用一种选择身份，并携带 QGIS 图层的 `database_name`。查询均由 Backend 执行，不运行 Pipeline 批任务、不持久化结果。

```json
{
  "detect_id": "12345678-1234-5678-1234-567812345678",
  "resolved_point_ids": [101, 102]
}
```

For original/Refine source rows, the alternative request is
`{"source_row_keys": ["101", "external-key"]}`; `detect_id` is optional.

Replay is ID-driven only: bbox requests are rejected, including from Web clients.
Point IDs identify selected subtracks; only those tracks are hydrated through
their source foreign keys. Intersection Replay follows candidate/event endpoint
references. Replay does not load all Refine source rows or recompute a full-source
fingerprint; unrelated source changes are not validated by this request.

Point Replay returns individual source segments tagged `replay_role=highlight`
or `context`. Entry/exit points highlight their referenced refined segment;
ordinary points highlight adjacent segments. Other segments of the same selected
subtracks are grey context only. Web and QGIS automatically zoom to highlighted
segments, not the extent of the whole subtrack. Context still requires bounded
reading of the selected subtracks; no full Refine scan is performed.

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
