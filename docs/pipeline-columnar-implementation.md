# Pipeline 列式架构分阶段实施任务卡

当前设计依据：[Pipeline 列式存储与计算架构 Spec](pipeline-columnar-architecture-spec.md)。
旧任务卡 [Pipeline 流式实施记录](pipeline-streaming-implementation.md)仅保留历史证据，
不再作为当前实施依据。

状态：实施中
创建日期：2026-09-19
更新日期：2026-09-20

## 1. 执行规则

- 实施代理统一使用 `gpt-5.6-sol`、`low` 推理；主对话负责验收、集成和继续派发。
- 写任务按依赖顺序执行；所有代理共享工作树和 Git 索引，禁止并发提交相互覆盖。
- 每个本地提交的新增与删除合计软限制为 700 行；大量纯重命名提交不受限制。
- 每张任务卡只提交卡内文件，不纳入用户已有或其他任务的改动；禁止推送远端。
- 不删除 `/data` 运行数据，不运行破坏性数据库操作。真实数据库清理只提供显式说明。
- 每卡必须先检查工作树，完成针对性测试，报告提交哈希、diffstat、测试与未决风险。
- DuckDB → Polars → 最小 Python 对象的每处回退理由必须由测试记录。
- 未完成 26M 实测前不得声明 2 GB 或一小时目标通过。

## 2. 阶段与依赖

