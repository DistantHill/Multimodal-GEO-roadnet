# Pipeline 列式存储与计算架构规格说明（specification，Spec）

术语遵循[项目术语与写作规范](terminology.md)，不在本文复制通用术语表。

状态：Detect 网格提取局部变更已实施并通过小样本冒烟；26M 性能验收未执行
创建日期：2026-09-19
更新日期：2026-09-24
访谈进度：Q19–Q79 均已回答；Detect 网格提取变更已确认并完成逐函数实施

> 本文是当前活跃设计。旧的[2,000 万行内存预算与验收标准 Spec](pipeline-20m-memory-budget-spec.md)
> 保留容量审计、旧进程池/磁盘暂存方案及历史实施证据，但已被本文取代。
> 用户已在 Q62 确认完整设计，并在 Q63 授权分阶段实施和本地提交。设计确认与实施
> 授权均不代表 26M 性能验收已经通过。
> 2026-09-24 的 Detect 网格提取局部方案已获 Q79 确认，并在同一回答中授权由
> `gpt-6-sol`、`low` 子代理逐函数实施。下文原有架构保留为历史基线；本次有效
> 更正见 Q64–Q79。该局部变更的实现和测试记录见下文，不能据此推定 26M
> 输入的内存和时间验收已通过。

## 1. 背景与问题

现有 Pipeline 为控制大规模数据的内存占用，自行实现了磁盘暂存、磁盘工作区、
写入器工作区、任务账本、资源预留、协调器和进程池。这些实现分散在 Detect、Clip、
Fitting 与持久化路径中，维护面大，并使业务算法、外存生命周期和并发恢复紧密耦合。

本次改向删除这些自研设施，以 PostgreSQL 为权威存储，以不可变 Parquet 数据集作为
Refine、Detect 之后的列式检查点，以 DuckDB 为主计算引擎，以 Polars 和最小 Python
对象作为受控回退。无法列式化的 Python 算法首版保持单进程、单计算线程；线程、进程
或 Dask 的进一步设计不属于本次范围。

2026-09-19 代码审计确认：

- 现有八个主要暂存/并发模块及写入工作区约 2,247 行，并耦合大量专用测试；
- PostgreSQL 已保存 Refine、Detect、Fitting Run 血缘和最终业务表；
- Clip 没有独立 Run，Network 当前不持久化；
- 当前业务路径尚未实际读写 Parquet，也未声明 DuckDB、Polars 依赖；
- 前后端依赖 Run 血缘、四类地图图层、GeoJSON metadata、resolved point、
  intersection、entry/exit 和 centerline 的既有字段。

## 2. 目标与非目标

### 2.1 目标

- 删除自行编写的计算磁盘暂存、外存溢写、计算多线程适配层、进程池及任务账本。
- PostgreSQL 继续作为源数据、Run 状态和最终业务结果的权威存储。
- Refine、Detect 每个成功 Run 各发布一个不可变、可登记、可校验的 Parquet 逻辑数据集。
- DuckDB 优先执行读取、过滤、连接、聚合、排序、窗口和可表达的列式计算。
- DuckDB 无法保持语义或性能明显更差时使用 Polars LazyFrame；仍无法表达时才按批次
  构建最小 Python 对象。
- 保持现有最终数据库结果、后端响应、前端字段和回放身份兼容。
- 在精确 26,000,000 原始输入行上达到默认 Pipeline 峰值内存小于
  2,000,000,000 B、全链路墙钟时间小于 3,600 秒。

2026-09-24 更正（Q76）：上述“现有最终数据库结果兼容”是 Q19–Q63 的原设计
目标，不要求本次新的网格跨轨迹算法与历史中间结果或当前旧算法输出相同。
开发阶段只把 `trajectory_points`、`day01` 等源数据表视为权威来源；此前生成的
中间结果不作为本次新算法的验收基线。后端/前端字段兼容要求未被本条更正撤销。

### 2.2 非目标

- 本次不引入自研线程池、进程池、Dask 或阶段内 patch 续跑。
- Parquet 不替代 PostgreSQL 成为业务权威存储。
- DuckDB 临时 catalog 或临时文件不作为可恢复的第三套数据库。
- 不兼容读取旧 spool、pickle、memmap 或任务制品。
- 不保留长期的新旧双实现开关。
- 不要求保存可删除的旧中间数据；重建以权威源和 completed 上游检查点为准。

## 3. 需求与使用场景

### 3.1 阶段数据流

1. Refine 从 PostgreSQL 权威源读取，经 DuckDB 及必要回退计算，写 PostgreSQL Refine
   结果并发布 Refine Parquet。
2. Detect 以 Refine Parquet 为大表输入，只从 PostgreSQL 读取 Run、配置等小元数据；
   其结果写入 PostgreSQL，并发布 Detect Parquet。
3. Clip 从 Detect Parquet 顺序读取。
4. Fitting 消费 Clip 顺序输出，并通过 psycopg 事务写最终 PostgreSQL 表。
5. Network 沿用紧凑结果路径。
6. Detect 发布后，resolved_point_id 与 (subtrack_index, sequence_in_subtrack) 保持
   稳定；Clip、Fitting、Network 不插入、删除或重排已解析点。

2026-09-24 局部变更（Q64–Q79，已实施）：Detect 的转向事件继续从 Refine Parquet
读取，跨轨迹候选从 PostgreSQL 读取同一 `refine_id` 的保留点与注入点。此项是上述
第 2 条的局部例外；实现采用进程内网格、点的相邻子轨迹邻居、局部 NumPy 求交和
跨网格线段对去重。

局部验证：在 `roadnet_test` 的 `trajectory_points` 写入两辆车各 4 个源点，使用
`services/pipeline/config/pipeline-roadnet-test.toml` 运行完整 Pipeline；Refine 保留
8 点，Detect 持久化 1 个跨轨迹候选，交点经纬度为 `(111.0, 30.0002)`，各阶段
均完成。Pipeline 主测试集为 1,040 passed、59 skipped；该记录不包含大规模性能验收。

### 3.2 失败与重跑

- 失败 Run 永久保持 failed，不改回 completed。
- 清理或对账 staging 及临时目录。
- 使用新 Run ID，从最近 completed 上游 Parquet 检查点重跑整个失败阶段。
- 不恢复阶段内部的单文件、patch 或局部进度。
- 默认运行硬上限为 2,000,000,000 B。预计单个不可拆分 Python 语义单元超限时停止，
  由操作者显式提高上限重跑并记录原因、新旧上限；最大为 3,000,000,000 B。

## 4. 方案与关键决策

### 4.1 组件职责

- PostgreSQL：权威源、Run 血缘、制品登记、生命周期事件和最终业务结果。
- Parquet：Refine、Detect 之间及 Detect、Clip 之间的不可变列式数据集。
- DuckDB：默认计算引擎，直接扫描 PostgreSQL/Parquet并执行列式算子。
- Polars：DuckDB 无法保持语义或基准明显更差时的 LazyFrame 回退。
- Python：两种引擎都无法表达时，按列式批次构建最小对象并顺序运行算法。
- psycopg：最终 PostgreSQL 原子写入、约束检查和 API 兼容边界。

每个非 DuckDB 回退必须由测试记录理由。禁止为复用旧函数而整表 fetchall 或整表构造
Python 对象。

### 4.2 路径与文件布局

宿主路径均从 DATA_ROOT 派生：

- Parquet：${DATA_ROOT}/parquet；
- DuckDB 临时外存：${DATA_ROOT}/duckdb/{run_id}；
- Polars 临时/文件缓存：${DATA_ROOT}/polars/{run_id}。

容器内默认对应 /data/parquet、/data/duckdb、/data/polars。Parquet 布局为：

${PARQUET_ROOT}/{stage}/{run_id}/{logical_table}/part-*.parquet

不按每辆车或每个空间网格建立目录。文件大小、行组大小和排序键由真实基准决定。

DuckDB 每阶段使用内存 catalog，不持久化 .duckdb 权威状态。临时目录按 Run 隔离，
成功或失败退出后清理；崩溃遗留由对账处理。

### 4.3 不可变发布协议

1. 在同一文件系统的 Run 专属 staging 目录写完整数据集。
2. 校验 Schema、行数、文件大小、Parquet footer 和 SHA-256。
3. PostgreSQL Run 保持未完成。
4. 将 staging 数据集原子改名到最终只读路径。
5. 在一个 PostgreSQL 事务中写制品元数据、追加 present 事件并将 Run 标记 completed。
6. 最后事务失败时，未登记文件是不可见 orphan，由对账删除。

发布后的文件和目录设为只读；应用只读打开。重算使用新 Run 路径，不允许修补或覆盖
part 文件。

### 4.4 制品登记与清理

新增不可变制品元数据和追加式生命周期事件。元数据至少包括所属 Run/阶段、逻辑表、
相对路径、文件清单、Schema 版本与规范指纹、来源指纹、行数、字节数、SHA-256 和
创建时间。生命周期只追加 present/deleted 事件，当前状态由最新事件推导。

消费者每次检查 present、受控根路径、Schema、文件清单、大小与 Parquet footer。
SHA-256 在发布时完整计算，周期审计或故障调查时重算，不在每次消费前重读全部内容。

当前成功血缘引用的制品不得自动删除。失败、取消或明确淘汰的 Run 制品默认经过 7 天
宽限期后才可清理。Pipeline CLI 提供 artifact reconcile/gc，默认 --dry-run；只有显式
--apply 才删除文件并追加 deleted 事件。调度由 Compose 或外部计划任务负责，不新增
后台线程。

### 4.5 来源一致性和 Schema 演进

DuckDB 扫描 PostgreSQL 前后核对来源指纹；不一致时丢弃 staging、标记失败并重跑。
制品元数据保存来源指纹。

每个逻辑表保存整数 schema_version 和规范 Schema 指纹。消费者拒绝未知版本；列或
类型变化产生新 Run 和新不可变数据集。兼容读取只允许显式注册的版本适配器。

### 4.6 投影数值基线

投影坐标计算以当前项目固定投影环境为准；这里的“当前”不表示自动联网升级依赖、切换
EPSG 或追随外部最新版。旧运行未固定完整投影依赖环境时，不要求恢复其最低位浮点结果，
历史持久化结果也不再作为不可替换的数值 oracle。允许清理可重算旧结果并在当前固定环境
中重跑，再以同源输入、相同参数下的参考计算与列式路径进行比较。

更换数值基线不等于差分通过，也不改变 Q49：原坐标与其他浮点容差、成员归属、crossing
配对、拓扑和性能目标均不自动放宽，失败结果不得改判通过。用户已授权“可删除后重跑”的
原则，但具体 Run、目录和数据删除范围尚未核定；实施前仍须逐项确认，不能据此扩大到
本次清理范围之外的原始源数据或其他项目。

## 5. 接口、数据与架构影响

### 5.1 Refine Parquet

按 refine_id 隔离运行，包含 kept_points 和 injected_points。

共同列：

- refine_id、point_id、vehicle_id；
- subtrack_index、refined_sequence_in_subtrack；
- timestamp、lon、lat、x、y、heading、speed。

kept_points 增加 source_row_key。injected_points 增加 generated_reason 和锚点顺序。
不复制当前 Detect 算法未使用的源表 extra。

### 5.2 Detect Parquet

按 detect_id 隔离并包含：

- resolved_points：resolved_point_id、subtrack_index、sequence_in_subtrack、
  vehicle_id、lon、lat、crossing_id、intersection_id、crossing_type，以及
  source_row_key/Refine 点身份；
- intersections：intersection_id、lon、lat、radius_m；
- entry_exit_points：稳定 crossing、intersection、resolved-point 关联及进出类型。

所有逻辑表显式携带 detect_id。候选、转向事件等诊断数据只保存在 PostgreSQL。

### 5.3 外部兼容性

保持现有 Run 血缘、地图图层、GeoJSON 顶层结构和 metadata，以及前端已消费的
detect_id、fit_id、source_row_key、resolved_point_id、intersection_id 等字段。
内部实现和可重建中间数据可以改变；最终业务语义和接口字段不能改变。

Q76 对 2026-09-24 的新跨轨迹算法作局部更正：保留外部接口字段要求，但不要求
候选、交叉口等可重建结果与旧算法或历史运行逐项对应；源数据表仍为权威输入。

### 5.4 删除范围

删除以下模块及只验证这些实现的测试：

- clip_spool.py、cross_member_store.py、detect_workspace.py、writer_workspace.py；
- patch_spool.py、patch_admission.py、patch_coordinator.py、patch_pool.py；
- db/patch_ledger.py 和 004_patch_ledger.sql。

保留 heartbeat.py。candidate_patches.py、dbscan_storage.py 仅在新路径仍需要其业务
算法语义时保留并去除旧设施耦合；最终无调用者则删除。

任务账本未由生产初始化器自动安装。仓库只提供曾手工安装者使用的明确 DROP TABLE
顺序，不让程序扫描或删除未知 Schema。

### 5.5 Detect 网格取点接口变更草案（Q64–Q78）

- `grids[col][row]` 是本次跨轨迹任务进程内矩阵的一格，保存处理坐标 XY 的
  闭区间 AABB 与 `todo/running/done/failed` 状态；`grid_index` 是矩阵下标，
  AABB 从对应格读取，不建立第二套独立网格权威值。
- 网格覆盖当前 Refine Parquet 的 XY 范围；边长为可配置算法参数，初值 5 km。
- `fetch_points_in_grid` 仅为跨轨迹路径从 PostgreSQL 读取同一 `refine_id` 的
  保留点和注入点；转向事件继续读 Refine Parquet。每个在 AABB 内命中的点带同
  一子轨迹的前驱与后继，按列分批输出构段所需身份、顺序、坐标和方向字段。
- 同一对线段跨网格重复求得时按稳定线段身份去重，再按车辆/线段的确定性全局
  顺序排列并连续编号。不要求输出集合与旧算法相同。
- 两端均在 AABB 外、但线段穿过 AABB 的情形允许漏检；此为已接受的算法取舍。

这是 Q79 已确认的设计摘要，并非已实现接口。

## 6. 约束与非功能要求

### 6.1 资源边界

默认验收使用新建 Pipeline 父控制组：

- memory.max = 2,000,000,000 B；
- memory.swap.max = 0；
- 计入 Python、DuckDB、Polars、原生线程和本控制组文件页缓存；
- PostgreSQL 容器单独计量；
- DuckDB 初始 memory_limit = 1 GB、threads <= 4；
- Polars 强制流式执行并限制线程数。

