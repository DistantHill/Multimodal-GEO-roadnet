# Run 级投影上下文

Pipeline 以来源表的 `geometry(Point, 4326)` 为地理坐标真值，并在每个
Clean Run 上保存唯一的计算坐标上下文：

```text
clean_runs.source_srid       = 4326
clean_runs.processing_srid   = 显式选择的米制投影 CRS
clean_runs.projection_config = {"coordinate_semantics_version": 2, ...}
```

正式 DB Detect 必须提供 `clean_id`，并在读取 extent 或点之前从 completed
Clean Run 继承该上下文与 `source_table`。调用者不必重复传
`SpatialContext`；若传入，它只是必须相等的一致性断言。原始来源表直跑只属于
显式诊断/基准入口。Fit Run 再通过 `detect_id` 继承上下文。
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

## 双 CRS 窗口查询

Run CRS 与 query CRS 是两个独立上下文。算法、持久化 x/y 和查询返回的 x/y
始终使用 `clean_runs.processing_srid`；窗口构造和落窗判断使用单个标准
WGS84 / UTM query CRS（EPSG:32601..32660 或 32701..32760）。旧的 Run CRS
`SpatialWindow` 进入 SQL 前先反投影其中心，保留数值米制宽高，再在 query UTM
网格中重建，因此其地理轮廓可能改变。纯内存 `SpatialWindowIterator` 仍保持原
Run CRS 语义。

窗口按最终完整地理 footprint 选择实际进入的最东标准 UTM 带；若切带，则保持
地理中心、宽和高并重建，随后重新验证最终 footprint。恰好触及 UTM 或三度网格
界线不算进入东邻带，但闭区间精筛仍返回边界点。三度网格的中央经线为 `3*n`、
边界为 `3*n-1.5`（如 109.5/112.5/115.5/118.5）；一个窗口最多实际进入两个
相邻格。该网格仅限制跨度，不是自定义投影。

query 专用尺度误差上限是 1.5%，不会写入或放宽 Run 默认 1% 规则。查询允许跨
标准 UTM area-of-use 经度边界，但仍拒绝非有限值、非正尺寸、UTM 纬度域之外、
跨半球、反经线、超过三度网格跨度、尺度超限和无法有界收敛的请求。

SQL 先以 4326 Geometry 的保守 bbox 使用现有 GiST 粗筛，再把原始点和 injected
点各自的 Geometry 用 `ST_Transform` 转到同一 query CRS，以数值闭区间精筛。
原始点返回值另行转换到 Run CRS；injected 返回已经 Clean 身份约束的 Run x/y，
绝不把 query x/y 泄漏给算法。数值闭区间直接使用 QueryWindow 的原始 double
界限，不作 ULP 或 epsilon 外扩；名义上的 inverse-forward 边界不替代数据库中
`ST_Transform` 的实际数值判断。

保守 bbox 仅支持上述标准 UTM、单半球、非反经线局部窗口，并要求 footprint
相对所选中央经线不超过 15°且位于受限的 UTM 本地投影 sheet。逆 TM 在该域内关于
中央经线对称；经度沿 easting 单调，纬度沿 northing 单调，水平边唯一可能的
内部纬度极值位于 false easting 500000。因此实现检查四角和中央经线与上下边的
交点，再以 IEEE `nextafter` 向外扩两 ULP。分带和三度格判断使用未外扩的真实
范围，不能被粗筛 padding 改变。超出已证明支持域时会在 SQL 前失败，不会退化为
全世界 bbox、全表慢扫描或固定 densify 近似。`CoordinateTransformer.inverse_bounds`
也只为此受支持 UTM 域提供保守承诺；旧 `densify_pts` 参数明确拒绝。

`ST_SetSRID` 只能标注已有坐标的 CRS，不能代替坐标变换。

## 双轨 cross-track 验证

同 CRS 算法等价/性能门禁使用 dense reference 与 optimized 实现，但两者的每个
线段端点和交点都处于相同 processing CRS。身份、来源、成员和顺序严格相等；
x/y 使用绝对 `1e-6 m`、lon/lat 使用绝对 `1e-9 degree` 起始预算，并在报告中
记录实际最大差值。完整性能门禁仍要求至少 500k 点和至少三次重复：

```sh
python -m track.v2.manual_thin_benchmark.manual_bench_cross_track_inner \
  --processing-srid 32650 --repetitions 3 \
  --write-baseline cross_track_500k.json --require-target --version CODE_VERSION
```

该 benchmark 在第一次查询前开启 PostgreSQL `REPEATABLE READ READ ONLY` 事务，
extent、完整消费字段/Geometry 的输入哈希及全部重复均绑定同一数据库快照。多
subtrack 的历史四元稳定排序保留为前缀，仅在原先键冲突时追加 track rank
tie-break；因此只可能明确化旧有歧义项的顺序。

legacy → fixed CRS 是独立的可审计快照差异报告。坐标位移使用 WGS84 椭球距离；
候选、cluster、Clip 有序成员、Clip 长度和 fitting 顶点分别报告。快照未包含的
阶段明确为 `not_evaluated`。写报告不会审批或覆盖已有文件：

输入文件采用 `track.v2-projection-snapshot-v1`，并要求 `provenance.source`、
`provenance.algorithm` 和 `crs`；各已提供阶段还必须包含完整必需字段。NaN/Inf、
非法经纬度、负长度或半成品阶段会失败，而不会被报告成零差异。

```sh
python -m track.v2.manual_thin_benchmark.projection_difference_report report \
  --legacy legacy.json --fixed fixed.json --output projection-diff.json \
  --version "$(git rev-parse HEAD)"
```

首次或结构变化结果保持 `pending_review`。人工审阅后，必须使用单独命令生成绑定
具体 report hash 与 input-pair hash 的不可覆盖审批记录；本次修复不替用户执行：

```sh
python -m track.v2.manual_thin_benchmark.projection_difference_report accept \
  --report projection-diff.json --output projection-diff.approval.json \
  --accepted-by REVIEWER --reason REVIEW_REASON
python -m track.v2.manual_thin_benchmark.projection_difference_report verify \
  --report projection-diff.json --approval projection-diff.approval.json
```

报告只能评估快照实际提供的阶段；它不会从 schema v3 复活 v1/v2 数据，也不代表
对真实投影变化基线的自动接受。

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
