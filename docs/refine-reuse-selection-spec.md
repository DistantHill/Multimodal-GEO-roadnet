# Refine 复用选择规格说明（specification，Spec）

术语遵循 [项目术语与写作规范](terminology.md) ，不在本文件复制通用术语表。

状态：已确认
创建日期：2026-09-26
更新日期：2026-09-27
访谈进度：Q1–Q13 已回答；完整设计已确认；指定 gpt-6-luna（high）实施和跑测，主 session 仅验收；2026-09-27 完成功能范围验收。

## 1. 背景与问题

用户希望输入来源后自动发现已完成的精炼阶段（Refine）运行批次（Run），通过命令行提示决定是否复用；另外提供可选 Bool 参数 `refine_resume_last`，为 true 时自动选择最近的历史结果。

2026-09-26 检索现有文档，未发现同主题的自动选择 Spec。阶段运行器重构文档讨论现有显式复用分支；配置集中化 Spec 规定历史 Refine 的投影继承，不代表已设计自动选择。

已核查事实（2026-09-26）：

- 当前 Make 全量及冒烟入口均要求 `SOURCE_TABLE`，固定传递 `--source-table`，未传递 `REFINE_ID`。依据： [/data/workspace/Multimodal-GEO-roadnet/Makefile:43](/data/workspace/Multimodal-GEO-roadnet/Makefile:43) 。
- CLI 要求 `--source-table` 与 `--refine-id` 二选一，无自动查找。依据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/cli/run_pipeline.py:56](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/cli/run_pipeline.py:56) 。
- 现有复用加载器检查 completed 状态并继承历史来源与投影，不比较当前 Refine 配置。依据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/stages/refine.py:39](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/stages/refine.py:39) 。
- 数据库包含 `started_at`、`finished_at`，已有来源表与开始时间降序索引。依据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/cli/init_db.py:542](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/cli/init_db.py:542) 。

- 来源表名保留原输入文本；`day01` 与 `public.day01` 不会自动统一。依据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/source_table.py:130](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/source_table.py:130) 。
- 正式 Detect 比较 Refine 产物和数据库保存指纹，未在启动前完整扫描当前来源表；已有指纹验证函数由 Replay/Compare 使用，范围为该 Refine 引用的历史来源行，并非源表所有行。依据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:444](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:444) 、 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/source_table.py:290](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/source_table.py:290) 。
- 产物缺失或校验失败抛错；当前 Detect Run 已创建，随后标为 failed；不自动尝试旧候选或重做 Refine。依据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/stages/detect.py:44](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/stages/detect.py:44) 、 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/stages/detect.py:185](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/stages/detect.py:185) 。

## 2. 目标与非目标

目标：减少手工查询和复制 Refine UUID；同时支持交互选择、自动选择、强制新建及显式 UUID 复用。

已确认范围（Q1、Q2）：Pipeline CLI，以及 Make 全量和冒烟入口；来源使用现有表名。两个 Make 入口支持 `SOURCE_TABLE` 或 `REFINE_ID`，以及与来源表配套的 `REFINE_RESUME_LAST`。

本次不新增来源 ID、不合并来源表别名、不增加来源扫描校验、不增加失败后自动选择旧批次或重算。Q13 已授权指定子代理实施和跑测；主 session 仅负责验收。

## 3. 需求与使用场景

| 场景 | 有最新 completed 候选 | 无 completed 候选 | 来源 |
| --- | --- | --- | --- |
| SOURCE_TABLE，未传 Bool | 展示候选并询问 `[Y/n]` | 提示后新建 Refine | Q3、Q4、Q6 |
| SOURCE_TABLE，true | 直接复用最新候选 | 提示后新建 Refine | Q3–Q5 |
| SOURCE_TABLE，false | 直接新建，不询问 | 直接新建 | Q3 |
| 仅 REFINE_ID | 直接复用指定 UUID，沿用已有校验 | 指定 UUID 不存在或未完成时报错，不新建 | Q8 |