DuckDB memory_limit 只约束其主要缓冲管理器，不能替代控制组硬边界。参数可依据基准
向下或向上校准，但默认验收不得突破 2 GB。

Q73–Q77 的新跨轨迹任务仅使用本地最小线程调度：可配置默认 4、最多 8 个线程，
每线程独立 PostgreSQL 连接；不建立通用调度框架或 Dask 集成。一般可恢复网格
失败最多重试 2 次，仍失败则 Detect 失败；单格内存超预算立即返回错误且不自动
重试或重跑。全部网格为 `done` 才完成跨轨迹任务。

### 6.2 配置与版本

DATA_ROOT、PARQUET_ROOT 等挂载根来自环境变量。DuckDB 内存、线程、临时盘上限、
Polars 线程、文件/行组目标和 GC 宽限期集中在 pipeline.toml [runtime]。每个 Run
记录实际有效参数和机器 CPU、内存、磁盘配置。

pyproject.toml 使用 DuckDB、Polars 的受控兼容范围；容器构建锁定解析后的精确版本和
DuckDB PostgreSQL 扩展。扩展在镜像构建期安装，运行时禁止联网下载。依赖或扩展升级
必须重跑语义差分与 26M 基准。

## 7. 验收标准

### 7.1 语义

下列严格差分标准属于 Q19–Q63 原列式迁移基线。本次 Detect 网格跨轨迹算法按
Q76 不与历史中间结果或当前旧算法输出比较，也不设置二者的数量/位置容差。

Q78 已选择独立功能验收：用少量基于权威源数据的已知交叉、无交叉、边界和
跨网格重复用例验证取点、前驱/后继、线段对去重与交点；再用 Pipeline 冒烟测试
确认聚类和持久化可完成。不与历史中间结果或旧算法输出比较。

- 在当前项目固定投影环境中，以同源输入、相同参数的参考计算与列式路径比较；
- 成员归属、crossing 配对和拓扑严格一致；
- 自增 ID 映射到稳定业务身份后比较；
- 米制坐标绝对误差不超过 1e-6 m；
- 经纬度绝对误差不超过 1e-9°；
- 其他浮点误差不超过 1e-12；
- 拒绝非有限值；
- 坐标误差不能豁免任何离散拓扑变化。

### 7.2 26M 性能

- Refine 原始输入精确为 26,000,000 行；
- 使用默认 2 GB 硬上限，不使用 3 GB 运营重跑上限；
- memory.swap.max = 0，结束时无新增 oom/oom_kill；
- Pipeline 父控制组 memory.peak < 2,000,000,000 B；
- PostgreSQL 内存、临时文件和磁盘单独报告；
- 从 Refine 开始至 Network 完成的墙钟时间 < 3,600 秒；
- 对原 Q19–Q63 列式迁移范围，输出通过 7.1 的语义差分；Q76 的新 Detect 网格
  跨轨迹路径不适用旧输出差分，按本变更独立功能验收。

## 8. 实施与验证计划

1. 新增受控依赖、镜像内固定扩展、集中配置和路径检查。
2. 新增制品登记、生命周期事件、只读发布、消费校验及 reconcile/gc CLI。
3. 改造 Refine：建立新列式计算与不可变数据集发布路径，完成小样本和 1M 差分。
4. 改造 Detect：消费 Refine Parquet，按 DuckDB → Polars → Python 顺序迁移算子，
   发布 Detect Parquet并完成差分。
5. 改造 Clip/Fitting：Clip 消费 Detect Parquet，Fitting 保持顺序输出和事务写入。
6. 完成故障注入、来源变化、orphan、未知 Schema、只读权限和 GC 测试。
7. 通过阶段差分后删除旧设施、任务账本、专用配置和测试；最终不保留双实现开关。
8. 更新 README 和活跃运行指南；相关旧文档顶部标记已被本文取代并链接本文。
9. 在最终删除后的代码上依次运行：单元/Schema/故障测试、真实 PostgreSQL + Parquet
   集成测试、小型逐表差分、1M 差分与资源剖析、26M 最终验收。

切换期间参考计算与列式路径按 4.6 和 Q49 比较。任一验证闸门失败，不得删除该阶段旧实现
或宣称切换完成。旧中间数据及其他可重算结果可在核定范围后删除重建；数据库最终结构、
Backend 和 Frontend 字段必须在切换前后核对。

Q78 规定后续项目用语：Pipeline 冒烟测试使用
`services/pipeline/config/pipeline-roadnet-test.toml`；Pipeline 全量测试使用
`services/pipeline/config/pipeline.toml`。这是配置与测试类别的映射，不等于已执行测试。
本次 Detect 网格跨轨迹路径先按第 7.1 节新增的独立功能验收运行冒烟测试；
不要求与旧输出做差分。
本次实施按用户先前要求一次讨论和修改一个函数，逐步验证，不批量改造其他函数。

## 9. 风险与待决事项

### 9.1 Detect 网格取点变更访谈（2026-09-24）

用户提出 `fetch_points_in_grid`：输入为 `grid_index(col,row)`、
`grid_AABB(A,A,B,B)`、继承的 `refine_id` 和 `spatial_context`；输出为该 Refine
运行的 `kept_points union inject_points` 中落入 AABB 的所有点。此前讨论还提出
使用 PostgreSQL 点空间索引、按网格构造线段、NumPy 求交，并接受两端都在网格外而
穿过网格的线段漏检。这些是本轮访谈输入，尚未形成已确认的接口设计。

代码事实（2026-09-24 核查）：当前 Detect 大表从 Refine Parquet 读取，
`RefineDatasetReader.__enter__` 将保留点和注入点合成 `refine_points` 视图；
Refine 导出 Parquet 时从 PostgreSQL 保留点归属表关联源点，并合并注入点。
见 `services/pipeline/src/track/v2/refine_parquet.py:73-109,778-807`。
现有 PostgreSQL GiST 索引覆盖源轨迹点和注入点，保留点归属表本身没有几何列；
见 `services/pipeline/src/track/v2/cli/init_db.py:125-167,555-582`。

本轮局部回答：Q64 指定 PostgreSQL 合并两类 Refine 点，转向事件仍读 Parquet；
Q65 要求对 AABB 内命中的点带同一子轨迹前驱与后继，并称“两侧闭区间”；Q66 指定
`grid_index` 为本次跨轨迹任务维护的 `n*m` 临时网格矩阵下标，
`grids[col][row]` 存放该格 AABB；Q67 指定按列分批输出。Q68 确认矩阵在进程内，
所有网格状态为 `done` 时跨轨迹任务完成；Q69 确认米制处理坐标上的闭区间，边界
可同时属于相邻格；Q70 确认按线段身份去重。Q71 要求 Parquet 与 PostgreSQL
基于同一份 Refine 结果，但明确不增加数据指纹复核，依赖既有同源、幂等处理。
Q72 确认使用当前 Detect 已读取的 Refine Parquet XY 范围建网格，5 km 为可配置
初值；Q73 确认仅在本跨轨迹任务中使用可配置线程数（默认 4、最多 8），每线程独立
PostgreSQL 连接；Q74 确认 `todo/running/done/failed`、失败最多重试 2 次、
仍失败则 Detect 失败，全部格为 `done` 才完成；Q75 确认去重后恢复原有车辆/线段
全局顺序与连续编号。用户特别要求最小实现：不建立 Dask 或通用线程池、任务框架，
仅为本跨轨迹任务编写局部调度逻辑。Q76 更正：开发阶段不拿历史中间数据或旧
算法结果作为权威基线，不设置新旧结果容差；只把 `trajectory_points`、`day01`
等源数据表视为权威来源。Q77 确认单格内存超预算立即返回错误，不重试该格，
与 Q74 的其他可恢复失败最多重试 2 次区分。Q78 采用少量权威源数据构造的
几何用例及 Pipeline 冒烟测试，不与旧算法输出比较；测试配置对应关系见第 8 节。
新方案的性能与聚类质量尚未验证；本次设计确认与逐函数实施授权来自 Q79，
不由原 Q62/Q63 自动推定。

### 9.2 已确认基线的既有风险

- DuckDB/Polars 执行计划和外存行为依赖固定版本，升级必须重新验收。
- Polars 临时目录并非与 DuckDB temp_directory 完全等价的总资源配额；控制组仍是
  最终内存边界。
- 单个不可拆分 Python 语义单元可能触发 3 GB 显式运营重跑；该结果不计入默认 2 GB
  验收，通过后仍需评估算法拆分。
- 文件系统和 PostgreSQL 不存在联合事务；方案通过“文件先完成、数据库最后发布”和
  orphan 对账避免 completed Run 指向半成品。
- 具体 Parquet 文件大小、行组大小、排序键和每个算子的最终引擎归属由基准与差分测试
  决定，不在缺少证据时预设。
- 可重算结果的具体删除范围仍待逐项核定；在此之前不得删除任何 Run 或目录，也不得把
  清理范围扩大到原始源数据或其他项目。
- Q62 已完成最终设计确认，Q63 已授权分阶段实施；26M 性能验收尚未进行。

### 9.3 网格查询性能与整数行键访谈（2026-09-25）

用户要求在 roadnet_test.public.day01 建立 EPSG:32649 的投影表达式索引，
用 `ST_Intersects(ST_Transform(s.geom, 32649), ST_MakeEnvelope(..., 32649))`
与现有临时命中表加服务端游标的两段式查询，对指定八个网格的扫描次数和耗时
进行比较。用户另要求将 `kept_trajectory_points` 与 `excluded_points` 的
`source_row_key` 改为 BIGINT。

代码事实（2026-09-25 核查）：当前 schema v7 的两列均为 TEXT；来源表契约
允许 smallint、integer、bigint、UUID 和文本主键，查询按来源主键类型转换
`source_row_key`。现有 v7 库不会因修改 CREATE TABLE IF NOT EXISTS 自动迁移。
已通过运行中的 Compose Pipeline 容器只读连接 roadnet_test：当前为 schema v7，
两列均为 TEXT；本轮性能对比尚未运行。

Q80 已确认两张表都改 BIGINT，并将来源限制为整数主键。Q81 指定方案 1
以 UTM 索引直接筛选 day01，再关联保留点表，一条查询输出；方案 2 保持先建
hits 临时表再读取的两段式流程。Q82 回答旧数据可以删除；Q83 确认两方案输出完全一致。Q84 指定清理保留点
及后续产物，保留 roadnet_test.day01 与 roadnet.trajectory_points；Q86 已确认两个数据库的派生数据都清理；Q87 已确认数据库派生数据及对应 Parquet 目录均清理；Q88 已确认先对比、再清理和改类型。Q89 已确认完整执行范围并授权实施；性能对比、清理和类型改造尚待实际执行与验收。本轮访谈尚未完成；
没有执行数据库变更。
新决定只覆盖本轮明确范围，不改写 Q79 的历史结论。

## 10. 确认记录

用户已在 Q62 以“确认”明确确认当时第 1–9 节为完整共享设计。设计确认日期为
2026-09-19。当时没有未决设计问题；具体文件/行组目标和算子最终归属按已确认的基准
与差分规则在实施中确定。2026-09-24 的第 9.1 节是后续局部变更，已由 Q79
单独确认，不把旧输出差分标准套用于该新跨轨迹算法。

用户随后在 Q63 授权按第 8 节分阶段实施并创建小型本地提交；每次提交的新增与删除
合计软限制为 700 行，大量纯重命名提交例外。Q63 后续补充要求由指定子代理实施、
主对话验收。该实施授权不包括推送远端或删除 `/data` 运行数据。

26M 性能目标尚未实测，不能视为验收确认。

用户在 Q79 回答“确认并让subagent(gpt6 sol low)实施”，确认 Detect 网格跨轨迹
局部设计，并授权 `gpt-6-sol`、`low` 子代理逐函数实施。沿用先前“一次讨论和
修改一个函数”的协作范围；该授权不表示已完成实现或验收。

## 附录：完整 QA 记录

以下 Q 编号延续历史 Spec；Q1–Q18 及其更正保留在
[旧 Spec](pipeline-20m-memory-budget-spec.md) 中。本文完整保存本次架构改向的
Q19–Q63 原题、推荐、用户回答和解释，以及 Q63 后续实施组织补充。

### 第 6 轮 — Parquet、DuckDB 与 Polars 架构改向的根决策（2026-09-19）

发问时前提：用户已明确启动新的 grilling 访谈；本轮要求会改变 Q12、Q13 及相关
实施结果。旧问答保留，当前状态恢复为草稿；尚未执行代码回撤。

#### Q19 — 自研设施的删除边界

> “全部回撤删除”是否指删除所有自行编写的**计算数据暂存与计算并发设施**，包括
> `clip_spool.py`、`cross_member_store.py`、`detect_workspace.py`、
> `writer_workspace.py`、`patch_spool.py`、`patch_admission.py`、
> `patch_coordinator.py`、`patch_pool.py` 及其专用测试和配置？以下基础设施是否保留：
> A. 保留进度心跳线程、数据库驱动内部线程、命名游标、事务/运行批次状态；
> B. 连上述非计算设施也全部删除；
> C. 只删进程池四件套，暂时保留现有磁盘工作区。

> 推荐 A。它完整删除自研磁盘暂存和计算并发机制，同时不误删可观测性、流式读取、
> 原子提交与续跑所需的基础设施。

> 用户回答：Q19 A

正文关联：10.1–10.3。
当前解释：删除所列自研计算暂存和计算并发设施；保留心跳、命名游标、事务及运行状态。

#### Q20 — 三种列式组件的职责

> 首版是否采用以下职责边界：PostgreSQL 继续作为源数据、运行状态和最终业务结果的
> 权威存储；Parquet 是阶段间列式数据集；DuckDB 负责 SQL、跨文件连接、聚合、排序
> 与空间分块候选计算；Polars LazyFrame 负责投影、过滤、表达式和批量列变换？备选：
> A. 采用上述分工；B. DuckDB 统一承担全部列式计算，Polars 仅在确有优势时使用；
> C. Polars 为主，DuckDB 只查询 Parquet。

> 推荐 A，但要求每项变换只选一个主执行引擎，避免 DuckDB 与 Polars 之间反复复制
> Arrow 数据；按算子能力和基准结果选择归属，不追求两个库都参与每一阶段。

> 用户回答：Q20 Postgresql作为权威存储，parquet放在/data/tmp/project_name/，维护每次refine detect后产生的parquet文件存在情况（存在、删除，不允许修改），duckdb作为主力，如果duckdb无法实现，则通过duckdb+polars+python构建对象+python算法

