# 前端图层与 QGIS Replay / Clip 规格说明

术语遵循 [项目术语与写作规范](terminology.md) 。

状态：已确认
创建日期：2026-09-13
更新日期：2026-10-02
访谈进度：Q1–Q8 已回答；用户确认实施。

## 1. 背景与问题

复用原前端工作规格，扩展 QGIS 与 Backend 联动范围，旧规格保留在历史附录。
用户要求新增 cross_track、turn_event_geom 前端图层；修复双 intersection Clip 的点图层限制、Replay 的单选限制，以及 track_cross/injected_points 的 source_row_keys 404；修正转向事件回放，最终得到 segment。

2026-09-30 只读源码调查事实：

- QGIS Clip 入口只接受 Point；Replay 交叉口分支仅允许一个选择。普通 Replay 缺少 resolved_point_id 时用 source_row_key/id/database_id 作为源行键，误用了事件及注入点实体 ID。证据： /data/workspace/Multimodal-GEO-roadnet/apps/qgis-plugin/roadnet_tools/plugin.py 与 /data/workspace/Multimodal-GEO-roadnet/apps/qgis-plugin/roadnet_tools/core.py 。
- Backend 已支持 intersection_ids，逐交叉口合并构成点。普通 Replay 返回完整子轨迹拆分的线段，分配 highlight/context。证据： /data/workspace/Multimodal-GEO-roadnet/apps/backend/src/backend/replay/select_intersection_members.py 与 /data/workspace/Multimodal-GEO-roadnet/apps/backend/src/backend/replay/build_segment_features.py 。
- 实际事件表名为 tracks_cross、turn_evnets_gemo。前端动态读取 Backend 图层目录，后者尚未暴露两种事件层。证据： /data/workspace/Multimodal-GEO-roadnet/apps/backend/src/backend/stores/map_layers.py 。
- resolved_subtrack_points 无 source_row_keys 列；实际通过 kept_point_id 关联 kept_trajectory_points.source_row_key 再查源表；注入点与穿越点分别有自己的坐标来源。injected_points 只有 refine_id，无 detect_id。
- QGIS 当前输出代码已创建 LineString 几何图层；仅凭用户贴出的 WKT 属性记录不能判定其无几何。仍需核对实际加载与线段范围。

## 2. 目标与非目标

目标为用户列出的图层新增、选择修复、身份追溯与可绘制 segment 输出。本轮不自动扩展到算法重算、数据库表重命名。

## 3. 需求与使用场景

- Q1：Replay 是生成所选要素的来源追溯；多交叉口分别追溯并合并，两口之间的片段属于 Clip。
- Q2：保留完整子轨迹，背景灰色且 alpha 为 0.5；相关 segment 用洋红色突出。
- Q3：前端新增两层同时支持 Replay，保留既有“子轨迹背景与相关线段突出”的交互设计。
- Q4：通过 Backend 查询，将 tmp 回放结果展开成可绘制 segment layer；source_row_keys 是结果属性，不是用户声称的 resolved 表列。
- Q5：注入点不依赖 Detect，沿 kept_points 到 trajectory_points 追溯。代码事实：以 refine_id、subtrack_index、after_sequence_in_subtrack 关联 kept 序号 n 与 n+1，两端 source_row_key 关联原始数据源 geom；完整背景取同一子轨迹的 kept 点按序回查源几何。高亮 n→n+1 来源段的具体解释符合 Q5 的来源回放要求。
- Q6：跨轨迹及转向事件来源线段保留实际参与计算的 injected_points.geom；直接选择 injected_points 回放则按 Q5 追溯 kept 来源段。
- Q7：点击 Replay 自动展开结果并加载 tmp segment 线图层，无需第二次操作。
- 总体原则：回放到带有 geom 列的数据源。

## 4. 方案与关键决策

Q1–Q7 已采纳用户回答。原 Q2“仅相关线段”、Q5“选择 Detect”建议已被用户否定，不进入实现。按 Q7 自动展开，按 Q6 保留事件来源段中的注入点几何。多交叉口分别沿构成事件追溯并合并。Q8 已确认完整设计并授权实施。

## 5. 接口、数据与架构影响

涉及 Frontend 图层目录消费、QGIS 选择与几何加载、Backend 图层投影与 Replay。需区分事件 ID、注入点 ID、已解析点 ID、源行键，不能统一以 id 代替。按 Q4 由 Backend 执行。按 Q5 新增不依赖 Detect 的注入点来源解析，返回可绘制线几何。

