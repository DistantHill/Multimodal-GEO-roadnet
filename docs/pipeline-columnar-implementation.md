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
| C11 | 进程池、协调器、资源预留、任务账本清除 | C02c–C10 | 每提交 ≤700 | 已完成（C11a–C11h） |
| C12 | 文档取代标记、全套回归和 1M 差分 | C11 | 每提交 ≤700 | 进行中（C12a–C12g3；待重跑 1M） |
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

提交：`2ee7f28`。

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

C11g 删除已无生产或测试调用者的 patch ledger Python 实现与可选 DDL 模板。
该可选结构从未由初始化器自动安装；迁移说明仅为曾手工安装的环境提供不含
`CASCADE` 的显式卸载顺序，本卡不执行任何数据库删除。`patches.py` 及
`test_patches.py` 仍留待下一卡清理，因此 C11 尚未完成。本步不改 heartbeat、
DuckDB/Polars、业务数据库结构初始化、API 或前端字段。

C11h 删除已只服务旧 patch 池与账本、且已无生产调用者的 patch 规划与签名
模型及其专用测试，并从惰性导入测试移除已删模块。候选 patch 算法、制品
`canonical_json`、heartbeat 单后台线程、只读环境快照，以及 DuckDB/Polars 引擎管理的
线程与临时目录均保留。至此手写 coordinator、admission、process pool、spool、ledger 与
planner 均已删除，C11 完成。

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

C11g 验证：Pipeline 与 scripts 全量测试 `1043 passed, 50 skipped`；已删除模块无法导入，
初始化器 DDL 不含四张账本表，生产与测试源码中已退役符号引用清零。

C11h 验证：lazy import 定向测试 `5 passed`；Pipeline 与 scripts 全量测试
`945 passed, 50 skipped`；生产与测试
源码中 patch 规划与签名模型引用清零，全域审计仅保留允许的 heartbeat 单后台线程和
DuckDB/Polars 引擎管理的线程、临时目录与外存溢写配置。

提交：C11a `2e12f20`；C11b `6116701`；C11c `5dedb83`。

提交：C11d 测试退休 `1a4264a`；C11d 实现删除 `8b9354d`。

提交：C11e `acef192`。

提交：C11f PostgreSQL 测试退休 `de7cde3`；单元测试退休 `b0f5605`。

提交：C11g `a63d17a`。

提交：C11h `c9e9ac4`。

### C12 — 集成与文档

范围：旧文档顶部取代标记、新 README/运行指南、前后端字段核对、最终全套回归和
1M 差分/资源剖析。删除迁移期双路径。

验证：Spec Q60 前四层及最终删除后的完整回归。

提交：待记录。

#### C12a — 文档切换与运维入口

范围：为旧流式实施文档增加历史/已取代标记；新增列式架构运维指南；更新 README 的
当前架构、运行时目录、schema v7 迁移链和 artifact reconcile 入口。本卡只修改文档，
不执行 `--apply`，不改代码、配置、测试、API 或前端字段，也不宣称 C12 或 C13 完成。

验证：Markdown 相对链接目标存在；CLI help 与 parser 一致；文档/配置定向测试；工作树
只包含本卡文档及既有未跟踪用户文件。

提交：`3bd828b`。

#### C12b — Schema guard 与最终字段兼容闸门

范围：修复 Pipeline 已使用 schema v7、Backend 却仍复制并硬编码 schema v4 guard 的
真实集成故障。Pipeline、Backend、命令行接口、手工基准与测试统一使用
`track.v2.db.schema.require_current_schema`；Backend 直接导入共享 `SCHEMA_VERSION` 和
guard，不保留旧名称或第二套校验实现。地图接口的活跃说明与错误提示同步为当前
schema v7。

最终字段兼容采用唯一方案：不改业务表、Parquet Schema 或 API 字段，以 Backend 的
精确 Feature 断言固定 intersection、resolved point、entry/exit 和 centerline 输出，
并以 Frontend FeatureCollection 回归固定 entry/exit、centerline 的 properties 与 metadata
透传。迁移说明同步当前 v7 以及 v3、v4/v5/v6 的既有迁移入口，保留 patch ledger 手工
清理说明。

验证：Backend 定向 `23 passed`、全量 `75 passed`；Pipeline schema 定向 `38 passed`、
全量 `945 passed, 50 skipped`；Frontend 全量 `npm test` 为 `14 passed`；全仓检索确认
活跃生产与测试不存在旧 guard 或 schema v4 错误提示。历史迁移
实现、历史文档和保留文件名中的 v4 阶段叙述不属于旧活跃 guard。C12b 不执行 1M 差分或
资源剖析，因此不宣称 C12 或 C13 完成。

提交：`4190c50`。

#### C12c1 — 真实 PostgreSQL/Parquet 与小样本逐表差分

范围：在删除旧实现后执行分层验证闸门的第 2、3 层。真实 PostgreSQL 使用
`postgresql://roadnet:***@127.0.0.1:15432/roadnet`，仅运行已审计为事务回滚或 UUID
测试 Schema 隔离的用例；Parquet 仅写 pytest 临时目录。未执行数据库 reset、迁移或
数据库级删除，也未读取或修改历史业务 Run。