| 卡片 | 目标 | 依赖 | 目标提交规模 | 状态 |
| --- | --- | --- | ---: | --- |
| C00 | 固化新 Spec、旧 Spec 跳转和本任务卡 | 无 | 文档 QA 例外 | 已完成（`e31eae2`） |
| C01a | 引入依赖兼容范围与集中运行配置 | C00 | ≤700 | 已完成（`fca33cf`） |
| C01b1 | 固定 Python 依赖解析版本 | C01a | ≤700 | 已完成（`12cdba3`） |
| C01b2 | 准备离线 DuckDB PostgreSQL 扩展 | C01b1 | ≤700 | 已完成（`ecebe2a`） |
| C02a | PostgreSQL Schema v5 制品登记 DDL 与迁移 | C01b2 | ≤700 | 已完成（`e05214d`） |
| C02b | 制品描述校验与最小登记 API | C02a | ≤700 | 已完成（`04d62ce`） |
| C02c | 制品登记真实 PostgreSQL 集成验证 | C02b | ≤700 | 已完成（`b0db129`） |
| C03a | 安全 staging 与同文件系统原子只读发布 | C01b2–C02c | ≤700 | 已完成（`bc7e77c`） |
| C03b | Parquet 检查、快速消费校验与 orphan 识别 | C03a | ≤700 | 已完成（`af4d098`） |
| C04a | artifact reconcile/gc 只读计划 | C03b | ≤700 | 已完成（`9bc7f6b`） |
| C04b1 | artifact reconcile/gc 显式 apply 核心 | C04a | ≤700 | 已完成（`615f4ce`） |
| C04b2 | artifact reconcile/gc CLI 与真实 PostgreSQL 验证 | C04b1 | ≤700 | 已完成（`9d62a5a`） |
| C05a | Refine Parquet Schema 与 staging 导出 | C04b2 | ≤700 | 已完成（`3e5f6a8`；语义修复 `cc7d25a`） |
| C05b | Refine Parquet 发布、登记与 Run 完成事务 | C05a | ≤700 | 已完成（`8050876`） |
| C05c1 | Refine 真实 PostgreSQL 差分与发布故障验证 | C05b | ≤700 | 已完成（`5a25516`） |
| C05c2 | completed 制品约束迁移与 Refine 最终切换闸门 | C05c1 | ≤700 | 已完成（`b6a9e03`） |
| C06a | Refine Parquet 只读消费边界 | C05c2 | ≤700 | 已完成（`1fe007f`） |
| C06b | Detect 接入 Refine Parquet reader | C06a | ≤700 | 已完成（C06b1 `aac372c`；C06b2 `832ff99`） |
| C07a | Detect 转向候选列式迁移 | C06b | 每提交 ≤700 | 已完成（`cfecadd`） |
| C07b | Detect 跨轨迹候选准备列式迁移 | C07a | 每提交 ≤700 | 已完成（`2fc3146`） |
| C07c1 | Detect 跨轨迹 DuckDB broad phase | C07b | 每提交 ≤700 | 已完成，`6551037`，待主对话确认 |
| C07c2 | Detect 跨轨迹精确内核与正式入口接线 | C07c1 | 每提交 ≤700 | 已完成（`a30ace3`） |
| C08a | Detect Parquet staging export 与 v1 Schema | C07c2 | 每提交 ≤700 | 已完成（`58013b9`；Q41 修复 `aece70c`） |
| C08b1 | Detect 制品数据库约束与事务中立 API | C08a | 每提交 ≤700 | 已完成（`2ee7f28`） |
| C08b2 | Detect Parquet 发布与失败生命周期接线 | C08b1 | 每提交 ≤700 | 已完成（`a541fe9`） |
| C08c1 | Detect Parquet v1 只读消费边界 | C08b2 | 每提交 ≤700 | 已完成（`fa63a9e`） |
| C08c2 | Clip 正式入口接线 | C08c1 | 每提交 ≤700 | 已完成（`319a110`） |
| C09a | Clip 规范化列式临时关系 | C08c | 每提交 ≤700 | 已完成（`1a6d1f3`） |
| C09b | Clip/Fitting 接线与 memmap 移除 | C09a | 每提交 ≤700 | 已完成（`51a9c97`） |
| C09c1 | Detect TurnEvent 规范化列式关系 | C09b | 每提交 ≤700 | 已完成（`1572764`） |
| C09c2 | Detect TurnEvent 接线及旧 spool 删除 | C09c1 | 每提交 ≤700 | 已完成（`853da8c`） |
| C10a1 | Detect CrossTrackCandidate 规范化列式关系 | C09c2 | 每提交 ≤700 | 已完成（`7b3e6c4`） |
| C10a2 | Detect 跨轨迹候选多 pass 簇成员关系 | C10a1 | 每提交 ≤700 | 已完成（`aa60989`） |
| C10a3 | Detect 通用成员序列 helper 剥离 | C10a2 | 每提交 ≤700 | 已完成（`a16bf52`） |
| C10a4 | Detect 正式接线并删除旧跨轨迹成员暂存 | C10a3 | 每提交 ≤700 | 已完成（`e753f71`） |
| C10b1 | Topology 窗口 Refine reader | C10a4 | 每提交 ≤700 | 已完成 |
| C10b2 | Topology 正式接线 | C10b1 | 每提交 ≤700 | 已完成，待主对话确认 |
| C10b3 | Detect 列式迁移及旧工作区删除 | C10b2 | 每提交 ≤700 | 已完成（待主对话确认） |
| C10c1 | Writer 原始关系与精确来源映射 | C10b3 | 每提交 ≤700 | 已完成，待主对话确认 |
| C10c2 | Writer 候选归属与热点关系 | C10c1 | 每提交 ≤700 | 已完成，待主对话确认 |
| C10c3 | Writer 最终行组合与配对校验 | C10c2 | 每提交 ≤700 | 已完成，待主对话确认 |
| C10c4 | Writer 正式接线 | C10c3 | 每提交 ≤700 | 已完成，待主对话确认 |
| C10c5 | Writer 旧工作区删除 | C10c4 | 每提交 ≤700 | 已完成，待主对话确认 |
| C11 | 进程池、协调器、资源预留、任务账本清除 | C02c–C10 | 每提交 ≤700 | 已开始（C11a–C11f） |
| C12 | 文档取代标记、全套回归和 1M 差分 | C11 | 每提交 ≤700 | 未开始 |
| C13 | 26M、默认 2 GB、全链路一小时最终验收 | C12 | 不适用 | 未开始 |

## 3. 任务卡

### C00 — 设计与任务卡基线

范围：

- 提交 `pipeline-columnar-architecture-spec.md`；
- 提交旧 20M Spec 顶部的“已取代”跳转和本次 QA 记录；
- 提交本任务卡；
- 不纳入 `docs/config-centralization-spec.md` 或其他用户改动。

验证：链接存在、Q19–Q63 连续、状态/实施授权一致、QA 附录位于文末。

提交：`e31eae2`。

### C01a — 依赖兼容范围、路径和引擎配置

范围：

- 在 Pipeline 依赖中加入受控 DuckDB、Polars 版本范围；
- 扩展 `RuntimeConfig`/`pipeline.toml`：DuckDB 1 GB、最多 4 线程、临时盘上限，
  Polars 线程、Parquet 文件/行组目标、GC 7 天；
- 保持 `DATA_ROOT`/`PARQUET_ROOT` 环境挂载语义，新增派生的 duckdb/polars 路径检查；
- 只建立配置与启动检查，不迁移业务算法。

验证：配置默认值、非法值和运行时路径测试。