交互规则（Q6、Q7）：只展示最新一条；回车、y/yes（忽略大小写和首尾空格）复用，n/no 新建，其他输入重复提示。存在候选但无交互终端，或者提示遇到 EOF，退出并要求显式 true/false；Ctrl+C 取消。无候选按 Q4 新建，无须终端回答。

来源匹配与候选（Q5、Q10）：在当前连接数据库中，按保存的 `source_table` 文本精确匹配，仅查询 completed；按 `started_at DESC` 取第一条。不把 `day01` 与 `public.day01` 合并。实现细节：开始时间相同时用 UUID 固定排序，避免每次查询随机变动。

## 4. 方案与关键决策

已确认的选择树：

1. 验证来源输入二选一，以及 Bool 只能随来源表使用（Q8）。
2. 显式 UUID：直接进入已有复用路径（Q8）。
3. 来源表 + false：进入新建路径（Q3）。
4. 来源表 + true 或未传：查找最新 completed（Q5、Q10）；查询错误直接暴露，不当作无候选（Q7、Q11 的失败处理原则）。
5. 无候选：提示后新建（Q4）；有候选 + true：复用；有候选 + 未传：按 Q6、Q7 交互。
6. 复用失败：输出 UUID 与原因并退出，不自动尝试旧批次，不自动重算（Q11）。

复用继承范围（Q9）：复用历史 Refine 数据及其持久化空间上下文，允许与当前 Parser/Refine/投影配置不同。当前 Parser/Refine 不重新执行；后续 Detect、Clip、Fitting、Network 仍使用当前启动配置，并创建新的 Detect/Fit Run。

Q13 已确认的具体输出：

- 候选及复用身份：UUID、来源表、开始时间、完成时间。
- 复用数据：历史 Refine 的保留点、注入点及对应轨迹结果；Parser/Refine 本次跳过。
- 继承投影：历史 `source_srid`、`processing_srid` 和完整 `projection_config` 值。
- 历史结果的生成参数：历史 `parameters.semantic_config.parser` 与 `.refine`；标明它们仅解释历史结果如何生成，不是重新执行配置。
- 当前生效配置范围：下游 Detect/Clip/Fitting/Network 使用当前配置。不得将整份历史 `parameters` 称为继承的运行配置。

交互模式在询问前展示上述历史摘要，接受后明确复用；自动模式和显式 UUID 模式直接展示。历史生成参数缺失时明确标注“历史记录未保存”，不拿当前值冒充；运行所需的历史空间上下文错误仍按现有校验报错。

## 5. 接口、数据与架构影响

Q13 已确认接口：

- CLI：`--source-table TABLE` 或 `--refine-id UUID`；可选 `--refine-resume-last true|false`，只配合来源表使用。
- Make：`SOURCE_TABLE=...` 或 `REFINE_ID=...`；可选 `REFINE_RESUME_LAST=true|false`，只配合来源表使用。未传不注入 false；显式非法或空 Bool 报错。
- 两个 Make 入口保留各自配置；配置文件名分别为 `pipeline-roadnet-test.toml` 与 `pipeline.toml`。
- 交互仅发生在 CLI/Make 启动流程；直接调用运行函数不依赖终端提示，保持现有显式来源或 UUID 的调用语义。
- 数据访问层增加最新 completed 查询及时间元数据读取，使用数据库既有字段与索引；不要求数据库迁移。
- Refine 结果持久化及下游阶段仍属于 Pipeline，Backend 不承载长任务。

参数约束（Q8）：同时给来源表和 UUID、两者都未给、UUID 加任何显式 Bool（包括 false），均报错，禁止忽略冲突参数。

## 6. 约束与非功能要求

沿用既有数据库、产物及投影校验。Q12 明确不增加全表扫描校验，也不新增基于历史引用来源行的指纹扫描。

候选发现为单来源已完成批次的有序查询，不读取轨迹点数据；无新增量化性能指标。无运行数据目录变更，无数据库结构变更。

现有产物校验可能在 Detect Run 创建后失败；保留既有 failed 状态处理，不承诺运行记录零写入。