现有固定小样本已充分覆盖本卡，不新增重复测试：Refine 对 `kept_points`、
`injected_points` 逐列比较 PostgreSQL 权威行，并把 Refine reader 与旧 PostgreSQL Detect
输入逐点比较；Detect 对 `intersections`、`resolved_points`、`entry_exit_points` 导出、
登记和读取进行检查，把 reader 与 PostgreSQL Clip reader 比较，并在包含/排除边界片段
两种配置下比较完整 Clip 输出。Fitting 不直接读取阶段 Parquet；其 Clip 列式关系消费、
中心线语义和 PostgreSQL 写入字段由独立定向测试覆盖。

验证命令与结果：

- 指定 `ROADNET_TEST_DSN`，运行 Refine/Detect Parquet、completion、source fingerprint、
  artifact reconcile、游标、PostGIS 投影及 Detect/Turn writer 的安全真实 PostgreSQL
  矩阵：`54 passed`；
- Clip/Fitting 列式消费、拟合、持久化与数据库 writer 定向矩阵：`27 passed`；
- 不设置 `ROADNET_TEST_DSN` 的标准 Pipeline 全量：`945 passed, 50 skipped`。

真实 Parquet fixture 为每次测试创建随机 Schema，在 `finally` 中只清理该测试 Schema；
其余真实 PostgreSQL 用例使用事务本地对象或随机 Schema，并在 fixture 末 rollback。
`test_refine_migration_postgres.py` 和 `test_artifact_registry_postgres.py` 会主动执行迁移，按
本卡禁止迁移的约束未运行；这两组不属于本卡的发布、读取或固定样本差分缺口。

C12c1 不包含 1M 差分、资源剖析或 26M 最终验收，因此不宣称 C12 或 C13 完成。

提交：`7979cde`。

#### C12c2a — 有界只读语义差分工具

范围：新增 `python -m pipeline semantic-diff`，接收同一数据库内 baseline/candidate 的
Refine、Detect、Fit Run ID，先校验两条血缘均存在、关系一致且 completed，再以服务端
命名游标逐行比较。数据库事务显式只读；工具不创建、修改或删除数据库对象，也不使用
项目自研外存、线程池、进程池或 Dask。结果以 JSON 输出逐表行数、最大误差、不可比较
字段及 pass/fail。

业务身份规范化不比较 Run UUID、自增主键或时间戳：Refine 使用来源键或注入锚点；
候选交叉口使用 turn resolved point 与 tracks_cross 两端 resolved point 的离散成员证据；
candidate crossing 再绑定候选身份、线段起点与类型；最终 intersection、entry/exit、
centerline、来源 clip 与派生 Network 拓扑均复用同一身份链。无候选成员、缺少引用证据或
业务键不唯一时 fail closed，不以行数或近似坐标替代语义。成员、crossing 配对和拓扑
严格比较；米制、经纬度和其他浮点分别使用
`1e-6`、`1e-9`、`1e-12` 绝对误差，并拒绝非有限值。

表读取前还严格比较两条血缘的源表、源指纹、投影配置，以及剔除顶层
`execution_config` 后的 Refine/Detect/Fit 语义参数；不兼容时输出具体 preflight 类别并
停止。当前覆盖 Refine kept/injected/excluded，Detect resolved、candidate intersection、
tracks cross、turn event、candidate crossing、最终 intersection 与 entry/exit，Fitting
centerline 与来源 clip，以及从 combined centerline 派生的 Network 拓扑。

本卡只实现工具和小样本回归，不创建数据库，不运行现有 1M 基线。Network 没有独立
持久化结果，issues/report 和 `include_isolated_nodes` 无法可靠比较并继续明确列为不可比较；
C12c2a 不代表 1M 差分、资源剖析、C12 或 C13 完成。

验证：语义差分定向测试 `11 passed`，CLI/import/API 合并定向测试 `22 passed`，标准
Pipeline 全量 `956 passed, 50 skipped`；CLI help 可用；逐表查询通过真实 PostgreSQL
只读 `EXPLAIN` 解析。测试覆盖 ID 偏移规范化、同数量
拓扑变化、三类容差边界、非有限值、缺失/重复业务身份，以及只读语句和命名游标。

提交：`742f919`。

C12c2b 补全验证：诊断表与 preflight 定向测试 `19 passed`；13 条逐表查询均通过真实
PostgreSQL 只读 `EXPLAIN` 解析；标准 Pipeline 全量 `964 passed, 50 skipped`。候选 ID
偏移不影响规范身份；诊断成员、excluded 或语义配置/源指纹变化均在最终表比较前失败。

提交：`7942439`。

#### C12c2c — 现有 1M 基线隔离实测

2026-09-20 只读前置核对确认：`roadnet_test` 无其他连接，schema v4，
`public.day01` 精确 `1,000,000` 行，历史 Refine/Detect/Fit Run 分别为
`bcac9210-3dd4-497a-ac64-8b872deafa1c`、`deb731ae-86b9-450f-a109-38b69a9b811c`、
`9338132b-0d96-4968-8f06-e8c680241709`，状态均为 completed。使用 PostgreSQL
`CREATE DATABASE ... TEMPLATE roadnet_test` 创建全新隔离库
`roadnet_columnar_c12_1m_20260920`，只对该副本执行 `init-db --migrate-artifacts` 至 v7；
迁移后源行数和历史血缘不变。未修改、迁移、重置或删除 `roadnet_test`/`roadnet`，也未
终止连接。