正文关联：10.2。
当前解释：PostgreSQL 是权威存储；Refine、Detect 发布不可变 Parquet 制品并维护存在/
删除状态；DuckDB 优先，不能实现时才组合 Polars 及 Python。具体路径含义、登记方式、
原子发布与回退边界进入下一轮。

#### Q21 — 不可列式化 Python 算法的首版策略

> 对暂时无法用 DuckDB/Polars 表达的几何、聚类或拓扑 Python 算法，首版选择：
> A. 保持单进程、单计算线程，按 Parquet 分区或 Arrow RecordBatch 顺序执行；
> B. 暂时保留现有进程池，直到逐个算法迁移；C. 本轮同步引入 Dask。

> 推荐 A。这样严格落实“后续再自行设计线程/进程/Dask”，本轮不留下两套并发架构；
> 但仍允许 DuckDB、Polars 及其底层库使用由配置统一限制的内部线程。

> 用户回答：Q21 A

正文关联：10.2–10.3。
当前解释：Python 回退首版单进程、单计算线程，顺序处理列式批次；不保留旧进程池，
本轮不引入 Dask。引擎内部线程数仍需下轮确定。

#### Q22 — 语义、接口与旧运行数据兼容

> 本次是否要求算法输出语义、数据库最终表、后端响应和前端消费字段保持兼容，但允许
> 删除旧运行的暂存/中间数据并从源数据重跑；不保留旧自研暂存格式的读取兼容？备选：
> A. 是；B. 连最终数据库/API 字段也可重做；C. 必须兼容读取旧暂存制品。

> 推荐 A。这继承你此前“历史数据可删除、重点核对前后端字段”的更正，同时避免为
> 即将删除的自研格式维护迁移层。

> 用户回答：Q22 A允许删除旧的中间数据重跑，但是保留前后端字段兼容性

正文关联：10.1–10.3，以及 Q18 后续更正。
当前解释：旧中间数据及旧格式兼容层均可删除；通过从权威源数据重跑恢复；最终数据库
结果、后端响应与前端消费字段保持兼容。

#### Q23 — 既有容量与性能目标

> 精确 26M 行、Pipeline 峰值内存 < 1 GB、全链路 < 1 小时的既有目标如何处理？
> A. 保留为最终验收目标，但本次先建立列式基线，未实测前不承诺达标；
> B. 只保留正确性与内存目标，取消 1 小时目标；C. 全部重新定义。

> 推荐 A。架构改向不应悄然降低目标，但首版串行 Python 热点可能暂时达不到 1 小时，
> 应以阶段剖析结果决定后续是否引入线程、进程或 Dask。

> 用户回答：Q23 内存峰值2GB，时间小于1小时

正文关联：第 2、5–8 节及 10.2–10.3。
当前解释：将既有峰值内存小于 1 GB 更正为小于 2 GB；全链路小于 1 小时不变。
精确 26M 行以及 Pipeline/PostgreSQL/文件页缓存的计量边界仍需确认。

#### Q24 — 引擎内部临时磁盘是否允许

> “删除自行编写的磁盘外溢”是否仍允许 DuckDB/Polars 在明确的 `DATA_ROOT` 子目录、
> 内存上限和清理规则下使用其**库内置**的外存执行/临时文件？选择：A. 允许引擎内置
> 外存，但禁止项目自研 spool/spill；B. 完全禁止临时磁盘，只允许显式阶段 Parquet；
> C. DuckDB 可用，Polars 不可用。

> 推荐 A。26M 行下排序、连接和分组若完全禁止引擎外存，可能直接 OOM；关键边界是
> 不再维护项目自研外溢算法，并把库内置临时目录、容量和生命周期纳入配置与验收。

> 用户回答：Q24 允许引擎外存，位置在data_root/duckdb或者data_root/polars

正文关联：10.1–10.3。
当前解释：允许 DuckDB、Polars 使用引擎内置外存，分别放入 `DATA_ROOT/duckdb` 和
`DATA_ROOT/polars`。目录容量、生命周期及引擎内存/线程限制待下轮确认。

### 第 6 轮合并回答 — 对应 Q19–Q24

接收日期：2026-09-19。

用户回答原文：

> Q19 A,Q20 Postgresql作为权威存储，parquet放在/data/tmp/project_name/，维护每次refine detect后产生的parquet文件存在情况（存在、删除，不允许修改），duckdb作为主力，如果duckdb无法实现，则通过duckdb+polars+python构建对象+python算法 , Q21 A, Q22 A允许删除旧的中间数据重跑，但是保留前后端字段兼容性，Q23 内存峰值2GB，时间小于1小时，Q24 允许引擎外存，位置在data_root/duckdb或者data_root/polars

解释范围：Q19、Q21、Q22 明确采纳对应 A；Q20 自定义了存储权威性、Parquet 路径、
不可变生命周期和计算回退顺序；Q23 更正内存目标为 2 GB 并保留一小时目标；Q24
允许引擎外存并指定两个目录。路径变量语义、登记/发布/删除协议、精确容量口径和
引擎限制未由该回答确定，进入后续问题。

正文同步：顶部状态、10.1–10.3，以及 Q19–Q24 各题当前解释。

本轮检查：Q1–Q24 连续且唯一；Q1–Q24 均已回答；旧决定、更正及回答原文完整保留；
新方向尚有下游问题，未作完整设计确认，完整 QA 附录仍位于文末。

### 第 7 轮 — 路径、制品发布、清理与资源边界（2026-09-19）

发问时前提：Q19–Q24 已回答。代码审计确认宿主路径与容器路径不同、当前业务尚未
使用 Parquet、Run 表不足以表达不可变多文件制品；DuckDB 有明确资源配置，但
`memory_limit` 不能约束全部进程内存，Polars 也没有与其完全等价的总内存配置。

#### Q25 — 宿主路径与容器路径

> Q20 的 `/data/tmp/project_name/` 是路径模式还是字面目录？选择：A. 将宿主
> `DATA_ROOT` 设为 `/data/tmp/<project_name>`（本项目实际为
> `/data/tmp/road-network`），Parquet 位于 `${DATA_ROOT}/parquet`；容器内分别为
> `/data` 和 `/data/parquet`；B. Parquet 直接放在宿主字面路径
> `/data/tmp/project_name/`，不设 `parquet` 子目录；C. 保持现有独立
> `PARQUET_ROOT`，它可以不属于 `DATA_ROOT`。

> 推荐 A。它与现有 Compose 挂载一致，并使宿主上的 Parquet、DuckDB、Polars
> 分别落在 `${DATA_ROOT}/parquet|duckdb|polars`，容器内为
> `/data/parquet|duckdb|polars`。

> 用户回答：Q25 路径模式，用data_root

正文关联：10.2、10.4。
当前解释：采用路径模式，宿主的 Parquet、DuckDB、Polars 目录均从 `DATA_ROOT`
派生；容器内路径沿用 Compose 固定挂载目标。

#### Q26 — 哪些阶段发布 Parquet 数据集

> 是否严格按你的表述，只让每次成功的 Refine 和 Detect 各发布一个逻辑 Parquet
> 数据集，后续阶段读取这些数据集；Clip、Fitting、Network 首版不额外发布阶段
> Parquet？选择：A. 是；B. Refine、Detect、Clip、Fitting 都发布；C. 只有 Refine
> 发布，Detect 结果仍只进 PostgreSQL。

> 推荐 A。它先建立两个明确检查点，避免把 Parquet 复制到每个阶段；Fitting 最终
> 结果仍写 PostgreSQL。若后续基准证明 Clip/Fitting 需要检查点，再扩展同一协议。

> 用户回答：Q26 A refine和detect发布，后续不改变resoled_index,无新增注入点，所以不用发布parquet

正文关联：10.2、10.4。
当前解释：仅 Refine 和 Detect 发布 Parquet。结合代码核查，`resoled_index` 暂解释为
Detect 后不再变化的已解析点序列索引/身份；仓库不存在同名字段，准确身份规则进入
Q35，不擅自把历史原文改写成某个代码字段。

#### Q27 — 制品存在/删除状态的登记模型

> PostgreSQL 如何维护 Parquet 文件的存在/删除状态？A. 新增不可变制品元数据表
> （路径、Schema 版本、行数、字节数、SHA-256、所属 Run）和追加式事件表
> （`present`/`deleted`、时间、原因），当前状态取最新事件；B. 单表并更新
> `deleted_at`；C. 只扫描文件系统，不在 PostgreSQL 登记。

> 推荐 A。Parquet 内容和身份不修改，生命周期只追加事件；既满足 PostgreSQL 权威性，
> 也保留删除审计和崩溃恢复依据。

> 用户回答：Q27 A

正文关联：10.2、10.4。
当前解释：新增不可变制品元数据与追加式 `present`/`deleted` 生命周期事件。

#### Q28 — 不可变数据集的原子发布

> 是否采用以下发布协议：先在同一文件系统的 Run 专属 staging 目录写完整数据集，
> 校验 Schema、行数、文件大小和校验和后原子改名到最终只读路径，再在 PostgreSQL
> 事务中追加 `present` 事件；数据库不可见的遗留目录由对账清理，不允许读半成品？
> 选择：A. 采用；B. 文件逐个写入最终目录并逐个登记；C. 先登记再异步写文件。

> 推荐 A。多文件 Parquet 数据集只有整体发布后才可消费；崩溃窗口产生的是可识别的
> orphan，不会把不完整数据集暴露给后续阶段。

> 用户回答：Q28 A

正文关联：10.2、10.4。
当前解释：采用同文件系统 staging、完整校验、原子改名、再登记可见的发布协议；
orphan 不对下游可见，由对账清理。

#### Q29 — 删除与保留策略

> 已发布制品何时可删除？A. 当前成功血缘引用的 Refine/Detect 制品必须保留；失败、
> 已取消或被明确淘汰的 Run 制品经过可配置宽限期后由显式垃圾回收删除并追加事件；
> B. 下游阶段成功后立即删除上游 Parquet；C. 只允许人工删除，系统永不清理。

> 推荐 A。它支持重试、回放和问题定位，又不会无限积累；“允许删除旧中间数据”不应
> 等同于运行仍被引用时自动删除。

> 用户回答：Q29 A

正文关联：10.2、10.4。
当前解释：保留当前成功血缘引用的制品；失败、取消或淘汰的 Run 制品经宽限期后由
显式垃圾回收删除并追加事件。宽限期数值后定。

#### Q30 — 2 GB 的计量边界与初始引擎限制

> 是否将 2 GB 定义为新建 Pipeline 父控制组的 `memory.max = 2,000,000,000` B，
> 包含 Python、DuckDB、Polars、原生线程及本控制组文件页缓存，`swap.max = 0`，
> PostgreSQL 容器单列；初始设置 DuckDB `memory_limit = 1 GB`、`threads <= 4`，
> Polars 强制流式执行并限制线程数，最终参数按 26M 基准向下或向上校准但不得突破
> 2 GB？选择：A. 接受；B. 2 GB 只计算进程 RSS，不含页缓存；C. PostgreSQL 也计入
> 同一个 2 GB。

> 推荐 A。DuckDB 官方明确指出 `memory_limit` 不覆盖所有分配，因此必须为 Python、
> Polars、查询结果和页缓存留余量，并以控制组峰值作最终判断。

> 用户回答：Q30 A

正文关联：第 5–8 节、10.2、10.4。
当前解释：接受 2 GB Pipeline 父控制组、无交换空间、PostgreSQL 单列，以及 DuckDB
初始 1 GB/最多 4 线程和 Polars 流式受控执行。该硬上限与 Q31 的临时突破冲突，
Q32 澄清前不能同时作为有效执行规则。

#### Q31 — 单个 Python 语义单元超过预算

> DuckDB/Polars 已尽量缩小输入后，若单个不可再安全拆分的车辆、空间分区、簇或拓扑
> 连通分量仍会让 Python 对象超过 2 GB，首版如何处理？A. 在物化前按估算/实测保护
> 阈值拒绝，标记 Run 失败并保留可重跑信息，不临时启用自研外溢或突破内存上限；
> B. 临时突破 2 GB；C. 自动恢复旧 spool。

> 推荐 A。可保持语义的进一步拆分属于后续算法设计；本轮不能用隐式回退重新引入
> 已决定删除的设施。

> 用户回答：Q31 临时突破

正文关联：10.2、10.4。
当前解释：用户选择允许临时突破，而非推荐 A。由于 Q30 已接受
`memory.max = 2,000,000,000` B，技术上不能在该控制组内突破；需决定是显式提高
硬上限、把 2 GB 降为软目标，还是撤销本回答。不得静默突破或自动恢复旧 spool。

本轮发问检查：Q1–Q31 连续且唯一；Q1–Q24 已回答，Q25–Q31 待回答；问题依赖均
已满足，Schema、分区键、具体表列和算子迁移仍依赖本轮决定，留待下一轮。

### 第 7 轮合并回答 — 对应 Q25–Q31

接收日期：2026-09-19。

用户回答原文：

> Q25 路径模式，用data_root ，Q26 A refine和detect发布，后续不改变resoled_index,无新增注入点，所以不用发布parquet，Q27 A，Q28 A,Q29 A,Q30 A ,Q31 临时突破

解释范围：Q25 选择以 `DATA_ROOT` 为根的路径模式；Q26–Q30 采纳 A，并补充只有
Refine、Detect 发布的理由；Q31 明确拒绝推荐的失败策略，要求临时突破。Q30 与 Q31
当前互相矛盾，不能推定某一回答覆盖另一回答，交由 Q32 明确更正。`resoled_index`
原文保留，正文暂作概念解释，不虚构同名字段。

正文同步：10.2、10.4，以及 Q25–Q31 当前解释。

本轮检查：Q1–Q31 连续且唯一；Q1–Q31 均有回答，但 Q30/Q31 存在待澄清冲突；
新方向尚未完整确认，完整 QA 附录仍位于文末。

### 第 8 轮 — 内存突破、数据集内容与稳定身份（2026-09-19）

发问时前提：Q25–Q31 已回答；Q30/Q31 存在硬上限与临时突破冲突。仅 Refine、Detect
发布 Parquet 已确认，具体逻辑表、稳定身份、文件布局和 PostgreSQL 连接方式尚未确定。

#### Q32 — “临时突破 2 GB”的可执行定义

> Q30 的 `memory.max = 2,000,000,000` B 无法被同一控制组内的算法临时突破。
> 请选择：A. 2 GB 改为软目标，父控制组从开始就设置更高硬上限，正常运行也可能超过
> 2 GB；B. 默认运行仍以 2 GB 为硬上限，预计单个不可拆分 Python 单元会超限时停止，
> 由操作者显式指定更高上限重跑，并把原因、旧/新上限记入 Run；C. 撤销 Q31，超过
> 2 GB 就失败。如果选择 A 或 B，还需给出允许的最大硬上限。