## 7. 验收标准

1. CLI、Make 全量和冒烟均可用来源表、显式 UUID 启动；非法组合报错（Q1、Q2、Q8）。
2. Bool 未传、true、false 分别触发交互、自动复用、强制新建；未传与 false 可区分，非法 Bool 不被当作 true（Q3；具体接口已由 Q13 确认）。
3. 无 completed 候选时提示并新建；数据库查询失败时暴露错误而非新建（Q4、Q7）。
4. 同来源多个批次时排除 running/failed，按开始时间降序选择；来源文本不一致不合并（Q5、Q10）。
5. 提示支持回车/y/yes、n/no、非法输入重试；no 新建；无终端/EOF/取消时退出，不替用户决定（Q6、Q7）。
6. 显式 UUID 不查询最新、不询问，并沿用指定批次的完成状态等校验（Q8）。
7. 当前 Refine 参数或投影不同仍允许复用；输出真实历史参数、空间上下文、复用结果范围与当前下游配置范围（Q9；具体输出已由 Q13 确认）。
8. 最新批次产物缺失或校验失败，显示批次 UUID 与原因并失败退出，不回退旧批次、不自动重算（Q11）。
9. 新功能不触发当前来源表或历史引用来源行的额外指纹扫描，保留现有产物验证（Q12）。
10. 复用不新增 Refine Run；后续创建新的 Detect/Fit Run；新建路径使用当前 Parser/Refine 配置。

## 8. 实施与验证计划

Q13 授权：指定 gpt-6-luna（high）子代理负责实施和跑测，形成文本报告；主 session 仅验收。按以下计划执行：

1. 数据访问层候选查询与时间读取。
2. CLI 三态 Bool、参数组合校验、交互和继承信息输出。
3. 两个 Make 入口传递及来源/UUID 二选一；文档更新。
4. 回归测试候选排序、交互与 EOF、非法参数、继承输出及失败不回退；核对 Make 命令生成。
5. 运行适用的 CLI/Make 回归与项目 `make test`，记录结果或实际阻塞。

冒烟测试使用 `pipeline-roadnet-test.toml`；全量测试使用 `pipeline.toml`；测试记录须注明实际配置。指定子代理已完成实现和跑测；主 session 审查现有测试证据，并核验用户补充的百万行冒烟日志。详见实施与验证报告。

## 9. 风险与待决事项

已接受的取舍：

- Q9：修改当前 Parser/Refine 参数不会改变复用结果；必须显式输出历史来源和继承范围。
- Q10：同一实际表的不同文本写法可能查不到彼此历史；保持现有规则。
- Q11：completed 不等于产物仍可用；最新产物失败时退出，由用户明确选择后续动作。
- Q12：不增加来源扫描，因此选择过程不保证当前来源数据与历史输入一致。

无新增业务分支待决。Q13 已确认完整设计、具体 Bool 接口与继承信息展示，并授权指定子代理实施。子代理报告已保留；主 session 已完成本功能范围验收。仓库整体 Backend 单测失败单列，不宣称全部测试通过。

## 10. 确认记录

完整设计：已由 Q13 确认。实施：已由 Q13 授权 gpt-6-luna（high）子代理实施和跑测。验收：主 session 于 2026-09-27 完成本功能范围验收；用户补充百万行新建路径真实冒烟成功，已核对日志。仓库整体 make test 仍有一个 Backend 失败项。

Q1–Q12 的局部决定及原文均已记录，不能替代完整设计确认。

访谈技能要求：“Do not act on it until the user confirms you have reached a shared understanding.” 因此完整设计确认前只维护文档。项目 hook 已授权文档维护；Q13 另行授权指定子代理实施和跑测。

### 实施与验收证据（2026-09-27）

实施与验证报告： [/data/workspace/Multimodal-GEO-roadnet/docs/refine-reuse-selection-implementation-report.md](/data/workspace/Multimodal-GEO-roadnet/docs/refine-reuse-selection-implementation-report.md) 。

