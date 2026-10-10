# Refine 子轨迹点计算航向规格说明（specification，Spec）

术语遵循[项目术语与写作规范](terminology.md)。

状态：待确认
创建日期：2026-10-10
更新日期：2026-10-10
访谈进度：已回答 Q1–Q12；待回答 Q13 完整设计确认及实施授权

## 1. 背景与问题

用户要求在精炼阶段（Refine）计算子轨迹（subtrack）点的 `heading_cal`，保留初始 `heading`。
相关文档为[列式架构 Spec](pipeline-columnar-architecture-spec.md)第 4.7 节：其范围是跨轨迹线段夹角及方向分桶，并未定义逐点 `heading_cal`。本文件记录新增逐点属性的独立设计，不改变该历史结论。

代码事实（2026-10-10 核查）：

- Refine 保留点（p）复制来源 `heading`，注入点（i）的 `heading` 为相邻源航向沿最短角差的中值。
- `RefinementSubtrack` 保存按序排列的 p/i 点列，没有 `heading_cal`。
- Refine Parquet v1 的保留点及注入点均保存 `heading`，没有 `heading_cal`。
- 现有几何方位角使用 `atan2(dx, dy)`，北向为 0°、顺时针增加。

证据位置： /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/models.py 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/results/parquet_schema.py 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/_geometry.py 。

## 2. 目标与非目标

已确认（Q1–Q3）：按最终 p/i 序列计算投影 XY 出段方向，新增 `heading_cal` 并持久化、支持读取；保留现有 `heading`。本次不切换下游算法输入。新增子轨迹最少点数过滤，暂定阈值 10，同步 Pipeline TOML 配置（Q7）。

## 3. 需求与使用场景

已确认：投影 XY 出段方向（Q1），去除静止冗余点、完成注入后的最终 p/i 序列（Q2），计算、持久化并支持读取（Q3）。单位为度，0° 为投影网格北，顺时针增加，范围 [0, 360)。已确认（Q4）：末点沿用本子轨迹最后一条非零线段方向。零位移留空，整条无非零线段时末点也留空（Q5）。新增最少点数 10 的子轨迹过滤（Q7），按静止清理后、注入前的 p 数量判断（Q8）；旧结果按 Q6 仅适配为空。

## 4. 方案与关键决策

设计树：

- Q1 航向定义 → 端点、零位移、单点、坐标方向精度。
- Q2 计算点序列 → 注入点字段语义、计算时机。
- Q3 消费范围 → 持久化、读取、版本兼容、具体下游算法切换。
- 以上收敛 → 验收标准 → 完整设计确认；实施授权单独记录。

有效决定：Q1 选择 A；Q2 选择最终 p/i 序列；Q3 选择 A。非零出段使用 `heading_cal=(degrees(atan2(dx,dy))+360)%360`。原 `heading` 保留现有含义：p 保存源值，i 保存当前生成的中值。Q4 已选择末点沿用最后非零线段方向；用户认为最短点数为 3，核查结果不支持该前提，Q7 已要求新增最少点数 10 的过滤，按静止清理后、注入前的 p 数量判断（Q8）。Q5 已选择 A：精确零位移留空，非零线段不新增距离阈值。Q6 选择 A：仅新运行计算，旧结果显式适配为空，不补算或改写。

Q8：按静止清理后 p 数量判断，数量等于阈值时保留。Q9：过滤出的剩余 p 点直接保存 GeoParquet，等待后续读取，不插入 PostgreSQL。此前写入 excluded 的建议未获采纳。静止已排除点与 short_subtrack 点为两批不同点，并非同点具有双重原因；静止清理既有行为不由本轮改动。

## 5. 接口、数据与架构影响

已确认（Q3）：扩展子轨迹列模型、Refine PostgreSQL/Parquet 输出及读取链路，使后续能够读取 `heading_cal`。Q7 同步修改三份 Pipeline 运行配置：主配置、冒烟配置、调试配置；字段映射文件不承载 Refine 配置。配置清单核查： /data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline.toml 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline-roadnet-test.toml 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline-debug.toml 。旧结果按 Q6 显式适配，新字段为空；仅新运行计算，不改写旧结果。