当前源码以保留容器 `roadnet-c12c2c-1m-20260920` 运行完整命令：
`python -m pipeline run --source-table public.day01 --processing-srid 32649`。宿主为
AMD Ryzen 5 5600H（6 核/12 线程）、14,497,005,568 B 内存，Docker 29.1.3、cgroup v2，
数据盘运行前可用 176,667,430,912 B。镜像
`sha256:df66d661b6d1ab17819709e0536feb06cc4c6791b633dea91f9cfefca8104754`；
Python 3.12.14、DuckDB 1.5.5、Polars 1.44.2、NumPy 2.5.3、psycopg 3.3.6、
pyproj 3.8.0。容器 `memory.max=2,000,000,000 B`，Docker `memory-swap` 与内存限制相同，
即无额外 swap；运行中采样为 225.9 MiB，容器退出前未读取到精确 `memory.peak` 与
`memory.events`，因此不能将该采样冒充峰值。容器退出记录为 `OOMKilled=false`。

Refine 成功：70.340 秒，读取 1,000,000 行，写入 614,879 kept、999 injected、
385,121 excluded，新 Refine ID 为 `e835f775-c30f-4ba4-a64e-1f8ad898a97a`，源指纹与
历史 baseline 相同。Detect ID `684842e0-f3b9-4d7a-8921-5eded0c2b7a6` 在 279.5 秒后
失败：已计算 56,239 turn events，随后 overflow fallback 预估
`6,495,190,184` 个跨轨迹线段对，超过 `20,000,000` 保护阈值。总墙钟 351.74 秒，
退出码 2；Detect 状态为 failed、七张 Detect 结果表均为 0 行，未创建 Fit Run，成功后
才写出的 `stats.json` 不存在。历史 baseline 的对应数量为 resolved 679,649、candidate
intersection 393、tracks cross 58,660、turn event 56,178、candidate crossing 83,947、
intersection 292、entry/exit 31,207、centerline 1,506。

由于当前 Run 未形成 completed Detect/Fit lineage，未运行 `semantic-diff`，没有逐表
pass 结果；首个阻塞差异是当前 Detect 候选生成触发跨轨迹 pair-space fail-closed，不能
通过放宽 Q49 容差或用空表比较规避。运行目录
`/data/tmp/road-network/c12c2c_1m_20260920`、隔离数据库和保留容器均未删除，供复核。
PostgreSQL 位于独立共享容器，前后 `memory.peak` 均为 5,551,116,288 B；该值含其他数据库
活动，只作单列环境证据，不计入 Pipeline 2 GB。C12 保持进行中，C13 未开始。

只读根因核对进一步确认新旧 Refine kept/excluded 集合完全一致，injected 仅 5 行存在
Q49 容差内的纳米级投影差。当前 Parquet 的 437,416 条 straight adjacent segments 中，
14,849 条 bbox 网格成员数超过 25，因此保护估算恰为
`14,849 * 437,416 = 6,495,190,184`；最长相邻直线约 9,966.406 m、覆盖 1,683 个 bbox
cells。这些是真实长跳段触发历史 global fallback，而非输入成员变化或意外全表物化。
历史 Detect 于 2026-09-14 22:10 UTC 启动，早于 2026-09-15 加入的 20M DB global
overflow guard；要保持历史候选语义并有界处理该规模，需要后续另行设计等价的
DuckDB/Polars broad phase，不能在本卡直接抬高保护阈值。

turn event 也按稳定 `(subtrack_index, refined_sequence_in_subtrack)` 做了只读对齐：
56,176 个公共事件的 bearing/turn 最大差为 `1.1369e-13`，另有 63 个新增、2 个缺失；
这 65 个集合差全部处于闭区间 45°/170° 边界，是 DuckDB 与历史 Python 三角函数及求和
舍入方向不同造成的边界翻转，并非 Refine 成员变化。该兼容修复不混入本卡。

本卡同时修正 `tracks_cross` 无向身份：候选成员证据与行身份都按稳定
`(subtrack_index, sequence_in_subtrack)` 端点排序，不再依赖 surrogate ID 的 from/to
顺序；端点反转等价回归已覆盖。定向测试 `20 passed`，标准 Pipeline 全量
`936 passed, 50 skipped`，13 条语义查询再次通过真实 PostgreSQL 只读 `EXPLAIN` 解析。

提交：`29b3f04`。

#### C12d1 — Cross-track AABB range join

针对 C12c2c 的 1M overflow 阻塞，Refine Parquet reader 的 broad phase 改由 DuckDB
执行闭区间 AABB overlap range join。不同车辆按 `a.segment_id < b.segment_id` 规范一次，
且两个 bbox 在 x/y 两轴均闭区间重叠。任意真实线段相交必满足该条件，因此不会漏掉进入
现有 NumPy exact kernel 的真实候选；只删除旧 `overflow × all` 中 AABB 已分离、最终必被
拒绝的 false positive。DuckDB 继续负责 window、join、排序和外存，Python 只解码有界
batch；遗留 grid 配置继续校验但不影响候选。固定 1M Parquet 的只读 `EXPLAIN` 为
`PIECEWISE_MERGE_JOIN`、估算约 2,098,224 行，不是 `CROSS_PRODUCT`。

验证：定向 `58 passed`，Pipeline 全量 `935 passed, 50 skipped`。
提交：`0f88034`。

#### C12d2 — Turn 闭区间边界兼容