主 session 实读证据：CLI 36 项通过、Make/Compose 14 项通过；百万行真实冒烟日志五阶段 completed、Committed、Done（1905.410 秒）。该运行是新建 Refine 路径，未作为历史复用的真实运行证据。Pipeline 完整套件 1167 通过、72 跳过为子代理记录；make test 的 Backend 单测失败已实读并保留。

冒烟配置按用户所述入口和 Make 约定为 `pipeline-roadnet-test.toml`，日志未打印配置路径；未运行业务全量配置 `pipeline.toml`。本功能范围验收通过，无新增手动验收要求；仓库全部测试尚未通过。

## 附录：完整 QA 记录

### 初始需求（2026-09-26）

用户原文（需求部分逐字摘录；其余为用户附带的现状说明）：

> 我要做到无痛选择，user输入source\_id，自动检测是否又refine\_id已完成，如果有则提示是否使用（命令行提示）
>
> 2、新增启动参数，refine\_resume\_last(optional),该参数为Bool，如果true则自动读取数据库里source\_table对应的refine\_id,按时间降序，也就是离现在越近的放在第一，然后选择第一个refine\_id的结果复用

用户附带的现状说明：当前 `make pipeline-full-test` 不支持 `REFINE_ID`，要求 `SOURCE_TABLE`，固定执行 `--source-table`；示例查询筛选 `status = 'completed'`，按 `started_at DESC` 排序。显式 UUID 复用可通过 `python -m pipeline run --refine-id` 运行；`--refine-id` 和 `--source-table` 二选一。该段为摘要，不冒充用户 QA 回答；代码事实须另行核查。

### 第 1 轮 — 输入与启动行为（2026-09-26）

发问时前提：用户已提出 true 自动复用最近 Refine 的意图；来源命名、Bool 其他取值和入口范围未确认；代码调研进行中。

#### Q1 — source_id 是什么

> 你说的 `source_id`，是指 `public.day01` 这样的来源表名，还是另一个独立 ID？A. 就是来源表名，沿用 `SOURCE_TABLE` / `--source-table`；B. 新增独立来源 ID，需要映射到来源表。
>
> 推荐 A：沿用已有来源表输入，只新增自动查找与选择行为。
>
> 用户回答（第 1 轮合并回答逐字摘录）：Q1:A

正文关联：第 3、4、5 节。
回答来源：第 1 轮合并回答，Q1 对应行。
当前解释：采纳本题 A；有效要求已同步正文。

#### Q2 — 覆盖哪些入口

> 这次功能覆盖哪些入口？A. Pipeline CLI，加上 Make 全量和冒烟入口；两个 Make 入口同时支持显式 `REFINE_ID`，并传递 `REFINE_RESUME_LAST`；B. 只做 Pipeline CLI；C. Pipeline CLI 加 Make 全量入口，冒烟入口保持现状。
>
> 推荐 A：让你平时使用的 Make 命令也能完成交互选择、自动选择和显式 UUID 复用。
>
> 用户回答（第 1 轮合并回答逐字摘录）：Q2：A

正文关联：第 2、4、5、8 节。
回答来源：第 1 轮合并回答，Q2 对应行。
当前解释：采纳本题 A；有效要求已同步正文。

#### Q3 — 不传与 false 的语义

> `refine_resume_last=true` 已明确为自动复用。剩下两种输入怎么区分？A. 不传时询问是否复用，false 时强制新建 Refine；B. 不传或 false 都询问是否复用，强制新建通过提示中回答否；C. 不传或 false 都直接新建，只有 true 才查找复用。
>
> 推荐 A：不传满足你要求的命令行提示；显式 false 提供跳过提示、重新执行 Refine 的方式。参数仍是可选 Bool，但需要保留“未传入”这个状态。
>
> 用户回答（第 1 轮合并回答逐字摘录）：Q3：A

正文关联：第 3、4、5、7 节。
回答来源：第 1 轮合并回答，Q3 对应行。
当前解释：采纳本题 A；有效要求已同步正文。

#### Q4 — 没有可复用结果