> 推荐 B，最大硬上限 3,000,000,000 B。这样“临时突破”是显式、可审计的重跑，
> 不会让日常 2 GB 验收目标失去含义，也不会尝试在技术上绕过控制组。

> 用户回答：Q32 B

正文关联：第 5–8 节、10.2、Q30–Q31。
当前解释：默认 2 GB 硬上限；预计不可拆分 Python 单元超限时停止，由操作者显式
提高上限重跑并记录。用户尚未回答允许的最大硬上限，转入 Q38。

#### Q33 — Refine Parquet 的内容

> Refine 的逻辑数据集应包含什么？A. 包含保留点、注入点、剔除记录及其稳定源引用，
> 以多个同 Run 逻辑表组成一个不可变数据集；B. 只包含 Detect 所需的保留点和注入点，
> 剔除记录只留 PostgreSQL；C. 只包含一张合并后的有效点表。

> 推荐 B。Parquet 是列式计算检查点而非 PostgreSQL 备份；保留 Detect 的完整输入，
> 审计型剔除记录继续由权威数据库保存，避免重复不参与下游计算的数据。

> 用户回答：Q33 用refine_id分离不同运行，保存kept_points、injected_points（lon,lat,timestamp,(subtrack_index, sequence_in_subtrack)

正文关联：10.2、10.4。
当前解释：按 `refine_id` 隔离运行，分别保存 `kept_points`、`injected_points`，用户
明确列出 `lon`、`lat`、`timestamp`、`subtrack_index`、`sequence_in_subtrack`。
代码审计显示 Detect 还需要车辆、点身份/类型、方向角、速度和处理坐标等字段；完整
最小 Schema 转入 Q39，不把用户未列出的字段静默视为已确认。

#### Q34 — Detect Parquet 的内容

> Detect 的逻辑数据集应包含什么？A. 只包含后续 Clip/Fitting 所需的已解析点序列、
> 交叉口、进出点及必要关系；候选、转向事件等诊断数据留 PostgreSQL；B. 镜像 Detect
> 写入 PostgreSQL 的全部逻辑表；C. 只包含已解析点序列。

> 推荐 A。它满足后续阶段的只读列式输入，同时保持 PostgreSQL 的权威性，避免复制
> 不参与后续计算的候选与诊断数据。

> 用户回答：Q34 A 只包含后续 Clip/Fitting 所需的已解析点序列、 交叉口、进出点及必要关系；候选、转向事件等诊断数据留 PostgreSQL

正文关联：10.2、10.4。
当前解释：Detect Parquet 只保存 Clip/Fitting 所需数据；候选、转向事件等诊断数据
只留 PostgreSQL。

#### Q35 — `resoled_index` 的准确含义

> 你所说“后续不改变 `resoled_index`”是否指：Detect 发布时确定
> `resolved_subtrack_points.id`（API 中的 `resolved_point_id`）以及
> `(subtrack_index, sequence_in_subtrack)` 顺序，Clip/Fitting/Network 不再插入、删除、
> 重排这些点，并始终引用这些身份？选择：A. 是，两类身份和顺序都稳定；B. 只要求
> 子轨迹内顺序稳定，数据库 ID 可以重建；C. 另有具体字段。

> 推荐 A。前端回放和 Clip API 已直接依赖 `resolved_point_id`，只保持顺序而重建 ID
> 会破坏已确认的接口兼容性。

> 用户回答：Q35 是

正文关联：10.2、10.4，以及 Q22、Q26。
当前解释：`resolved_point_id` 与 `(subtrack_index, sequence_in_subtrack)` 均在 Detect
发布后保持稳定，后续阶段不插入、删除或重排已解析点。

#### Q36 — Parquet 文件布局

> 首版文件布局选择：A. `${PARQUET_ROOT}/{stage}/{run_id}/{logical_table}/part-*.parquet`，
> 控制每个文件/行组的目标大小并按主要访问键排序，不按每辆车或每个网格建立目录；
> B. Hive 风格按车辆和空间网格分区；C. 每个逻辑表只有一个巨大文件。

> 推荐 A。它避免 26M 行下产生大量小文件；DuckDB 可利用列裁剪、谓词下推、行组
> 统计和排序局部性。文件及行组的具体目标大小由真实基准确定，不在设计阶段伪定最优值。

> 用户回答：Q36 A

正文关联：10.2、10.4。
当前解释：采用按阶段、Run、逻辑表组织的多文件布局；不按单车/单网格建目录，具体
文件和行组大小由基准决定。

#### Q37 — PostgreSQL 与 DuckDB 的连接边界

> 首版采用哪种方式？A. 在 Pipeline 镜像中固定安装 DuckDB PostgreSQL 扩展，DuckDB
> 直接扫描 PostgreSQL 权威输入并写 Parquet；最终业务结果仍由现有 psycopg 中央事务
> 写回和校验；B. 全部通过 psycopg Python 行对象搬运到 DuckDB；C. DuckDB 直接写入
> 最终 PostgreSQL 表并取代现有事务写入器。

> 推荐 A。读取侧避免重新构建大批 Python 行对象，写入侧保留现有原子提交、约束和
> API 兼容边界；扩展必须在镜像构建期固定版本，不允许运行时联网下载。

> 用户回答：Q37 A

正文关联：10.2、10.4。
当前解释：DuckDB PostgreSQL 扩展在镜像构建时固定安装，读取侧由 DuckDB 直接扫描
PostgreSQL，最终写入继续使用 psycopg 事务与校验；禁止运行时下载扩展。

本轮发问检查：Q1–Q37 连续且唯一；Q1–Q31 已回答，Q32–Q37 待回答；具体 Parquet
Schema、排序键、行组大小、算子迁移和验收矩阵依赖本轮答案，留待后续轮次。

### 第 8 轮合并回答 — 对应 Q32–Q37

接收日期：2026-09-19。

用户回答原文：

> Q32 B,Q33 用refine_id分离不同运行，保存kept_points、injected_points（lon,lat,timestamp,(subtrack_index, sequence_in_subtrack) , Q34 A 只包含后续 Clip/Fitting 所需的已解析点序列、 交叉口、进出点及必要关系；候选、转向事件等诊断数据留 PostgreSQL ，Q35 是， Q36 A,Q37 A ,

解释范围：Q32 选择 B，但未给出推荐中同时询问的最大硬上限；Q33 自定义 Refine
数据集隔离和已明确字段，括号虽未闭合但字段含义可识别，未据此推定其他必需列；
Q34、Q36、Q37 采纳 A；Q35 的“是”对应 A 所述两类身份与顺序都稳定。

正文同步：10.2、10.4，以及 Q32–Q37 当前解释。

本轮检查：Q1–Q37 连续且唯一；Q1–Q37 均有回答，但 Q32 最大硬上限与 Q33 最小
可执行 Schema 仍待补充；新方向尚未完整确认，完整 QA 附录仍位于文末。

### 第 9 轮 — Schema、跨介质发布与消费校验（2026-09-19）

发问时前提：Q32–Q37 已回答；默认 2 GB 与显式高上限重跑、两类 Parquet 数据集、
稳定身份、文件布局及 DuckDB/PostgreSQL 边界已经确定。最大重跑上限和数据集的最小
可执行字段仍未确定。

#### Q38 — 显式重跑的最大硬上限

> Q32 选择 B 后，操作者显式重跑时允许的 `memory.max` 最大值是多少？选择：
> A. `3,000,000,000` B；B. `4,000,000,000` B；C. 不设项目上限，只要求显式记录。

> 推荐 A。它为不可拆分 Python 对象留出一次受控余量，同时仍能暴露必须进一步列式化
> 或拆分的算法；若 3 GB 仍失败，应停止并重新设计，而非连续抬高。

> 用户回答：Q38 A

正文关联：第 5–8 节、10.2、Q30–Q32。
当前解释：显式高上限重跑的最大 `memory.max` 为 `3,000,000,000` B；超过后停止并
重新设计算法，不继续抬高。

#### Q39 — Refine 最小可执行 Schema

> 代码核查表明 Detect 不只需要 Q33 列出的经纬度、时间戳与顺序，还直接需要
> `vehicle_id`、点类型/稳定身份、`heading`、处理坐标 `x/y`；`source_row_key` 用于
> 血缘与接口兼容。是否采用以下列：共同列为 `refine_id, point_id, vehicle_id,
> subtrack_index, refined_sequence_in_subtrack, timestamp, lon, lat, x, y, heading,
> speed`；`kept_points` 另有 `source_row_key`；`injected_points` 另有
> `generated_reason` 和锚点顺序；不复制源表 `extra`？选择：A. 采用；B. 仍只保留
> Q33 明列的五类值，缺失字段运行时回查 PostgreSQL；C. 连 `extra` 也完整复制。

> 推荐 A。它让 Refine Parquet 成为 Detect 可直接扫描的完整列式输入，同时避免每行
> 回查 PostgreSQL 和复制当前 Detect 算法未使用的任意 JSON。

> 用户回答：Q39 A

正文关联：10.2、10.4、Q33。
当前解释：采用所列 Refine 最小可执行 Schema；不复制源表 `extra`。

#### Q40 — Detect 最小可执行 Schema

> 是否按当前 Clip 读取需求固定三个逻辑表：`resolved_points` 保存
> `resolved_point_id, subtrack_index, sequence_in_subtrack, vehicle_id, lon, lat,
> crossing_id, intersection_id, crossing_type`；`intersections` 保存
> `intersection_id, lon, lat, radius_m`；`entry_exit_points` 保存稳定 crossing、
> intersection、resolved-point 关联及进出类型？所有表显式带 `detect_id`，且
> `source_row_key`/refined point identity 也保留在 `resolved_points` 供血缘核验。
> 选择：A. 采用；B. 删除血缘列，只保留 Clip 当前构造对象所需列；C. 增加 Detect
> 全部诊断表。

> 推荐 A。它覆盖 Clip/Fitting 与身份校验，又不把候选和转向诊断表复制进 Parquet。

> 用户回答：Q40 A

正文关联：10.2、10.4、Q34–Q35。
当前解释：采用 `resolved_points`、`intersections`、`entry_exit_points` 三个逻辑表，
全部显式携带 `detect_id`，并保留源血缘/精炼点身份。

#### Q41 — PostgreSQL 与 Parquet 的发布顺序

> 文件系统与 PostgreSQL 无法组成单一原子事务。是否采用：staging 写入并校验；
> PostgreSQL 结果先保持未完成；原子改名发布文件；最后在一个 PostgreSQL 事务中
> 写制品元数据/`present` 事件并把 Run 标为 `completed`；若最后事务失败，文件成为
> 不可见 orphan，后续对账删除？选择：A. 采用；B. PostgreSQL Run 先 completed 再写
> Parquet；C. Parquet 出错也允许 Run completed。

> 推荐 A。任何 completed Run 都拥有完整已登记数据集；崩溃只会产生可安全回收的
> 未登记文件，不会产生引用缺失文件的成功 Run。

> 用户回答：Q41 A

正文关联：10.2、10.4、Q27–Q29。
当前解释：采用 staging 校验、文件原子发布、最后一个 PostgreSQL 事务登记制品并
完成 Run；未登记 orphan 对下游不可见并由对账清理。

#### Q42 — 消费前校验强度

> 下游每次打开数据集时如何校验？A. 校验 PostgreSQL 最新事件为 `present`、路径位于
> 允许根目录、Schema 版本、文件清单、大小和 Parquet footer；SHA-256 在发布时完整
> 计算，周期审计或故障调查时重算，不在每次读取时重算全部文件；B. 每次读取都重算
> 全部 SHA-256；C. 只检查路径存在。

> 推荐 A。它能快速拒绝缺失、替换或 Schema 错误，并避免每个阶段重复全盘读取大型
> 数据集；不可变存储权限和周期审计补足内容完整性。

> 用户回答：Q42 A

正文关联：10.2、10.4、Q27–Q29。
当前解释：每次消费执行快速结构校验；SHA-256 发布时完整计算，周期审计或故障调查
时重算，不在每次消费前全量重算。

#### Q43 — DuckDB 扫描 PostgreSQL 时的源一致性

> DuckDB 直接扫描 PostgreSQL 期间若源表发生变化，如何处理？A. 扫描前后计算并核对
> 现有来源指纹，仅在指纹一致时发布；不一致则丢弃 staging、标记失败并重跑；
> B. 长事务锁定源表直到 Parquet 发布；C. 假定源表不会变化，不做检查。

> 推荐 A。它复用现有来源指纹，避免长时间锁表；发布数据集必须记录该指纹，使后续
> Run 能验证输入身份。

> 用户回答：Q43 A

正文关联：10.2、10.4、Q37。
当前解释：DuckDB 扫描前后核对现有来源指纹；不一致时丢弃 staging、标记失败并
重跑；制品元数据记录来源指纹。

本轮发问检查：Q1–Q43 连续且唯一；Q1–Q37 已回答，Q38–Q43 待回答；算子迁移、
旧模块/数据库结构删除、测试和最终验收矩阵在本轮确定数据契约后继续展开。

### 第 9 轮合并回答 — 对应 Q38–Q43

接收日期：2026-09-19。

用户回答原文：

> Q38 A, Q39 A,Q40 A, Q41 A, Q42 A, Q43 A

解释范围：Q38–Q43 全部采纳各题推荐 A，无附加限制。

正文同步：10.2、10.4，以及 Q38–Q43 当前解释。

本轮检查：Q1–Q43 连续且唯一；Q1–Q43 均已回答；数据 Schema、发布与校验协议
已经收敛，新方向尚待算子迁移、删除范围和验收设计，完整 QA 附录仍位于文末。

### 第 10 轮 — 算子迁移、旧设施删除与切换验收（2026-09-19）

发问时前提：Q38–Q43 已全部采纳推荐。依赖审计确认，八个自研暂存/并发模块合计
约 2,247 行，并广泛耦合 Detect、Clip、Fitting、Detect 写入器及专用测试；独立的
`db/patch_ledger.py` 与 `004_patch_ledger.sql` 只用于显式选择的任务账本，未由生产
初始化器自动安装。`candidate_patches.py` 与 `dbscan_storage.py` 是算法分块/紧凑内存
结构，不属于线程池或磁盘暂存，但其调用方式需要随列式改造重新评估。

#### Q44 — DuckDB/Polars/Python 回退门槛