## 6. 约束与非功能要求

遵循现有服务边界，运行数据不入仓库；保留已有无关工作区修改。本轮无新增性能指标、数据目录或迁移要求。

## 7. 验收标准

前端两层可加载、可 Replay；双交叉口 Clip/Replay 可执行；合法事件/注入点不因 ID 误用而 404；注入点无需 Detect；结果展开为来源 segment，完整子轨迹为灰色 alpha=0.5，相关段为洋红色。需验证 geom 来源及追溯身份。按 Q6 验证事件来源段保留 injected_points.geom，并与直接选择注入点时的 kept 来源回放区分；按 Q7 验证一次 Replay 自动加载线图层。自动验证结果与环境验收限制见第 8 节。

## 8. 实施与验证计划

实现已完成：

- Backend 增加 tracks_cross_ids、turn_event_ids、injected_point_ids 三种互斥身份；交叉口沿构成事件展开来源线段，事件精确选择来源段并保留完整子轨迹；直接注入点回放通过 kept 前后锚点读取源 geom。
- 新增 tracks_cross 与 turn_event_geom 图层投影，前端动态读取目录并使用事件身份 Replay；实际转向存储表仍为 turn_evnets_gemo。
- QGIS 放开交叉口面图层和多选 Replay，Clip 优先使用数据库 ID；修复 GeoJSON 几何加载。两端背景统一 #808080、alpha=0.5，来源段 #ff00ff；QGIS 使用符号层级将洋红段画在背景之上。
- 不修改数据库结构，不重跑 Pipeline 作业，不新增运行数据目录。

2026-09-30 验证记录：

| 检查 | 结果与范围 |
| --- | --- |
| make test | Compose 18 项通过；Backend 117 passed、3 skipped；Pipeline 1266 passed、76 skipped、12 个既有 rasterio 弃用警告 |
| Backend 全套，启用 ROADNET_TEST_DSN | 120 passed，无跳过；PostgreSQL 临时表验证后回滚，不修改业务表 |
| 新增 PostGIS 回归 | 验证跨轨迹精确高亮、含 crossing 的来源段、保留 injected 几何、多交叉口合并、无 Detect 的注入点来源回放、两个图层范围查询，以及源 geom 缺失时报错 |
| make frontend-test | 3 个测试文件通过，静态构建成功；含新增事件请求与选择身份回归 |
| make qgis-plugin-test | 30 项通过；实际 QGIS 运行时验证双选面交叉口 Replay/Clip 请求与线几何、分类颜色和透明度 |
| git diff --check | 通过 |

make test 的 Pipeline 实际配置为 /app/config/pipeline.toml ，对应仓库 services/pipeline/config/pipeline.toml 。这是完整 pytest 测试套件，不是全量数据 Pipeline 作业；未执行冒烟或全量数据运行。

环境限制：本机 QGIS 为 3.22.4，已验证可用的几何解析接口及无窗口运行行为；尚未在目标 QGIS 3.44 上验证。浏览器工具无可用浏览器，尝试 in-app browser 返回“Browser is not available: iab”，因此未完成真实浏览器图层及地图操作验收。以上自动验证不声称覆盖用户当前数据库与桌面会话。

## 9. 风险与待决事项

Q1–Q8 已回答并确认实施；浏览器端实际操作与 QGIS 3.44 目标版本仍需环境验收。用户要求保留完整子轨迹，不得按原 Q2 建议删除 context。

## 10. 确认记录

设计确认：Q8 用户明确回答“确认实施”。用户已确认实施；代码与自动验证已完成，环境验收限制见第 8 节。

## 2026-10-02 扩展：交叉口分层溯源

用户直接提出新功能，实施已获该请求授权，不启动新一轮访谈。原 0.2.0 的问答和测试记录保持其历史适用范围。

交叉口 Replay 遍历 `intersection → turn_events / tracks_cross → resolved 点与来源 segment → 完整 subtrack`，将各层保留到返回结果中。递归的终点是完整子轨迹，不发现或生成新的业务要素。

QGIS 的 `tmp` 组包含 `tmp.turn_events`、`tmp.cross_points`、`tmp.segments`、`tmp.subtracks`。其中 cross_points 对应产生交叉口的 tracks_cross 事件；完整子轨迹灰色 alpha=0.5，相关来源段洋红色，事件点另用蓝色及橙色。每个图层可独立开关。无该类事件时提供空图层。

