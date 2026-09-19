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
| C02a | PostgreSQL Schema v5 制品登记 DDL 与迁移 | C01b2 | ≤700 | 已实施，待主对话验收 |
| C02b | 制品描述校验与最小登记 API | C02a | ≤700 | 未开始 |
| C02c | 制品登记真实 PostgreSQL 集成验证 | C02b | ≤700 | 未开始 |
| C03 | 不可变 Parquet staging/发布/校验基础模块 | C01b2–C02c | ≤700 | 未开始 |
| C04 | artifact reconcile/gc dry-run/apply CLI | C03 | ≤700 | 未开始 |
| C05 | Refine Parquet Schema 与发布路径 | C03 | 每提交 ≤700 | 未开始 |
| C06 | DuckDB 读取 Refine Parquet 的 Detect 输入适配 | C05 | 每提交 ≤700 | 未开始 |
| C07 | Detect 算子列式迁移及 Python 回退收敛 | C06 | 每提交 ≤700 | 未开始 |
| C08 | Detect Parquet 发布与 Clip 列式读取 | C07 | 每提交 ≤700 | 未开始 |
| C09 | Clip/Fitting 顺序计算与旧 spool 清除 | C08 | 每提交 ≤700 | 未开始 |
| C10 | Detect/写入器旧磁盘工作区清除 | C07–C09 | 每提交 ≤700 | 未开始 |
| C11 | 进程池、协调器、资源预留、任务账本清除 | C02c–C10 | 每提交 ≤700 | 未开始 |
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

提交：本卡提交（待主对话验收）。

### C02b — 制品登记 API

范围：实现制品描述校验与不提交事务的最小登记 API；生产者接线留在 C05/C08。

验证：合法/非法清单、重复发布、跨 Run 隔离、未知 Schema 拒绝和事务边界测试。

提交：待记录。

### C02c — 真实 PostgreSQL 集成验证

范围：在隔离 Schema 中验证 v4→v5、数据库约束、并发事件顺序和事务回滚。

验证：真实 PostgreSQL 上迁移、约束、锁定、幂等和回滚均符合 C02a/C02b 语义。

提交：待记录。

### C03 — 不可变 Parquet 基础模块

范围：Run staging、同文件系统原子改名、只读发布、文件清单/大小/footer/SHA-256、
快速消费校验、orphan 识别；不实现阶段业务 Schema。

验证：成功发布、半写失败、改名后 DB 失败、篡改/缺失、只读和路径逃逸测试。

提交：待记录。

### C04 — 对账与垃圾回收

范围：实现默认 dry-run、显式 apply 的 reconcile/gc CLI；当前成功血缘不可删除，
未引用制品默认宽限 7 天，删除追加事件；不新增后台线程。

验证：dry-run 零写入、引用保护、宽限边界、orphan、幂等 apply 和失败恢复测试。

提交：待记录。

### C05 — Refine Parquet

范围：按 Spec 5.1 定义 kept_points/injected_points Schema；以 `refine_id` 隔离，
来源指纹前后校验；完成 PostgreSQL 结果与 Parquet 的发布顺序。

验证：Schema 指纹、行数/身份/顺序、来源变化、发布失败、小样本逐表差分。

提交：待记录。

### C06 — Detect 输入

范围：镜像内固定 PostgreSQL 扩展；DuckDB 扫描 Refine Parquet，按子轨迹批次向现有
不可列式内核提供最小对象；禁止整表 Python 物化。

验证：kept/injected 合并顺序、车辆边界、稳定身份、内存批次和旧读取路径差分。

提交：待记录。

### C07 — Detect 列式迁移

范围：逐算子迁移过滤、连接、排序、窗口、候选准备；DuckDB 不适用时才用 Polars，
最后才使用最小 Python 对象。每处回退在测试中记录理由。保留离散拓扑语义。

验证：Q49 容差、成员/crossing/拓扑严格一致、边界网格和超大语义单元保护。

提交：待记录。

### C08 — Detect Parquet 与 Clip

范围：按 Spec 5.2 发布 resolved_points/intersections/entry_exit_points；Clip 只从已登记
Detect Parquet 读取，保持 API 身份和顺序。

验证：三个逻辑表逐表差分、Clip 输出差分、未知版本/缺失文件、只读消费。

提交：待记录。

### C09 — Clip/Fitting 清理

范围：以列式输入和顺序计算替换 `DiskSequence`；删除 `clip_spool.py` 及专用测试，
确保 Fitting 最终 psycopg 事务兼容。

验证：Clip/Fitting 行为、失败回滚、配对顺序和中心线/清单差分。

提交：待记录。

### C10 — Detect/写入器工作区清理

范围：移除 `cross_member_store.py`、`detect_workspace.py`、`writer_workspace.py` 及专用
测试；按列式关系或有界 Python 内核保留业务语义。无调用者的辅助模块同时删除。

验证：Detect 持久化、成员归属、来源摘要、Replay 与 Backend 字段回归。

提交：待记录。

### C11 — 进程池与任务账本清理

范围：删除 patch spool/admission/coordinator/pool、`db/patch_ledger.py`、
`004_patch_ledger.sql` 和专用测试；提供手工 DROP 顺序；保留 heartbeat。

验证：全仓无生产引用、初始化器不引用旧结构、Pipeline 单进程路径完整。

提交：待记录。

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