Q9 新增直接从 Refine 内存结果写 GeoParquet 的短子轨迹输出；现有 Refine 正常 Parquet 经 PostgreSQL 导出，不能直接复用该数据来源。已确认：短子轨迹每个 p 点一行（Q10），不计算 heading_cal（Q11），作为同一 Refine Run 制品的一部分一起发布、提供独立读取入口，不自动进入 Detect（Q12）。复用现有 pipeline_artifacts 表，manifest.tables 增加 short_subtrack 逻辑表及文件清单，不新增 PostgreSQL 制品表或短子轨迹点表。

## 6. 约束与非功能要求

遵循既有列式 Pipeline 架构。若实施涉及 SQL，按项目空间索引审查要求提供真实检查证据。性能验收范围待确认；目前没有实测。配置暂定阈值为 10（Q7），拟采用 `[refine].min_subtrack_points`，正整数；具体字段及范围在最终设计确认时一并确认。

## 7. 验收标准

待 Q13 最终确认的验收标准：

- 正常轨迹 p/i 点新增 heading_cal，投影网格北为 0°、顺时针 [0,360)，出段计算正确；末点使用本子轨迹最后非零段方向；零位移、整条无非零段时相应值为空。
- 原 heading 值和含义保留，下游现有判定不切换输入。
- 清理后 p 点数在阈值前后边界（9/10/11）行为正确，注入点不参与凑数；配置读取、验证和三份 TOML 一致，值暂定 10。
- 短子轨迹直接流式写 GeoParquet，每 p 点一行，保存源行键、车辆/子轨迹身份、顺序、时间、lon/lat、XY、heading、speed、geometry 及 CRS 元数据；无 heading_cal，不生成 i 点，点记录不写 PostgreSQL。
- stationary 排除沿用原行为，与短子轨迹点集不重叠；运行统计单独报告短子轨迹数和点数。
- 正常结果和短子轨迹文件纳入同一 Refine 制品 manifest，复用现有登记表及发布/失败清理机制；空短结果可读取，统计为 0。
- 提供短子轨迹独立读取入口，正常 Detect 输入不含这些点；旧 Refine 结果经明确适配，heading_cal 为空且不产生短子轨迹历史补算。
- 文件读写及登记信息一致；验证 GeoParquet geometry 与 CRS 元数据。

## 8. 实施与验证计划

当前仅开展访谈及文档维护，未修改运行代码。以下计划待 Q13 实施授权：

1. 扩展 Refine 配置、数据模型和结果分流：静止清理 → 最少 p 点数过滤 → 短点直接写文件／正常轨迹注入并计算航向。
2. 扩展正常航向的 PostgreSQL/Parquet 写入读取与旧版本适配；短子轨迹流式写 GeoParquet，提前创建 staging，沿用同 Run 发布。
3. 复用 pipeline_artifacts 的既有 manifest，新增 short_subtrack 文件逻辑表，调整 Refine 专用严格校验及独立读取接口，不增加登记数据库表。
4. 增加行为回归测试：几何航向、空值、点数边界、源点保留、短点不入库、统计/读取、旧制品适配及发布失败清理；运行 make test，遵守 SQL 审查。
5. 本次不启动 Pipeline 全量数据任务。功能验证与实际数据运行分别记录；若后续请求实际冒烟，使用项目指定冒烟 TOML。

## 9. 风险与待决事项

实际取舍：出段方向与局部切线在转弯点不同；投影平面方位角与经纬度真北方位角可能不同；新增属性与替换下游输入的行为范围不同。
已确认 Q8：静止清理后、注入前按 p 数量过滤。已确认 Q9：short_subtrack 直接保存 GeoParquet，不写入 PostgreSQL 点表。Q10–Q12 已确定短子轨迹逐点保存、不计算航向、同 Run 发布及独立读取。剩余待确认：Q13 完整设计及实施授权。新增过滤将改变新 Refine 运行的点集合；现有旧结果按 Q6 保持原内容。