Backend 继续返回 GeoJSON FeatureCollection，属性 replay_layer 标记输出层，metadata.replay_layers 声明图层及空层类型。事件层保留数据库身份和所选交叉口关系；线段、子轨迹属性 events 保留准确上游事件关系。共享事件、来源段和子轨迹去重，完整子轨迹保持原顺序及 injected 几何。普通点、单独事件与 Clip 的既有返回方式继续适用。

查询使用既有预算：超出车辆、点数或事件数上限时拒绝残缺结果；来源缺失时不替换上次输出。前端兼容分层标记，分别显示事件、子轨迹背景和来源段。

2026-10-02 验证完成：

- Backend 全套启用 PostgreSQL 临时表集成测试：122 passed；涵盖单交叉口转向分支、多交叉口共享事件去重、完整子轨迹及上游属性、中间事件 geom 缺失拒绝部分输出。
- QGIS：31 项通过；实际本机 QGIS 3.22.4 运行时验证相同几何类型按语义分层、四层命名、事件点可见样式、空图层与失败保留旧输出。
- 前端：3 个测试文件通过，静态构建成功；事件点样式兼容分层返回。
- make test：Compose 18 项通过；Backend 119 passed、3 skipped；Pipeline 1266 passed、76 skipped、12 个既有 rasterio 警告。Pipeline 使用 /app/config/pipeline.toml ，对应 services/pipeline/config/pipeline.toml ，仅执行 pytest 套件，无全量数据作业。
- git diff --check 通过。插件 0.3.0 安装包 ZIP 校验通过，并确认版本字段；文件位于 apps/qgis-plugin/qgis插件.zip ，属于 reid 且权限为 644。

环境限制延续上轮：未验证目标 QGIS 3.44 桌面及真实浏览器操作。上述后端验证使用临时表并回滚，未改业务数据。

### 前端界面同步

用户追加要求“前端界面也同步实现”。前端将交叉口 Replay 返回的四种语义要素分别加载为独立矢量图层，归入 `tmp` 组：`tmp.turn_events`、`tmp.cross_points`、`tmp.segments`、`tmp.subtracks`。支持展开、折叠、整组或单层显隐、按名称或中文说明筛选，并显示各层数量；无该类要素时保留声明的空图层。完整子轨迹为灰色 alpha=0.5，相关来源段为洋红色，事件点显示在来源段之上。

选择临时子图层后，可点选或框选查看来源属性，包括 detect_id、subtrack_index、resolved_point_ids、source_row_keys、events 和 intersection_ids 等返回字段。单选默认展开属性，多选逐项展开。临时结果不作为新 Replay 或 Clip 的输入；切回业务图层后恢复原操作。普通点、独立事件 Replay 与 Clip 的既有输出归入 `tmp.result`，保留原样式及几何。

新结果完成分层校验与几何解析后才替换旧结果。清除结果会移除所有临时子图层及其选择；刷新业务图层保留临时结果。总要素数仅统计各个实际图层，避免与组的汇总重复计数。

2026-10-02 验证记录：

- `make frontend-test`：4 个测试文件通过，新增 6 项分层及属性回归用例，静态构建通过；主模块语法检查通过。
- 当前开发前端容器构建通过；通过 HTTP 读取 main.js、result-layers.js、style.css，均返回 200 且内容与挂载源码一致。
- `make test`：Compose 18 项通过；Backend 119 passed、3 skipped；Pipeline 1266 passed、76 skipped、12 个既有 rasterio 警告。Pipeline 使用 /app/config/pipeline.toml ，对应 services/pipeline/config/pipeline.toml ，仅运行 pytest 套件。
- 当前浏览器工具未连接任何浏览器；本轮未完成真实浏览器点击与截图验收。

## 历史附录：原规格（2026-09-13）

以下原文仅记录当时状态，不替代本轮决定。

# 前端工作规格

状态：草稿，待补充本轮功能目标；尚未分派功能实现。

日期：2026-09-13。

## 需求与职责

用户原始要求：

> 前端工作在这里，你是主agent，写好spec文档，发任务给subagent，你负责验收