DuckDB 继续负责按子轨迹 partition/window、leg/elapsed/speed 条件、圆均值近似、稳定排序
与单一有序结果流。SQL 的 turn 阈值改为 Q49 `1e-12°` 宽筛，绑定下限/上限分别用
`math.nextafter` 向外扩张；远离阈值且数值稳定的行直接使用 SQL 值。位于 45°/170° 等
阈值邻域的行仅携带有序 in/out heading 小窗口，由现有 Python
`_circular_mean_deg`、`_angle_delta_deg` 按历史闭区间精算，未通过的行跳过且不占
`event_calculate_index`，因此最终索引连续、无第二查询归并。

病态圆向量也强制进入同一最小 fallback：SQL 携带 resultant magnitude 与 sin/cos
绝对和，以 `gamma_n = n*u/(1-n*u)`、`u=2^-53` 判断累计舍入误差是否足以覆盖 resultant。
该规则防止近零向量的 SQL 方位近似决定成员，但与历史 oracle 的最终一致仍以固定镜像和
libm 为验证前提，不声称对任意 libm 提供数学同一性。Q44 上，DuckDB 保持列式、可外存的
全量工作，Python 只处理阈值邻域或病态窗口，不引入全表 Python、自研 spill、并发池或
Dask。本卡不重跑 1M，C12 仍为进行中。

验证：turn 定向 `6 passed`；Pipeline 与 `scripts/cv_human_check/tests` 合并全量
`967 passed, 50 skipped`，其中 scripts 单独 `29 passed`；`git diff --check` 通过。

提交：待记录。

#### C12e1 — Cross-track 保守数值宽筛

C12d1 的 AABB range join 只排除包围盒分离的线段对。固定 1M 现场运行实际在 cross 阶段
处理了 `12,845,056` 个 pair，并在 `2,557.8 s` 时仍处于该阶段；暂停容器从
08:05:24 到 08:53:56 共运行 48 分 32 秒，`OOMKilled=false`，退出码 137 来自人工停止。
因此 C12d1 文中约 210 万行的 `EXPLAIN` 估算不能视作实际 pair 数或性能实测。

本卡在 AABB join 内增加 DuckDB 数值宽筛：先按角度范围排除稳定的无效 pair，再用与
现有 NumPy 核同形的 determinant、`ta_num`、`tb_num` 判断线段参数是否可能落在闭区间。
每个比较按操作数尺度向外扩张 `64 * 2^-52`；超大 heading、非有限 SQL 中间结果、端点
边界与近退化不确定行全部保留并交给原 `_paired_segment_intersections` 确认。SQL 不产生
最终交点，Python 原核仍决定命中和坐标，因而不以 SQL 与 NumPy 浮点表达式完全相同为
前提，也不改变 Q49 的成员、配对和拓扑要求。DuckDB 继续承担可外存 join、宽筛与排序；
Polars 对该逐 pair 标量筛选不会减少搬运，Python 仅解码宽筛保留的有界批次，不引入
自研 spill、线程、进程或 Dask。

按用户指导，pair 应尽早在 DuckDB 内过滤，以减少数据库 I/O 和 Python 对象构建；巨大
中间关系不应携带宽行元数据参与排序。本实现的 AABB 与数值关系只携带两端 segment ID、
坐标和 heading，宽筛保留后才按 ID 回连来源元数据并执行稳定排序。读取仍为单条 SQL、
单次顺序 `fetchmany()`，没有第二次扫描结果、逐行 `INSERT` 或 Python 侧归并。小样本回归
固定报告 AABB pair 数和宽筛后解码 pair 数，以验证对象构建确有减少而非仅移动计算位置。
当前长线段、角度边界、端点和近退化组合样本由 10 个 AABB pair 缩减为 4 个解码 pair，
其中近退化不确定 pair 被保守保留；最终候选仍与 legacy Q49 oracle 一致。

验证：cross-track 定向 `11 passed`；Pipeline 与 `scripts/cv_human_check/tests` 合并全量
`967 passed, 50 skipped`。本卡不重跑 1M，C12 仍为进行中。

提交：`75087f0`。

#### C12e2 — TurnEventRelation 有界列式批写

正式 Detect 的 turn reader 原先逐事件调用 `append`，每条事件单独执行事务、标量
`INSERT` 和 `source_ids` `executemany`。本卡保留 `append` 的单条立即可读和原子语义，新增
显式 `extend` / `append_many` API：输入 iterator 按调用方批量上限切分，每批在一个事务中
用 NumPy 列字典注册 DuckDB 临时关系，再以 `INSERT SELECT` 写入事件和来源 ID。成功提交
后才推进 relation 计数；失败批整体回滚，先前已提交批保持可读，不存在隐藏延迟缓冲区。

正式 `detect.py` 直接把 `iter_turn_candidates` 的 iterator 以 `stream_batch_size` 喂给批写
API，避免为写入重新构建全量事件列表。nullable BIGINT 使用 Python `int`/`None` 的 object
数组，避免经过浮点数丢失大于 `2^53` 的身份；浮点列显式使用 `float64`，NaN 即使被注册
层表示为空值也会被既有 `seal` 校验拒绝。可空的 `g_star_z` 是例外：scanner 若把 NaN
映射为合法 NULL 会绕过 `seal`，因此批次注册前显式拒绝非有限的非 None 值；失败批不进入
事务，先前批仍保留。`source_ids` 跨事件形成有界行流；单个事件即使
包含大量来源 ID，也只按 relation batch size 注册和写入，不会整体展开。每次注册均在
`finally` 中注销，不增加 pyarrow 依赖，也不改 CrossCandidateRelation 或 WriterRelation。

