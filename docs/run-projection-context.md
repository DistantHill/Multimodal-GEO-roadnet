# Run 级投影上下文

Pipeline 以 `geometry(..., 4326)` 作为地理坐标真值，并在每个 Clean Run 上保存唯一的计算坐标上下文：

```text
clean_runs.source_srid
clean_runs.processing_srid
clean_runs.projection_config
```

`projection_config.coordinate_semantics_version=2` 表示该 Run 的所有派生 x/y 已按固定 CRS 生成。读取端要求此标记，避免仅回填 `processing_srid` 就误解锁仍为 local-origin 的旧数据。数据库触发器会冻结已经标记为 v2 的投影字段；legacy Run 仍保留一次 `NULL → v2` 的事务性迁移通道。

Detect Run 通过 `clean_id` 继承，Fit Run 再通过 `detect_id` 继承。派生表不重复保存 CRS，也不持久化 `geom_projected`。`trajectory_points`、事件点、交叉口 Polygon 和中心线 LineString 均继续输出 4326 Geometry；算法半径、距离、聚类、Clip 和 Fitting 则统一使用 `processing_srid` 下的米制 x/y。

## 配置

新 Clean Run 必须显式选择投影，不能从首点、数据中心或旧 `origin_lon/origin_lat` 猜测：

```sh
python -m pipeline run --csv /data/input.csv --processing-srid 32650
```

也可使用区域配置文件：

```toml
[projection]
source_srid = 4326
processing_srid = 32650
enforce_area_of_use = true
max_scale_error = 0.01
```

运行时会检查 EPSG 是否存在、目标 CRS 是否为投影坐标、线性单位是否为米、数据范围是否落在 area of use 内，以及投影尺度误差是否超过阈值。EPSG:3857 虽以米为单位，但在中纬度地区尺度畸变较大，默认阈值下会被拒绝。跨城市、跨 UTM Zone 的数据应选择经验证的区域 CRS；未来需要一个 Run 内多 CRS 时，再引入独立 `spatial_contexts` 分区模型。

数据库窗口查询先将米制窗口边界加密后反投影为保守的 4326 bbox，复用现有原始 Geometry GiST 做粗筛；随后通过 `ST_Transform` 返回固定 CRS 的 x/y，并在 processing CRS 中执行精确窗口判断。当前不假设某个仓库级固定 CRS；若性能分析证明转换仍是热点，可为常用固定转换增加与查询表达式完全匹配的函数 GiST 索引。`ST_SetSRID` 仅用于给已知坐标标注 CRS，不能代替坐标变换。

参考：[PostGIS ST_Transform](https://postgis.net/docs/manual-dev/en/ST_Transform.html)、[Geometry/Geography FAQ](https://postgis.net/documentation/faq/geometry-or-geography/)。

## 表存储策略

- `trajectory_points`、`excluded_points`、`injected_points`：保留一份 4326 Point。
- `kept_trajectory_points`：继续只保存成员引用。
- `injected_points`、`tracks_cross`、`turn_events`、candidate crossing：已有 x/y 的语义统一为 Run 的 processing CRS。
- candidate/intersection Polygon：保留 4326 输出；圆从 processing CRS buffer 后反投影生成。
- `centerlines`：保留 4326 LineString 与 `length_m`；拟合只在内存使用 processing CRS。
- 只有性能分析证明固定转换是热点，且区域长期固定时，才考虑投影表达式 GiST 索引或单表 `geom_projected`。

## 旧数据迁移

这是分阶段迁移。旧 Clean Run 自动得到 `source_srid=4326`、`processing_srid=NULL`，明确表示其 x/y 仍是 legacy/未知语义。当前阶段：

1. 新 Run 只写固定 CRS x/y；逐行 `origin_*` 改为可空且新数据写 `NULL`。
2. 同一份 Clean 输入分别跑 legacy 基线和 fixed-CRS Detect，比较候选 source refs、cluster membership、Clip 序列、半径、长度和 Fitting 输出。
3. 运维人员提供显式 `clean_id -> processing_srid` 映射后，在同一事务内按 Run 从 4326 Geometry 重新计算全部旧 x/y，最后同时设置 `processing_srid` 和 `coordinate_semantics_version=2`；不得从旧 x/y 或大量为零的 origin 回推。
4. 确认不存在“有派生 x/y 但 processing_srid 为 NULL”的 Run 后，再单独执行删除 `origin_lon/origin_lat` 的收尾迁移。

因此，读取 legacy Run 时会明确失败，不会静默退回首点局部投影，也不会把旧坐标误标成新的 EPSG。