## 10. 确认记录

设计确认：未取得。实施授权：未取得。验收确认：不适用，尚未实施。

## 附录：完整 QA 记录

### 初始请求（2026-10-10）

> 在pipeline里，增加一个计算项，refine时就计算subtrack点的实际heading_cal，并且保留初始的heading
> $grill-me

### 第 1 轮 — 航向定义与使用范围（2026-10-10）

发问时前提：已核查 Refine 保留点/注入点航向及现有列结构；用户尚未确认算法定义。

#### Q1 — “实际航向”的定义

> 你希望 `heading_cal` 表示哪种方向？A. 当前点到下一点的出段方向，使用现有投影 XY；B. 前后邻点确定的局部切线方向；C. 当前点到下一点的经纬度真北方位角。统一用角度，0° 为北、顺时针增加，范围 [0, 360)。

> 推荐 A：它直接对应 subtrack 的实际线段，也与现有线段几何方向计算一致。末点等边界情况下一轮单独确定。

> 用户回答（第 1 轮合并回答逐字摘录）：Q1:A

正文关联：第 3、4、7 节。当前解释：已采纳本题所选项，见第 1 轮合并回答。

#### Q2 — 按哪一条点序列计算

> `heading_cal` 是在去除静止冗余点、完成注入点生成后，按最终 p/i 点序列计算，还是只按原始保留点 p 计算？

> 推荐最终 p/i 序列：保留点和注入点都有 `heading_cal`，计算方向反映 Refine 实际输出的几何路径；原 `heading` 保留现有含义，p 保存源值，i 保存当前生成的中值。

> 用户回答（第 1 轮合并回答逐字摘录）：Q2:p/i序列

正文关联：第 2–5、7 节。当前解释：已采纳本题所选项，见第 1 轮合并回答。

#### Q3 — 新字段的使用范围

> 本次要做到哪一级？A. Refine 计算、持久化并允许后续读取 `heading_cal`；B. 在 A 的基础上，让下游现有使用 `heading` 的算法改用 `heading_cal`。如果选 B，请指出希望改变的算法或行为，其余代码事实由我查清。

> 推荐 A：先明确交付为可保存、可读取的几何航向属性。若你的目的就是改变后续判定，请选 B，我们继续确认具体算法范围。

> 用户回答（第 1 轮合并回答逐字摘录）：Q3：A

正文关联：第 2、4、5、7、8 节。当前解释：已采纳本题所选项，见第 1 轮合并回答。

### 第 1 轮补充 — 未标明题号的回答（2026-10-10）

用户回答原文：

> A

当前解释：未指定题号；随后收到明确的 Q1–Q3 回答，以后者确定各题决定。

### 第 1 轮合并回答 — 对应 Q1–Q3

接收日期：2026-10-10。
用户回答原文：

> Q1:A
> Q2:p/i序列
> Q3：A

解释范围：回答全部三题。正文同步：第 2–5、9 节及访谈进度。

### 第 2 轮 — 边界与旧结果（2026-10-10）

发问时前提：已确认投影 XY 出段方向、最终 p/i 序列及新增字段计算/持久化/读取范围。

#### Q4 — 末点和单点子轨迹

> 末点没有下一点，`heading_cal` 怎么取？A. 沿用本 subtrack 最后一条非零线段的方向；B. 留空。单点或整条子轨迹没有非零线段时，是否留空？

> 推荐 A，并且无任何非零线段时留空；不会跨 subtrack 借用方向。

> 用户回答（第 2 轮合并回答逐字摘录）：Q4：A，对于单点子轨迹，这不是该模块需要考虑的，因为我们在toml里已经设置了最短subtrack点数为3，对吧
正文关联：第 3、4、7 节。当前解释：末点选择 A；最短点数前提需核查，单点行为尚未确认。