固定 2,000 事件、每事件一个 source ID、单进程、DuckDB 单线程、batch 256 的镜像微基准：
旧 `append` 循环 6.876317 秒，新批写 3.116454 秒，墙钟加速 2.21 倍；事务由 2,000 降至
8，事件与来源 INSERT 调用合计由 4,000 降至 16（若计 BEGIN/COMMIT，SQL 调用由 8,000
降至 32）。两条路径均 `seal`，新路径精确回读 2,000 条且末条语义相同。该微基准只用于
摄取开销对比，不替代 1M 或 C13 验收。

验证：关系、正式接线和严格 `rel=0` Q49 reader 定向 `98 passed`；Pipeline 与
`scripts/cv_human_check/tests` 合并全量 `977 passed, 50 skipped`。本卡不重跑 1M，
C12 仍为进行中。

提交：`878b30c`。

#### C12e3 — CrossCandidateRelation 有界列式批写

CrossCandidateRelation 新增与单条 `append` 明确分离的 `extend` / `append_many` API。
iterator 按调用方上限形成有界批次，每批将 39 个业务字段构造成 NumPy 列字典并注册为
DuckDB 临时关系，以一个 `INSERT SELECT` 和一个显式事务提交；成功提交后才推进 `_count`，
失败批回滚而此前批保留，注册关系始终在 `finally` 注销。nullable BIGINT 使用 `int64`
数据列与独立空值掩码，避免浮点转换损失大于 `2^53` 的身份；可空字符串和布尔值同样以
掩码重建 NULL。强制浮点 NaN 即使被 scanner 映射为 NULL，也由既有 `seal` 校验拒绝。

列式 cross-track 提取不再逐命中 `result.append`。原精确核的命中顺序直接进入候选生成器，
以每批开始时的 relation 长度和局部 `enumerate` 连续编号，再交给 `extend`；因此批尚未提交
时不会重复使用 `len(result)`，也不额外构造 numbered candidate 列表。无 store 的内存路径
仍以同一生成器扩展 list，顺序和业务语义不变。本卡未改 Turn、Writer 或聚类成员写入。

固定 2,000 候选、单进程、DuckDB 单线程、单个 2,000 行有界批次的镜像微基准：旧
`append` 循环 9.278646 秒，新批写 0.030783 秒，墙钟加速 301.42 倍；autocommit/显式事务
由 2,000 降至 1，候选 INSERT 由 2,000 降至 1，若计 BEGIN/COMMIT 则 SQL 调用由 2,000
降至 3。两条路径均通过 `seal`，新路径精确回读 2,000 条且末条语义相同。该隔离结果不
替代 1M 或 C13 验收。

事务故障回归在 Cross 主表 INSERT 和 Turn `source_ids` INSERT 内分别注入唯一约束失败，
验证失败批主表/来源表零残留、此前批保留、`_count` 不前移且注册关系已注销。Cross relation
定向 `40 passed`；Pipeline 与 `scripts/cv_human_check/tests` 合并全量
`987 passed, 50 skipped`。本卡不重跑 1M，C12 仍为进行中。

主对话验收复测：CrossCandidateRelation、TurnEventRelation 与 Refine reader 定向
`116 passed`（6.17 秒）；Compose 配置单元测试 `5 passed`；Backend 在同时挂载当前
Pipeline 源码后 `75 passed`。只挂载 Backend 的旧镜像会因其内置旧 Pipeline Schema
缺少 `require_current_schema` 而在收集期失败，部署前需重建 Backend 镜像。

提交：`81ed237`。

#### C12e4a — Semantic diff 目标 Run 限定与候选成员流

语义差分的公共 CTE 由入口绑定目标 `detect_id`，member evidence、candidate、crossing 与
intersection 均只读取该 Run；Fitting 与 Network 查询显式同时接收其 lineage detect ID
和 fit ID，不再为一个 fit 扫描全库 Detect 成员。候选业务键由全成员 `string_agg` 改为
固定大小的最小 evidence 代表；新增独立 `detect.candidate_membership` 有序流，以 evidence
为唯一行键并输出代表值，严格比较完整成员关系。非最小成员变化、无成员、重复 evidence，
以及同一 evidence 分属不同代表均 fail closed；不能只比较最小成员。几何输出统一使用
`ST_AsGeoJSON(geom,15)`，避免默认最多九位小数在 `1e-9°` 边界掩盖差异。

真实 PostgreSQL 验证在隔离数据库的随机 Schema 中安装生产 DDL，对全部 semantic query
执行带真实参数数量的 `EXPLAIN`，包括 fit 查询的 lineage detect ID；随后用事务内临时表
实际执行 shifted surrogate ID、非最小成员变化、无关 Run 排除、无成员、重复 evidence、
跨代表重复 evidence，以及 GeoJSON 九位比较通过而十五位比较失败的 `1.4e-9°` 边界。
测试结束回滚并删除随机 Schema，不修改既有 Run 或业务表。真实 PG 定向 `24 passed`；
Pipeline 与 `scripts/cv_human_check/tests` 合并全量 `990 passed, 51 skipped`。