- 主 agent 负责需求整理、维护本规格、拆分任务、审查实现与独立验收。
- subagent 根据任务书完成具体实现，报告改动文件、验证结果和未解决事项。
- 本轮目标页面、预期行为、视觉要求和功能优先级：待用户补充。当前草稿不代表已确定功能范围。

## 仓库现状与边界

- 前端位于 `apps/frontend/`，使用 ESM JavaScript；页面入口为 `index.html` 与 `src/main.js`，样式为 `src/style.css`。
- `index.html` 通过外部 CDN 引入 OpenLayers 10.6.1。浏览器验收需要确认外部资源实际加载成功。
- `src/map-api.js` 封装 `/api/v1/map/*` 请求；已有图层、数据库及 Run 查询接口。
- `src/main.js` 已实现数据库与 Clean → Detect → Fit 级联、活动图层选择、点选/框选、中键平移和 Replay/Clip 临时分析；顶部菜单目前没有对应功能绑定。这些是现状记录，不自动纳入本轮实现范围。
- `src/style.css` 包含 980、760、560px 响应式断点；窄屏会隐藏部分上下文、图层或选择控件，是否调整取决于本轮需求。
- `README.md` 记录地图选择、Replay、Clip 与内存 `tmp` 图层工作流。具体功能规格须结合当前实现核实，不能仅依赖 README。
- 依照 `AGENTS.md`：前端负责 UI 和 Backend API 调用；长时间任务属于 Pipeline。运行数据、模型和数据库存储不得进入仓库。
- 当前未要求更换前端框架、变更数据库 schema 或执行 Pipeline 作业。

## 本轮需求与方案

以下内容在获得功能目标后补齐，并在分派实现前形成可执行任务书：

| 项目 | 当前状态 |
| --- | --- |
| 要解决的问题及用户操作场景 | 待确认 |
| 目标页面、功能和不包含的改动 | 待确认 |
| 交互、视觉和响应式要求 | 待确认 |
| API 请求、响应和错误处理变化 | 待确认 |
| 可观察的验收结果 | 待确认 |

## 任务分派

| 任务 | 负责人 | 范围与交付物 | 状态 |
| --- | --- | --- | --- |
| T0 前端现状盘点 | subagent `frontend_inventory` | 只读审查现有交互、API、测试及浏览器验收入口，提供文件证据 | 已完成，主 agent 已核对结构、API 和验证命令；交互结论尚未浏览器复核 |
| T1 完成功能规格与拆分 | 主 agent | 根据用户目标补齐需求、文件边界、依赖和逐项验收标准 | 等待功能目标 |
| T2 功能实现 | 待分派 subagent | 按 T1 任务书实现，提交改动说明及验证结果 | 尚未分派 |
| T3 独立验收 | 主 agent | 对照规格检查 diff、执行适用验证、记录问题并要求修正 | 等待实现 |

后续实现任务书须写明允许修改的文件或模块、依赖任务、预期行为、必要回归检查与交付内容。共享工作区中须保留他人改动，避免多个 agent 同时修改同一文件。

## 验收流程

1. 主 agent 检查实现是否符合已明确的需求、仓库职责和 API 契约。
2. 执行 `make frontend-test`，检查前端测试及静态资源构建；`build.js` 仅复制静态文件，构建成功不能证明浏览器运行成功。
3. 涉及 UI 或地图交互时，在浏览器核对对应流程和控制台错误，保留适用的截图或运行证据；无法执行时明确记录未验收项。
4. 遵循仓库提交前要求运行 `make test`，如受环境限制，记录实际命令和失败原因。
5. 主 agent 独立复核 subagent 结果；发现问题后分派修正并复验，未验证的行为不得标记通过。

具体功能的成功、空数据、错误和状态切换场景，在 T1 根据实际需求确定。

现有 Node 测试不能覆盖真实 OpenLayers 操作、上下文快速切换、按钮状态及响应式显示；相关改动需要浏览器验证。`dev-server.js` 的 API 代理目标为 `backend:8000`，验收使用 Compose 网络或合成 API fixtures，并先核实连接条件。

## 当前验证记录

- `cd apps/frontend && npm test`：通过，Node 测试运行器报告 3 个测试文件通过、0 失败。覆盖入口为 `map-api.test.js`、`pointer-tools.test.js`、`selection.test.js`。
- 当前没有实施功能改动，没有进行浏览器功能验收或完整 Backend/Pipeline 测试。

## 待决事项