提交：`fca33cf`。

### C01b1 — 精确版本锁

范围：为 Python 3.12 容器生成带哈希的完整依赖锁，锁定构建后端版本，并令镜像先按锁
安装依赖，再以禁用依赖解析和构建隔离的方式安装项目。本卡保持开发依赖在镜像中；
生产与开发锁拆分留待后续处理。

验证：锁文件重新生成无差异，DuckDB/Polars 精确版本，镜像构建及容器内版本；全量测试
`1165 passed, 55 skipped`。

提交：`12cdba3`。

### C01b2 — 离线 DuckDB PostgreSQL 扩展

范围：镜像构建期准备固定 DuckDB PostgreSQL 扩展，运行时禁止下载。依赖或扩展
升级必须重跑语义差分与 26M 基准。

验证：构建仅允许 `linux/amd64`，下载的 gzip 和解压扩展均校验 SHA-256；容器在
`--network none` 下关闭 DuckDB 扩展自动安装和自动加载，从固定绝对路径显式 `LOAD`，
并确认 `postgres_scan`、`postgres_attach` 可识别；`TARGETARCH=arm64` 构建按预期拒绝；
Pipeline 全量测试 `1165 passed, 55 skipped`。

提交：`ecebe2a`。

### C02a — Schema v5 制品登记 DDL 与迁移

范围：新增不可变制品元数据、追加式 present/deleted 事件、Schema/来源指纹和 Run
关联；提供事务性 v4→v5 加法迁移。不得增加生产者尚未满足的 completed Run 制品约束。

验证：DDL 约束、追加生命周期、不可变保护、迁移选择、幂等及事务回滚单元测试。

提交：`e05214d`。

### C02b — 制品登记 API

范围：实现制品描述校验与不提交事务的最小登记 API；生产者接线留在 C05/C08。

验证：合法/非法清单、重复发布、跨 Run 隔离、未知 Schema 拒绝和事务边界测试。

提交：`04d62ce`。

### C02c — 真实 PostgreSQL 集成验证

范围：在隔离 Schema 中验证 v4→v5、数据库约束、并发事件顺序和事务回滚。

验证：真实 PostgreSQL 上迁移、约束、锁定、幂等和回滚均符合 C02a/C02b 语义。

实现：新增 `ROADNET_TEST_DSN` 显式启用的真实 PostgreSQL 测试；普通用例在事务内创建
唯一 Schema 并回滚，并发用例仅提交自身 UUID Schema，验证事件触发器以制品行锁串行化，
最终显式清理该测试 Schema。

提交：`b0db129`。

### C03a — 安全 staging 与原子只读发布

范围：建立受控根路径内的 Run staging，以一次同文件系统目录改名完成不可覆盖的只读
发布，并提供严格受限的 staging 放弃操作；不接生产者、数据库或业务清单。

验证：成功发布、空或半写失败、目标不覆盖、路径逃逸、跨设备、改名后数据库失败仍
保留 final，以及 staging 放弃边界测试。

提交：`bc7e77c`。

### C03b — Parquet 校验与 orphan 识别

范围：在 C03a 发布原语上增加文件清单、大小、Parquet footer、SHA-256、快速消费校验和
orphan 识别；不实现阶段业务 Schema。

验证：篡改、缺失、未知 Schema、footer、清单与只读消费测试。

提交：`af4d098`。

### C04a — 对账与垃圾回收只读计划

范围：以一次只读数据库查询和受控目录扫描生成确定排序的对账计划；区分登记状态、
缺失、残留、orphan、staging 和异常项。已完成或运行中的 Run 均受保护；只有已失败且
超过宽限期的制品，以及超过宽限期的未登记目录，才可进入后续 apply。

验证：查询零写入、状态保护、7 天边界、orphan/staging、符号链接与异常状态测试。

提交：`9bc7f6b`。

### C04b1 — 显式 apply 核心

范围：消费 C04a 计划，在全批预检和数据库加锁复核后，只删除受控叶子；
对仍为 `present` 的失败 Run 制品，删除文件后追加 `deleted` 事件。调用者拥有事务。

验证：计划过期、路径异常和符号链接全批零修改；删除/事件顺序、残留、orphan、
staging 和事件失败语义由伪游标与临时目录单元测试覆盖。

提交：`615f4ce`。

### C04b2 — CLI 与真实 PostgreSQL 验证

范围：实现默认 dry-run、显式 apply 的 reconcile/gc CLI，不新增后台线程；
用真实 PostgreSQL 验证加锁复核、事件约束和失败恢复。