性能优化按实际读取/传输行字节数、SQL 调用次数和 Python 物化行/对象数评估，不把采用
DuckDB 或 Polars 本身视为加速；来源前后指纹和兼容约束保持不变。

另行只读性能试验未形成生产改动：固定 1M 上，v2 原 JOIN 的 COUNT 180 秒未返回，源校验
0.414 秒、峰值 `479,576,064 B`；v3 仅把四个 AABB 条件前置，COUNT 同样 180 秒未返回，
源校验 0.377 秒、峰值 `487,759,872 B`。两次 swap/OOM 均为 0，均人工超时停止并返回
137；实际 count 未知，约 2,098,224 的估算不能视为实值。因此条件重排不合入本卡，也不
重跑完整 1M。审计容器日志保留，旧数据与 PostgreSQL 均无写入。C12 仍为进行中。

#### C12e4b — Semantic diff 鲜活来源与完整范围门禁

语义差分先比较两侧 lineage 保存的 `source_table` 与 `source_fingerprint`；两者一致后，
仅以共同来源的 baseline Refine Run 执行一次既有 `verify_refine_source_fingerprint`，不为
两侧重复扫描来源。该校验沿用既有指纹契约和命名只读游标的有界读取；来源漂移或校验异常
会在打开逐表差分游标前结构化 fail closed，不新增散列体系。

结果拆分为 `persisted_equivalent`、`complete_scope` 和 `acceptance_passed`。当前 Network 的
nodes、issues、report 与 config 尚未持久化，均作为带 `blocks_complete_scope=true` 的结构化
排除项；因此即使现有持久化逐表差分完全等价，`complete_scope`、`acceptance_passed` 以及
兼容字段 `passed` 仍为 false，CLI 返回非零，不能误报完整验收成功。后续卡仍须补齐这些
Network 非持久化产物的可复核覆盖；本卡不重建 Network，也不改变数据 Schema。

定向回归覆盖共同来源仅校验一次、来源漂移结构化失败且不打开逐表游标、来源契约不一致时
提前失败，以及 CLI 对“仅持久化表等价”返回非零。C12e4a 主验收同时确认 Pipeline 与
`scripts/cv_human_check/tests` 合并全量 `990 passed, 51 skipped`；本卡当前树合并全量为
`993 passed, 51 skipped`。

性能工作应研究更有效的空间连接，而不是重复已超时的 COUNT；之后仍需完成 1M 全量差分和
26M 最终验收。只有整个列式任务验收完成后，才另行安排 Python trajectory point 对象改为
Subtrack 列数组，并让 Segment 按 `isequence` 引用重建；当前阶段不删除这些 Python 类型，
也不删除或改变 PostgreSQL 表。

#### C12e5a — Semantic diff combined 端点方向规范化

Semantic diff 仅对业务语义为无向的 `combined` centerline 按候选 intersection 稳定业务键
规范端点方向。端点反序时同时交换业务键、`forward_clip_count` / `reverse_clip_count`，并以
`ST_Reverse` 反转几何；source clip 的 line key 采用相同规范化，并切换
`reverse_to_centerline_direction`，但 start/end crossing 仍保持原 clip 行进方向，不交换。
Network topology 只读取 `combined`，因此同样规范无向端点 pair。空值或不可比较业务键仍
输出既有 sentinel 并 fail closed。

`directed` centerline 不执行无向规范化，正反两条线仍是不同业务行，方向或几何互换不会被
视为等价。这是消除 shifted surrogate ID 导致的稳定端点反序假阴性，不是放宽差分容差，
也不改变生产 Fitting 或 Network。隔离 PostgreSQL 随机 Schema 实际执行覆盖 combined 反向
端点的等价，以及几何未反转、方向计数未交换、source clip 标志未切换的失败；同时验证
directed 正反两行均保留且交换方向不等价。Semantic diff 定向真实 PG `29 passed`。

#### C12e5b — Network 受控参数化重放

新增独立 Network replay 验收模块，以当前 `network.py` 文件字节的固定 SHA-256 锚定受控
算法版本；运行时哈希不符即 fail closed。`NetworkConfig` 必须仍且仅含
`include_isolated_nodes: bool=True`，并穷举 false/true 两种配置；新增配置字段或类型、默认值
变化不会被静默遗漏。该证据的 kind 为 `controlled_replay`，明确标记
`historical_output_reconstructed=false`：它证明同一未变 Network 算法对两侧持久化输入的
参数化等价，不冒充历史瞬时输出快照。

每侧先校验 completed Fit→Detect lineage，通过命名游标各读取一次 intersection 小表和
combined centerline 小表，再由两种配置复用；稳定 intersection identity 由目标 detect 的
既有 semantic key CTE 单独窄读一次。该路径不读取 trajectory points、clip manifests，也不
构造全量点对象。最小 Fit adapter 只提供生产 `build_network` 实际消费的 centerlines 与
detect ID，不伪造 FitReport；生产 Network 算法文件保持不变。

规范结果按两种配置分别生成 nodes、edges、issues、report 共八张表。节点以稳定
intersection identity 为键；无向边按稳定端点 pair 排序，反向时同步反转 points 并交换
forward/reverse count；issues 使用稳定 pair 与 reason；report 的五个整数严格比较。证据同时
记录两侧 detect/fit ID、算法 expected/actual hash、穷举配置及三类读取行数。缺失或重复稳定
identity、非有限值、错误 lineage、算法锚点或配置契约变化均 fail closed。