- 用户需提供本轮要实现或修复的功能，或指定已有需求文档。
- 功能目标确定后，再将本草稿细化为对应主题的规格，或复用覆盖同一问题的现有文档。

## 附录：完整 QA 记录

### 第 1 轮（2026-09-30）

发问前提：已核查源码，尚未复现用户数据库结果。

#### Q1 — 双交叉口 Replay

> 选中两个 intersection 后，是分别回放各自的构成要素并合并，还是只返回连接两个交叉口的轨迹？交叉口 Replay 也要改成线段吗？

> 推荐分别回放并合并；两口之间的片段交给 Clip。交叉口输出是否从构成点改成构成线段，由你明确。

> 用户回答（第 1 轮合并回答摘录）：Q1：按推荐，分工很明确了，设计的时候就设计了replay是产生这个要素的溯源回放，而不是基于选中要素的新要素产生，所以两口片段交给clip

正文关联：第 3–9 节。当前解释：以本轮合并回答为准，原推荐与用户不一致之处未采纳。

#### Q2 — 事件线段范围

> 跨轨迹事件、转向事件和 injected_points，是仅返回相关 segment，还是保留完整子轨迹并突出相关 segment？

> 推荐仅返回相关线段：跨轨迹事件取参与相交的两条来源线段；转向事件和注入点取该点前后相邻线段。结果为可绘制的线图层，保留追溯属性，不附带其余 context 段。

> 用户回答（第 1 轮合并回答摘录）：Q2：保留完整子轨迹并突出相关segment，子轨迹用grey 透明度α=50%，相关segment用洋红色

正文关联：第 3–9 节。当前解释：以本轮合并回答为准，原推荐与用户不一致之处未采纳。

#### Q3 — 前端操作范围

> 前端新增两个图层，是只需显示、选择和查看属性，还是也需要 Replay？

> 推荐同时支持 Replay，与 QGIS 使用相同线段范围；本轮不改数据库表名。

> 用户回答（第 1 轮合并回答摘录）：Q3：同时支持replay，前端的replay segment已经设计好了，保留子轨迹并突出相关segment

正文关联：第 3–9 节。当前解释：以本轮合并回答为准，原推荐与用户不一致之处未采纳。

#### Q4 — 查询执行位置

> “在 qgis 里直接查找”是要求插件直接执行 SQL，还是允许 Backend 按这些身份追溯，再返回线几何给 QGIS？

> 推荐由 Backend 统一追溯，QGIS 加载线要素，前端复用接口。保持现有 HTTP 调用方式。

> 用户回答（第 1 轮合并回答摘录）：Q4：通过backend，source_row_keys是回放的结果，是tmp图层的属性，我要求是把tmp图层展开成segment layer

正文关联：第 3–9 节。当前解释：以本轮合并回答为准，原推荐与用户不一致之处未采纳。

#### Q5 — 注入点的 Detect 上下文

> injected_points 只有 refine_id，没有 detect_id；同一 Refine 可以对应多个 Detect。遇到多个 Detect 时，应该让你选择，还是自动使用最新一个？

> 推荐只有一个匹配 Detect 时自动使用；多个时让你明确选择，不静默使用最新一个。

> 用户回答（第 1 轮合并回答摘录）：Q5：injected点不用detect_id，而是回放的时候直接溯源到其kept_points再到trajectory_points

正文关联：第 3–9 节。当前解释：以本轮合并回答为准，原推荐与用户不一致之处未采纳。

本轮检查：Q1–Q5 已回答，正文已同步；最终共享理解尚未确认。

### 第 1 轮合并回答 — Q1–Q5（2026-09-30）

用户首次消息原文（随后主动中断）：

> Q1：按推荐，分工很明确了，设计的时候就设计了replay是产生这个要素的溯源回放，而不是基于选中要素的新要素产生，所以两口片段交给clip
> Q2：保留完整子轨迹并突出相关segment，子轨迹用grey 透明度α=50%，相关segment用洋红色
> Q3：同时支持replay，前端的replay segment已经设计好了，保留子轨迹并突出相关segment
> Q4：通过backend，source_row_keys是回放的结果，是tmp图层的属性，我要求是把tmp图层展开成segment layer
> Q5：injected点不用detect_id，而是回放的时候直接溯源到其kept_points再到trajectory_points
> 总体原则，回返到带有geom列的数据源

用户重新发送的完整消息原文（当前有效）：

