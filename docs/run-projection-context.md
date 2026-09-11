# Run 级投影上下文

Pipeline 以来源表的 `geometry(Point, 4326)` 为地理坐标真值，并在每个
Clean Run 上保存唯一的计算坐标上下文：

```text
clean_runs.source_srid       = 4326
clean_runs.processing_srid   = 显式选择的米制投影 CRS
clean_runs.projection_config = {"coordinate_semantics_version": 2, ...}
```

Detect Run 通过 `clean_id` 继承该上下文，Fit Run 再通过 `detect_id` 继承。
派生表不重复保存 CRS，也不持久化 `geom_projected`。算法半径、距离、聚类、
Detect 和 Fitting 统一使用 `processing_srid` 下的米制 x/y；对外 Geometry 使用
EPSG:4326。

## 配置

新 Clean Run 必须显式选择投影，不能从首点或数据中心猜测。例如：

```sh
python -m pipeline run \
  --source-table public.day01 \
  --processing-srid 32650
```

也可使用区域配置文件：

```toml
[projection]
source_srid = 4326
processing_srid = 32650
enforce_area_of_use = true
max_scale_error = 0.01
```

运行时会检查 EPSG 是否存在、目标 CRS 是否为投影坐标、线性单位是否为米、
数据范围是否落在 area of use 内，以及投影尺度误差是否超过阈值。EPSG:3857
虽以米为单位，但在中纬度地区尺度畸变较大，默认阈值下会被拒绝。跨城市或跨
UTM Zone 的数据应选择经验证的区域 CRS；若未来需要一个 Run 内使用多个 CRS，
应引入独立的空间上下文分区，而不是让同一列混用坐标语义。

数据库窗口查询把米制窗口边界加密后反投影为保守的 4326 bbox，利用来源表的
Geometry GiST 做粗筛，再用 `ST_Transform` 返回固定 CRS 的 x/y 并完成精确判断。
`ST_SetSRID` 只能标注已有坐标的 CRS，不能代替坐标变换。

参考：[PostGIS ST_Transform](https://postgis.net/docs/manual-dev/en/ST_Transform.html)、
[Geometry/Geography FAQ](https://postgis.net/documentation/faq/geometry-or-geography/)。

## v3 存储边界

- 外部来源表保存原始点及 4326 Geometry；Pipeline 通过
  `clean_runs.source_table` 和 `source_row_key` 回查，不逐点复制来源坐标。
- `kept_trajectory_points`、`excluded_points` 只表达某次 Clean 的成员关系；
  `kept` 的内部 ID 可被 resolved 行引用。
- `injected_points` 保存插值点的 4326 坐标及 processing CRS 的 x/y。
- `resolved_subtrack_points` 只物化 p/i/c 的稳定身份和 cleaned/final 两套顺序，
  不复制坐标或 vehicle；Clip 查询从被引用实体取得这些值。
- `tracks_cross` 和 candidate crossing 保存 Detect 已计算出的 x/y 与 lon/lat；
  `turn_events` 通过 resolved point 回溯坐标，不重复存储。
- candidate/intersection Polygon 和 `centerlines` LineString 保存 4326 Geometry；
  `centerlines.length_m` 是明确持久化的米制结果。
- Clip 不再计算 segment-circle root，只读取最终 resolved 序列；完整交叉口圆仅用于
  初始化 active set。

## Schema v3 切换

v3 是破坏性切换，不提供 v1/v2 原地迁移，也不接受未标记的旧 Pipeline 表。
初始化前必须核对实际 database、user 与 schema，然后显式全量 reset：

```sh
python -m pipeline init-db --database roadnet --reset
```

reset 只创建 schema version 3。Pipeline 与 Backend 启动时会校验版本，不匹配
立即失败。旧 Run 若需保留，必须在 reset 前独立备份；v3 不会把旧 local-origin
x/y 猜测或重标为新的 EPSG。