顶层结果保留 `persisted_equivalent`，新增分别报告 false/true 的
`network_parameterized_equivalent`。只有十四张持久化表与两种 Network replay 都等价且完整
覆盖成立，`complete_scope`、`acceptance_passed` 和兼容字段 `passed` 才为 true；Network
失败不会抹去已经得到的持久化表结论。隔离 PostgreSQL 随机 Schema 实际运行小表 SQL 与
两种配置，定向覆盖孤立点、稳定端点反序、同步几何/计数、同边数不同配对、坐标容差边界、
issues/report/count 变化和各类契约失败。

#### C12f1 — Cross-track 一次物化与统计驱动空间连接

`iter_cross_track_pair_batches` 在同一 DuckDB 连接内把完整 straight payload（含 AABB）
一次性物化为引擎管理的临时表并执行 `ANALYZE`。这是一项 DuckDB 算子中间关系，不是自研
spool、外存协议或 Python 缓存。后续 self-join 仍只投影原有 ID、坐标、heading 与 AABB
窄列，保守角度/相交表达式、浮点边界和稳定排序均不改变；命中 ID 最后回连同一临时表获取
payload，因此不再重读 Parquet、重跑窗口或为未命中 pair 构建 Python 点对象。零长段没有
新增过滤，既有 grid 配置兼容校验、memory limit、temp directory 和精确 NumPy 核保持不变。

临时表使用每次迭代唯一名称，并在正常完成、异常和 generator 提前关闭时由 `finally` 删除；
reader 已先关闭时连接自身已经释放临时关系，不再用已关闭连接执行 DROP。回归覆盖来源关系
仅物化一次、pair SQL 不重读 `refine_points`、提前关闭、解码失败、reader 先关闭和同一
reader 重复调用，Refine reader 定向 `42 passed`。

真实 1M 只读探针在 2 GB、swap 0 配置下以完整 payload 物化全部 `437,416` 段，其中保留
`56,066` 个零长段；物化 `0.590` 秒、`ANALYZE` `0.055` 秒。该探针同时把四个 AABB 条件
移到了 self-join 条件首位，并非只验证物化这一项，也并非与 C12f1 当时的生产最终 SQL
同形。组合方案的保守 COUNT 实计为 `59,018` pair、耗时 `16.675633` 秒；该数字不是最终
exact 候选数，该时间也只覆盖 COUNT，不代表完整 cross-track 或全链路耗时。C12f1 单独
合入物化与 `ANALYZE` 后，生产最终 SQL 的 `IE_JOIN` 仍以 `segment_id <` 为主要 range，
完整 cross 阶段 225 秒仍未产生首批，因此不能用上述组合探针证明 C12f1 已解决热点。

#### C12f3 — 生产最终查询 AABB range 主条件

在 C12f1 的同一临时表 self-join 中，仅把四个闭区间 AABB 条件移到 ON 首位，把
`segment_id <` 与车辆不等条件移到末尾；保守角度/数值宽筛、payload 回连、稳定排序、精确
NumPy 核和全部 `437,416` 段均不改变，也不增加零长过滤或 `MATERIALIZED` CTE。此前仅对
原 CTE 调整条件顺序而未改善超时，与这里“先物化统计、再调整生产最终 self-join range”
并不矛盾。

新增回归直接拦截 `iter_cross_track_pair_batches` 生成的生产最终查询；该查询同时包含完整
payload 回连与 ORDER BY。测试对同一 SQL 执行 `EXPLAIN`，验证选择 `IE_JOIN` 且首个主要
range 是 AABB 列而不是 segment ID，不以另写 COUNT 查询替代。Q49、端点、近退化、批次和
临时表生命周期继续由完整 Refine reader 定向覆盖。

C12f1 旧生产形状的完整运行在 cross 阶段 405 秒仍无首批，峰值 `1,179,484,160 B`，人工
停止返回 137，OOM 为 0。C12f3 相同 SQL 内容的受控探针使用 2 GiB 容器限制（不是十进制
2 GB 门禁），完整经过 Reader payload、ORDER BY、decode、原 exact kernel 与 typed candidate
store：`59,018` 个 conservative pair 产出 `58,660` 个候选，共 `21.5003` 秒，峰值
`1,083,285,504 B`，swap/OOM 均为 0。探针发生在提交前的相同工作树内容上，且 SQL 文本
包含每次运行不同的临时表 UUID；因此不把该次 raw SQL hash 描述为可跨运行复现的文件锚点。
正式门禁仍须由固定提交快照在小于 `2,000,000,000 B` 限制下完成。

#### C12g1 — Writer source mapping typed 有界批写

WriterRelation 新增显式 `add_mappings`：按 relation batch size 有界消费 iterator，以
`int64` 保存 mapping order、point ID、subtrack index 和 resolved ID，以 NumPy Unicode
保存必填 point kind，通过临时注册关系执行一次 `INSERT ... SELECT ... ORDER BY
mapping_order`。每批独立事务，commit 后才推进 `_mapping_order`；失败批回滚并注销关系，
此前已提交批保留。单条 `add_mapping` 继续保留立即可读的兼容语义，重复 exact mapping 仍由
最高 mapping order 胜出。