> Q1：按推荐，分工很明确了，设计的时候就设计了replay是产生这个要素的溯源回放，而不是基于选中要素的新要素产生，所以两口片段交给clip
> Q2：保留完整子轨迹并突出相关segment，子轨迹用grey 透明度α=50%，相关segment用洋红色
> Q3：同时支持replay，前端的replay segment已经设计好了，保留子轨迹并突出相关segment
> Q4：通过backend，source_row_keys是回放的结果，是tmp图层的属性，我要求是把tmp图层展开成segment layer
> Q5：injected点不用detect_id，而是回放的时候直接溯源到其kept_points再到trajectory_points
> 总体原则，回放到带有geom列的数据源

解释范围：两次回答 Q1–Q5 一致；总体原则措辞由“回返”改为“回放”，按最后消息记录有效要求。正文第 3–9 节已同步。设计确认、实施及验收状态未改变。

### 第 2 轮 — 来源几何与展开交互（2026-09-30）

发问前提：Q1–Q5 已回答；注入点前后 kept 锚点不依赖 Detect；跨轨迹及转向事件实际计算线段可能包含 injected 端点。

#### Q6 — 事件线段中的注入端点

> 跨轨迹或转向事件的来源线段，有时包含 injected 端点。这时保留该端点的 injected_points.geom，还是也继续追溯到 kept→trajectory_points？

> 推荐保留事件实际参与计算的 injected 几何，以准确呈现产生事件的线段；直接选中 injected_points 做 Replay 时，按 Q5 回放生成它的 kept 来源段。

> 用户回答（第 2 轮合并回答摘录）：Q6：按推荐，保留injected.gemo

正文关联：第 3–10 节。当前解释：推荐已采纳；Q6 原文 injected.gemo 按已核查的实际字段 injected_points.geom 实施，原文保持不变。

#### Q7 — tmp 自动展开

> 是否点击 Replay 后自动展开并直接加载 tmp segment 线图层，无需第二次点击“展开”？

> 推荐自动完成，沿用现有 Replay 操作；完整子轨迹灰色 alpha=0.5，相关来源段洋红色。

> 用户回答（第 2 轮合并回答摘录）：Q7:按推荐

正文关联：第 3–10 节。当前解释：推荐已采纳，自动展开并加载线图层。

补充代码证据： /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/validation/replay_lineage.py 中 _injected_brackets 已实现 kept 前后锚点查询。

本轮检查：Q1–Q5 原文与更正已保留、正文已同步；Q6–Q7 待回答；QA 附录在文末。

### 第 2 轮合并回答 — Q6–Q7（2026-09-30）

用户回答原文：

> Q6：按推荐，保留injected.gemo
> Q7:按推荐

解释范围：Q6–Q7 推荐均采纳；原文 gemo 指已核查的 geom 字段。正文第 3–10 节已同步，尚无全文最终确认。

### 第 3 轮 — 最终共享理解（2026-09-30）

#### Q8 — 确认完整设计并进入实施

> 确认按以下完整设计开始修改吗？
> 1. 前端新增跨轨迹、转向事件图层，均支持 Replay。
> 2. QGIS 双交叉口 Replay 分别溯源并合并；Clip 提取两口之间片段，修复选择限制及错误 ID 解析。
> 3. Backend 回放到带 geom 的来源，Replay 自动展开为 tmp 线图层；保留完整子轨迹，灰色 alpha=0.5，相关 segment 洋红色。
> 4. 直接回放 injected_points 无需 Detect，沿 kept 点追溯原始轨迹；跨轨迹或转向事件的来源段则保留 injected_points.geom。

> 推荐按此实施，并按第 7–8 节进行回归与几何显示验证。

> 用户回答：确认实施

正文关联：第 2–10 节。当前解释：Q1–Q7 需求已收敛；仅待完整设计确认。确认后依原始修改请求实施，不另设重复许可。

本轮检查：Q6–Q7 完整原文与逐题摘录均已保存，正文同步；Q8 待确认；代码尚未修改、测试尚未执行；QA 附录在文末。

### Q8 确认记录（2026-09-30）

> 确认实施

全文设计与实施已获确认；进入代码修改和验证，验收尚未完成。

### 实施完成记录（2026-09-30）

Q8 授权后的实现与验证已记录于第 8 节；无新增设计决定。插件版本为 0.2.0。用户桌面与浏览器验收未冒充已完成。