> 是否把 Q20 的优先级落实为强制评审顺序：每个算子先尝试 DuckDB SQL；只有无法
> 保持语义或基准明显更差时才使用 Polars LazyFrame；只有两者都不能表达时才按批次
> 构建最小 Python 对象并运行现有算法；每处回退都在代码注释/测试中记录理由，禁止
> 为调用旧函数而整表 `fetchall()`？选择：A. 是；B. 可由开发者自由选择三者；
> C. 只用 DuckDB 和 Python，不引入 Polars。

> 推荐 A。它使“DuckDB 作为主力”成为可检查的设计约束，同时保留必要的几何、聚类
> 与拓扑 Python 内核。

> 用户回答：Q44 A,在测试中记录理由

正文关联：10.2、10.4、Q20–Q21。
当前解释：强制按 DuckDB → Polars → 最小 Python 对象的顺序评审；每处回退理由必须
由测试记录，不只写代码注释；禁止整表 Python 物化以适配旧函数。

#### Q45 — 代码删除清单

> 是否删除 `clip_spool.py`、`cross_member_store.py`、`detect_workspace.py`、
> `writer_workspace.py`、`patch_spool.py`、`patch_admission.py`、
> `patch_coordinator.py`、`patch_pool.py`、`db/patch_ledger.py` 及所有仅验证这些实现的
> 测试；保留 `heartbeat.py`；`candidate_patches.py`、`dbscan_storage.py` 只有在列式
> 路径仍需要其算法语义时才保留并去除旧暂存耦合？选择：A. 是；B. 连
> `candidate_patches.py`、`dbscan_storage.py` 也无条件删除；C. 暂时保留旧模块作回退。

> 推荐 A。删除设施而不误删 DBSCAN 连通关系、边界覆盖和容量保护等业务算法语义；
> 最终未被新路径引用的算法辅助模块仍应删除。

> 用户回答：Q45 A

正文关联：10.1–10.4、Q19。
当前解释：删除九个旧设施模块及专用测试，保留心跳；算法辅助模块按新路径是否仍需
业务语义决定，最终无调用者则删除。

#### Q46 — 切换方式

> 实施时如何切换？A. 分阶段先建立 Parquet/DuckDB 新路径及差分测试，通过后在同一
> 变更系列删除旧路径；最终产品不保留双实现开关；B. 先立即删除旧路径，再从不可运行
> 状态重建；C. 长期保留新旧运行模式。

> 推荐 A。它仍实现最终彻底回撤，同时让每个阶段能以当前语义作 oracle，降低一次性
> 重写 7,000 余行耦合面的风险。

> 用户回答：Q46 A

正文关联：10.3–10.4。
当前解释：分阶段建立新路径及差分测试，通过后在同一变更系列删除旧路径；最终不保留
双实现开关。

#### Q47 — 独立任务账本数据库结构

> `004_patch_ledger.sql` 从未由生产初始化器自动安装。是否：A. 删除安装器、模板及
> 专用测试；文档提供对曾手工安装者的显式 `DROP TABLE` 顺序，但程序不自动连接未知
> Schema 删表；B. 保留一版清理 CLI 自动搜索并删除所有 Schema 中的账本表；
> C. 保留账本代码但停用。

> 推荐 A。它删除仓库内旧机制，同时避免程序越权扫描和删除用户自建 Schema；历史
> 数据不重要的环境可按明确 SQL 手动清理。

> 用户回答：Q47 A删除

正文关联：10.1、10.4、Q19、Q22。
当前解释：删除任务账本安装器、模板和专用测试；只在文档提供手工清理 SQL，不让程序
扫描或删除未知 Schema。

#### Q48 — 旧制品垃圾回收宽限期

> Q29 的默认宽限期设为多久？A. 7 天；B. 24 小时；C. 只配置、不提供默认值。

> 推荐 A。足以覆盖失败调查和短期回滚，同时由磁盘容量告警触发人工提前清理；当前
> 成功血缘引用的制品无论时间多久都不得被自动删除。

> 用户回答：Q48 7天

正文关联：10.2、Q29。
当前解释：默认宽限期为 7 天；当前成功血缘引用的制品不受宽限期影响，不自动删除。

#### Q49 — 语义差分标准

> 是否继续沿用 Q17：成员归属、crossing 配对和拓扑严格一致；业务身份映射后比较；
> 米制坐标绝对误差不超过 `1e-6 m`、经纬度不超过 `1e-9°`、其他浮点不超过
> `1e-12`，拒绝非有限值，微小坐标误差不能豁免拓扑变化？选择：A. 沿用；
> B. 全部浮点逐位一致；C. 重新放宽容差。

> 推荐 A。列式引擎可能改变合法的浮点归约顺序，但不能改变离散拓扑和业务身份。

> 用户回答：Q49 A 沿用

正文关联：第 8–9 节、Q17。
当前解释：沿用 Q17 的离散严格一致与浮点容差标准。

#### Q50 — 历史文档处理

> 旧实现的验证文档如何处理？A. 保留历史证据，在活跃任务卡和旧方案顶部标记“已被
> 2026-09-19 列式架构取代”，并更新 README/运行指南只描述新路径；B. 删除所有提及
> spool/进程池的历史文档；C. 不修改文档。

> 推荐 A。代码和活动配置彻底删除不等于抹掉决策历史；明确标记 superseded 可防止
> 后续误把旧方案当成当前要求。

> 用户回答：Q50 A保留但在顶文档顶部标记并引用新文档

正文关联：本文顶部、第 9–10 节及 `pipeline-streaming-implementation.md`。
当前解释：保留历史文档，在每份相关旧文档顶部标记已被列式架构取代并链接当前 Spec；
README 与活跃运行指南只描述新路径。

#### Q51 — 依赖版本策略

> DuckDB、Polars 及扩展如何固定版本？A. `pyproject.toml` 使用受控兼容范围，容器
> 构建锁定解析后的精确版本和扩展，升级必须重跑语义差分与 26M 基准；B. 永远使用
> 最新版；C. 只固定 DuckDB，Polars 不固定。

> 推荐 A。DuckDB/Polars 的流式和外存行为会随版本变化，不能让镜像重建静默改变
> 执行计划或结果。

> 用户回答：Q51 A

正文关联：10.2、10.4、Q24、Q30、Q37。
当前解释：项目依赖使用受控兼容范围，容器构建锁定实际 DuckDB、Polars 与扩展版本；
升级必须重跑语义差分和 26M 基准。

本轮发问检查：Q1–Q51 连续且唯一；Q1–Q43 已回答，Q44–Q51 待回答；配置字段、
分阶段实施顺序和完整验收命令将在本轮确定迁移政策后进入下一轮。

### 第 10 轮合并回答 — 对应 Q44–Q51

接收日期：2026-09-19。

用户回答原文：

> Q44 A,在测试中记录理由， Q45 A， Q46 A, Q47 A删除，Q48 7天， Q49 A 沿用， Q50 A保留但在顶文档顶部标记并引用新文档 ，Q51 A

解释范围：Q44–Q47、Q49–Q51 采纳 A；Q44 增加“在测试中记录理由”的强制要求；
Q47 强调删除；Q48 明确默认 7 天；Q50 强调旧文档顶部标记并引用新文档。

正文同步：10.1–10.4，以及 Q44–Q51 当前解释。

本轮检查：Q1–Q51 连续且唯一；Q1–Q51 均已回答；迁移与删除政策已经收敛，
运行数据流、失败恢复、清理入口和最终验收口径仍待确认，完整 QA 附录位于文末。

### 第 11 轮 — 运行数据流、失败恢复、配置与验收（2026-09-19）

发问时前提：Q44–Q51 已回答；算子选择、删除清单、切换方式、宽限期、语义标准、
文档和版本策略均已确定。以下问题决定首版的实际运行闭环。

#### Q52 — 阶段数据流

> 是否采用：Refine 从 PostgreSQL 权威源读取，经 DuckDB/必要回退计算，写 Refine
> PostgreSQL 结果和 Refine Parquet；Detect 以 Refine Parquet 为大表输入，只从
> PostgreSQL 读取 Run/配置等小元数据，写 Detect PostgreSQL 结果和 Detect Parquet；
> Clip 读取 Detect Parquet；Fitting 消费 Clip 的顺序输出并以 psycopg 写 PostgreSQL；
> Network 沿用紧凑结果路径？选择：A. 采用；B. Detect 仍从 PostgreSQL 点表读取；
> C. Clip 仍从 PostgreSQL 读取 Detect 点表。

> 推荐 A。这样 Parquet 真正成为阶段间列式数据层，避免“生成了 Parquet 但主链仍走
> Python/数据库行读取”的伪迁移。

> 用户回答：Q52 A

正文关联：10.2、Q20、Q26、Q33–Q40。
当前解释：采用 Refine/Detect 发布列式检查点、Detect/Clip 消费上游 Parquet、最终结果
由 psycopg 写 PostgreSQL 的阶段数据流。

#### Q53 — DuckDB/Polars 临时状态

> 是否让 DuckDB 每阶段使用临时内存 catalog，不把 `.duckdb` 文件作为持久状态；其
> 内置外存放入 `${DATA_ROOT}/duckdb/{run_id}`，Polars 临时/文件缓存环境在导入前指向
> `${DATA_ROOT}/polars/{run_id}`；成功或失败退出后清理，崩溃遗留由对账处理？选择：
> A. 是；B. 持久保存 DuckDB 数据库文件；C. 共用一个跨 Run 临时目录。

> 推荐 A。权威状态只有 PostgreSQL 和已登记 Parquet；不引入第三套可恢复数据库，
> Run 隔离也避免并发/崩溃残留互相污染。

> 用户回答：Q53 A

正文关联：10.2、10.4、Q24–Q25。
当前解释：DuckDB 使用阶段内临时 catalog，不持久化 `.duckdb` 权威状态；DuckDB 与
Polars 临时目录按 Run 隔离，正常退出清理，崩溃残留进入对账。

#### Q54 — 失败后的恢复粒度

> 移除 patch 账本后，阶段中途失败如何恢复？A. 将当前 Run 标为 `failed`，清理或对账
> staging/临时目录；以新 Run ID 从最近一个 completed 上游 Parquet 检查点重跑整个
> 失败阶段，不恢复阶段内部局部进度；B. 在同一 Run 内从单个文件继续；C. 重新引入
> patch 状态表。

> 推荐 A。它用 Refine/Detect 不可变检查点替代复杂的自研局部恢复；失败 Run 永不
> 改回 completed。

> 用户回答：Q54 A

正文关联：10.2、Q16、Q27–Q29、Q45–Q47。
当前解释：失败 Run 永久为 failed；新 Run 从最近 completed 上游 Parquet 检查点重跑
整个阶段，不保留阶段内局部续跑。

#### Q55 — 对账与垃圾回收入口

> 是否提供 Pipeline CLI 子命令进行 artifact reconcile/gc：默认 `--dry-run`，仅列出
> orphan、缺失文件和超过 7 天且不再被当前成功血缘引用的制品；只有显式 `--apply`
> 才删除文件并追加 `deleted` 事件；调度频率交给 Compose/外部计划任务，不新增后台
> 线程？选择：A. 采用；B. Pipeline 每次启动自动删除；C. 只写 SQL 手工维护。

> 推荐 A。删除可审计且默认安全，也不重新引入常驻适配器或线程。

> 用户回答：Q55 采用

正文关联：10.2、Q27–Q29、Q48。
当前解释：采用默认 dry-run、显式 apply 的制品对账/垃圾回收 CLI；外部调度，不新增
后台线程。

#### Q56 — Parquet Schema 演进

> 是否为每个逻辑表保存显式整数 `schema_version` 和规范 Schema 指纹；消费者只接受
> 已知版本，新增/改变列时产生新 Run 和新不可变数据集，不就地改写旧文件；兼容读取
> 只允许显式注册的版本适配器？选择：A. 采用；B. 始终按列名猜测兼容；C. 修改旧
> Parquet 追平最新 Schema。

> 推荐 A。它把“不允许修改”扩展到可验证的数据契约，防止库升级或列类型变化静默
> 影响计算。

> 用户回答：Q56 A

正文关联：10.2、Q27–Q28、Q40–Q42。
当前解释：采用整数 Schema 版本与规范指纹；拒绝未知版本，只允许显式适配器；变更
产生新 Run，不修改旧文件。

#### Q57 — 文件系统不可变性

> 最终 Parquet 发布后是否将文件/目录设为只读，应用只以只读方式打开；任何重算都写
> 新 Run 路径；删除只能经 Q55 的 GC 流程？选择：A. 是；B. 仅靠代码约定，不改权限；
> C. 允许修补单个 part 文件并更新校验和。

> 推荐 A。只读权限、不可变路径和登记校验共同约束误覆盖；删除目录不等同于修改文件，
> 仍由显式 GC 执行。

> 用户回答：Q57只读

正文关联：10.2、Q20、Q27–Q29。
当前解释：采纳 A；最终文件/目录只读，应用只读打开，重算写新 Run，删除仅经 GC。

#### Q58 — 配置归属

> 是否只用环境变量确定挂载根（`DATA_ROOT`、派生/可覆盖的 `PARQUET_ROOT`），而将
> DuckDB 内存、线程、临时盘上限、Polars 线程、文件/行组目标、GC 宽限期等非敏感
> 执行参数集中放入现有 `pipeline.toml [runtime]`，并把本次有效配置写入 Run？选择：
> A. 采用；B. 全部使用环境变量；C. 全部改为 CLI 必填参数。

> 推荐 A。路径挂载与容器部署一致，算法/执行参数仍保持单一配置来源和可复现记录。

> 用户回答：Q58 A

正文关联：10.2、10.4、Q14、Q25、Q30、Q51。
当前解释：挂载根使用环境变量，非敏感引擎/文件/GC 参数集中于
`pipeline.toml [runtime]`，每个 Run 记录有效配置。

#### Q59 — 26M 最终性能验收

> 是否定义为：精确 26,000,000 原始输入行；使用默认 2 GB 硬上限而非 3 GB 临时
> 重跑上限；`memory.swap.max=0`；Pipeline 父控制组 `memory.peak < 2,000,000,000` B
> 且无新增 OOM 事件；PostgreSQL 单独报告；从 Refine 开始至 Network 完成的墙钟时间
> < 3,600 秒；输出通过 Q49 语义差分？选择：A. 采用；B. 允许用 3 GB 重跑结果通过
> 2 GB 验收；C. 只验阶段时间，不验全链路。

> 推荐 A。3 GB 是异常数据的运营回退，不应被计作 2 GB 默认目标达标。