#### Q5 — 相邻同坐标点

> 非末点与下一点 XY 完全相同时，这个点的 `heading_cal` 怎么取？A. 留空；B. 跳过重复坐标，朝后续第一个不同坐标点计算；C. 沿用之前有效方向。是否还要把很短但非零的线段视为无方向？

> 推荐 A，仅精确零位移留空；非零线段均按公式计算，不新增距离阈值。这样逐点属性仍对应相邻出段。

> 用户回答（第 2 轮合并回答逐字摘录）：Q5：会有这种情况存在吗？你先看看代码里对这种情况有无特殊处理?我感觉这个情况会被识别kept点的时候就过滤了，还轮不到p/i序列的时候来处理
正文关联：第 3、4、6、7 节。当前解释：请求代码核查，尚未选择处理规则。

#### Q6 — 已有 Refine 结果

> 旧结果没有 `heading_cal`，希望怎样兼容？A. 仅新运行计算；读取旧结果时通过明确的版本适配，将新字段标为空，不改写旧结果；B. 读取旧结果时按相同规则即时计算；C. 对选定旧结果做一次补算，范围另行确认。

> 推荐 A：保留旧结果版本，新字段有值从新 Refine 运行开始。需要历史值时可以另行重跑。

> 用户回答（第 2 轮合并回答逐字摘录）：Q6：A
正文关联：第 5、8、9 节。当前解释：选择 A：旧结果适配为空，仅新运行计算。



### 第 2 轮合并回答 — 对应 Q4–Q6

接收日期：2026-10-10。
用户回答原文：

> Q4：A，对于单点子轨迹，这不是该模块需要考虑的，因为我们在toml里已经设置了最短subtrack点数为3，对吧
> Q5：会有这种情况存在吗？你先看看代码里对这种情况有无特殊处理?我感觉这个情况会被识别kept点的时候就过滤了，还轮不到p/i序列的时候来处理
> Q6：A

解释范围：确认 Q4 末点 A 与 Q6 A；单点前提及 Q5 要求先核查，不代表接受零位移留空建议。正文同步：第 3–5、9 节及访谈进度。

### 第 3 轮 — 代码核查与边界决定（2026-10-10）

代码核查事实：主配置、冒烟配置的 Refine 均无最短 subtrack 点数为 3 的键。Refine 的 `len(indices) >= 3` 仅控制静止冗余点扫描；长度 1、2 的轨迹仍输出。静止筛选按速度和时长，保留长静止段两端，不按相邻坐标去重。注入允许射线距离为 0，可与端点重合。现有测试明确用同坐标两点生成 p/i/p。

证据（2026-10-10）： /data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline.toml:33 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline-roadnet-test.toml:33 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py:25 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py:79 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/tests/v2/test_refine.py:118 。本轮为只读核查，未运行测试。

#### Q5 后续提问 — 已证实存在零位移

> 已确认重复坐标可以进入最终 p/i 序列。是否采用 Q5 的 A：仅精确零位移留空，非零线段仍按公式计算，不新增距离阈值？若整条子轨迹都无非零线段，末点也留空。

> 推荐采用；不改变既有 kept 筛选和注入规则。

> 用户回答（第 3 轮合并回答逐字摘录）：Q5：A，留空
正文关联：第 3、4、7、9 节。当前解释：已选择 A，零位移留空。

#### Q7 — 当前允许输出的短子轨迹

> 当前 Refine 确实允许输出不足 3 点的 subtrack。A. 本次保留现有筛选语义，单点的 `heading_cal` 留空，两点按出段和末点规则计算；B. 本次新增最少 3 点过滤，另行定义在注入前还是注入后筛选，以及如何记录被排除点。

> 推荐 A：当前任务只新增航向字段；最短点数过滤会改变 Refine 的输出成员。