验证：dry-run 零写入、幂等 apply、删除/事件顺序和失败恢复测试。

提交：`9d62a5a`。

### C05a — Refine Parquet Schema 与 staging 导出

范围：按 Spec 5.1 定义 kept_points/injected_points Schema；以 `refine_id` 隔离，
从 PostgreSQL 权威结果导出并校验 staging，不发布或登记，也不完成 Run。

验证：Schema、行数/身份/交错顺序、SQL 隔离、DuckDB 离线配置和空表文件。

提交：`3e5f6a8`；语义修复 `cc7d25a`。

### C05b — Refine Parquet 发布与登记

范围：核对来源指纹，原子发布文件，并在一个 PostgreSQL 事务中登记制品、追加
`present` 事件及完成 Refine Run；失败时不暴露不完整数据集。

验证：来源变化、发布/登记/完成顺序、各故障点恢复和 orphan 行为。

提交：`8050876`。

### C05c1 — Refine 真实集成与故障验证

范围：以真实 PostgreSQL/PostGIS 和镜像内 DuckDB `postgres_scanner` 执行逐表差分与
发布故障验证；不在本卡增加 completed Run 的数据库约束。

验证：小样本逐表差分、独立连接可见性、来源变化、发布后数据库事务回滚及 orphan
保留语义。

提交：`5a25516`。

### C05c2 — Refine 最终切换闸门

范围：增加 completed Refine Run 必须关联 present 制品的数据库级强制约束，并在真实
PostgreSQL 上完成迁移、并发和回滚验证；通过后才允许 C06 消费 Refine Parquet。

验证：既有数据迁移、completed/failed/running 状态约束、制品事件状态、并发提交和
回滚。

提交：`b6a9e03`。

### C06a — Refine Parquet 只读消费边界

范围：按 `refine_id` 从登记表严格重建制品及最新生命周期状态；消费前校验发布结构、
清单、大小、Schema 和 footer。DuckDB 仅扫描清单声明文件，并按车辆及子轨迹返回有界
批次；禁止整表 Python 物化。

验证：登记重建、最新事件、未知或非 present 拒绝、声明文件、v1 表及 Schema、
kept/injected 合并顺序、车辆边界、稳定身份、只读校验和批次接口。

提交：`1fe007f`。

### C06b1 — Detect 输入适配与旧路径差分

范围：在 C06a reader 上增加全数据集有界顺序读取及列式 extent 查询，逐子轨迹适配为
现有 `TrajectoryPoint` 输入对象；保留稳定身份并严格拒绝损坏数据。本卡不切换正式
Detect/Pipeline 入口，也不改变 Detect 算子。

验证：跨读取批次、多个车辆及子轨迹、kept/injected 顺序、身份、extent 和损坏输入；
真实 PostgreSQL 样本与旧读取路径逐轨差分。Parquet v1 不携带 `extra`，适配结果固定为
空字典，差分明确排除此字段。

提交：`aac372c`。

### C06b2 — Detect/Pipeline 正式接线

范围：将正式 Detect/Pipeline 入口切换至 C06b1 只读接口；PostgreSQL 路径仅作
显式未传 reader 的兼容和测试入口，正式 Pipeline 不自动回退；不改变 Detect 算法。

验证：入口选择、生命周期与错误传播、extent 消费、端到端 Detect 回归及资源边界。

提交：`832ff99`。C06 总阶段已完成。

### C07a — Detect 转向候选列式迁移

范围：以 DuckDB 对 Refine 清单视图执行按车辆、子轨迹排序的转向窗口、圆均值和阈值
过滤；过滤后才构造最小 `TurnEvent`。正式 Parquet 路径不再调用 Python 转向算子，旧
PostgreSQL 路径保留为差分参考实现。

验证：真实 Parquet 与 `detect_turn_patch` 按 Q49 差分，覆盖窗口、圆周、阈值、异常
时间、短轨、多车辆/子轨迹和读取批次边界；断言正式路径未调用 Python 转向算子。

提交：`cfecadd`。

### C07b — Detect 跨轨迹候选准备列式迁移

范围：以 DuckDB `lead` 窗口迁移真实相邻线段构造、稳定排序、车辆/轨迹 rank 和直行
过滤；Python 仅构造精确内核所需的最小线段对象。旧实现按
`vehicle_id || chr(0) || CAST(subtrack_index AS VARCHAR)` 排序轨迹，因此本卡明确保留其
十进制字符串字典序，而不是子轨迹数值序。本卡不构造线段对、不切换正式跨轨迹入口，
也不删除 Detect 工作区。

