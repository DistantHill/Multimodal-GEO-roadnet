# 地图视口瓦片加载、分页与回放

## 视口区域加载

Web UI 按 OpenLayers 当前 EPSG:3857 视口轴对齐包围盒（AABB）规划 XYZ tile，
将视口和 tile 包围盒转换为 EPSG:4326。移动、缩放和地图尺寸变化会立即取消旧区域请求，
防抖 180 ms 后重新加载；不会为了初始定位请求全库。业务图层仅在 zoom >= 12 且
视口覆盖不超过 64 个 tile 时加载，否则明确提示放大。

每个请求的 `bbox` 是 tile AABB，`view_bbox` 是当前视口 AABB，`tile_key` 为 `z/x/y`；
同时固定 database、Refine、Detect 和需要的 Fit 上下文。Backend 在 SQL 中按 tile
包围盒和 `ST_Intersects` 过滤，不在读取全量结果后用 Python 筛选。显式 tile 请求
缺少 `bbox` 会报错；空区域正常返回空结果，不降级为无 `bbox` 查询。

所有图层共享最多 4 个并发请求；每个 tile 内仍按下述分页规则读至末页。
缓存键包含完整数据上下文、图层及 `z/x/y`；共享缓存最多 64 个 tile、100,000 个
feature，采用最近最少使用（LRU）淘汰。当前绘制的业务图层合计也最多 100,000 个
feature；每个 tile 的逐页读取同样受预算保护。视口内跨 tile 的相同稳定 feature ID 只绘制一份，
绘制结果由当前 tile 集合合并产生，不通过删单 tile 时删除共有 feature。
取消的旧请求不更新缓存或图层；上下文切换及手动刷新清理缓存。

图层计数仅表示当前视口覆盖 tile 的已加载去重要素数，不是全库总数，也不是精确裁剪
视口矩形后的数量（边界 tile 会略超出视口）。加载中显示 tile 进度，失败保留明确状态，
用户可点击刷新重试；超过区域 feature 预算会要求缩小区域/放大地图，不能静默截断。
隐藏图层暂停区域请求。Replay 与 Clip 的 ID 选择和各自规则不变。

## 地图图层分页

`GET /api/v1/map/layers/{layer_id}` 每次最多返回一个有界页面。`limit` 默认采用
Backend 配置 `map.layer_limit_default`，最大值受 `map.layer_limit_max` 限制；两项配置
定义单页大小，不再表示整个图层的最大结果数。`offset` 是非负整数，默认值为 `0`。

服务按稳定顺序查询 `limit + 1` 条记录，用额外一条判断是否有后续页。页面的
`metadata` 至少包含：

| 字段 | 含义 |
| --- | --- |
| `count` | 当前页面返回的要素数 |
| `has_more` | 是否还有后续页面 |
| `next_cursor` | 有后续页面时的下一 `offset`；末页为 `null` |
| `truncated` | 与 `has_more` 相同，保留旧客户端兼容 |

客户端从 `offset=0` 开始读取；当 `has_more` 为真时，使用 `next_cursor` 请求下一页，
直到它为假。页面声明 `has_more=true` 却缺少严格前进的 `next_cursor`，或空页面仍声明
有后续结果，属于无效分页响应，客户端应报错而不能将已读页面当作完整图层。

前端 `loadMapLayer` 自动读取一个请求区域的所有页面，并在浏览器内存中合并为 FeatureCollection。最终 metadata
中的 `count` 是累计要素数，`loaded_page_count` 是读取页面数，`has_more`、`truncated` 为
假，`next_cursor` 为 `null`。分页限制单次响应大小，不限制完整加载后浏览器持有的图层总大小。
Web UI 将该能力限制在单个 tile 内，不再对无 `bbox` 的全运行数据自动完整加载。
若首页面由 Backend 解析出 `detect_id`、`fit_id` 或
`refine_id`，后续页面会固定使用这些运行上下文，避免“最新已完成运行”在分页过程中变化。