> 对应来源没有任何已完成 Refine 时，尤其是 `refine_resume_last=true`，怎么办？A. 显示未找到历史结果，然后新建 Refine 继续执行；B. 自动模式报错退出，交互模式允许新建。
>
> 推荐 A：第一次运行也能使用同一条命令，无需先判断是否有历史结果。
>
> 用户回答（第 1 轮合并回答逐字摘录）：Q4：A

正文关联：第 3、4、7 节。
回答来源：第 1 轮合并回答，Q4 对应行。
当前解释：采纳本题 A；有效要求已同步正文。

#### Q5 — 最近按什么时间排序

> 数据库同时有 `started_at` 和 `finished_at`。“最近”采用哪一个？A. 按开始时间 `started_at DESC`，只考虑 completed；B. 按完成时间 `finished_at DESC`，只考虑 completed。两者在任务运行时间不同时可能选中不同的 UUID。
>
> 推荐 A：与你给出的 SQL 一致，也已有对应索引。
>
> 用户回答（第 1 轮合并回答逐字摘录）：Q5：A

正文关联：第 3、4、5、7 节。
回答来源：第 1 轮合并回答，Q5 对应行。
当前解释：采纳本题 A；有效要求已同步正文。

第 1 轮更新检查：Q1–Q5 完整问题、选项、推荐及逐题回答已保存；完整合并原文见下；有效决定已同步正文。


### 第 1 轮合并回答 — 对应 Q1–Q5

接收日期：2026-09-26。
用户回答原文：

> Q1:A
> Q2：A
> Q3：A
> Q4：A
> Q5：A

解释范围：Q1–Q5 全部选择 A，无额外限定；不构成完整设计确认或实施授权。
正文同步：第 2–5、7、9 节及访谈进度。

### 第 2 轮 — 选择与复用边界（2026-09-26）

发问时前提：Q1–Q5 均确认 A；沿用来源表、覆盖三个入口、三种启动行为已定、无候选新建、按开始时间降序。

#### Q6 — 交互提示如何选择

> 默认交互模式找到多个已完成结果时，怎么提示？A. 只展示最新一条的 UUID、来源表、开始时间，询问“是否复用？[Y/n]”；回车或 yes 复用，no 新建，其他输入重新询问；更旧结果通过显式 REFINE_ID 指定。B. 展示历史列表，用户输入序号选择，也可选择新建。
>
> 推荐 A：与你的“提示是否使用”一致，通常一次回车即可继续。
>
> 用户回答（第 2 轮合并回答逐字摘录）：Q6 A

正文关联：第 3、4、7 节。
回答来源：第 2 轮合并回答，Q6 对应行。
当前解释：采纳 A：最新一条、默认复用、拒绝新建、非法输入重问。

#### Q7 — 无交互输入怎么办

> 未传 Bool 且存在候选，但没有交互终端，或提示遇到 EOF 时，怎么办？A. 退出并提示显式传 true（自动选择）或 false（新建）；Ctrl+C 也取消运行。B. 自动复用最新结果。C. 自动新建 Refine。无候选仍按 Q4 新建。
>
> 推荐 A：脚本明确选择执行方式；无法回答提示时，退出而不自行决定复用或重新计算。
>
> 用户回答（第 2 轮合并回答逐字摘录）：Q7 A，暴露问题而不是自作主张

正文关联：第 3、4、7 节。
回答来源：第 2 轮合并回答，Q7 对应行。
当前解释：采纳 A；用户强调暴露问题，不自行决定。

#### Q8 — 显式 UUID 与其他参数的组合

> 显式 REFINE_ID 应怎样与其他输入组合？A. SOURCE_TABLE 与 REFINE_ID 保持二选一；REFINE_RESUME_LAST 只允许随 SOURCE_TABLE 使用；REFINE_ID 单独使用时直接复用，不查询最新、不提示；冲突参数报错。B. 同时传入时 REFINE_ID 优先，忽略其他选择参数。
>
> 推荐 A：保留现有二选一规则，并让每种启动输入只表达一个执行意图。
>
> 用户回答（第 2 轮合并回答逐字摘录）：Q8 A，二选一，reine\_resume\_last是source——table的同时使用