验证：真实 Parquet 与旧 `_prepare_segments` 按 Q49 严格差分，覆盖多车辆/子轨迹、
batch=1、方向角圆周、包含阈值、单点轨迹和禁止跨子轨迹连接；拒绝损坏身份、非有限值、
非相邻序列及全局 `subtrack_index` 车辆混合，并断言不调用 `fetchall()`。

提交：`2fc3146`。

### C07c1 — Detect 跨轨迹 DuckDB broad phase

范围：用 DuckDB 展开普通线段网格并以共享最小 cell 去重；overflow 线段沿用旧 oracle
与所有异车有限线段配对的语义，并在 SQL 执行前按现有 pair-space 上限 fail closed。
提供只读取批接口；不运行精确 NumPy 求交，不接正式 Detect，不迁移 DBSCAN 或拓扑。

验证：真实 Parquet 与旧 grid/overflow oracle 的规范线段身份 pair 集合严格一致，覆盖
边界/负坐标、多 cell 去重、同车排除、不同子轨迹、overflow、批量和空输入；断言不调用
`fetchall()`。

提交：`6551037`。

### C07c2 — Detect 跨轨迹精确内核与正式入口接线

范围：按 C07c1 有界 pair 批次接线现有精确 NumPy 求交内核与正式 Parquet Detect 入口；
本卡才比较 crossing 输出，并仍不迁移 DBSCAN 或拓扑。

验证：Q49 crossing 配对与坐标容差、正式入口选择、错误传播及资源边界。

提交：`a30ace3`。

后续 C07d 收敛 DBSCAN/聚类的最小 Python 回退，并明确候选存储移除边界；本卡保留
`CandidateStore` 和拓扑所需 tracks 工作区，不扩大到聚类或拓扑迁移。

### C08a — Detect Parquet staging export 与 v1 Schema

范围：按 Spec 5.2 从已提交的 Detect PostgreSQL 权威结果行，以只读 DuckDB 扫描写出
resolved_points/intersections/entry_exit_points 三张显式 v1 逻辑表；Run 在导出、校验和
后续发布期间仍为 running。本卡只构建 staging 与描述符，不发布、不登记、不完成 Run、
不控制事务。

验证：Schema、清单、空表、owner/status、数值/枚举/引用、连续顺序、未知版本，以及
真实 PostgreSQL 与现有 Clip 查询的 intersection 和 resolved p/i/c 序列差分。Q44
评审结论由测试固定：本卡仅需 DuckDB 关系投影和校验，不需要 Polars 或 Python 行对象。

提交：`58013b9`；Q41 修复 `aece70c`。

### C08b1 — Detect 制品数据库约束与事务中立 API

范围：提供严格 Detect 制品查询、同事务登记并完成 Run 的存储 API，以及 v6→v7
加法迁移；fresh 初始化和 v3/v4/v5 迁移链最终统一到 v7。只建立数据库约束，不接
发布生命周期。

验证：事务中立 API、迁移版本顺序，以及真实 PostgreSQL 上缺失、错误归属、重复、
deleted 状态和失败回滚。

迁移不为 v6 中已有的 completed Detect Run 回填或伪造制品；C08c 消费时对此类历史 Run
按 fail closed 处理，需要按 Q45/Q54 以新 Run 重跑。

提交：待记录。

### C08b2 — Detect Parquet 发布与失败生命周期接线

范围：把 C08a staging exporter 接入 Detect 完成路径，落实原子发布、制品登记、来源
指纹复核与失败/orphan 生命周期；不改变 Clip 消费入口。
权威 Detect 结果行先提交以供 exporter 可见，Run 在来源指纹前后复核、原子发布
和最终登记事务完成前保持 `running`。`detect_persist_s` 覆盖结果持久化、制品导出、
发布与登记的完整持久路径。最终事务失败时保留未登记的只读 orphan，并将仍为
`running` 的 Run 永久标记为 `failed`。

验证：发布顺序、登记事务、失败注入、只读权限和对账可见性。

提交：待记录。

### C08c1 — Detect Parquet v1 只读消费边界

范围：实现已登记 Detect Parquet v1 的严格只读读取器，逐子轨迹构造现有 Clip 模型；
本卡不改变 Clip 正式入口。

验证：三张逻辑表的归属、Schema、序列、车辆和 p/i/c 关系，真实 PostgreSQL 读取器
差分，以及未知版本、缺失/额外文件和只读消费。

提交：`fa63a9e`。

