# 地图视口瓦片加载、分页与交叉口回放

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