正文关联：第 4、5、7 节。
回答来源：第 2 轮合并回答，Q8 对应行。
当前解释：采纳 A；原文标识符拼写保留，正文采用 refine_resume_last 与 source_table。

#### Q9 — 当前 Refine 配置改变后是否仍复用

> 已完成候选的 Parser/Refine 参数或投影与当前配置不同，是否仍允许选择它？复用会沿用历史 Refine 结果与投影，当前 Parser/Refine 设置不会重新作用于历史数据。A. 允许，保持现有显式 UUID 复用语义；希望应用新配置时传 false 新建。B. 只有相关配置一致的批次才成为自动或交互候选；显式 UUID 仍按原逻辑处理。
>
> 推荐 A：按来源表查找最新 completed，与这次目标和现有复用行为一致；自动模式也输出选中的 UUID、时间及历史投影。
>
> 用户回答（第 2 轮合并回答逐字摘录）：Q9 继承历史结果，显式输出继承了那些

正文关联：第 3–5、7、9 节。
回答来源：第 2 轮合并回答，Q9 对应行。
当前解释：采纳历史结果继承，并增加具体继承信息输出要求；输出范围随 Q13 最终确认。


#### Q10 — 来源表名怎样匹配

> `day01` 与 `public.day01` 可能指同一张表，但当前数据库保存原输入文本。自动查找怎样匹配？A. 按保存的 source_table 文本精确匹配，建议统一输入 public.day01；不追溯合并别名。B. 解析实际表身份，将不同写法指向同一张表的历史结果一起查找。
>
> 推荐 A：沿用当前保存和匹配方式，本次不增加历史名称解析。
>
> 用户回答（第 2 轮合并回答逐字摘录）：Q10 沿用当前规则

正文关联：第 3–5、7 节。
回答来源：第 2 轮合并回答，Q10 对应行。
当前解释：沿用当前原输入文本规则，即 A。

#### Q11 — 最新 completed 的产物不可用

> 最新 completed 的文件缺失或校验失败时怎么办？A. 明确报错退出，不自动尝试更旧结果或重新计算；用户可以显式指定另一个 UUID，或传 false 重算。B. 尝试更旧的可用 completed，全部不可用才新建 Refine。
>
> 推荐 A：保持“选择最新一条”的含义及当前产物失败行为，显示失败 UUID 和原因。
>
> 用户回答（第 2 轮合并回答逐字摘录）：Q11 A

正文关联：第 3、4、7、9 节。
回答来源：第 2 轮合并回答，Q11 对应行。
当前解释：采纳 A：失败退出，不自动尝试旧结果或重算。

#### Q12 — 是否提前核对当前来源数据

> 自动发现候选时，是否新增当前来源表与历史 Refine 的完整指纹核对？现有正式运行不会在启动前做这次完整扫描。A. 本次保持现有校验范围，只增加发现和选择；不额外全表扫描。B. 在提示或自动复用前完整核对当前来源表；不一致则排除该候选。B 会额外读取完整来源数据，具体无合格候选和失败规则在后续轮次确认。
>
> 推荐 A：本次按 source_table 和 completed 查找并复用，保持现有运行成本与校验范围；Spec 明确它不保证当前来源表与历史运行时的数据相同。
>
> 用户回答（第 2 轮合并回答逐字摘录）：Q12 不增加全表扫描校验

正文关联：第 3–7、9 节。
回答来源：第 2 轮合并回答，Q12 对应行。
当前解释：不增加全表扫描校验；本次也不增加历史引用来源行的指纹核对。

第 2 轮更新检查：Q6–Q12 逐题回答及完整合并原文均保存；正文已同步所有有效要求。


### 第 2 轮合并回答 — 对应 Q6–Q12

接收日期：2026-09-26。
用户回答原文：