### C08c2 — Clip 正式入口接线

范围：将正式 Clip 入口切换到 C08c1 列式检查点；保持现有 PostgreSQL 路径作为短期
差分 oracle，不改变前后端字段。正式入口只从 PostgreSQL 读取 Run 与制品登记元数据，
不再读取点流或复核 Refine 源表指纹；制品缺失、已删除或损坏时直接失败，不回退。
PostgreSQL 事务边界仍由 runner 管理，加载器不隐式提交；读取器上下文覆盖完整轨迹迭代。

验证：三个逻辑表逐表差分；两种边界片段配置下的完整 Clip 输出、顺序、报告和交叉口
严格差分；批量大小为 1 的读取边界；制品状态与内容损坏时关闭 DuckDB 和磁盘暂存并
直接失败；正式模块不存在旧 PostgreSQL 点流或源指纹入口。Q44 评审结论由测试固定：
DuckDB 已完成列式读取、排序和关系校验，Polars 不会减少数据搬运；Python 仅逐条构造
一个现有 ClipTrack 并执行现有状态机，不引入全量对象图。

提交：`319a110`。

### C09a — Clip 规范化列式临时关系

范围：建立 DuckDB 管理的 Clip 临时列式关系，规范化保存片段标量、端点与片段内点，
支持按原始顺序和交叉口对稳定重复读取。本卡不接正式 Clip/Fitting，也不删除旧暂存。
采用 DuckDB 是为了避免 Python 全量对象和自写外溢；现有 SQL 已覆盖筛选、排序和关系
校验，Polars 不会减少数据搬运，Python 仅逐片段重建现有模型。

验证：空关系、无片段内点、批量大小为 1、重复迭代、精确读取、配对与方向筛选、
完整性校验、关闭语义及 DuckDB 资源配置。

提交：`1a6d1f3`。

### C09b — Clip/Fitting 接线与 memmap 移除

范围：将 Clip/Fitting 正式路径接入 C09a 关系，删除 Fitting 的自写内存映射外溢，保持
psycopg 事务兼容与输出字段不变。DuckDB 负责片段关系的重复筛选与稳定排序；变长几何
插值无法直接交给 DuckDB，Polars 对逐片段插值和按采样位置精确中位数没有减少数据搬运
的收益，因此 Python 只按交叉口对重建片段，并以有界采样进度块多遍读取关系完成精确
中位数，不保留全量采样矩阵。

验证：Clip/Fitting 行为、失败回滚、配对顺序和中心线/清单差分。

提交：`51a9c97`。

### C09c1 — Detect TurnEvent 规范化列式关系

范围：以 DuckDB 管理的规范化临时列式关系无损保存转向事件及有序来源 ID，支持完整事件
与紧凑 G* 坐标的有界重复读取。本卡不改变 Detect 正式入口，也不删除旧暂存。DuckDB
负责存储、排序和完整性校验；Polars 不会减少关系读取或对象重建的数据搬运，Python 仅
逐事件重建既有模型。

验证：空关系、完整可空引用、来源顺序、批量大小为 1、重复迭代、索引、坐标流、损坏
关闭、资源配置、临时目录碰撞与构造失败。

提交：`1572764`。

### C09c2 — Detect TurnEvent 接线及旧 spool 删除

范围：以 DuckDB 列式关系承接 Detect 转向事件，移除最后的 `DiskSequence` 调用者并删除
旧暂存模块及专用测试。

验证：Detect 转向候选、持久化与完整 Pipeline 回归；生产源码不再引用旧暂存。

提交：`853da8c`。

### C10a1 — Detect CrossTrackCandidate 规范化列式关系

范围：以 DuckDB 管理的规范化临时列式关系无损保存跨轨迹候选，支持完整候选与紧凑坐标
的有界重复读取。本卡不接正式 Detect，不迁移成员 pass，也不删除旧暂存。

验证：空关系、完整可空引用、批量大小为 1、重复迭代、索引、坐标流、损坏关闭、资源
配置、临时目录碰撞与构造失败。

提交：`7b3e6c4`。

### C10a2 — Detect 跨轨迹候选多 pass 簇成员关系

范围：在 `CrossCandidateRelation` 内以 DuckDB 管理可同时存活、独立关闭的多 pass 簇成员
归属记录，并提供与旧 `CandidateStore` 成员视图等价的有界重复读取、完整事件重建与来源
摘要。本卡不接 Detect 正式入口，不删除 `cross_member_store.py`。

验证：与旧成员接口差分、批量大小为 1、多 pass、噪声、标签错误、关闭与异常回滚。