> 用户回答：Q59 A

正文关联：第 2、5–8 节、Q23、Q30–Q32、Q38、Q49。
当前解释：采用精确 26M、默认 2 GB 硬上限、无交换空间/新增 OOM、全链路小于一小时
及 Q49 语义差分的最终验收；3 GB 运营重跑不能用于通过默认验收。

#### Q60 — 验证闸门

> 是否按顺序要求：单元测试与 Schema/故障注入；真实 PostgreSQL + Parquet 集成测试；
> 小型固定样本逐表差分；现有 1M 基线差分与资源剖析；最后 26M 性能验收。任何一层
> 失败都不得删除该阶段旧实现或宣称切换完成；最终删除旧实现后再跑全套回归？选择：
> A. 采用；B. 直接以 26M 结果替代前置测试；C. 只跑单元与 1M。

> 推荐 A。Q46 的短期双路径只用于提供 oracle，必须在旧路径删除后的最终代码上再次
> 验证，避免差分测试通过但清理阶段破坏主链。

> 用户回答：Q60 A

正文关联：第 8–9 节、10.3、Q46、Q49、Q51。
当前解释：采用分层验证闸门；删除旧实现后的最终代码必须再次运行完整回归。

本轮发问检查：Q1–Q60 连续且唯一；Q1–Q51 已回答，Q52–Q60 待回答；本轮回答后
将汇总分阶段实施与回滚边界，并进入完整 Spec 最终确认。

### 第 11 轮合并回答 — 对应 Q52–Q60

接收日期：2026-09-19。

用户回答原文：

> Q52 A, Q53 A, Q54 A, Q55 采用 ，Q56 A, Q57只读 ,Q58 A, Q59 A, Q60 A,

解释范围：Q52–Q54、Q56、Q58–Q60 采纳 A；Q55“采用”采纳推荐方案；Q57“只读”
采纳 A 所述文件系统不可变规则。

正文同步：10.2–10.4，以及 Q52–Q60 当前解释。

本轮检查：Q1–Q60 连续且唯一；Q1–Q60 均已回答；运行闭环已经收敛。Q50 所称
“新文档”与当前复用旧 Spec 的文档归属仍需 Q61 明确，完整 QA 附录位于文末。

### 第 12 轮 — 活跃 Spec 的文档归属（2026-09-19）

发问时前提：Q1–Q60 均已回答，技术设计树已清空。Q50 要求旧文档顶部标记并引用
“新文档”，但本次此前按项目 hook 复用了已有 20M Spec 并在第 10 节记录新设计。

#### Q61 — 是否拆出新的列式架构主 Spec

> 是否新建 `docs/pipeline-columnar-architecture-spec.md` 作为当前活跃 Spec，将本次
> Q19–Q60 的完整原文 QA、有效决定、实施计划与验收标准整理进去；现有
> `pipeline-20m-memory-budget-spec.md` 保留 Q1–Q18 和历史实施记录，在顶部标记旧架构
> 已被新 Spec 取代并链接过去？选择：A. 是；B. 不新建，继续让旧 Spec 第 10 节作为
> 当前方案。

> 推荐 A。它准确落实 Q50 的“引用新文档”，让实施者不必穿过旧进程池/外存方案才能
> 找到当前要求，同时仍完整保留历史审计链。

> 用户回答：A 新建spec文档

正文关联：顶部状态、第 9–10 节、Q50。
当前解释：新建 `docs/pipeline-columnar-architecture-spec.md` 作为当前活跃 Spec；旧
20M Spec 保留历史并在顶部标记已取代、链接新文档。

本轮发问检查：Q1–Q61 连续且唯一；Q1–Q60 已回答，Q61 待回答；Q61 后将整理最终
共享理解并提出单独的完整设计确认，不把文档重组自动视为设计确认或实施授权。

### 第 12 轮合并回答 — 对应 Q61

接收日期：2026-09-19。

用户回答原文：

> A 新建spec文档

解释范围：采纳 Q61 的 A，新建列式架构主 Spec；旧 Spec 保留历史并标记已取代。

正文同步：本文顶部、10.3，以及新建的
`docs/pipeline-columnar-architecture-spec.md`。

本轮检查：Q1–Q61 连续且唯一；Q1–Q61 均已回答；技术设计树和文档归属已收敛，
下一题只做完整设计确认，不自动构成实施授权。

### 第 13 轮 — 最终共享理解确认（2026-09-19）

发问时前提：Q19–Q61 已回答，本文第 1–9 节已按全部有效回答整理；旧方案已在顶部
标记被本文取代。技术设计树与文档归属已无其他未决分支。

#### Q62 — 完整设计确认

> 是否确认本文第 1–9 节准确表达你的完整要求：删除自研磁盘暂存/外溢、计算线程
> 适配层、进程池和任务账本；PostgreSQL 为权威存储；Refine/Detect 发布不可变
> Parquet；DuckDB 优先、Polars 次之、最小 Python 对象最后；默认 2 GB、26M 全链路
> 小于一小时；保持前后端字段与业务语义兼容；按分层验证后彻底删除旧路径？

> 推荐确认本文为后续实施与验收依据。该确认只确认设计，不自动授权开始代码实施，
> 也不代表 26M 性能验收已经通过。

> 用户回答：确认

正文关联：第 1–10 节。
当前解释：用户明确确认本文第 1–9 节的完整设计。Spec 状态改为“已确认”；本回答
不授权开始代码实施，也不代表性能验收通过。

本轮发问检查：Q19–Q62 连续且唯一；Q19–Q61 已回答，Q62 待回答；正文、未决项、
设计确认、实施授权和验收确认的边界明确，完整 QA 附录仍位于文末。

### 第 13 轮合并回答 — 对应 Q62

接收日期：2026-09-19。

用户回答原文：

> 确认

解释范围：明确确认本文第 1–9 节的完整共享设计；不推定实施授权或验收确认。

正文同步：顶部状态、第 9–10 节及 Q62 当前解释。

最终检查：Q19–Q62 连续且唯一，均已回答；没有待决设计问题；完整 QA 附录仍位于
文末。设计已确认、实施未授权、验收未进行，三者分别记录。

### 第 14 轮 — 实施授权（2026-09-19）

发问时前提：Q62 已确认完整设计，Spec 状态为“已确认”；代码尚未按本文修改，性能
验收尚未进行。

#### Q63 — 是否开始分阶段实施

> 是否授权现在按本文第 8 节开始修改代码、数据库定义、容器依赖、配置、测试和相关
> 文档，并在各验证闸门通过后删除旧暂存/进程池/任务账本路径？选择：A. 开始实施；
> B. 仅保留已确认 Spec，暂不实施。

> 推荐 A。按已确认顺序分阶段实施并报告每阶段验证证据；不推送远端、不删除运行时
> `/data` 数据、不把尚未完成的 26M 基准宣称为通过。

> 用户回答：开始实施，分阶段实施，每次commit 小点，软限制700行，如果有大量重命名的commit,则不设置comit行数限制

正文关联：第 8、10 节。
当前解释：已授权按第 8 节开始完整实施并进行本地提交；每个提交的新增与删除合计
软限制为 700 行，大量纯重命名提交不受该限制。未授权推送远端或删除 `/data` 运行数据。

本轮发问检查：Q19–Q63 连续且唯一；Q19–Q62 已回答，Q63 待回答；设计状态保持
已确认，实施授权与验收确认仍独立。

### 第 14 轮合并回答 — 对应 Q63

接收日期：2026-09-19。

用户回答原文：

> 开始实施，分阶段实施，每次commit 小点，软限制700行，如果有大量重命名的commit,则不设置comit行数限制

解释范围：授权分阶段代码实施和本地提交；700 行按每次提交新增与删除合计作为软限制，
大量纯重命名提交例外。该授权不是验收确认。

正文同步：第 8、10 节及 Q63 当前解释。

### Q63 后续补充 — 子代理任务卡与主对话验收（2026-09-19）

关联原记录：第 14 轮 Q63 及其合并回答。

用户补充原文：

> 分阶段任务卡的形式，发给subagent（5.6 sol low)实施，现在这个对话来验收

当前解释：各实施阶段写入任务卡并交给 `gpt-5.6-sol`、`low` 推理子代理；当前主对话
负责审查、集成验证、验收证据和后续派发。所有代理共享工作树，写任务按依赖顺序派发，
避免并发 Git 提交互相污染；只读审查和独立验证可并行。

正文同步：第 8 节实施组织方式；具体卡片和提交证据记录在
`pipeline-columnar-implementation.md`。

### Q49/Q63 后续补充 — 投影数值基线与可重算数据（2026-09-22）

关联原记录：第 10 轮 Q49、第 14 轮 Q63 及其后续补充。

用户补充原文：

> 我有一个问题啊，你说这是第四点，C12严格差分未通过。坐标差对比的是哪两个坐标？同一个点在哪两个算法之间的坐标差？旧投影库环境又是什么？我在之后的工作里可能有一个原则，就是数据留存不重要，或者说对，数据留存不重要。 你直接把这个数据删了，重新跑就好了。至于你说的这个数据差的问题，我们以最新的投影坐标系计算的结果为准。

当前解释：C12 反例比较的是同一业务点在历史持久化结果与当前列式路径结果中的坐标；
“旧投影库环境”指历史运行未完整留存、因而无法精确恢复的 pyproj/PROJ 等依赖环境。
后续不追求恢复该旧环境的最低位浮点结果，以当前项目固定投影环境中同源输入、相同参数的
参考计算和列式路径为有效比较；“最新”不表示自动联网升级依赖或切换 EPSG。Q49 的原数值
容差、离散拓扑与性能目标不变，更换 baseline 不等于通过，也不能把失败改判通过。

用户授权可删除可重算旧结果后重跑这一原则，但尚未核定任何具体 Run、目录或数据删除范围；
本次不执行删除或重跑，也不能据此扩大到本次清理范围之外的原始源数据或其他项目。

正文同步：4.6、7.1、8、9 节；实施任务卡 C12h3 的待决基线政策同步更新。该补充不新增
Q 编号，也不构成 C12 或 C13 的验收确认。

最终检查：Q19–Q63 连续且唯一，均已回答；设计已确认、实施已授权、验收尚未进行；
完整 QA 附录仍位于文末。

### 第 15 轮 — Detect 网格取点接口（2026-09-24）

发问时前提：Q19–Q63 的列式架构基线已确认；当前 Detect 从 Refine Parquet 读取大表。
用户在本轮提出新的 `fetch_points_in_grid` 输入和输出；本轮只澄清设计，不实施。

用户本轮起始消息原文：

> [$grill-me](/home/reid/.codex/skills/skills/grill-me/SKILL.md) `fetch_points_in_grid`  的输入和输出，输入：grid\_index(col,row),grid\_AABB(A,A,B,B),refine\_id（继承）,spatial\_context)
> 输出，继承当前refine\_id的kept\_points union inject\_points,，返回AABB内的所有点

用户随后提供的技能内容原文：

> <skill>
> <name>grill-me</name>
> <path>/home/reid/.codex/skills/skills/grill-me/SKILL.md</path>
> ---
> name: grill-me
> description: A relentless interview to sharpen a plan or design.
> disable-model-invocation: true
> ---
>
> Call the Skill tool with "grilling".
>
> </skill>

#### Q64 — 网格取点的数据源

> `fetch_points_in_grid` 是否有意改为从 PostgreSQL 读取本次 Refine 的保留点与注入点，
> 从而使用现有点 GiST 索引？这会改变 Q52 已确认的“Detect 以 Refine Parquet 为大表输入”。
> 选择：A. 是，明确把这一步设为 PostgreSQL 读取的例外；B. 否，仍从 Refine Parquet
> 经 DuckDB 按网格读取。

> 推荐 A。前面提出的点索引方案需要 PostgreSQL 作为本步数据源；把例外写清楚，
> 再单独衡量索引筛选和跨库数据流的成本。

> 用户回答：Q64 A，用postgres union两个表

正文关联：第 3.1、4.1、9.1 节。
当前解释：本次变更拟将跨轨迹取点切换到 PostgreSQL；转向事件仍从 Parquet 读取。
Q52 是先前确认的基线，本次局部回答尚非完整变更设计确认。

#### Q65 — 边界相邻点的接口归属

> 你本次说函数返回“AABB 内的所有点”，此前又要求取点时自动补全边界点。
> 当前接口应采用哪种输出？选择：A. 仅返回 AABB 内的点，边界补点由后续函数负责；
> B. 返回 AABB 内的点，并附带每段连续命中点在同一子轨迹中的前驱、后继；
> C. 仅返回 AABB 内的点，允许边界上由缺少相邻点造成的额外漏检。

> 推荐 B。这样网格任务可直接构造从网格内点向两侧延伸的线段；两端都在网格外的
> 穿越线段仍按此前讨论允许漏检。输出需标记点是否属于核心 AABB。

> 用户回答：Q65 B ,类似于subtrack[n] in AABB，then return subtrack[n-1,n+1]，两侧闭区间；

正文关联：第 3、5、9.1 节。
当前解释：选 B；对每个命中点带同一子轨迹相邻点。“两侧闭区间”的具体 AABB
边界含义留待 Q69 澄清，不自行解释为左闭右开。

#### Q66 — 网格坐标和边界

> `grid_index(col,row)` 与 `grid_AABB(A,A,B,B)` 分别如何定义？选择：A. 两者都按
> `spatial_context.processing_srid` 的米制 XY 网格计算，AABB 为左闭右开范围；
> `grid_index` 是身份，传入 AABB 必须与它及网格边长一致；B. AABB 使用经纬度；
> C. 其他规则，请写明。

> 推荐 A。它对应现有 `x/y` 求交坐标，且可验证索引和范围是否指向同一网格。

> 用户回答：Q66 gird\_index是grid表的索引啊，grid表是本次cross\_points任务维护的tmp data，形如n\*m的矩阵，每个矩阵内存grid\_AABB，通过girds[col][row]直接获取AABB数据

正文关联：第 5、9.1 节。
当前解释：`grid_index` 是本次跨轨迹任务的临时网格矩阵索引，AABB 来自该位置；
这不是推荐 A 或 B 的完整选择，矩阵所在地、坐标系和索引/AABB 参数是否同时传递
仍待澄清。5 km 是此前提议，尚未确认为接口参数。

#### Q67 — 输出形式与顺序

> “返回所有点”的物理形式选哪种？A. 按列分批输出，并包含 `vehicle_id`、
> `subtrack_index`、精炼后序号、点身份、`x/y`、`heading` 等构段所需字段，
> 每个子轨迹内部顺序稳定；B. 返回完整 Python 点对象列表；C. 其他形式。