各图层的页面顺序由唯一键稳定打破并列：解析点按子轨迹、序列和 resolved point ID；
交叉口与中心线按数据库 ID；进出点按子轨迹、序列和 crossing ID。分页读取面向已完成且
不再变化的 Run 数据。

### 解析点分页查询与空间索引

解析点图层先按上述顺序选出当前页及用于判断末页的额外一条记录，再查询这些记录的
来源属性与所属子轨迹的车辆身份。带 `bbox` 时，保留点、注入点与穿越点分别按各自
持久化来源几何过滤，合并后分页。车辆身份仍由当前页涉及的完整子轨迹计算，车辆锚点
可以位于页面或包围盒之外；不会把页内第一个点当成车辆身份来源。

转向事件表 `turn_events_geom` 和中心线表 `centerlines` 的 `geom` 均在初始化定义中
增加 GiST 索引。新库初始化及已有 schema v9 数据库的非 reset `init-db` 流程都会
通过 `CREATE INDEX IF NOT EXISTS` 补建，重复执行不改变数据；本次不变更 schema 版本。
索引增加存储和 Pipeline 写入维护成本。在线业务库尚未执行这两项索引变更；已有库需
在允许建索引的维护时段执行初始化流程，普通建索引会等待并阻塞同表写入。

2026-10-05 使用 PostgreSQL `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 验证：

| 场景 | 改动前 | 改动后 | 验证范围 |
| --- | --- | --- | --- |
| 无包围盒首页，读取 1001 条 | 5793.631 ms | 92.282 ms | 交替重复三次的执行时间中位数，结果逐行一致 |
| 小视口，锚点经纬度各 ±0.02°，读取 1001 条 | 超过 12 秒超时 | 3185.513 ms | 单次计划；旧查询超时，未做该实际视口的逐行对比 |
| 较大视口，锚点经纬度各 ±0.2° | 超过 12 秒超时 | 超过 12 秒超时 | 空间候选扫描及排序仍是限制 |
| 转向事件临时副本，增加 GiST | 384.126 ms | 317.393 ms | 各三次中位数，1001 条结果一致 |
| 中心线临时副本，增加 GiST | 0.802 ms | 0.459 ms | 各三次中位数，9 条结果一致 |

分页样本来自 `roadnet_debug` 的 Detect `729e3579-3fb2-4af9-af43-fbb22fd8730d`，
包围盒锚点为 `(109.27434095410156, 34.630783354492195)`；无包围盒查询的共享块
命中与读取总数由每次 85820 降至 16467。转向事件副本来自同库，中心线副本来自
`roadnet_columnar_c12_1m_20260920`；索引对比在复制现有索引的事务内临时表上进行，
包围盒为各表首条几何内部点的经纬度各 ±0.01°，结束后回滚。上述执行时间包含当时
缓存和并发负载的影响，只代表这些查询样本，不是所有视口的响应时间保证。

回归覆盖三种点来源、零及负数来源键、包围盒边界、空页面、非零 `offset`、跨运行
隔离、页外车辆锚点以及 `has_more` 和 `next_cursor`。空间索引回归在已有数据的
临时表上重复执行两次定义，验证 GiST 有效且原始行不变。

本轮 `make test` 通过：Compose 19 项通过，Backend 145 项通过、5 项跳过，
Pipeline/CV 1309 项通过、80 项跳过。另外设置 `ROADNET_TEST_DSN` 后执行全部
Backend 测试及两项新增索引测试，152 项通过、零跳过。后者只写事务内临时表；
这些测试结果不代表 Pipeline 实际数据运行的冒烟或全量验收。

## 交叉口 Replay

`POST /api/v1/map/replay` 的 `intersection_ids` 可合并一个或多个交叉口各自构成点的回放：

```json
{
  "detect_id": "<Detect Run UUID>",
  "intersection_ids": [12, 34]
}
```

该字段必须包含一个或多个互不重复的正整数，并与 `intersection_id`、`source_row_keys`、
`resolved_point_ids` 互斥。选择数量受 `map.replay_selection_ids_max` 限制。
`detect_id` 可固定并验证所有交叉口所属的同一个 Detect Run。
Web UI 支持一个或多个同 Detect Run 的交叉口；混选交叉口与点时明确禁用。
只有 Clip 要求恰好两个交叉口。点 Replay 与原有单交叉口
`intersection_id` API 请求继续有效。

Backend 对所选交叉口分别执行既有交叉口 Replay，再按 resolved point feature ID 合并并
去重点要素。共享点只保留一份几何要素，但各个交叉口的来源事件都会保留；事件及
点属性记录交叉口 ID 和候选交叉口 ID。共享点的单数 `intersection_id` 与
`candidate_intersection_id` 置为 `null`，完整归属记录在复数字段及事件来源中。
去重后要素数超过 `max_points` 时，整个请求失败，
不会返回部分结果。成功响应 metadata 标记 `operation=replay`、
`selection=intersection_ids`、所选 `intersection_ids`、所属 `detect_id`、候选交叉口 ID、
`intersection_sources`（交叉口 ID 与候选交叉口 ID 的对应关系列表；无构成点时也保留映射）、
`returned_point_count` 和 `truncated=false`。

## 中心线 Replay

`POST /api/v1/map/replay` 支持 `centerline_ids`，与点及交叉口选择互斥。
请求必须指定 `fit_id`、`detect_id`，可指定 `refine_id`；Backend 验证完整运行及其
归属，未知中心线、跨运行选择、缺失或不完整的中心线清单（centerline manifests）均失败。
Web UI 只允许回放属于当前 Fit、Detect、Refine Run 的中心线。

查询从选中中心线的清单定位穿越点（crossing points），再读取同子轨迹起终序列闭区间
内的解析点；不使用邻近点、视口包围盒或完整 Detect 轨迹代替清单成员。
返回去重的 Point 及每条清单对应的来源片段 LineString，而不是将中心线顶点冒充轨迹点。
点按 `resolved_point_id` 合并，`centerline_ids`、`clip_sources` 保留全部归属。
片段 ID 为 `centerline:{id}:clip:{ordinal}`，方向反转时保留持久化起终点及方向标记。

`vehicle_id` 表示持久化 Refine 车辆身份，成员必须与同子轨迹锚点一致；
`source_vehicle_id` 单独表示保留点当前源行的原始车辆字段，允许原样返回空值。
缺失源行或坐标仍使请求失败。旧 Run 的持久化身份
可能带有分段后缀，不能要求它与原始字段相等，也不通过剥后缀猜测映射。
车辆预算统计持久化身份，metadata 以
`vehicle_identity_semantics=persisted_refine_vehicle_id` 明示这一口径。

`max_points` 仅统计唯一解析点，`max_vehicles` 统计车辆数；来源片段数量受
`map.clip_feature_limit` 限制，片段与点的归属引用总量另受 `map.replay_max_points_max`
限制。共享点不会重复消耗唯一点预算，但会计入归属引用上限。选择数量受
`map.replay_selection_ids_max` 限制。任何超限或缺失成员均使整个请求失败，不返回部分结果。
响应分别报告 `returned_point_count`、`returned_clip_count`、`membership_reference_count`
与 `membership_reference_limit`，`truncated=false`；几何要素数量为点数与片段数之和。

本分支仅确认持久化成员身份与运行关联，坐标及属性读取当前源数据行，未运行整 Refine
来源指纹校验。因此明确返回 `source_fingerprint_verified=false` 和
`value_semantics=persisted_clip_members_with_current_source_values`，界面同时提示
“当前源值投影；未验证历史来源指纹”。不能将结果解释为已认证的历史拟合输入值还原。
已有 Clip 和 Pipeline 来源指纹检查保持不变。