> Q6 A
> Q7 A，暴露问题而不是自作主张
> Q8 A，二选一，reine\_resume\_last是source——table的同时使用
> Q9 继承历史结果，显式输出继承了那些
> Q10 沿用当前规则
> Q11 A
> Q12 不增加全表扫描校验

解释范围：Q6–Q12 均已回答；Q9 要求显式输出继承内容，其具体展示范围整理在正文，待 Q13 最终确认。未作完整设计确认或实施授权。
正文同步：第 2–10 节及访谈进度。

### 第 2 轮后事实澄清（2026-09-26）

前轮 Q12 问题中的“完整核对当前来源表”保留为原始提问，不代表现有函数实现。进一步核查发现，已有 Replay/Compare 指纹函数仅核对 Refine 引用的历史来源行，并非源表全部行。本次不新增该调用，也不新增其他全表扫描；不影响用户 Q12 决定。

历史参数快照包含 `semantic_config`、`execution_config`、`config_digest`；后续阶段明确使用当前配置。因此不能将整份快照称为继承的配置。依据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/config/pipeline_config_loader.py:105](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/config/pipeline_config_loader.py:105) 、 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/orchestration/run_pipeline.py:73](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/orchestration/run_pipeline.py:73) 。

### 第 3 轮 — 完整设计确认（2026-09-26）

发问时前提：Q1–Q12 的有效决定已同步正文，业务分支已收敛；具体接口与继承展示随本轮完整设计确认。

#### Q13 — 完整设计与实施意向

> 请确认正文第 2–9 节的完整方案：覆盖 CLI 与两个 Make 入口；SOURCE_TABLE/REFINE_ID 二选一；可选 --refine-resume-last true|false，Make 为 REFINE_RESUME_LAST=true|false；未传时交互、true 自动复用、false 新建；精确匹配来源文本，选择 started_at 最新的 completed；无候选新建；无交互输入或复用失败时报错退出；不回退旧批次、不增加来源扫描。复用时展示 UUID/来源/开始和完成时间、复用的数据范围、历史 Parser/Refine 生成参数、历史 source_srid/processing_srid/projection_config；明确当前下游仍用当前配置。A. 确认完整设计并开始实施；B. 确认完整设计，暂不实施；C. 需要修改，指出具体内容。
>
> 推荐 A（如果现在希望完成这项功能）；设计确认与实施授权会分别记录。
>
> 用户回答：Q13 确认，指定让subagent gpt-6-luna-high实施和跑测，形成文本结果返回，本session只验收

正文关联：第 2–10 节。
当前解释：完整设计已确认；实施与跑测授权给 gpt-6-luna（high）子代理，主 session 仅负责验收；文本报告已保留；恢复与验收结论见后续记录。

本轮检查：Q1–Q12 回答原文及全部问题已保存；正文与有效决定一致；状态已确认，Q13 原文已保存；实施和验收角色已明确；QA 附录位于文末。


### Q13 后续恢复与验收补充（2026-09-27）

关联原记录：第 3 轮 Q13；用户先前限定指定子代理实施和跑测、主 session 只验收，本次继续遵循。

用户补充原文：

> 任务意外中断了，检查修改情况，我跑了下100w行的smoke-test，全流程跑通没问题，日志在/data/tmp/road-network/logs/20260926\_234453\_620938\_pid-1\_log.txt &#x20;
>
> 上次工作到哪了？继续工作，需要我手动验收什么吗？如果不需要就继续工作，如果需要及时暴露提醒我

当前解释：用户要求恢复核查并继续收尾，提供百万行真实冒烟结果；没有修改设计。主 session 核对现有实现、子代理报告、保留的测试日志及该冒烟日志后完成功能范围验收，无需用户额外操作。Backend 既有工作区单测失败单列；Pipeline 完整测试未保留独立原始日志这一证据限制也在报告标明。
正文同步：更新日期、第 8–10 节及实施与验收证据；QA 历史保持原样。

本次检查：Q1–Q13 历史记录保留，新增用户补充原文与验收结论；设计已确认、实施完成、本功能验收完成；仓库整体测试未全通过；QA 附录位于文末。