> 推荐 A。它延续当前列式输入目标，避免一个 5 km 网格一次性构造全部点对象；
> 后续构段仍可按车辆、子轨迹和序号恢复相邻关系。

> 用户回答：Q67 按列分批输出

正文关联：第 5、6、9.1 节。
当前解释：确定按列分批输出；具体列集合与批大小待后续细化。

### 第 15 轮合并回答 — 对应 Q64–Q67

接收日期：2026-09-24。

用户回答原文：

> 补充 detect我觉得可以有多个输入，比如turn\_event就用parquet，cross\_points就用postgres
> Q64 A，用postgres union两个表 ；Q65 B ,类似于subtrack[n] in AABB，then return subtrack[n-1,n+1]，两侧闭区间；
> Q66 gird\_index是grid表的索引啊，grid表是本次cross\_points任务维护的tmp data，形如n\*m的矩阵，每个矩阵内存grid\_AABB，通过girds[col][row]直接获取AABB数据
> Q67 按列分批输出

解释范围：Q64 选 A 且转向事件仍读 Parquet；Q65 选 B；Q66 修正问题前提，
说明网格索引是临时矩阵下标；Q67 选列式分批。未确认网格矩阵所在地、坐标与边界细节。

正文同步：第 3.1、9.1 节及 Q64–Q67 当前解释。

本轮检查：Q19–Q67 连续且唯一且均已回答；既有 Q19–Q63 原文和确认保留；
本次变更仍为草稿，完整 QA 附录仍位于文末。

### 第 16 轮 — 临时网格与混合数据源（2026-09-24）

发问时前提：Q64–Q67 已回答；网格是本次跨轨迹任务的 `n*m` 临时矩阵，
PostgreSQL 提供跨轨迹点，Parquet 继续提供转向事件，输出按列分批。

#### Q68 — 网格矩阵放在哪里

> 你说的 `grids[col][row]` 是哪种临时数据？选择：A. Pipeline 进程内的矩阵，
> 每格只保存 AABB 与任务状态，工作线程各自建立 PostgreSQL 连接；B. PostgreSQL
> 临时表中的网格记录；C. 其他，请说明可见范围和生命周期。

> 推荐 A。它对应直接按下标访问的心智模型；点数据仍按批次从 PostgreSQL 取，
> 网格矩阵不保存所有点。

> 用户回答：Q68 进程内就行，全都statu标记为done后就完成cross\_points任务了；

正文关联：第 5、6、9.1 节。
当前解释：进程内矩阵；全部网格状态为 `done` 才完成跨轨迹任务。工作线程连接方式
未由此回答确认。

#### Q69 — AABB 的坐标与闭区间

> Q65 的“两侧闭区间”是否指每格 AABB 在处理坐标 `x/y` 上为
> `[xmin,xmax] × [ymin,ymax]`，边界上的点可同时命中相邻网格？选择：A. 是；
> B. 使用经纬度闭区间；C. 指其他范围，请说明。

> 推荐 A。求交本身使用 Refine 的处理坐标；边界重复由后续候选去重处理。

> 用户回答：Q69 A，可以同时属于相邻网格 ；

正文关联：第 5、9.1 节。
当前解释：处理坐标 XY 的闭区间；边界点可同时命中相邻网格。

#### Q70 — 跨网格重复候选

> 闭区间和相邻点补全会让同一对线段在多个网格任务中出现。最终候选如何处理？
> 选择：A. 合并时按两条线段的稳定身份去重，每对最多留下一个候选；
> B. 保留重复候选，由后续聚类处理；C. 其他归属规则。

> 推荐 A。重复候选会改变后续聚类看到的事件数，去重可保留原来每对线段至多
> 一个候选的口径；具体外存方式留到实施设计。

> 用户回答：Q70 按线段身份去重 ；

正文关联：第 5、7、9.1 节。
当前解释：最终按一对线段的稳定身份去重；具体排序与编号仍待澄清。

#### Q71 — 两种输入的 Refine 一致性

> 转向事件读 Refine Parquet、跨轨迹点读 PostgreSQL 时，是否要求两者严格对应
> 同一次 `refine_id` 的同一份点数据？选择：A. 要求，若 PostgreSQL 来源点在
> Parquet 发布后变化，应检测并使 Detect 失败；B. 只要求 `refine_id` 相同，允许
> 来源点数据后续变化；C. 其他规则。

> 推荐 A。这样混合输入仍对应同一份 Refine 结果；当前 Parquet 导出已有源数据
> 指纹检查，但新 PostgreSQL 读取路径需要单独核对一致性边界。

> 用户回答：Q71 要求基于同一份结果，但是不要过度验证数据指纹，我们就简单的认为parquet和postgres本来就是同源的，来pipeline过程中两个幂等

正文关联：第 3.1、4.5、7、9.1 节。
当前解释：采用题目 C 的自定义规则：两者应来自同一 Refine 结果，但不增加逐点
指纹复核；基于已有来源关系和幂等处理。最低身份检查和源数据变更边界留到实施
细化，不把推荐 A 的变更检测当作用户决定。

### 第 16 轮合并回答 — 对应 Q68–Q71

接收日期：2026-09-24。

用户回答原文：

> Q68 进程内就行，全都statu标记为done后就完成cross\_points任务了；Q69 A，可以同时属于相邻网格 ；Q70 按线段身份去重 ； Q71 要求基于同一份结果，但是不要过度验证数据指纹，我们就简单的认为parquet和postgres本来就是同源的，来pipeline过程中两个幂等

解释范围：Q68 选择进程内矩阵并定义全部 `done` 为完成；Q69 选 A；Q70 要求
线段身份去重；Q71 明确拒绝额外数据指纹复核，不采纳推荐 A 的检测失败要求。

正文同步：第 9.1 节及 Q68–Q71 当前解释。

本轮检查：Q19–Q71 连续且唯一且均已回答；正文与最新回答一致；本次变更
仍未最终确认，QA 附录仍位于文末。

### 第 17 轮 — 网格调度与结果顺序（2026-09-24）

发问时前提：Q64–Q71 已回答；进程内网格矩阵维护任务状态，PostgreSQL 提供跨轨迹
点，处理坐标 XY 的闭区间允许重复命中，最终按线段身份去重。

#### Q72 — 网格覆盖范围与边长

> 进程内 `n*m` 网格如何生成？选择：A. 使用当前 Detect 已读取的 Refine Parquet
> 全体点 XY 范围，固定初始边长 5 km；B. 从 PostgreSQL 重新统计本次 Refine 点的
> XY 范围，初始边长 5 km；C. 外部传入固定区域或其他边长。边长是否要配置化？

> 推荐 A，边长作为可配置的算法参数，初值 5 km。它复用当前 Detect 已取得的
> `refine_reader.extent()`，避免为建网格再读一次 PostgreSQL 全量点；本方案允许
> 边界漏检，因此边长变化可能改变候选结果，不能当成纯执行参数。

> 用户回答：全部按照推荐

正文关联：第 3、5、6、9.1 节。
当前解释：采纳推荐 A：复用 Refine Parquet XY 范围建网格；5 km 初值，边长
配置化且属于算法语义参数。

#### Q73 — 网格任务的并发连接

> 你此前提出 4/8 个线程处理网格。选择：A. 线程数可配置，默认 4、最多 8，
> 每个工作线程持有独立 PostgreSQL 连接并依次领取网格；B. 固定 8 个线程；
> C. 先串行验证一个网格，再决定并发方案。

> 推荐 A。并发连接与任务状态由进程内调度者管理；实际线程数仍需按 PostgreSQL
> 查询压力和内存实测调整。

> 用户回答：全部按照推荐

正文关联：第 5、6、8、9.1 节。
当前解释：采纳推荐 A：仅本跨轨迹任务可配置线程数，默认 4、最多 8，
每线程独立 PostgreSQL 连接；不抽象为通用并行框架。

#### Q74 — 失败网格与任务完成

> 若某格查询或计算失败，任务状态如何推进？选择：A. 状态为
> `todo/running/done/failed`，失败最多重试 2 次；仍失败则 Detect 失败，只有
> 所有格为 `done` 才完成跨轨迹任务；B. 失败格可跳过并继续发布结果；
> C. 其他重试与失败规则。

> 推荐 A。它落实 Q68 的全部 `done` 完成条件，并避免无限重试或漏报失败。

> 用户回答：全部按照推荐

正文关联：第 3.2、6、7、9.1 节。
当前解释：采纳推荐 A：四种状态、一般可恢复失败最多重试 2 次，仍失败则
Detect 失败；全部格为 `done` 才完成本跨轨迹任务。Q77 后续明确内存超预算
属于立即终止错误，不适用这两次重试。

#### Q75 — 并行合并后的候选顺序

> 去重后是否需要恢复当前跨轨迹候选的全局顺序和连续
> `cross_track_calculate_index`？选择：A. 保持现有车辆/线段顺序与连续编号；
> B. 只保证新的确定性网格顺序与连续编号，允许与现有结果顺序不同；
> C. 顺序不要求稳定。

> 推荐 A。当前候选关系校验要求编号与存储序号一致，原始路径按车辆/线段排序；
> 若放宽为 B，应把编号变化列为有意语义变更并重新核对下游。

> 用户回答：全部按照推荐

正文关联：第 5、7、9.1 节。
当前解释：采纳推荐 A：去重后按车辆/线段的确定性全局顺序排列并连续编号。
Q76 后续更正意味着不要求候选集合、坐标或编号逐项对应旧算法结果。

### 第 17 轮合并回答 — 对应 Q72–Q75

接收日期：2026-09-24。

用户回答原文：

> 全部按照推荐   注意一点：写进程的时候简单点，我要最小实现，不要过度扩大框架化，我不需要一个很宏大的dask或者threadpool框架用来适配所有任务，我只想在这里单点执行该任务

解释范围：“全部按照推荐”共同适用于 Q72–Q75。补充约束：本次仅做跨轨迹任务的
最小局部实现，不引入 Dask 或适配其他任务的通用线程池框架。“写进程”按本条
上下文记录为实施简化要求；Q73 明确采纳的是工作线程方案，不自行改为多进程。

正文同步：第 9.1 节及 Q72–Q75 当前解释。

本轮检查：Q19–Q75 连续且唯一且均已回答；正文与最新回答一致；本次变更
仍未最终确认，QA 附录仍位于文末。

### 第 18 轮 — 近似结果与单格容量（2026-09-24）

发问时前提：Q64–Q75 已回答；跨轨迹任务采用局部线程调度，允许部分边界线段
漏检，最终按线段身份去重并恢复原有顺序。

#### Q76 — 允许漏检后的验收对象

> 既然接受少量跨格线段漏检，新路径与当前路径的比较应以什么为准？选择：
> A. 允许跨轨迹候选数量和身份变化，但要求最终候选交叉口及完整交叉口的数量、
> 位置在明确容差内；B. 只看几处地图样例，无整体结果比较；C. 仍要求全部跨轨迹
> 候选严格一致。

> 推荐 A。它直接检验你提出的“候选少一点、多一点不影响交叉口聚类”假设；
> 具体数量与位置容差待选择 A 后另定，不把旧路径严格差分自动套到本变更。

> 用户回答：Q76 哪来的容差？你在和谁对比啊？我反复强调现在是开发dev过程，不要求历史数据兼容性或者能够和当前算法结果对应，当前优先策略，那就是认为除了trajectory\_points和day01这种数据源表，其他中途产生的历史数据无权威性

正文关联：第 2、7、9.1 节。
当前解释：用户拒绝本题 A–C 的新旧输出对照前提。本次新算法不以历史中间结果
或当前旧算法输出为基线，不设置与之比较的数量/位置容差；开发阶段以
`trajectory_points`、`day01` 等源数据表为权威来源。新算法自身的功能验收
留待 Q78，不把旧结果兼容性重新引入。

#### Q77 — 单个 5 km 网格超预算

> 某个网格的点或线段对过多，按列分批后仍无法在运行内存预算内计算时，
> 这次最小实现如何处理？选择：A. 报出网格索引和规模并使 Detect 失败，
> 暂不实现网格递归细分；B. 在本任务内继续拆小该网格；C. 超预算格跳过。

> 推荐 A。保持局部调度代码简单且失败可见；若实测出现单格超预算，再针对
> 具体网格分布设计拆分，不先建通用空间任务框架。

> 用户回答：q77 如果内存超预算，明确返回error并不再重跑

正文关联：第 3.2、6、7、9.1 节。
当前解释：单格内存超预算应明确返回错误，不自动重试该格或自动重跑 Detect；
Q74 的最多两次重试仅适用于其他可恢复失败。

### 第 18 轮合并回答 — 对应 Q76–Q77

接收日期：2026-09-24。

用户回答原文：

> Q76 哪来的容差？你在和谁对比啊？我反复强调现在是开发dev过程，不要求历史数据兼容性或者能够和当前算法结果对应，当前优先策略，那就是认为除了trajectory\_points和day01这种数据源表，其他中途产生的历史数据无权威性
> q77 如果内存超预算，明确返回error并不再重跑

解释范围：Q76 否定了问题中与旧算法按容差对照的前提；本次仅以源数据为
权威，不要求新旧输出对应。Q77 确认内存超预算立即报错且不自动重跑，覆盖
Q74 对该类失败的重试解释；其余可恢复失败仍按 Q74。

正文同步：第 2.1、7.1、9.1 节及 Q74–Q77 当前解释。

本轮检查：Q19–Q77 连续且唯一且均已回答；正文与最新更正一致；本次变更
仍未最终确认，QA 附录仍位于文末。

### 第 19 轮 — 新路径自身的功能验收（2026-09-24）

发问时前提：Q76 已明确拒绝与历史中间结果或旧算法输出对照；Q77 已确认单格
内存超预算立即报错且不自动重跑。

#### Q78 — 只以源数据验证新算法

> 本次新跨轨迹路径不比较旧输出，应采用哪组独立功能验收？选择：A. 用从权威
> 源数据构造的少量已知交叉、无交叉、边界与重复网格用例，验证点选取、
> 前驱/后继、线段对去重和交点；再用一次开发环境完整 Pipeline 运行确认结果
> 可供下游聚类与持久化使用，不设旧结果数量/位置阈值；B. 只要求开发环境
> Pipeline 跑完，不加几何用例；C. 其他规则。

> 推荐 A。它检验本次实现自身的几何和数据流行为，不依赖历史候选、簇或其他
> 中间结果的权威性；测试范围限定为该任务和必要的主链接口。