提交：`aa60989`。

### C10a3 — Detect 通用成员序列 helper 剥离

范围：将无磁盘行为的通用成员序列辅助接口迁入轻量模块，并让旧、新成员视图共享只读
序列的相等与切片语义。本卡不接 Detect 正式入口，不删除 `CandidateStore` 或旧暂存模块。

验证：普通与惰性序列、反向切片、重复项、来源回退、提前关闭，以及新旧成员视图等价。

提交：`a16bf52`。

### C10a4 — Detect 正式接线并删除旧跨轨迹成员暂存

范围：将 Detect 跨轨迹候选及成员 pass 正式接入 `CrossCandidateRelation`，并删除
`cross_member_store.py`。已完成，待主对话确认。

验证：Detect 聚类、消融、持久化、来源摘要及旧模块生产引用清零。

提交：待记录。

### C10b1 — Topology 窗口 Refine reader

范围：为 Refine Parquet reader 增加基于真实相邻线段包围盒命中的完整子轨迹流式读取，
并将其 DuckDB 临时目录约束到显式数据根目录下的独占叶目录。本卡不改 Detect 编排，
不删除当时仍由 Detect 使用的旧磁盘工作区。

验证：闭区间边界、长穿窗、窗口与轨迹去重、完整顺序、损坏输入、有界读取及临时目录隔离。

提交：`c14c4ba`、`9413c18`。

### C10b2 — Topology 正式接线

范围：将 Topology 正式入口接入 Refine Parquet 窗口读取接口，保留既有持久化语义。
已完成，待主对话确认。

验证：Topology 算法、持久化及完整 Pipeline 回归。

提交：待记录。

### C10b3 — Detect 列式迁移及旧工作区删除

范围：正式 DB Detect 已收敛到 `RefineDatasetReader`、`CrossCandidateRelation` 与
Topology 窗口读取路径，并删除 SQLite 工作区、磁盘候选提取器及 PostgreSQL 直接读取
回退路径；handle 与纯内存参考接口保持不变。已完成，待主对话确认。

验证：Topology、Detect 持久化、Replay 与 Backend 字段回归。

提交：待记录。

### C10c1–C10c5 — WriterWorkspace 列式迁移及旧工作区删除

范围：C10c1 建立 Writer 原始关系与精确来源映射；C10c2 迁移候选归属与热点关系；
C10c3 完成最终行组合与跨轨迹配对校验；C10c4 正式接线；C10c5 删除无调用者的旧辅助模块。
C10c1–C10c5 已完成，待主对话确认；生产写入路径已接入
`WriterRelation`，旧工作区、临时兼容接口及纯旧原型测试均已删除。流式写入、稀疏来源映射、
候选归属、跨轨迹配对和失败回滚断言已迁移到关系实现或生产写入器测试；
`validated_cross` DuckDB 关系及 `iter_validated_cross_rows` 保持为生产接口。

验证：Writer 定向测试含真实 PostgreSQL 用例 `92 passed`；标准 Pipeline 全量测试
`1434 passed, 77 skipped`。另一次显式启用全部 PostgreSQL 用例的全量运行因旧 patch ledger
测试缺少 `ROADNET_TEST_LEDGER_DDL`，且数据库随后停止而未作为通过证据。

提交：待记录。

### C11 — 进程池与任务账本清理

范围：删除 patch spool/admission/coordinator/pool、`db/patch_ledger.py`、
`004_patch_ledger.sql` 和专用测试；提供手工 DROP 顺序；保留 heartbeat。

C11a 已删除无生产调用者的 `patch_coordinator.py` 及仅覆盖该协调器的单元测试和
PostgreSQL 集成测试。

C11b 已删除随协调器孤立的 `patch_admission.py` 手写内存与外存溢写配额预留器及其专用
测试。`patch_pool.py`、`patch_spool.py`、`db/patch_ledger.py`、`resource_budget.py` 及其
专用测试暂时保留，后续任务卡继续清理。本步不改 heartbeat、DuckDB/Polars 引擎资源
控制、PostgreSQL 业务持久化、API 或前端字段；C11 尚未完成。

C11c 已从 `resource_budget.py` 删除只服务于手写多进程 patch 池的并行预算规划器，并将
运行环境快照的执行参数白名单收敛为单进程列式执行仍使用的 `memory_limit_bytes`、
`fetch_rows` 和 `insert_rows`。只读、无凭据的 CPU、内存、cgroup、磁盘与运行参数快照
继续保留。`patch_pool.py`、`patch_spool.py`、`db/patch_ledger.py` 及其测试仍留待后续
任务卡删除；本步不改 heartbeat、DuckDB/Polars 引擎资源控制、业务 PostgreSQL、API 或
前端字段，C11 尚未完成。