`_persist_resolved_sequence` 复用已有 members 与预留 IDs 的批，直接以 generator 交给
`add_mappings`；PostgreSQL COPY、ID 分配、p/i/c 顺序、接口与结果不变。批量 API 明确拒绝
bool、非整数和超出有符号 int64 的值，避免 NumPy 静默截断或转换；合法大于 `2^53` 的整数
保持精确，字符串不经 object ndarray 慢路径。输入 iterator 在成功、转换异常和 SQL 异常时
均关闭。回归覆盖空、1、N、N+1、大整数、重复 mapping 最后胜出、SQL 批失败回滚、注册资源
清理与先前批保留。该改动针对静态审计发现的约 `615,878` 次单行 DuckDB mapping 调用；
不扩展到 collect_sources、annotations 或 Turn 摄取。

C12g1 主对话独立验收的 Writer、db_writer 与 Turn/Detect streaming 真实 PostgreSQL 回归为
`84 passed`。冻结 `5cd0ce2` 的 10k 局部 microbenchmark 中，单条 mapping 为 `10.7396` 秒、
10k 次 execute，批写为 `0.04321` 秒、6 次 execute；五列、大于 `2^53` 的整数、Unicode 与
重复 identity 最后胜出逐项相同。该 `248.5x` 只描述 source mapping 局部摄取，不代表 Detect
端到端加速。

#### C12g2 — Writer collect_sources typed 实体批

`collect_sources` 按 turn、cross、pending 的原业务顺序分别有界读取，每批最多
`WriterRelation.batch_size` 个实体，不把整个输入流转为列表。turn 批在同一 DuckDB 事务写
source request、turn raw 与 annotation request；cross 批在同一事务写两个端点 request、
cross raw 与 annotation；pending 批在同一事务写 source request 与 crossing raw。任一表
失败则整批回滚；commit 后才推进 `_request_order`、annotation order 与对应 `counts`，此前
已提交批保持可用且失败批可重试。

整数列使用严格有符号 int64，字符串使用 NumPy Unicode；nullable crossing/candidate ID 使用
值列与 null mask 后由 SQL `CASE` 恢复。nullable 浮点同时使用 value、null mask 与 NaN mask，
显式区分 SQL NULL 和 NaN；±inf 保持原值，不新增有限性拒绝。cross 在注册任何批关系前验证
两个端点引用；pending 继续在双方 iterator 的关闭上下文内使用 `zip(..., strict=True)`。
正常、输入异常、SQL 异常和 `KeyboardInterrupt` 均关闭活动 iterator 并注销临时关系。

回归覆盖 batch size 1/2/大值及连续 collect 的原顺序、两个 cross 端点缺失、pending/ID
长度不等、nullable ID、NULL/NaN/±inf、大于 `2^53` 的 ID、第二/第三张表故障的整批回滚、
先前批保留、retry 无主键冲突，以及注册与 iterator 生命周期。C12g2 不修改
`prepare_annotations`、TurnEventRelation 或最终 PostgreSQL COPY。

C13 尚无可确认的旧 26M 完整基线；现有原数据库 v4 不能被当作已证明的历史完整 baseline。
未来只能在隔离 clone 上按用户确认的基线方案执行，不修改旧数据。当前已向用户询问该方案，
在获得确认前只继续 C12，不宣称 C13 已具备差分基线或完成验收。

C12g2 主对话独立真实 PostgreSQL 定向验收为 `88 passed`（2.96 秒），并复核业务顺序、
NULL/NaN 区分与注册关系清理。该验收不扩展 C12g2 的范围，也不代表完整 1M 或 C13 通过。

#### C12g3 — Writer annotation typed 有界批写

`prepare_annotations` 将 members 与 hotspots 的逐行 DuckDB execute 改为 typed 有界批次。
members 的 kind 使用 NumPy Unicode，calculation index、encounter 与 candidate ID 使用严格
int64；hotspot 的 z 使用 value、null mask 与 NaN mask，显式保持 SQL NULL、NaN 和 ±inf。
每个批次的 `INSERT ... SELECT` 都显式按 encounter 或 input ordinal 排序，保持 first
membership 冲突判定、同 candidate 重复成员、last-hotspot 和 annotation request 顺序不变。

本卡刻意保留整个 `prepare_annotations` 原有的单一 `BEGIN` / `COMMIT`，不复用 C12g2 的
逐批事务 helper。任意后批转换、SQL 或 iterator 错误会回滚本次方法已写入的全部 members、
hotspots 与 annotations；成功后才设置 prepared 状态。每次注册关系均在 `finally` 注销，
cluster、member identity 与 hotspot iterator 在正常及 `BaseException` 路径都关闭。

回归覆盖跨 batch 的同 candidate 重复成员、跨 candidate 冲突全回滚、last-hotspot 为 NULL
或 NaN、第二批 SQL 故障回滚前批、注册清理与删除故障注入后重试。C12g3 不修改
TurnEventRelation、collect_sources、最终 COPY 或 Network。

### C13 — 26M 最终验收

范围：精确 26M，默认 2 GB、无 swap、Refine 至 Network 小于一小时；PostgreSQL
单列报告；3 GB 运营回退结果不得作为通过证据。

验收：归档命令、代码提交、配置、机器/磁盘、控制组事件、时间、逐表差分和日志。

结果：尚未执行。

## 4. 主对话验收记录

每卡完成后由主对话记录：提交哈希、diffstat、测试命令与结果、审查问题、是否接受、
是否允许派发下一卡。子代理自述不能替代主对话复核。