> 用户回答（第 3 轮合并回答逐字摘录）：Q7：新增subtrack最少点数过滤，先暂定为10点，config/*.toml文件里同步增加该配置选项
正文关联：第 3、4、7、9 节。当前解释：新增最少点数过滤，阈值暂定为 10，同步运行 TOML 配置；不是采纳原建议 A。



### 第 3 轮合并回答 — 对应 Q5、Q7

接收日期：2026-10-10。
用户回答原文：

> Q5：A，留空
> Q7：新增subtrack最少点数过滤，先暂定为10点，config/*.toml文件里同步增加该配置选项

解释范围：确认 Q5 A；Q7 扩大范围，新增最少点数过滤，暂定阈值 10，同步配置。尚未决定计数时机及排除记录。正文同步：第 2–6、9 节及访谈进度。

### 第 4 轮 — 最少点数过滤的语义（2026-10-10）

发问时前提：Q5 零位移留空；Q7 新增子轨迹最少点数，暂定 10。现有排除记录携带源行身份及 reason，支持按原因报告与回放。

#### Q8 — 在什么时候计数

> “至少 10 点”按哪一步的点数判断？A. 时间间隔分段后、静止清理前的源点数；B. 静止清理后的保留点 p 数量，注入前判断；C. 注入后的最终 p/i 总点数。数量等于 10 时保留，少于 10 时整条过滤。

> 推荐 B：至少有 10 个保留下来的源观测点，注入点不会把较短的源轨迹凑到阈值。例如清理后 8 个 p 加上 2 个 i，B 过滤、C 保留。

> 用户回答（第 4 轮合并回答逐字摘录）：Q8:B
正文关联：第 3、4、7 节。当前解释：选择 B：静止清理后、注入前按 p 数量判断。

#### Q9 — 被过滤点如何记录

> 被最少点数过滤的 subtrack，其源点是否写入现有 excluded 记录，使用新原因 `short_subtrack`？若其中已有静止清理排除点，A. 保留它们的 `stationary` 原因，其余源点记为 `short_subtrack`；B. 整段源点统一记为 `short_subtrack`。

> 推荐写入 excluded，并选 A：每个源点只记录一次，保留已有排除原因，报表和回放可以追踪新过滤。被过滤的 subtrack 不保存注入点，也不计入输出注入点数。

> 用户回答（第 4 轮合并回答逐字摘录）：Q9:怎么可能会有stationary和short_subtrack一起的点呢？short_subtrack判断在静止清理后的保留点 p 数量，注入前判断，参见Q8
> short_subtrack直接保存geoparquet，等待读取就好了，不要写入postgres数据库
正文关联：第 4、5、7、8 节。当前解释：要求 short_subtrack 直接保存 GeoParquet，不写 PostgreSQL；不同意原 excluded 写入建议。



### 第 4 轮合并回答 — 对应 Q8–Q9

接收日期：2026-10-10。
用户回答原文：

> Q8:B
> Q9:怎么可能会有stationary和short_subtrack一起的点呢？short_subtrack判断在静止清理后的保留点 p 数量，注入前判断，参见Q8
> short_subtrack直接保存geoparquet，等待读取就好了，不要写入postgres数据库

解释范围：Q8 B；Q9 明确直接写 GeoParquet、等待后续读取，不写 PostgreSQL。原问题提到的是同一源轨迹的不同点集，措辞易产生同点双原因误解，本轮澄清；不把本次回答解释为修改 stationary 写入规则。正文同步：第 3–5、9 节及进度。

### 第 5 轮 — 短子轨迹 GeoParquet（2026-10-10）

发问时前提：短子轨迹只含静止清理后的剩余 p 点，判定发生在注入前；用户要求直接写 GeoParquet，不写 PostgreSQL 点表。

#### Q10 — 保存点还是线

> short_subtrack 的 GeoParquet 采用哪种结构？A. 每个 p 点一行，保存源点身份、子轨迹身份、顺序、时间、坐标、原 heading、speed 和点几何；B. 每条 subtrack 一行，保存线几何及点属性数组。

> 推荐 A：能完整保存逐点信息；也能表达过滤出的单点子轨迹。

> 用户回答（第 5 轮合并回答逐字摘录）：Q10:A
正文关联：第 3–5、7 节。当前解释：选择 A：每个 p 点一行。

#### Q11 — 短子轨迹是否计算 heading_cal

> 被过滤的短子轨迹是否也要计算并保存 heading_cal？A. 要，按其剩余 p 序列计算，不生成 i 点；B. 不计算，仅保存原 heading。

> 推荐 A：沿用已确认的出段/末点/零位移规则；单点没有可计算方向，heading_cal 留空。正常通过过滤的轨迹仍按最终 p/i 序列计算。

> 用户回答（第 5 轮合并回答逐字摘录）：Q11：B，不计算了
正文关联：第 3、4、7 节。当前解释：选择 B：短子轨迹不计算 heading_cal。

#### Q12 — 文件归属与读取范围

> 是否将短子轨迹文件作为本次 Refine Run 制品的一部分，与正常结果一起发布，PostgreSQL 仅登记制品路径、版本和统计等元数据，不写短子轨迹点记录？本次提供文件读取入口，读取这些记录不自动送入 Detect，可以吗？

> 推荐采用：文件随 Run 管理；可单独读取 short_subtrack，后续如何利用另行定义。若“不写 PostgreSQL”也包括这类文件元数据，请明确。

> 用户回答（第 5 轮合并回答逐字摘录）：Q12：采用，但是你要在哪登记？又新建一张表？不会吧，我记得好像有人工制品登记表，可以登记不同类型的制品产出吧
正文关联：第 5、8、9 节。当前解释：采用文件同 Run 发布、元数据登记及独立读取；要求核查复用既有制品登记表。



### 第 5 轮合并回答 — 对应 Q10–Q12

接收日期：2026-10-10。
用户回答原文：

> Q10:A
> Q11：B，不计算了
> Q12：采用，但是你要在哪登记？又新建一张表？不会吧，我记得好像有人工制品登记表，可以登记不同类型的制品产出吧

解释范围：逐点保存，不计算短子轨迹航向；接受 Q12 发布与读取边界，要求核查既有登记表。正文同步：第 5、7–9 节及状态/进度。

登记核查（2026-10-10）：现有 pipeline_artifacts 保存 Run 归属和 JSONB manifest；manifest.tables 支持多张文件逻辑表并存，每表记录 schema、文件、行数及哈希。Refine Run 具有唯一制品约束，因此 short_subtrack 加入本次既有制品的 manifest，不建立第二个 Refine 制品记录，不新建登记表。这里逻辑表指文件数据集，不是 PostgreSQL 表。

证据： /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/cli/init_db.py:501 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/cli/init_db.py:581 、 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/artifact_registry.py:120 。

### 第 6 轮 — 完整设计确认（2026-10-10）

#### Q13 — 确认设计与实施范围

> 完整方案：三份运行 TOML 新增 [refine].min_subtrack_points=10（正整数）；按静止清理后 p 数量过滤，少于阈值直接保存逐点 GeoParquet，不注入、不计算航向、不写 PostgreSQL 点表；其余轨迹完成注入后按 p/i 序列计算 heading_cal，原 heading 保留，零位移为空，末点沿用最后非零段方向。短点文件与正常结果共用一次 Refine 制品登记，在 pipeline_artifacts 的同一 manifest 增加 short_subtrack 文件逻辑表；支持独立读取，不自动进入 Detect。旧结果不补算，heading_cal 适配为空。按第 7 节验收标准及第 8 节实施验证。是否确认？可回答“确认设计并实施”“仅确认设计”或指出修改。

> 推荐确认设计并实施。

用户回答：待回答。
正文关联：第 2–10 节。当前解释：设计已收敛，待完整确认；此前局部采纳不作为最终设计确认或实施授权。

本轮检查：Q1–Q12 及全部原文保留，正文与最新决定一致；Q13 待回答，状态为待确认，尚未实施；QA 附录位于文末。