C11d 以两笔提交删除自行编写的进程池：先退休专用池测试以及仅验证 pool、spool、ledger
组合的 PostgreSQL 测试，再删除已无调用者的 `patch_pool.py`。`patch_spool.py`、
`db/patch_ledger.py` 及其完整性和账本测试暂留后续任务卡；本步不改 heartbeat、
DuckDB/Polars 引擎线程或外存设置、业务 PostgreSQL、API 或前端字段，C11 尚未完成。

C11e 删除已无生产调用者的自行编写 patch 磁盘暂存模块、专用测试，以及 ledger 测试中
仅验证 spool 损坏回滚的组合用例。DuckDB/Polars 管理的临时目录与外存溢写、普通测试
临时目录、`db/patch_ledger.py`、patches 模型及其余账本测试继续保留；本步不改 heartbeat、
引擎资源配置、业务 PostgreSQL、API 或前端字段，C11 尚未完成。

C11f 以两笔提交退休已无生产调用者的 patch ledger 专用测试：先删除真实 PostgreSQL
覆盖，再删除伪连接单元覆盖并记录验证结果。本步仅为下一卡删除孤立实现解除测试约束；
`db/patch_ledger.py`、`004_patch_ledger.sql` 和 `patches.py` 仍须删除，不改 heartbeat、
DuckDB/Polars、业务 PostgreSQL、API 或前端字段，C11 尚未完成。

验证：全仓无生产引用、初始化器不引用旧结构、Pipeline 单进程路径完整。

C11a 验证：Pipeline 与 scripts 全量测试 `1447 passed, 68 skipped`；生产代码和测试中的
协调器模块及其公开符号引用清零。

C11b 验证：Pipeline 与 scripts 全量测试 `1383 passed, 68 skipped`；生产代码和测试中的
`patch_admission`、`ReservationBook`、`ReservationToken`、`ResourceUnavailable` 引用清零。

C11c 验证：环境快照定向测试 `52 passed`；Pipeline 与 scripts 全量测试
`1332 passed, 68 skipped`；四个并行预算规划器符号引用清零，旧池/外溢协调执行参数已从
快照白名单删除。

C11d 验证：第一笔 ledger/spool 定向测试 `88 passed, 17 skipped`；第二笔 Pipeline 与
scripts 全量测试 `1256 passed, 67 skipped`；生产代码和测试中的 `patch_pool`、
`ProcessPoolExecutor`、`run_bounded_patches`、`PatchTask`、`DispatchPause`、
`PatchExecutionError`、`PoolShutdownUnconfirmed`、`RemotePatchFailure` 引用清零。

C11e 验证：Pipeline 与 scripts 全量测试 `1168 passed, 66 skipped`；生产代码和测试中的 `patch_spool`、
`SpoolArtifact`、`write_patch_artifact`、`iter_artifact_chunks`、`ArtifactIntegrityError`、
`SpoolLimitError` 引用清零。

C11f 验证：第一笔 Pipeline 与 scripts 全量测试 `1168 passed, 50 skipped`；第二笔为
`1043 passed, 50 skipped`。除待删除实现、DDL、迁移说明及仍独立测试的 patches 模型外，
生产代码和测试中不再存在 patch ledger 调用者。

提交：C11d 测试退休 `1a4264a`；C11d 实现删除为本提交。

提交：C11e 为本提交。

提交：C11f PostgreSQL 测试退休 `de7cde3`；单元测试退休为本提交。

### C12 — 集成与文档

范围：旧文档顶部取代标记、新 README/运行指南、前后端字段核对、最终全套回归和
1M 差分/资源剖析。删除迁移期双路径。

验证：Spec Q60 前四层及最终删除后的完整回归。

提交：待记录。

### C13 — 26M 最终验收

范围：精确 26M，默认 2 GB、无 swap、Refine 至 Network 小于一小时；PostgreSQL
单列报告；3 GB 运营回退结果不得作为通过证据。

验收：归档命令、代码提交、配置、机器/磁盘、控制组事件、时间、逐表差分和日志。

结果：尚未执行。

## 4. 主对话验收记录

每卡完成后由主对话记录：提交哈希、diffstat、测试命令与结果、审查问题、是否接受、
是否允许派发下一卡。子代理自述不能替代主对话复核。