> 用户回答：A 用少量源数据构造一下，再跑pipeline冒烟测试（规定一下以后项目中的冒烟测试就是/data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline-roadnet-test.toml
> 全量测试就是/data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline.toml）

正文关联：第 2、7、8、9.1 节。
当前解释：选择 A；基于少量源数据构造用例，随后运行 Pipeline 冒烟测试。
后续项目中“冒烟测试”固定指向 `services/pipeline/config/pipeline-roadnet-test.toml`，
“全量测试”固定指向 `services/pipeline/config/pipeline.toml`；不把配置映射
误记为已完成的测试结果。

### 第 19 轮合并回答 — 对应 Q78

接收日期：2026-09-24。

用户回答原文：

> A 用少量源数据构造一下，再跑pipeline冒烟测试（规定一下以后项目中的冒烟测试就是/data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline-roadnet-test.toml
> 全量测试就是/data/workspace/Multimodal-GEO-roadnet/services/pipeline/config/pipeline.toml）

解释范围：选 A；并明确后续项目的冒烟测试、全量测试各自使用的配置文件。
未授权在访谈中立即执行 Pipeline 或修改算法实现。

正文同步：第 7.1、8、9.1 节及 Q78 当前解释；项目级约定另同步 `AGENTS.md`。

本轮检查：Q19–Q78 连续且唯一且均已回答；正文与最新回答一致；本次变更
尚待完整设计确认，QA 附录仍位于文末。

### 第 20 轮 — 本次局部设计确认（2026-09-24）

发问时前提：Q64–Q78 已回答；第 3.1、5.3、5.5、6.1、7.1、8、9.1 节已按
新输入路径、网格规则、最小线程调度、结果口径和测试配置同步。没有未决技术分支。

#### Q79 — Detect 网格跨轨迹方案的共享理解

> 是否确认本次局部设计：转向事件继续读 Refine Parquet；跨轨迹任务从
> PostgreSQL 按同一 `refine_id` 读取保留点与注入点；进程内 5 km 可配置网格
> 保存闭区间 XY AABB 与任务状态；命中点附带同一子轨迹前驱/后继，按列分批
> 输出；本任务最多 8 个线程、默认 4、各自数据库连接；跨格结果按线段身份去重，
> 按车辆/线段顺序连续编号；一般失败最多重试 2 次，内存超预算立即报错不重试；
> 允许两端均在网格外的线段漏检；只以源数据构造用例并使用指定配置做 Pipeline
> 冒烟测试，不比较历史中间结果或旧算法输出；实现仅限这个任务，不建通用框架，
> 且一次讨论和修改一个函数？
> 选择：A. 确认；B. 尚需更正，请指出条目。

> 推荐 A。它汇总 Q64–Q78 的有效回答；本题仅确认设计，不代表已实施、
> 测试通过或授权扩大到其他 Pipeline 算子。

> 用户回答：确认并让subagent(gpt6 sol low)实施

正文关联：第 3、5–10 节。
当前解释：用户确认 Q64–Q78 汇总的局部设计，并明确授权 `gpt-6-sol`、`low`
子代理实施；按逐函数协作，不扩展为通用并行框架。设计确认与实施授权均不表示
代码已完成或测试通过。

### 第 20 轮回答 — 对应 Q79

接收日期：2026-09-24。

用户回答原文：

> 确认并让subagent(gpt6 sol low)实施

解释范围：确认本次局部设计并授权指定模型与推理力度的子代理实施；
不扩大到其他 Pipeline 算子，也不构成实现完成或验收确认。

正文同步：顶部状态、第 5.5、9.1、10 节及 Q79 当前解释。

最终检查：Q19–Q79 连续且唯一，均已回答；本次局部设计已确认、逐函数实施
已授权、实现与验收尚未完成；QA 附录仍位于文末。

### 第 21 轮 — UTM 索引、比较口径与整数行键（2026-09-25）

发问时前提：Q79 已确认此前网格方案；本轮要求新增数据库索引、对比查询，
并将两张结果表的来源行键改为 BIGINT。

#### Q80 — 两张表的行键类型

> `source_row_key` 目前支持整数、UUID 和文本来源表。你要把通用 schema 的
> `kept_trajectory_points` 与 `excluded_points` 都收窄为 BIGINT，并同步迁移
> 现有列及相关查询吗？选择：A. 保留 TEXT，本次只做索引对比；
> B. 两张表改 BIGINT，来源限整数主键；C. 仅 roadnet_test 改 BIGINT，
> 通用 schema 保留 TEXT。
>
> 推荐 A：先保留 TEXT，把类型改造与本次索引性能对比分开；若明确只支持整数
> 来源，则按 BIGINT 全链路改造。
>
> 用户回答：两张表改 BIGINT，来源限整数主键

正文关联：第 9.3 节。当前解释：选择 B；通用 schema、现有数据库迁移和相关
读写查询均进入设计范围，不把仅改新建表 DDL 当作现有库迁移。

#### Q81 — 性能比较范围

> UTM 方案是直接执行完整的网格加邻点查询，还是只替换当前两段式方案中
> 生成临时 hits 表的空间筛选？选择：A. 完整查询对比，分阶段记录；
> B. 只比较网格 hits 筛选；C. 完整查询和 hits 筛选都比较。
>
> 推荐 A：比较完整端到端耗时，并分别记录两段式的建表与游标阶段、
> 每个计划节点的 Actual Loops、缓冲区读写和超时。
>
> 用户回答：UTM方案就是一段直出结果，不走临时hits表了，申明下，方案1、用utm索引  WHERE ST_Intersects(
>       ST_Transform(s.geom, 32649),
>       ST_MakeEnvelope(xmin, ymin, xmax, ymax, 32649)
>   )直接筛出day01源表里的点，接着去匹配kept_trajectory_points；方案2、先hits临时表，在从临时表里去读

正文关联：第 9.3 节。当前解释：方案 1 一条查询直出；方案 2 保持两段式。

#### Q82 — v7 旧数据的迁移失败策略

> 把两张表的 `source_row_key` 原地转为 BIGINT 前，应检查所有值及关联来源表。
> 若发现非整数或超出 BIGINT 范围的值，选择：A. 预检失败即停止，不删数据；
> B. 清理不兼容行后继续迁移；C. 仅改新库 DDL，旧库暂不迁移。
>
> 推荐 A：停止并报告具体数量，不自动删数据。
>
> 用户回答：数据全删了，我都不需要

正文关联：第 9.3 节。当前解释：允许删除数据，但未明确限于两张目标表、
关联 Run，还是整个 roadnet_test；删除范围由 Q84 确定。

本轮检查：Q80–Q82 已回答；Q82 的删除范围待澄清；未进行最终设计确认或数据库实施。

#### Q83 — 两种方案的输出一致性

> 现有两段式查询除网格命中点外，还为每个点查询前驱和后继。
> 方案 1 在关联 kept_trajectory_points 后，是否也要查询相同的前驱、
> 后继和注入点，并保持相同排序？选择：A. 两方案输出完全一致；
> B. 只比较源表与 kept 匹配的 hits；C. 分别报告 hits 和完整输出两层结果。
>
> 推荐 A：保持输出完全一致，才能比较整条查询的扫描次数和耗时。
>
> 用户回答：建议：两方案输出完全一致

正文关联：第 9.3 节。当前解释：选择 A；比较前须核对行数和稳定身份。

#### Q84 — 旧数据删除范围

> 你说“数据全删了”，是指旧结果数据已经被你清空，还是要我现在执行删除？
> 这次对比仍需要 day01 和同一 Refine Run 的 kept_trajectory_points 数据。
> 选择：A. 已清空；先核实，不再删除；B. 清空两张结果表，再重建基准数据；
> C. 清空整个 roadnet_test，再重建基准数据。
>
> 推荐 A：先核实现状；若仍有旧数据，再按指定范围清理，并确保基准数据可用。
>
> 用户回答：清除kept_points和后续产物，保留roadnet_test.day01(注意一下，roadnet_test.trajectory里有某个测试用的几个点，是之前agent自己放进去的，你找到对应的test_*,至于后续怎么改，之后再说)和roadnet.trajectory_points

正文关联：第 9.3 节。当前解释：清理保留点与下游产物；保留两处指定
源表。roadnet_test.trajectory_points 中 8 个 smoke 测试点的处置以后再定；
清理范围和数据库由 Q86 继续澄清。

#### Q85 — 数据库执行入口

> 本轮要对 roadnet_test 真正建索引并运行 EXPLAIN ANALYZE。当前执行环境没有
> 可用的 Pipeline Python 虚拟环境，也无法访问 Docker socket，暂时连不上
> 数据库。请提供可用的执行入口，无需在聊天中发送密码。
> 选择：A. 提供可用容器或主机命令；B. 数据库稍后开放，先完成代码和脚本；
> C. 只准备脚本，用户自行执行数据库操作。
>
> 推荐 A：提供可用执行入口，完成真实数据库对比。
>
> 用户回答：提供可用容器或主机命令，但是这你直接从docker compose dev里找就好了呗，还要我提供吗？

正文关联：第 9.3 节。当前解释：已从 Compose 开发栈找到运行中的 Pipeline
容器，并成功只读连接 roadnet_test；不再要求用户提供连接入口。真实性能指标尚未测得。

本轮检查：Q80–Q83 已回答；Q84–Q85 待回答；没有执行数据库变更。

#### Q86 — 清理数据库范围

> 本次索引和对比的目标是 roadnet_test；你同时要求保留
> roadnet.trajectory_points。选择：A. 只清理 roadnet_test 派生数据；
> B. 两个数据库的派生数据都清理；C. 只清理指定 Refine Run 的派生数据。
>
> 推荐 A：只清理 roadnet_test 的 Refine 及后续 Run 数据，保留该库的
> day01 与 trajectory_points；roadnet 完全不动。
>
> 用户回答：两个数据库的派生数据都清理

正文关联：第 9.3 节。当前解释：roadnet_test 与 roadnet 的派生数据都进入清理范围；
保留 roadnet_test.day01、roadnet_test.trajectory_points 和 roadnet.trajectory_points。

#### Q87 — Parquet 文件范围

> “后续产物”是否包括已登记的 Refine/Detect Parquet 文件？
> 选择：A. 清理数据库派生数据及对应 Parquet；B. 只清理数据库派生数据；
> C. 连同所有 Pipeline 运行文件一起清理。
>
> 推荐 A：清理对应 Run 的数据库记录和专属 Parquet 目录，保留 day01、
> 两库 trajectory_points、日志和未登记的其他文件；清理前列出实际目录。
>
> 用户回答：建议：清理数据库派生数据及对应 Parquet

正文关联：第 9.3 节。当前解释：选择 A；只清理两库登记的 Refine/Detect
产物专属目录及派生数据库记录，保留源表、日志和未登记文件。

#### Q88 — 比较与清理的顺序

> 清理会移除性能对比所需的 kept 点。选择：A. 先用现有 roadnet_test
> Refine Run 完成两方案同数据对比，保存计划和报告，再清理两库派生数据并
> 改行键类型；B. 先清理改类型，重跑 Refine 后对比；C. 只准备对比脚本，
> 先清理和改类型。
>
> 推荐 A：先对比，再清理和改类型，避免为本次对比额外重跑 Refine。
>
> 用户回答：待回答

正文关联：第 9.3 节。当前解释：待回答。

本轮检查：Q80–Q87 已回答；Q88 待回答；尚未修改数据库。

### 第 22 轮 — 本次数据库对比与清理的共享理解（2026-09-25）

发问时前提：Q80–Q88 已回答。两个数据库和九个已登记的 Parquet 目录已只读盘点；
roadnet_test 的八个 smoke 点尚未找到对应 test_* 文件，保持原状。

#### Q89 — 完整执行范围确认

> 是否确认按以下顺序执行：
> 1. 仅在 roadnet_test.public.day01 建立 EPSG:32649 的 ST_Transform
>    表达式 GiST 索引。
> 2. 对原定八个网格（包含重复目标），比较一条 SQL 直出与当前 hits 临时表加
>    游标两段式；两方案返回完全相同的命中点、前后邻点、注入点和排序。
>    保存完整 EXPLAIN、扫描循环/行数、分阶段及总耗时，并核对结果一致性。
> 3. 对比完成后，清理 roadnet_test 和 roadnet 的 Refine 及下游数据库数据和
>    已登记的专属 Refine/Detect Parquet 目录；保留
>    roadnet_test.day01、两个数据库的 trajectory_points、日志与未登记文件。
>    roadnet_test.trajectory_points 的八个 smoke 点暂不更改。
> 4. 将 kept_trajectory_points 和 excluded_points 的 source_row_key 改为
>    BIGINT，通用来源表契约限整数主键；同步 schema 版本、SQL、读写及测试。
>    不把只改 CREATE TABLE IF NOT EXISTS 当作现有数据库迁移。
> 选择：A. 确认；B. 请指出需要更正的条目。
>
> 推荐 A：按此范围实施；若性能对比未取得可核对的结果，先保留基准数据并报告，
> 不先行清理。

> 用户回答：建议：确认，按此范围实施

正文关联：第 9.3、10 节。当前解释：确认并授权按 Q89 的四步顺序实施；
若性能对比未取得可核对的结果，先保留基准数据。

本轮检查：Q80–Q89 已回答；设计已确认并授权实施，尚待实际执行与验收；
QA 附录仍位于文末。

### Q89 实施与验收结果（2026-09-25）

Q89 四步已完成：八个网格的一段直出与两段式查询均返回完全相同的有序结果；
八个直出 EXPLAIN 计划都实际使用 UTM GiST 索引。派生数据库数据和九个登记的
Refine/Detect Parquet 目录已清理，三个保护源表行数保持不变。两个数据库已从
schema v7 迁移到 v8，kept_trajectory_points.source_row_key 与
excluded_points.source_row_key 均为 BIGINT。

性能计划、耗时、行数与循环、清理清单、迁移状态和测试结果记录在本机
`services/pipeline/generated/cross-track-explain/` 下的 `q89-results.md`、
`grid_compare_summary.json` 和 `cleanup_manifest.json`。该 `generated/` 路径由
`.gitignore` 忽略；这些生成证据只保存在本地，不纳入 Git。

迁移预检保留旧字符串身份：只接受规范十进制行键，并拒绝仍引用 UUID、TEXT
等非整数 id 来源表的保留 Refine Runs。验证结果为 Pipeline smoke 1,141 passed、
1 skipped；make test 中 Backend 83 passed、Pipeline 1,075 passed、67 skipped，
迁移 PostgreSQL 集成测试 8 passed。
