# Pipeline 列式存储与计算架构规格说明（specification，Spec）

术语遵循[项目术语与写作规范](terminology.md)，不在本文复制通用术语表。

状态：Q99 列批重构已实施并通过功能测试；26M 性能验收未通过；Q100–Q107 与后续审核方案已确认，历史实施授权保留；本轮性能优化 Q108–Q110 为未回答的讨论草稿，无新增实施授权
创建日期：2026-09-19
更新日期：2026-09-30
访谈进度：历史 QA 保留；Q99 已实施；Q100–Q107 及审核1/3/4和索引2已确认。Q108–Q110 待回答，当前仅调查与讨论。原5文件预算变更已在历史提交 `3c777b6`。

> 本文是当前活跃设计。旧的[2,000 万行内存预算与验收标准 Spec](pipeline-20m-memory-budget-spec.md)
> 保留容量审计、旧进程池/磁盘暂存方案及历史实施证据，但已被本文取代。
> 用户已在 Q62 确认完整设计，并在 Q63 授权分阶段实施和本地提交。设计确认与实施
> 授权均不代表 26M 性能验收已经通过。
> 2026-09-24 的 Detect 网格提取局部方案已获 Q79 确认，并在同一回答中授权由
> `gpt-6-sol`、`low` 子代理逐函数实施。下文原有架构保留为历史基线；本次有效
> 更正见 Q64–Q79。该局部变更的实现和测试记录见下文，不能据此推定 26M
> 输入的内存和时间验收已通过。
> Q100–Q107 本轮 grid 超预算处理的权威实施正文见下文
> [“Q100–Q107 现行实施计划”](#q100q107-现行实施计划2026-09-28已获恢复授权)；
> 第 6.1、7.3 节与历史 QA 中较早的 failfast/待授权表述均以该节和本轮实际验证记录为准。
> 2026-09-29 早先 4,000,000,000 B 父控制组硬上限与 swap 0 对应已失败的
> 26M 历史实测，不再是现行资源边界。开发人员 @reid 于 2026-09-29 22:34
> （Asia/Shanghai）要求不设置 Pipeline 容器硬内存上限，只记录 6,000,000,000 B
> 观测阈值；超过时写 ERROR，作业继续。Turn/Cross/Grid 独立 DuckDB 实例各用
> 4GB memory_limit，其他实例保留公共 1GB，grid 工作线程仍为 4。现行条件见
> 第 6.1、7.2、7.3 节；旧 2GB 和 4GB 运行数据只作历史失败证据。

2026-09-30 本轮补充：跨轨迹几何夹角与方向分桶修正已获用户实施授权；用户随后调用
grill-me 讨论列式计算。Q113 已选择保留 NumPy 主路径并同步修正 DuckDB，本轮范围已确认，不撤销上一条修正授权，
也不改变上述 Clip/Fitting 独立工作的状态。方案见第 4.7 节，完整原文见文末。

## 1. 背景与问题

现有 Pipeline 为控制大规模数据的内存占用，自行实现了磁盘暂存、磁盘工作区、
写入器工作区、任务账本、资源预留、协调器和进程池。这些实现分散在 Detect、Clip、
Fitting 与持久化路径中，维护面大，并使业务算法、外存生命周期和并发恢复紧密耦合。

本次改向删除这些自研设施，以 PostgreSQL 为权威存储，以不可变 Parquet 数据集作为
Refine、Detect 之后的列式检查点，以 DuckDB 为主计算引擎，以 Polars 和最小 Python
对象作为受控回退。无法列式化的 Python 算法首版保持单进程、单计算线程；线程、进程
或 Dask 的进一步设计不属于本次范围。

2026-09-28 新的 day01 性能调查与未决 Q108–Q110 见文末“性能调查与新一轮设计问题”；该讨论不改变本节历史架构目标，也未获新增实施授权。

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

本轮 Clip/Fitting 有界批量读写、拓扑重复取数和数据变化检测仅为候选，证据及限制见文末性能调查；Q108–Q110 未回答前不替换上述职责和已确认的不可变发布规则。

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

### 4.7 跨轨迹几何夹角的列式计算（2026-09-30，Q113 已确认）

已确认语义及实施授权：两条线段使用投影端点向量计算夹角，方向分桶也复用该几何方向，
不能继续用起点 `heading` 代替线段方向。授权原文保存在文末本轮补充记录。

问题证据：固定 grid(0,0) 导出中 segment_id=86321 的两端 `heading` 都为 0°，
端点向量的北向顺时针方位角约为 257.94°。其 2,586 条已导出交点记录全部满足源
`heading` 差的 80–100° 条件，但按端点向量计算，满足 80–100° 的记录为 0。
两线段较小夹角为 1.33–19.57°。此处仅审计原结果，没有重新导出。

2026-09-30 修改前代码事实：网格主路径已使用 NumPy 坐标列和候选 ID 对数组；
方向分桶和精确内核仍读取源 `heading`。已有 DuckDB 配对路径也有源 `heading`
夹角的前置筛选，修正内核时必须同步该筛选，以免在内核之前丢失几何合格配对。
定位： /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/detect_grid_crossings.py 、
 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/build_patches.py 、
 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/_geometry.py 、
 /data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/results/read_parquet.py 。

现行方案（Q113 已确认，结合此前实施授权执行）：

1. 每个网格或线段块按列计算 `dx=x_end-x_start`、`dy=y_end-y_start`，批量计算
   `bearing=(degrees(atan2(dx,dy))+360)%360`，供方向分桶、候选夹角和精确判定复用。
2. 两个方向列按候选 ID 配对，批量求最短方向差，保留闭区间 80–100°；沿用配置阈值。
   不构造全网格 N×N 矩阵，不增加逐线段 Python 对象。
3. 内存网格、长线段回退、外存线段块和已有 DuckDB 路径使用一致的几何角度定义。
   源 `heading` 继续作为来源属性；本轮不扩大到重定义原有直线条件或速度条件。
4. 以 heading 与几何不一致的拒绝/保留样例、角度边界、零长度段及各路径一致性测试验收。
   使用固定 grid00 输入进行只读复测，不覆盖已有 CSV；运行仓库要求的 `make test`。
   功能测试与实际 Pipeline 冒烟/全量运行分别记录，不把功能测试称为全量 Pipeline 验收。

设计树：几何夹角及分桶语义已确定 → Q113 列式范围（NumPy 主路径复用 / 主路径迁移
DuckDB）→ 若选择整体迁移，再讨论空间配对执行计划和同条件性能验收。
Q113 已选择 NumPy 主路径复用，不迁移主计算引擎；已实施并通过功能回归及固定 grid00
几何角度校验。验证记录见 [端点向量夹角修正验证](validation/cross-track-endpoint-angle-20260930.md) 。
本次没有重新导出 CSV，也不代表隔离性能验收通过。

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

### 5.6 点对象与模型粒度重构审计（2026-09-26，已确认、实施已授权）

本次复用当前列式架构 Spec，补充内存数据表示重构，不新建同主题 Spec。
用户已提出：Segment 是最小计算单位；SubTrack 直接存整条轨迹的向量，
不由 Segment 对象组成；删除 TrajectoryPoint、DerivedPoint 等逐点类，
并审计本节模型文件的全部类。该方向已明确，完整方案已由 Q99 确认并授权实施；本记录不表示运行代码已完成修改。
原有已确认方案与实施记录继续保留，本节不是对历史性能验收的追认。

#### 5.6.1 Q99 实施前生产数据流与分配证据（2026-09-26 基线）

审计范围覆盖 Parser、Refine、Detect、Clip、Fitting、Network、持久化、Pipeline 回放与
Backend 交叉口回放。模型文件顶层共 60 个 class，包含基类、Protocol 和 Enum。
以下主要是 Q99 实施前的静态调用与代码结构记录，不是当前性能基准；
转向路径条目已按后续 SQL 实现更新，其余逐点分配描述仍属实施前基线：

- Parser 的 PostgreSQL 快速路径已避免逐行 dict，但仍逐行构建 PointRef、
  TrajectoryPoint 和空 extra 字典。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/parse_rows.py:33](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/parse_rows.py:33)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/parse_rows.py:135](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/parse_rows.py:135) 。
- Refine 在投影后为每个保留点再次构建 TrajectoryPoint；kept、derived、
  subtracks.points 是引用同批点的多组列表，没有复制全部几何，但重复保存引用。
  正式阶段未消费增量结果的 kept/derived 列表。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py:45](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py:45) 、
   [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py:103](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py:103)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/stages/refine.py:221](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/stages/refine.py:221) 。
- DerivedPoint 与 TrajectoryPoint 的字段完全相同，没有新增行为；注入点写出依据
  generated_reason 区分，不依赖 DerivedPoint 类型检查。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/models.py:150](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/models.py:150)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/write_refine_results.py:171](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/write_refine_results.py:171) 。
- 正式 Detect 转向候选走 SQL，网格跨轨迹计算消费列批和 NumPy 数组；不能将旧
  detect_turn_patch 的 Point 列表路径误判为正式转向主路径。网格构段仍先建逐线段
  字典/元组再转列数组；命中交点后仍包装点引用与候选对象。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:1170](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:1170) 、
   [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/grid_segments.py:35](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/grid_segments.py:35)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/detect_grid_crossings.py:148](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/detect_grid_crossings.py:148) 。
- Detect 拓扑按窗口选择完整子轨迹，逐行构建 TrajectoryPoint、PointRef、
  RefinedPointRef；不同窗口可再次解码同一子轨迹。几何计算又将点列表转换为坐标
  元组列表、起止端点列表与 NumPy 数组。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/results/read_parquet.py:208](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/results/read_parquet.py:208)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/extract_topology.py:546](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/extract_topology.py:546) 。
- Clip 读取时逐点创建 ResolvedTrackPoint，片段内点再创建 ClipPoint，临时关系
  每次读取再次创建 ClipPoint；Fitting 定向过程随后又形成坐标列表、投影元组和
  np.asarray 输入。三种中心线方向各有准备/配对/长度/采样读取。证据：
   [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/results/read_parquet.py:87](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/results/read_parquet.py:87) 、
   [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/extract_clips.py:222](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/extract_clips.py:222) 、
   [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/clip_input.py:253](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/clip_input.py:253)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:225](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:225) 。
- Detect 写出仍为每个 Refine 成员构建 _RefineMember 和 RefinedPointRef。
  Backend 交叉口回放也直接创建 PointRef/TrajectoryPoint，必须同步迁移。
  证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/write_detect_results.py:248](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/write_detect_results.py:248)  和 [/data/workspace/Multimodal-GEO-roadnet/apps/backend/src/backend/replay/select_intersection_members.py:15](/data/workspace/Multimodal-GEO-roadnet/apps/backend/src/backend/replay/select_intersection_members.py:15) 。
- TrackModel 没有 __slots__；声明 slots=True 的后代仍继承实例字典。
  结构及隔离加载验证已确认机制，但宿主 Python 为 3.10，隔离加载使用内存中的
  StrEnum 替身；不得将该验证的对象大小当成生产 Python 3.11 性能结果。
  证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/models.py:29](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/models.py:29) 。
- 全仓库未找到 InOutIntersectionPoint 的构造调用或 inout_points 的赋值；
  仍有默认空字段和 replay_inout_points 消费接口。实际观测生产者是
  EntryExitObservation。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/models.py:616](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/models.py:616)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/extract_topology.py:398](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/extract_topology.py:398) 。
- NetworkEdge 已共享 Centerline.points，没有重新构建几何；仅合并这两类不能
  解释为节省几何分配。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/road_network/build_road_network.py:67](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/road_network/build_road_network.py:67) 。

#### 5.6.2 全部 60 类的审计建议

下表审计建议已纳入 Q99 确认范围；实际状态按 2026-09-26 当前工作树核对。功能整合和性能验收另记，类定义落地不代表已完成全部验收。“保留”不要求继续集中放在一个文件。
配置由加载器动态构造，不能用显式构造次数为零判断无用。

| 类 | 粒度、重复关系及建议 | 实际落地状态 |
| --- | --- | --- |
| TrackModel | 保留生产使用的 as_dict 能力；候选方案为无实例字典的基类或独立序列化函数。 | 保留 as_dict；__slots__=()，移除 from_dict/format_print。 |
| _DictModel | 仅 from_dict 包装使用，调用仅在测试；与 from_dict/format_print 一起作为清理候选。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| PointRef | 删除逐点实例；source 元数据在容器共享，来源行键、派生锚点等存列。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| RefinedPointRef | 删除逐点实例；点类型、点 ID、子轨迹与顺序存列，保留身份语义。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| TrajectoryPoint | 删除，改子轨迹/读取批次列式载荷。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| DerivedPoint | 与 TrajectoryPoint 字段完全重复；删除，以 kind/reason 列区分。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| FieldSpec | 每字段配置定义，保留；不在逐点规模构建。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| FieldMapping | 每来源映射且有 specs_by_canonical 缓存，保留；与配置定义职责不同。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| FieldConfig | 字段定义及解析质量阈值，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| SourceInfo | 每数据源元数据，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ParseIssue | 保留错误语义与受限采样；point_ref 改标量身份，不为正常点构建。 | 保留受限错误样本；point_ref 为标量身份。 |
| ReadReport | 来源级计数与错误样本，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| RefineReport | 阶段计数，保留；样本点引用改标量。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ExcludedPoint | 删除逐排除点包装，改来源行键/原因列。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| ExcludedIndex | 替换或合入列式排除结果容器；过滤/计数直接消费列。 | 已删除，排除结果为 ExcludedBatch 来源键/原因/几何列。 |
| RefineConfig | 阶段配置，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ProjectionConfig | 投影政策，保留；与具体运行上下文不重复。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| SpatialContext | 运行共享 CRS 上下文，保留；另审 projection_config 字典是否必要重复。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| RuntimeConfig | 跨阶段 I/O 与执行参数、校验，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| TrajectorySourceHandle | Protocol 本身不逐点分配；改读取批次/整轨接口。 | 保留 Protocol；iter_vehicle_batches 返回整车 SourceBatch。 |
| GeneratedReason | 共享 Enum 常量，保留，非逐点对象热点。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| RefineResult | 与增量结果统一数组数据契约；按 Q92 迁移仓库内调用，清理无剩余调用的重复入口。 | 已改数组子轨迹与排除批次；eager/stream 共用数据契约。 |
| RefinementSubtrack | 保留每子轨迹容器，改整轨数组，不含 Point/Segment 实例列表。 | 已改 ndarray；运动、身份、kind、after_sequence 列，原车名保留。 |
| RefineIncrementalResult | 简化为子轨迹、排除列与统计，去重复 kept/derived 列表。 | 已简化为 subtracks、ExcludedBatch、统计；无 kept/derived 点列表。 |
| CsvSource | 每源输入描述，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| PostgresSource | 每源查询与连接描述，保留；与 CsvSource 不构成无意义重复。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| TurnEvent | Q90 已选择逐事件载荷改批量列，保留转向事件语义。 | 已删除，转向载荷为 TurnBatch。 |
| CrossTrackCandidate | Q90 已选择候选列/关系接口，消除嵌套点引用对象。 | 已删除，候选载荷为 CrossCandidateBatch 与关系批次。 |
| HotspotCluster | 保留聚类级容器，成员以索引/关系关联，去逐点引用包装。 | 保留聚类语义；MemberBatch 引用事件 namespace/index/order，无重复事件载荷。 |
| EntryExitObservation | 与两个穿越点关联的一次观测，非 CrossingPoint 同义类；Q90 已选择列式化。 | 已删除，观测载荷为 ObservationBatch。 |
| CrossingPoint | 删除逐穿越点包装，保留线段身份、fraction、进出、交叉口关联等列。 | 已删除，穿越载荷为 CrossingBatch。 |
| InOutIntersectionPoint | 无构造生产者；删除候选，连带空字段与残留回放接口清理。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| IntersectionTopology | 每聚类拓扑摘要、分支与转移计数，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| IntersectReport | 阶段统计，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ClipIntersection | 每交叉口圆定义，保留；不是轨迹点规模的分配。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ResolvedTrackPoint | 删除逐点包装，改已解析子轨迹列。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| ClipTrack | 与统一子轨迹表示复用数组与身份规则；避免独立的一套点包装。 | 已改 resolved ID、XY/lonlat、穿越关联与独立有效性列。 |
| ClipInput | 阶段输入与上下文容器，保留或简化接口。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ClipEndpoint | 随 Q92 迁移为片段端点索引与元信息，删除点包装；来源依据见第 5.6.4 节。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| ClipPoint | 删除，片段内几何及 resolved IDs 存数组/切片。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| TrackClip | 每片段容器有独立类型和端点语义，保留；几何改数组。 | 保留片段语义；端点元信息及内点 ID 列，几何 ndarray/slice。 |
| ClipReport | 阶段计数，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ClipResult | 输出、报告、上下文与关系生命周期，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ClipConfig | 单字段阶段配置；可结构合并，但无逐点性能证据。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| FittingConfig | 阶段配置，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| Centerline | 每路对结果，保留；几何可统一数组表示。 | 保留中心线语义；readonly float64(N,2) 几何。 |
| FitIssue | 拒绝片段/路对的原因，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| FitReport | 阶段计数，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| FitResult | 阶段输出容器，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| CenterlineFitBundle | 三种不同方向拟合结果，保留；不是同一结果的重复包装。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| NetworkConfig | 单字段配置，可结构合并，但非逐点性能重点。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| NetworkNode | 比交叉口圆增加网络 degree，保留网络语义。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| NetworkEdge | 与 Centerline 有部分字段相同但已有几何共享；保留或公共几何接口，非优先合并。 | 保留网络 degree/方向等语义；共享中心线 ndarray 几何。 |
| NetworkIssue | 与 FitIssue 有部分重叠，可公共错误结构；目前低频且字段约束不同。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| NetworkReport | 阶段计数，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| NetworkResult | 阶段输出容器，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ReplayFitEndpoint | 随 Q92 与 ClipEndpoint 一起迁移为端点元信息；保留 resolved ID、边界可空语义及回放字段。 | 已删除，关联语义迁入阶段数组/端点元信息。 |
| ReplayFitClip | 保留回放容器，内部点改数组；方向和输入顺序不同于普通 TrackClip。 | 保留回放语义；完整 ID/几何列，JSON 边界恢复原字段。 |
| CenterlineInputReplay | 每中心线回放元数据，保留。 | 保留原业务/配置/报告/资源职责，非逐记录载荷。 |
| ReplayRefineResult | 保留 original/excluded/injected/active 分组语义，按 Q92 迁移数组与索引，清理无剩余调用的重复入口。 | 保留分组语义；原始 SourceBatch、排除索引、active 子轨迹数组，边界序列化。 |


#### 5.6.3 数组方案草案与设计树

建议：每条 SubTrack 持有 XY、lon/lat、heading、timestamp、speed 及所需身份列；
共享 vehicle/source/CRS 元数据放容器。Segment i 由相邻成员 i、i+1 定义，
不创建 Point、也不由 Segment 实例列表组成 SubTrack。跨网格的局部线段可存
独立线段列批，不要求为了局部求交加载整条子轨迹。

Q99 已确认 XY 权威内部布局为 (N,2) / float64；新构建数组默认 C 连续，
基本切片允许非连续 strides，下游只读。接口需要 (2,N) 时优先转置 view，
必要算法工作区仍可分配，不承诺全链零复制。
保留点、注入点、穿越点的种类、来源行键、Refine 点 ID、resolved ID、
顺序与锚点有不同含义，不合成一个通用 id 列。仅为实际消费者配置必要列，
不将所有阶段可选字段堆入统一大容器。

设计树：

1. 已明确的根方向：删除逐点类；整轨数组；Segment 为最小几何计算单位。
2. 已明确范围：Q90 同时列式化逐点和逐事件载荷，允许拆成多个 commit；Q92 迁移旧对象与仓库内调用，不留旧 Point API 兼容层。
3. Q91 保持算法规则；不做容差检查，不比较历史派生结果，以最新生成数据为准。Q95 后续更正明确取消 vehicle_id 的 _segNN 后缀，subtrack_index 是唯一子轨迹分区依据。
4. Q93 选择少量阶段容器；Q94 明确构建可写、交付下游只读；Q95 保留 Refine 整车读取；Q96 保存 XY 和 lon/lat。
5. Q97 已选择同 Run 内仅按 subtrack_index 排除同轨；同原始 vehicle_id 的不同子轨迹允许求交。事件批次、成员关系、身份与锚点列和回放契约见第 5.6.4–5.6.6 节。
6. 数据契约与验证范围已整理；分 commit 依赖顺序见第 8.1 节，性能目标沿用本 Spec 既有基线。
7. Q98 更正及布局审计完成；Q99 已选择 A，确认完整修订设计并授权指定子代理分 commit 实施。

Q91 已明确只将 trajectory_points 与 day01 源表作为必须保留的数据，其他历史派生数据
允许抛弃，以最新产生的数据为准；因此不要求兼容旧派生数据的 ID、布局或历史结果。
旧派生数据可抛弃不等于删除点的来源追溯字段；新生成结果仍需满足当前算法的身份关联、
顺序、锚点与回放功能。Q92 的前后端响应字段、命令行功能与回放能力继续保留。
派生表/Parquet 的新布局按实际字段需求决定，本轮尚未执行任何数据清理。
性能测量尚未执行，不宣称对象数量减少的倍数、内存下降比例或全链路加速比例。

#### 5.6.4 Q90–Q97 后的数据约束与契约（2026-09-26）

已确认决定：

- Q90：逐点与逐事件同时列式化，可以按模块分 commit。
- Q91：保持算法规则；取消本次容差检查/旧结果比较；保留 trajectory_points、
  day01 源表；历史派生数据可以抛弃，以最新生成数据为准。
- Q92：旧对象和仓库内调用方直接迁移，不保留旧 Point API 兼容层，保留既有功能。
- Q93：采用少量阶段容器，按阶段所需字段直接持有列数组，共享坐标与切片约定。
- Q94：构建期间可写，交给下游后只读。用户明确的是写读边界；共享连续切片与批量
  构建结果仍为符合该边界的实现建议，不解释为用户要求每处都零复制。
- Q95：本轮保留 Refine 整车读取，内部子轨迹数组化；vehicle_id 保留源值，
  不再添加 _segNN，subtrack_index 是所属 Run 内唯一权威子轨迹分区键。
  此更正取代前轮关于保持后缀命名的建议，是 Q91 保持算法规则的明确局部例外。
- Q96：Clip→Fitting 临时关系同时保存 XY 与 lon/lat，后续计算复用 XY，
  使用该 Run 的 SpatialContext，不重复逐点投影。
- Q97：同 Run 内跨轨迹配对只要求 subtrack_index 不同，允许同原始 vehicle_id
  的不同子轨迹求交；grid、Parquet 与内存路径统一按子轨迹身份排除同轨。

根据现有代码及上述决定，可直接落实而无需重复提问的约束：

- Q99 确认坐标为 (N,2) / float64，新构建数组默认 C 连续，下游只读；
  非连续切片可用，需要 (2,N) 时优先转置 view。小型 NumPy 实验已确认转置、相邻切片及同 dtype 的 asarray 共享内存；
  已检查的求交/Fitting 内核未要求 C 连续。未在完整生产环境执行内核基准，
  不宣称所有算法都不分配临时数组。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/_geometry.py:122](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/_geometry.py:122)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:293](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:293) 。
- 来源行键、Refine 类型/ID、resolved ID 分别表达不同关联，改列后仍保留语义，
  无须保留历史 ID 数值。正式 subtrack_index 按 Run 递增分配，不必另加重复的
  字符串子轨迹 ID。证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/pipeline_store.py:276](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/pipeline_store.py:276) 。
- 来源整数主键没有正值约束，因此不能拿 0/-1 代替缺失来源键，使用有效性标记；
  现有同车辆同时间源行按 id::text 排序，数值化存储不能顺手改成数值排序。
  证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/source_table.py:83](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/source_table.py:83)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/source_table.py:138](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/source_table.py:138) 。
- Clip 端点来自 resolved 序列的穿越点或自然首尾点，内点是连续区间；
  可把端点的索引、kind、crossing/intersection 关联作为片段元数据，
  删除 ClipEndpoint/ReplayFitEndpoint 的点包装，回放输出在边界映射原字段。
  证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/extract_clips.py:214](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/extract_clips.py:214) 。
- 投影工具的 forward_many 当前仍创建输入列表、经纬度列表和输出元组列表，
  也需要改数组接口，否则只换模型仍保留这条转换链。
  证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/projection.py:288](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/projection.py:288) 。

身份与生命周期的实施约束：

- 同一 Run 内分组、相邻点构段、时间窗口、子轨迹内顺序与片段读取统一使用
  subtrack_index；vehicle_id 仅保存源车辆归属，不再作为子轨迹分区的替代值。
  跨 Run 访问仍由所属 Run 隔离，不把 subtrack_index 当成跨所有 Run 的全局主键。
- 不按车辆名后缀恢复子轨迹，不把同车不同子轨迹连成跨时间间隔的线段；旧后缀
  派生数据无需兼容，按 Q91 可重建。相关测试应使用同 vehicle_id、不同
  subtrack_index 的用例，验证分组、邻点、写出与回放。
- 下游获得只读数组；过滤、注入用批量索引和一次输出分配完成。连续片段可共用
  只读切片，沿用逐条子轨迹写出及释放，避免长期保留父数组。
- Q96 每个片段成员新增 16 B 的 XY 数值载荷，实际临时存储增长及性能收益待测。
  当前重复投影证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:214](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:214) 。

#### 5.6.5 少量阶段容器的模型实现草案

依据 Q90–Q96 与现有字段用途，采用以下少量阶段容器；具体名称随模块职责调整，
不新增通用模型框架，不保留逐点实例链。

- SourceBatch：整车 source_id、原 vehicle_id、lonlat 数组（与坐标布局一致为 (N,2)）、timestamp/heading/speed、来源行键与必要 extra 列。
- RefinementSubtrack：Run、vehicle_id、subtrack_index、XY/lonlat、运动列，以及 kind、来源键、Refine ID、生成原因与锚点列。
- resolved 子轨迹：Detect/Run、vehicle_id、subtrack_index、XY/lonlat、resolved ID、crossing 关联及可空关联的有效性列。
- TrackClip：subtrack_index、片段序号、类型、距离、连续内点范围与端点元信息；临时成员关系列式保存 XY、lon/lat 与关联 ID。
- Turn/Cross/Crossing/Observation：各自的事件批次列；HotspotCluster 保存成员索引或关系与摘要。成员键为 kind + calculation_index，member_order 独立；crossing 键为 cluster index + crossing index。
- Segment 是最小几何计算单位；完整轨迹使用相邻点索引，网格局部 SegmentBatch 可持有显式端点列，SubTrack 不保存 Segment 实例链。

模型文件外的 TurnCoordinate、CrossCandidateCoordinate、_RefineMember 等逐项包装
同步列式化；低频配置、报告与资源所有者保留独立对象。构建可写、下游只读，
连续切片可共享数组，不承诺每处零复制。Q97 已选仅按 subtrack_index 排除同轨。

#### 5.6.6 全部模型及文件外包装的最终清理建议

第 5.6.2 节保留全部 60 类逐项审计记录；以下清理建议已按 Q98 第 6 项采纳，完整设计已由 Q99 确认，
不表示已执行删除。

- 删除 PointRef、RefinedPointRef、TrajectoryPoint、DerivedPoint、ExcludedPoint、
  ResolvedTrackPoint、ClipPoint、CrossingPoint 的逐点包装；ClipEndpoint 与
  ReplayFitEndpoint 改端点索引/元信息，删除包装类。事件类改批次列，保留各事件语义。
- 删除无生产构造者的 InOutIntersectionPoint 及空字段/残留接口；删除仅测试使用的
  _DictModel 与无生产用途的 from_dict/format_print。生产 as_dict 功能保留为边界序列化函数。
- ExcludedIndex 合入排除列结果；RefineResult、RefineIncrementalResult 与
  ReplayRefineResult 共享子轨迹/排除/统计数据契约，去重复 kept/derived 列表及无调用入口，
  保留回放 original/excluded/injected/active 分组功能。
- 文件外 TurnCoordinate、CrossCandidateCoordinate、_RefineMember 等逐项包装
  同步改列/关系；ClipTrack 复用子轨迹数组与身份，ClipInput 保留必要阶段输入职责。
- 保留低频配置、来源描述、报告、问题样本、运行上下文及资源生命周期容器；
  不为性能目标合并职责不同的 FieldSpec/FieldMapping 或 CsvSource/PostgresSource。
- 保留 Centerline、NetworkNode/Edge 及结果语义，继续共享几何数组；
  保留不同方向拟合 bundle、拓扑摘要与回放容器，不建立逐点链。

其余单字段配置、错误结构与序列化函数的局部形式由实施者按实际调用选择，
无需新增设计问题；不能由此扩大为通用框架。完整范围见 Q98。

#### 5.6.7 主要几何内核布局审计（2026-09-26 基线；转向行后续更新）

本轮按正式生产内核族核查，不以函数数或未经测量的运行时占比计票。
审计推荐权威 XY 布局为 float64 (N,2)，已由 Q99 明确采纳，取代此前暂定 (2,N)。

| 生产内核族 | 审计时输入/主要运算；转向行已更新 | 布局证据 |
| --- | --- | --- |
| Refine 与投影 | 整车状态机、点对象标量；forward_many 物化经纬度列表和输出元组列表 | 无二维 ndarray 布局偏好。 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py:52](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/refine_trajectories.py:52)  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/projection.py:286](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/projection.py:286)  |
| Detect 转向、G*、DBSCAN | 正式转向和内存入口复用 DuckDB SQL 窗口与阈值；G*/DBSCAN 为 x/y 标量或独立一维列。 | 均不计作 (2,N) 矩阵证据。 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/results/read_parquet.py:280](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/results/read_parquet.py:280)  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/collect_candidates.py:15](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/collect_candidates.py:15)  |
| Detect 正式网格跨轨迹求交 | 独立输入列 column_stack 成 (N,2) starts/ends，配对索引 gather 后批量求交 | 天然 (N,2)，有界 segment/pair 批次。 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/detect_grid_crossings.py:65](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/detect_grid_crossings.py:65)  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/_geometry.py:306](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/_geometry.py:306)  |
| Detect Topology 圆求交 | 完整 subtrack 元组列表转 (N,2)，显式 shape 校验、按行广播/einsum | 天然 (N,2)。 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/extract_topology.py:555](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/extract_topology.py:555)  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/_geometry.py:436](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/_geometry.py:436)  |
| Clip | 完整 track 状态机、内点元组、重投影后相邻 hypot | 无 ndarray 布局偏好；tuple 与重投影物化为当前边界。 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/extract_clips.py:222](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/extract_clips.py:222)  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/extract_clips.py:380](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/extract_clips.py:380)  |
| Fitting | (N,2) 折线；diff(axis=0)/norm(axis=1)、分列 interp；sampled(K,B,2) 沿 axis=0 median | 天然 (N,2)，按路对/采样块反复消费。 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:225](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:225)  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:293](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/centerlines/fit_centerlines.py:293)  |

真正消费二维 XY ndarray 的生产内核族为上述跨轨迹求交、圆求交、Fitting：
3/3 使用 (N,2)，0/3 使用 (2,N)。这是接口族统计，不是性能测量或运行时百分比。
旧 eager/参考求交及非当前正式入口的 DuckDB pair 路径也为 (N,2)，仅作佐证，
不重复计入生产分母。DB/Parquet 的独立列不决定二维矩阵 shape。

shape 与内存顺序分别约定：推荐新构建的权威数组默认 C 连续；交付下游只读，
基本切片允许非连续 strides，不为所有视图强制 C 连续。需要 (2,N) 的接口优先转置
view。C 连续 (N,2) 的 x/y 视图步幅为 16 B；C 连续 (2,N) 转置为 (N,2) 后是
F 连续视图。现有 NumPy 基础运算/einsum/diff/norm 未发现 C 连续强制要求，
但不能据此保证 pyproj 等生产版本内部零复制。

已确认分配包括 column_stack、元组列表转 asarray、整数/布尔高级索引与采样/stack
工作区；转置免复制不代表全链路免复制，选择 (N,2) 也不取消必要算法分配。
宿主 NumPy 1.21.5 的小 shape/strides 实验观察到转置共享内存、高级索引不共享；
该版本不满足生产 NumPy >=2，未测生产速度、峰值内存、容差或旧输出。
Q99 已确认最终布局并单独明确实施授权；审计本身不构成性能验收。

## 6. 约束与非功能要求

### 6.1 资源边界

现行 26M 测量使用新建 Pipeline 父控制组（2026-09-29 22:34，开发人员 @reid 更正）：

- Pipeline 容器不设置硬内存上限，实际 `memory.max=max`；
- 不强制 `memory.swap.max=0`，记录实际值；
- `memory.current` 超过 6,000,000,000 B 时在当次日志写 `ERROR`，作业继续；该值不是内存上限或失败条件；
- 计入 Python、DuckDB、Polars、原生线程和本控制组文件页缓存；
- PostgreSQL 容器单独计量；
- DuckDB 公共 memory_limit = 1 GB；Turn/Cross/Grid 独立实例各自 memory_limit = 4 GB；threads <= 4；
- Polars 强制流式执行并限制线程数。

DuckDB memory_limit 只约束各实例的主要缓冲管理器，不是 Pipeline 容器硬上限。
旧 2GB 与 4GB 父控制组运行属于历史失败记录；不把旧运行的 swap 0、
`memory.peak < 4,000,000,000 B` 或控制组 OOM 条件沿用为本轮验收标准。

Q73–Q77 的新跨轨迹任务仅使用本地最小线程调度：可配置默认 4、最多 8 个线程，
每线程独立 PostgreSQL 连接；不建立通用调度框架或 Dask 集成。一般可恢复网格
失败最多重试 2 次，仍失败则 Detect 失败。Q100–Q107 已覆盖旧的单格超预算
立即失败规则：配对工作量、membership、整 grid 输入、候选与全 Run 去重累计四层
按现行计划切换精确处理路径；DuckDB 临时额度仅按每实例 50→100→200GB 的
受限规则扩容重放，封顶或其他不可恢复错误才失败。全部网格为 `done` 才完成
跨轨迹任务；一般网格重试不能把封顶额度反复重建为初始额度。

### 6.2 配置与版本

DATA_ROOT、PARQUET_ROOT 等挂载根来自环境变量。DuckDB 内存、线程、临时盘上限、
Polars 线程、文件/行组目标和 GC 宽限期集中在 pipeline.toml [runtime]。每个 Run
记录实际有效参数和机器 CPU、内存、磁盘配置。

pyproject.toml 使用 DuckDB、Polars 的受控兼容范围；容器构建锁定解析后的精确版本和
DuckDB PostgreSQL 扩展。扩展在镜像构建期安装，运行时禁止联网下载。依赖或扩展升级
必须重跑语义差分与 26M 基准。

## 7. 验收标准

2026-09-26 的点对象重构已确认、实施已授权：性能与资源目标沿用本 Spec 既有基线。
结构验收要求生产数据流不再构建逐点/逐事件实例链，阶段容器以数组与索引交接；
功能验收覆盖点顺序、来源/派生身份、锚点、可空关联、端点与回放字段，
以及同车不同子轨迹允许求交、同子轨迹排除的 grid/Parquet/内存独立用例。
生产环境测量对象构建、耗时与内存，不以审计推算代替实测。
本次代码审计不构成性能验收；宿主隔离加载的 slots 机制验证不替代生产环境测量。

### 7.1 语义

2026-09-26 更正（Q91，适用于本次全部逐点/逐事件重构；Q95 明确更正车辆/子轨迹身份）：保持当前算法规则，
不设置或执行新旧结果容差检查，不以历史派生数据或旧算法输出作为验收基线；
trajectory_points 与 day01 源表必须保留，其他历史派生数据可抛弃，使用最新生成结果。
Q97 明确同 Run 的跨轨迹配对仅要求 subtrack_index 不同，同 vehicle_id 不构成排除条件；
grid、Parquet、内存路径统一执行该规则。以下历史差分和容差条款不适用于本次重构。
独立功能用例、顺序/身份/锚点完整性、
回放字段及运行完成性仍需验证；这些检查不要求与旧输出逐项对应。

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
- 不设置 Pipeline 容器硬内存上限；测量开始前核对 `memory.max=max`；
- 记录 `memory.swap.max`、`memory.peak` 与 `memory.events` 实际值，不要求 swap 0 或峰值低于旧 4GB 上限；
- 超过 6,000,000,000 B 仅在日志记 `ERROR`，继续运行，不据此判定性能失败；
- PostgreSQL 内存、临时文件和磁盘单独报告；
- 从 Refine 开始至 Network 完成的墙钟时间 < 3,600 秒；
- Q99 本次全链路重构按 7.1 的更正规则执行独立功能、身份/顺序/锚点和回放验证，
  不执行旧输出差分或容差检查。

### 7.3 本轮 grid 预算策略（Q100–Q107 已确认并获实施授权）

用户要求本修改先计划并 grilling，随后回答“全部采用推荐”：Q100=A，配对工作量、
membership 索引、整 grid 输入、候选及全 Run 去重累计四层均纳入完整处理方案，
超预算切换处理策略而非预算直接失败；Q101=A，超过 1 小时继续完成，但明确记录
性能目标未达标。该决策覆盖 Q77 旧 grid failfast 约束。初稿时的 5 文件预算
变更已由历史提交 `3c777b6` 纳入；完整方案及授权见现行实施计划。Q102 要求保留普通快路径，超预算使用 DuckDB 原生外存，
禁止项目显式 save/spool/COPY 落盘流程；Q103 要求沿用 50GB 初始额度，自动上调
生效额度并记录日志。每个独立 DuckDB 实例初始 50GB，倍增至 100GB、200GB 封顶，
不设置 Run 合计配额；Q106 仅对 DuckDB 临时额度耗尽执行受限重试。用户已确认
完整实施计划与后续索引方案，实施授权已恢复。
本轮 Pipeline 容器无硬内存上限，6GB 仅记日志；精确完整性保持，不跳过 grid 或
配对，普通 grid 路径保留。1 小时仍是性能目标，不因继续运行而放宽。
当前实现已接入整 grid 输入 TEMP 切换和全 Run 候选暂存/去重路径；这些结构及
独立功能测试不等于 26M 实际全部 grid 已完成，也不证明整条链路达成内存/时间目标。

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

### 8.1 点对象重构实施顺序（2026-09-26，已授权）

主要几何内核布局审计与完整确认已完成；先定义整轨/批次列与身份语义，
随后迁移 Parser/Refine 及其写出；
随后迁移 Detect 拓扑、穿越和候选关系边界，再迁移 Clip/Fitting 交接、
Pipeline 与 Backend 回放；最后清除无剩余调用的旧类和接口。
Q90 已确认逐事件一并列式化并允许分 commit 提交；Q91 保持算法规则、取消本次
新旧结果容差/历史派生对比；Q92 迁移旧对象接口与调用方，不保留兼容层。
Q93–Q96 已确认阶段容器、构建可写/下游只读、Refine 整车读取、保存 XY/lonlat；
子轨迹身份迁移同步取消 vehicle_id 后缀，并以 subtrack_index 分区；按 Q97，
grid/Parquet/内存求交路径统一仅排除同 subtrack_index，允许同车不同子轨迹求交。
验证应覆盖独立功能用例、顺序/锚点/边界和回放字段，并在相同生产环境测量
对象构建、时间与内存；性能目标沿用本 Spec 既有基线，不新增旧输出容差。
实施阶段仍需运行项目要求的 make test，实际 Pipeline 冒烟/全量运行分别注明
指定配置。Q99 已授权按此计划分 commit 实施；本次文档收尾不编辑代码或提交，
由执行负责人统一集成与提交。

实施清单初始状态（授权已生效；状态不等于验收结果）：

| 项目 | 状态 |
| --- | --- |
| 阶段模型、数组与身份契约 | 已实施；结构入口及身份/只读独立审查已闭环 |
| Parser/Refine 及写出 | 已实施；真实 PG 与规则回归通过 |
| Detect 事件、拓扑与候选关系 | 已实施；最终诊断/只读交付审查收尾中 |
| Clip/Fitting 双坐标与端点关系 | 已实施；真实 PG 下游验证通过 |
| Pipeline/Backend 回放与边界调用迁移 | 已实施；Backend 84 项通过 |
| 旧类、无生产者接口与重复结果清理 | 已实施；生产残留扫描无匹配 |
| 功能、make test、指定冒烟/全量及性能验证 | 完整 make 已通过；首次 1M 诊断运行失败，修复后重跑待执行，26M 未执行 |
| 审查、分 commit 集成与提交 | 独立交叉审查已闭环；b12b960、10f27a6 已提交 |

## 9. 风险与待决事项

2026-09-28 新增待决：性能优化顺序、用户所说 append 的层次、原始 PostgreSQL 写入是否可统一登记版本，分别见文末 Q108–Q110。已发布 Parquet 的不可变要求仍沿用 Q27–Q28/Q57；没有本轮实施授权。

### 9.1 Detect 网格取点变更访谈（2026-09-24）

用户提出 `fetch_points_in_grid`：输入为 `grid_index(col,row)`、
`grid_AABB(A,A,B,B)`、继承的 `refine_id` 和 `spatial_context`；输出为该 Refine
运行的 `kept_points union inject_points` 中落入 AABB 的所有点。此前讨论还提出
使用 PostgreSQL 点空间索引、按网格构造线段、NumPy 求交，并接受两端都在网格外而
穿过网格的线段漏检。这些是本轮访谈输入，尚未形成已确认的接口设计。

代码事实（2026-09-24 核查）：当前 Detect 大表从 Refine Parquet 读取，
`RefineDatasetReader.__enter__` 将保留点和注入点合成 `refine_points` 视图；
Refine 导出 Parquet 时从 PostgreSQL 保留点归属表关联源点，并合并注入点。
见 `services/pipeline/src/track/v2/trajectories/results/read_parquet.py` 的
`RefineDatasetReader.__enter__` 和
`services/pipeline/src/track/v2/trajectories/results/export_to_parquet.py` 的
`export_refine_parquet()`。
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
等源数据表视为权威来源。Q77 的单格内存超预算立即返回错误是历史决定，
现由 Q100=A 覆盖：四层预算超限切换处理策略，保持完整处理；具体方案见
“Q100–Q107 现行实施计划”。Q78 采用少量权威源数据构造的
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

### 9.4 点对象与模型粒度重构访谈（2026-09-26）

用户已明确 SubTrack 整轨数组、Segment 最小几何计算单位与删除逐点类的方向；
要求审计模型文件全部类并共同讨论重构方案。审计结论与全部 60 类建议见第 5.6 节。
Q90–Q92 已回答：逐点与逐事件一起列式化、允许分 commit；算法规则保持，
不做容差检查，保留 trajectory_points/day01、允许抛弃历史派生数据，以新结果为准；
旧对象与调用方直接迁移。Q93–Q96 已回答：阶段容器、构建可写/下游只读、Refine
整车读取以及 Clip 临时关系保存 XY/lonlat；Q95 进一步更正为取消 vehicle_id 后缀、
subtrack_index 唯一分区。核查已确认正式网格路径按 vehicle_id 建立 rank，
配对排除相同 rank：取消后缀会使同车不同子轨迹从原本可配对变为被排除。
证据： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/detect_grid_crossings.py:89](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/detect_grid_crossings.py:89)  和 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/build_patches.py:229](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/build_patches.py:229)  及 [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/build_patches.py:264](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/candidates/build_patches.py:264) 。
Q97 已选 A：同 Run 内只要求 subtrack_index 不同，允许同车不同子轨迹求交，
不允许同子轨迹内部参与跨轨迹配对。所有路径同步迁移该身份判断。
约束、模型草案与最终清理建议见第 5.6.4–5.6.6 节。Q98 第 2–8 项已采纳，
第 1 项已完成布局审计；Q99 已选择 A，确认权威 (N,2) / float64，
完整设计已确认并授权实施，当前进入实施阶段，尚待验收。

协作方式按第 24 轮后续补充：主代理协调、访谈和汇总，详细仓库调查、编码和审查
交给 gpt-6-sol low 子代理，必要时由其调用 gpt-6-luna high 并行。
所有子代理默认要求 Fast；模型及推理强度仍为 sol low、按需 luna high。
本工具会话无法显式启用或验证 Fast：spawn_agent 及其他可用工具无 Fast/service_tier 控制，
也无自动继承证据；
不声称已成功启用，不将 low 推理强度当作 Fast。
协作补充本身不替代设计确认或实施授权；两者现已由 Q99 明确授予。
本次未修改运行代码，未运行 Pipeline 数据任务，未删除数据或发布制品。

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

本次点对象重构 Q90–Q97 已回答，Q98 两条局部更正及布局审计已完成。
用户在 Q99 明确选择 A，确认权威 (N,2) / float64 的完整修订设计，并要求开始按计划
实施，授权 gpt-6-sol low 分 commit 实施和审查、必要时派 gpt-6-luna high。
设计确认及实施授权均已生效；功能与性能验证、提交仍待执行，不构成验收通过。

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
历史解释：单格内存超预算明确返回错误，不自动重试该格或自动重跑 Detect。
当前更正：Q100=A 已覆盖此预算 failfast 决定，四层均需完整处理策略；
普通可恢复错误的 Q74 重试规则不因本题自动改变。

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

性能计划、耗时、行数与循环、清理清单、迁移状态和测试结果见
 [Q89 实施报告](../services/pipeline/generated/cross-track-explain/q89-results.md) ；
机器可读的完整计划与汇总见
 [网格查询汇总](../services/pipeline/generated/cross-track-explain/grid_compare_summary.json) ，
九个清理路径见
 [清理清单](../services/pipeline/generated/cross-track-explain/cleanup_manifest.json) 。

迁移预检保留旧字符串身份：只接受规范十进制行键，并拒绝仍引用 UUID、TEXT
等非整数 id 来源表的保留 Refine Runs。验证结果为 Pipeline smoke 1,141 passed、
1 skipped；make test 中 Backend 83 passed、Pipeline 1,075 passed、67 skipped，
迁移 PostgreSQL 集成测试 8 passed。

### 第 23 轮 — 点对象与模型粒度重构（2026-09-26）

发问时前提：复用列式架构 Spec；第 5.6 节已完成全部 60 类及正式数据流审计。
本轮只讨论方案，运行代码未改，未开展性能基准。

#### 本轮需求原文与先前补充

用户最初提出：

> 我要修改底层class，尽可能去除掉class points ->class segment -> class subtrack ->class ...
> 以后计算的最小对象是segment，而且不要用segment组成subtrack，subtrack直接用向量存一整条subtrack，比如
> subtrack :
> points[[x1,x2,...],[y1,y2...]],id:str,hedings[heading1,heading2,...],....
> 这样可以减少python对象的构建吧

本轮调用访谈并补充：

> [$grill-me](/home/reid/.codex/skills/skills/grill-me/SKILL.md) 对，大方向没错，你详细检查下代码，和我一起讨论个方案，refactor，去掉trajectoryPoint class，derivedPoint class等point class
> 并且同步检查一下，models.py里所有class是否有重复？能合并的？没必要的？粒度太小导致性能浪费的？

解释范围：认可整轨数组和线段计算方向，明确要求逐点类删除方案及全部模型审计。
属于设计讨论授权，未确认完整新方案，不继承 Q79 指定模型实施局部函数的授权范围。

#### Q90 — 逐事件对象是否一并列式化

> 你已明确删除逐点类。本轮是否同时把 TurnEvent、CrossTrackCandidate、EntryExitObservation 等逐事件载荷改为列数组？A. 同时列式化逐点和逐事件载荷，保留子轨迹、批次、片段、交叉口等容器；B. 本轮只删除逐点类，逐事件类留待下一轮重构。CrossingPoint 的穿越身份与属性无论选哪项都必须保留为数据，不能随类删除。

> 推荐 A：正式数据流中事件临时关系仍会逐条解码为对象，CrossTrackCandidate 转成聚类事件时还会构建 TurnEvent；只改轨迹点会留下这部分包装和重复读取。不是把不同事件的业务含义合并。

> 用户回答：90：同时列式化，但是commit可以分开提交

正文关联：第 5.6、7、8、9.4 节。
回答来源（合并原文摘录）：第 23 轮合并回答，第 1 行。
当前解释：选择同时列式化逐点与逐事件载荷；允许分多个 commit 提交。
本条没有确认完整方案或触发立即实施。

#### Q91 — 本轮是否保持算法语义

> 这次是纯数据表示与执行方式重构，还是也允许改变算法规则？A. 保持当前算法规则、点顺序、身份、去重及边界行为，只改变存储和计算方式；B. 同时调整算法规则，逐项另定预期结果。

> 推荐 A：先验证数组路径与当前同源输入的参考结果，再评估性能。浮点运算顺序可能因向量化改变，具体容差与必须精确一致的字段在下一轮明确；不以历史数据库产物代替当前参考算法。

> 用户回答：91：先保持算法规则，注意不要检查容差，唯一需要尊重保留的是trajectory\_points和day01这两个table，其他的历史派生直接可抛弃，已最新产生数据为准

正文关联：第 5.6、7、8、9.4 节。
回答来源（合并原文摘录）：第 23 轮合并回答，第 2 行。
当前解释：保持算法规则，但明确撤销本次新旧结果容差检查及历史派生基线要求；
保留 trajectory_points/day01 源表，历史派生可抛弃，以新生成结果为准。
新结果仍需满足功能、身份关联、顺序和锚点规则；本轮没有执行任何删除。

#### Q92 — 旧 Python 点对象接口的兼容边界

> 删除点类会改变 Pipeline 的公开 Python 导出和函数签名；Backend 交叉口回放也实际调用了这些类。A. 允许直接迁移所有仓库内调用方，不保留旧 Point 构造/API 兼容层；仅由旧点接口支撑的重复入口在确认无剩余调用后删除；B. 保留现有功能入口与函数名，统一改成数组输入输出，但不保留点类。两项均保留现有前后端字段、命令行功能和回放能力。

> 推荐 A：这与彻底删除逐点对象的目标一致，也允许清除仅服务旧对象链的包装；实际使用中的导入、回放和命令行入口必须迁移，不能仅因使用旧类就删除功能。

> 用户回答：92：按推荐，旧对象也迁移

正文关联：第 5.6、7、8、9.4 节。
回答来源（合并原文摘录）：第 23 轮合并回答，第 3 行。
当前解释：采纳 A，迁移旧对象及仓库内全部调用方，不保留旧 Point API 兼容层；
无剩余调用的重复入口可清理，前后端字段、命令行功能及回放能力仍需保留。

本轮检查：Q90–Q92 连续且唯一，全部已回答；正文记录已明确方向和审计事实，
未决项与实施状态明确；QA 附录保留在文末。

### 第 23 轮合并回答 — 对应 Q90–Q92

接收日期：2026-09-26。
用户回答原文：

> 90：同时列式化，但是commit可以分开提交
> 91：先保持算法规则，注意不要检查容差，唯一需要尊重保留的是trajectory\_points和day01这两个table，其他的历史派生直接可抛弃，已最新产生数据为准
> 92：按推荐，旧对象也迁移

解释范围：Q90 同时列式化且可分 commit；Q91 保持算法规则，同时以用户更正取代
原推荐中的新旧结果/容差对比，保护两类源表并允许抛弃历史派生；Q92 直接迁移旧对象
及调用方。没有将局部回答解释为完整设计确认或立即删除数据/实施代码的指令。
正文同步：顶部访谈进度、第 5.6、7.1、8.1、9.4 节及各题当前解释。

### 第 24 轮 — 数组容器与生命周期（2026-09-26）

发问时前提：Q90–Q92 已回答；同时列式化逐点/逐事件，保持算法规则，
不做容差/旧结果比较，保护 trajectory_points/day01，旧对象直接迁移。
(2,N) 布局、身份字段和 Clip 端点来源已按代码核实，不重复询问已明确语义。

#### Q93 — 统一模型还是少量阶段容器

> 数组化后，模型边界选哪种？A. 保留少量阶段容器：Refine 子轨迹、resolved 子轨迹、各类事件批次，共用坐标和切片约定；B. 所有阶段使用一个通用 SubTrack/列容器，通过可选字段和类型标记区分。两项都直接持有数组，不恢复 Point/Segment 对象链。

> 推荐 A：Refine 需要时间、方向、速度和来源身份；Clip 消费的 resolved 序列主要需要坐标、resolved ID 与穿越关联。按实际用途配置列，构建频率是每条子轨迹或每批事件，而不是每个点。

> 用户回答：93：A

正文关联：第 5.6.4、8.1、9.4 节。
回答来源（合并原文摘录）：第 24 轮合并回答，第 1 行。
当前解释：采用少量阶段容器，直接持有各阶段必需数组，共享坐标与切片约定。

#### Q94 — 数组共享与修改规则

> 数组在算法之间如何传递？A. 构建期间可写，交给下游后只读；连续片段共用切片，Refine 删除/注入时批量生成新数组，写出后释放父子轨迹；B. 每次交给下游都复制成独立数组，避免共享存储。

> 推荐 A：与现有阶段输出不可修改的约定一致，也能消除 Clip 的逐内点复制。只读切片会保留父数组，所以沿用当前逐条子轨迹写临时关系并释放的流程，不累积跨轨迹切片。

> 用户回答：94：构建期间可写，交给下游后只读； &#x20;

正文关联：第 5.6.4、8.1、9.4 节。
回答来源（合并原文摘录）：第 24 轮合并回答，第 2 行。
当前解释：明确构建期间可写、交付下游只读；共享连续切片与批量构建是符合此边界
的实现建议，不扩大成用户要求每处零复制或强制复制。

#### Q95 — 本轮是否改变 Refine 的读取粒度

> 当前 Refine 先读完整车辆，再按时间间隔拆子轨迹；Detect/Clip 已按子轨迹读取。本轮选：A. Refine 先改为整车列批和子轨迹数组，保留当前读取粒度；B. 同时把 Refine 改为逐子轨迹读取，额外改造分组查询或预扫描。

> 推荐 A：先完成全链路对象迁移。当前是否给 vehicle_id 加 _segNN 后缀取决于整车拆分总数，B 必须额外取得这个信息，并保持解析后的时间间隔规则；不能把删除 Point 类等同于已经实现单子轨迹驻留。

> 用户回答：95：A, PS:以后不再给vehicle\_id加segNN，subtrack唯一权威partition是subtrack\_index

正文关联：第 5.6.4、8.1、9.4 节。
回答来源（合并原文摘录）：第 24 轮合并回答，第 3 行。
当前解释：保留整车读取，并明确更正原推荐中的命名保留规则：vehicle_id 保留原值，
不追加 _segNN；subtrack_index 是 Run 内唯一权威子轨迹分区键。此更正覆盖 Q91
“保持算法规则”中有关旧后缀命名的部分。跨轨迹配对规则现已由 Q97 明确为仅按 subtrack_index 排除同轨。

#### Q96 — 是否在 Clip 临时关系保存投影坐标

> Clip 计算长度时已投影坐标，Fitting 多次读取又重复投影。本轮选：A. Clip→Fitting 临时关系同时保存 XY 和 lon/lat，Fitting 直接使用 XY；B. 临时关系只保存 lon/lat，Fitting 仍在读取时投影。

> 推荐 A：每个片段成员增加 16 B 的 float64 XY 数值载荷，可省去后续重复投影；实际临时存储增长还包含数据库开销，收益需要测量。沿用该 Run 的 SpatialContext，可独立提交。

> 用户回答：96：A,保存XY和lon/lat，后续方便计算

正文关联：第 5.6.4、8.1、9.4 节。
回答来源（合并原文摘录）：第 24 轮合并回答，第 4 行。
当前解释：临时关系同时保存 XY 与 lon/lat，后续计算复用 XY。

本轮检查：Q90–Q92 的合并原文及逐题摘录已保存，正文已同步；
Q93–Q96 连续且唯一，全部已回答。完整设计尚未确认，未实施运行代码；
QA 附录仍位于文末。

### 第 24 轮合并回答 — 对应 Q93–Q96

接收日期：2026-09-26。
用户回答原文：

> 93：A
> 94：构建期间可写，交给下游后只读； &#x20;
> 95：A, PS:以后不再给vehicle\_id加segNN，subtrack唯一权威partition是subtrack\_index
> 96：A,保存XY和lon/lat，后续方便计算

解释范围：阶段容器、构建可写/下游只读、保留整车读取、缓存 XY 已明确。
Q95 另明确更正车辆命名与子轨迹分区，以该更正为准，不再保留旧后缀建议。
未确认完整设计，未执行代码实施或派生数据清理。
正文同步：顶部进度、第 5.6.3、5.6.4、7.1、8.1、9.4 节及 Q93–Q96 当前解释。

### 第 24 轮后续补充 — 主代理与子代理职责（2026-09-26）

关联原记录：第 24 轮 Q93–Q96 合并回答后收到的独立消息。
用户补充原文：

> astra，你是我的助理，而不是执行者，详细的repo detect、coding和review都交给subagent(gpt-6-sol-low)，并且允许subagent唤起(gpt-6-luna-high)进行必要的并行任务

当前解释：主代理协调、访谈、汇总；详细调查、编码和审查由 gpt-6-sol low 承担，
必要时可派 gpt-6-luna high 并行。本条未授权立即代码实施，未回答配对规则，
也未确认完整设计。正文同步：第 9.4 节。

### 第 25 轮 — 取消车辆名后缀后的跨轨迹配对（2026-09-26）

发问时前提：Q90–Q96 已回答；Q95 要求保留原 vehicle_id，subtrack_index 为 Run 内
唯一权威子轨迹分区。按 vehicle_id 排除同 rank 的配对受取消后缀影响，需明确规则。

#### Q97 — 同车不同子轨迹是否参与跨轨迹求交

> 取消 _segNN 后，同一辆车的不同子轨迹能否参与跨轨迹求交？A. 只要求 subtrack_index 不同，允许同车不同子轨迹求交；B. 还要求原始 vehicle_id 不同，同车所有子轨迹相互排除。两项都不允许同一子轨迹内部参与该跨轨迹配对。

> 推荐 A：与 subtrack 唯一 partition 一致，并延续旧后缀实际区分子轨迹后的参与范围。

> 用户回答：Q97 只要求subturack\_index不同

正文关联：第 5.6.5、7.1、8.1、9.4 节。
当前解释：原文 subturack_index 的拼写解释为 subtrack_index；已选择 A。
同 Run 内仅要求 subtrack_index 不同，允许同原始 vehicle_id 的不同子轨迹参与，
不允许同子轨迹内部配对。

本轮检查：Q90–Q97 已回答，编号连续且唯一；第 24 轮后续补充原文独立
保存，未冒充 Q97 回答。正文与当前决定一致，完整设计尚未确认；未修改运行代码、
未运行数据任务、未删除数据；QA 附录仍位于文末。

### 第 25 轮发问前后续补充 — 子代理默认 Fast（2026-09-26）

关联原记录：第 24 轮主代理与子代理职责补充；本条不是 Q97 回答。
用户补充原文：

> 补充，所有subagent默认fast模式

当前解释：所有子代理默认要求 Fast，模型与推理强度不变（sol low、按需 luna high）。
本工具会话无法显式启用或验证 Fast：spawn_agent 及其他可用工具无 Fast/service_tier 控制，
也无自动继承证据；
不能记录为已成功启用，也不能把 low 推理强度当作 Fast。
正文同步：第 9.4 节；发问时 Q97 待回答，现已由后续回答收敛；完整设计尚未确认。

### Q97 回答收录 — 跨轨迹配对决定（2026-09-26）

用户回答完整原文：

> Q97 只要求subturack\_index不同

当前解释：保留原文拼写；解释为 subtrack_index，选择 A。正文同步：顶部进度、
第 5.6、7、7.1、8.1、9.4 节及 Q97 逐题记录。独立技术决策已收敛，待 Q98 最终设计确认。

### 第 26 轮 — 点对象重构完整设计确认（2026-09-26）

发问时前提：Q90–Q97 已回答，独立技术决策已收敛；以下题目明确区分设计确认与实施授权；未回答前不启动实施，不表示验收通过。

#### Q98 — 本次重构完整设计确认

> 完整共享设计：
> 1. 全链路逐点与逐事件同时列式化，覆盖 models.py 全部 60 类及文件外逐项包装；采用少量阶段容器，NumPy 坐标为 (2,N) float64，运动、来源键、kind、Refine/resolved ID、顺序、锚点与有效性按消费者所需存列。
> 2. SubTrack 持有整轨数组，不含 Point/Segment 实例链；Segment 为最小几何计算单位，用相邻点索引或网格局部端点列批表示。删除点/引用/端点包装、无生产者 InOutIntersectionPoint、仅测试 _DictModel 等；简化 Refine 重复结果/索引，保留低频配置、报告、资源容器和 Centerline/Network 几何共享。
> 3. 构建期间可写，交付下游只读；连续范围可共享视图但不保证每处零复制。Refine 保留整车读取，内部子轨迹数组化。
> 4. vehicle_id 保留原值，不追加 _segNN；subtrack_index 为 Run 内唯一权威 partition。grid/Parquet/内存路径统一排除同 subtrack_index，同原始 vehicle_id 的不同子轨迹允许求交，禁止同子轨迹内部跨轨迹配对。
> 5. Clip 内点使用连续范围，端点使用索引与 kind/crossing/intersection 元信息；Clip→Fitting 临时关系列式保存 XY、lon/lat 和关联 ID。事件成员使用 kind + calculation_index，member_order 独立；crossing 使用 cluster index + crossing index。
> 6. 直接迁移旧类及全部仓库调用方，包括 Pipeline/Backend 回放与前后端边界，不保留 Point API 兼容层；保留既有外部响应字段、命令行、方向/输入顺序、边界可空及回放功能。
> 7. 仅 trajectory_points、day01 源表必须保留；旧派生可抛弃，以新数据为准。不作容差检查或旧输出比较；除已确认的车辆/子轨迹身份与配对更正，其余算法规则保持。源追溯、身份关联、顺序与锚点仍需正确。
> 8. 可分 commit：先容器/身份契约，再 Parser/Refine 与写出，随后 Detect 事件/拓扑/候选关系，再 Clip/Fitting 和回放，最后清理旧类。实施需 make test、独立功能/身份/边界/回放验证；Pipeline 冒烟配置为 services/pipeline/config/pipeline-roadnet-test.toml ，全量配置为 services/pipeline/config/pipeline.toml ，记录实际配置。性能与资源标准沿用本 Spec 既有基线，在生产环境测量；代码审计不表示通过验收。
> 9. 主代理仅协调、访谈、汇总；gpt-6-sol low 执行详细调查、coding、review，必要时可调用 gpt-6-luna high 并行。所有子代理默认要求 Fast，但本工具会话无法显式启用或验证，不把 low 推理强度当作 Fast。
>
> 是否确认上述为本次重构完整设计，并选择实施范围？A. 确认完整设计，并授权指定子代理按方案分 commit 实施；B. 仅确认设计，暂不实施；C. 指出需要更正的条目。A 同时授予设计确认和实施授权，B 仅确认设计；两者均不构成验收通过。

> 推荐 A：完整设计已收敛，可进入实施；选 B 也完成设计确认，但不启动实施。

> 用户回答：待回答

正文关联：第 5.6、7、8.1、9.4、10 节。
当前解释：后续两条原文构成 C 类局部更正；实际用户回复的第 2–8 项已采纳，
第 1 项 shape 暂定并要求主要几何内核审计。原问题和推荐作为历史保留。
用户未明确选择 A，当前仅授权布局审计与 Spec 修订，未获立即编码实施授权。

本轮检查：Q19–Q98 连续且唯一；Q90–Q97 已回答，Q98 待回答；历史 QA 保留，
配对规则已同步，协作补充独立记录；QA 附录仍位于文末。未运行数据任务或删除数据。

### Q98 后续更正（一）— 暂定布局与第 2–3 项采纳（2026-09-26）

关联原记录：第 26 轮 Q98；这是独立收到的第一条消息，原问题与推荐不覆盖。
用户完整原文：

> **SubTrack 采用列式 ndarray 存储；XY 的权威内部布局暂定&#x20;****`(2,N)`****&#x20;/&#x20;****`float64`****。Segment 由相邻索引&#x20;****`i → i+1`****&#x20;定义，不创建逐 Segment Python 对象。算法如需要&#x20;****`(N,2)`****，优先通过转置 view 提供，不复制数据。**
> 至于最终到底选 `(2,N)` 还是 `(N,2)`，我反而建议在正式实施前让 repo 审计重点检查一次：**Refine / Detect / Clip / Fitting 当前所有主要几何 kernel 到底哪种 shape 用得更多**。如果绝大多数现有 NumPy/几何接口天然吃 `(N,2)`，为了 `(2,N)` 不断 `.T` 虽然通常不复制，但也未必值得把 `(2,N)` 定成整个 Pipeline 的规范。
>
> 2、采用
> 3、采用

当前解释：列式 ndarray 与 float64 保持，(2,N) 只是暂定布局；正式实施前需审计
四阶段主要几何内核的实际 shape 需求，最终契约及适配规则待审计结果。实际用户面前
的第 2 项为 Segment 相邻索引/局部列批，不建对象链；第 3 项为构建可写、下游只读及
Refine 整车读取，两项已采纳。本条没有明确选择实施授权。
正文同步：顶部进度、第 5.6.3–5.6.5、8.1、9.4、10 节；不改写 Q98 原题历史。

### Q98 后续更正（二）— 第 4–8 项采纳与修订要求（2026-09-26）

关联原记录：第 26 轮 Q98；这是随后独立收到的第二条消息，不并入第一条。
用户完整原文：

> 4、采用
> 5、采用
> 6、采用
> 7、采用
> 8、采用
> Q98 按照我说的小范围修改一下

当前解释：按实际用户面前的八项汇总对应：第 4 项为原 vehicle_id、Run 内唯一
subtrack_index 分区及不同子轨迹求交；第 5 项为 Clip 端点索引元信息和 XY/lonlat/ID；
第 6 项为删除逐点/引用/端点/无生产者包装、简化 Refine 结果，保留配置/报告/资源与
共享几何；第 7 项为全部调用与回放迁移、无 Point 兼容层、保留外部字段功能；
第 8 项为保护源表、旧派生可弃、不作容差/旧输出对比、分 commit、指定 sol low
实施审查（可派 luna high）、功能与配置性能验证。上述均已采纳。
Spec 原 Q98 将这些内容分为九项，因此按语义对应，不机械按原题数字错配。
整体属于 C 类局部更正，未明确选择 A 实施授权；只开展布局审计与 Spec 修订。
正文同步：第 5.6.6、8.1、9.4、10 节及 Q98 当前解释。

本次检查：两条用户原文独立保存，HTML 转义、拼写和标点保留；Q98 原题和原推荐
未覆盖。正文布局仅暂定，待本轮审计结论；修订后的确认题尚未发问，未开始编码。

### Q98 更正后的审计结果 — 布局建议（2026-09-26）

只读审计已完成，详细矩阵和限制见第 5.6.7 节。生产二维 XY ndarray 的三个内核族
均天然使用 (N,2)；其余标量/独立一维列没有二维布局偏好。因此推荐将暂定 (2,N)
改为权威 (N,2) / float64，而非宣称用户已采纳。新构建数组默认 C 连续，基本切片
可非连续，下游只读；需要另一 shape 时优先转置 view。未测性能，不宣称全链零复制。
正文同步：顶部进度、第 5.6.4、5.6.7、9.4、10 节；Q98 原题及两条更正继续保留。

### 第 27 轮 — 布局小范围修订与实施范围确认（2026-09-26）

发问时前提：Q98 第 2–8 项已采纳；第 1 项布局暂定并要求审计。
审计推荐 (N,2)，不能替代用户决定；没有开始运行代码实施。

#### Q99 — 确认布局修订并选择实施范围

> 主要几何内核审计已完成：生产批量 XY ndarray 的跨轨迹求交、Topology 圆求交、Fitting 三个内核族均天然使用 (N,2)；Refine、Clip、转向/G*/DBSCAN 的标量或独立列不计作二维布局偏好。推荐将权威 XY 从暂定 (2,N) 调整为 (N,2) / float64：新构建数组默认 C 连续，下游只读，基本切片允许非连续 strides；接口需要 (2,N) 时优先转置 view，必要算法工作区仍可分配，不承诺全链零复制。其余已采纳的第 2–8 项保持不变，包括 Segment 相邻索引/局部列批、Refine 整车读取、子轨迹分区与求交规则、Clip 双坐标与端点元信息、模型清理、调用/回放迁移、源表保护和验证标准，以及指定子代理分 commit 的协作方式。
>
> 是否确认这次小范围修订，并选择实施范围？A. 确认修订后的完整设计，并授权 gpt-6-sol low 按方案分 commit 实施和审查，必要时可派 gpt-6-luna high 并行；B. 仅确认修订设计，暂不实施；C. 指出需要更正的条目。A 同时授予设计确认和实施授权，B 仅确认设计；均不构成验收通过。Fast 仍为默认要求，当前工具无法显式启用或验证。

> 推荐 A：生产内核接口支持 (N,2) 作为规范，其他决定已采纳，可进入实施；选 B 也完成设计确认，但不启动实施。

> 用户回答：Q99 A 确认修订
> 开始按计划实施

正文关联：第 5.6.7、8.1、9.4、10 节。
当前解释：用户明确选择 A，设计已确认且实施已授权；权威布局为 (N,2) / float64。
按计划开始指定子代理的分 commit 实施，尚未通过验收。

本轮检查：Q19–Q99 连续唯一；Q98 原题、推荐及两条更正保留；Q99 已回答并选择 A。
审计推荐与用户决定、shape 与内存顺序、已确认分配与未测性能明确区分。
只更新 Spec，未修改运行代码、未运行数据任务、未删除数据。

### Q99 完整回答 — 设计确认及实施授权（2026-09-26）

用户完整原文：

> Q99 A 确认修订
> 开始按计划实施

当前解释：完整修订设计已确认，立即开始按计划实施的授权已明确。
正文同步：顶部进度、第 5.6、7、8.1、9.4、10 节。实施清单初始全部待做；
设计确认、实施授权与验收结果分开记录。本次仅完成 Spec 收尾，没有编辑代码或提交。


### Q99 实施记录（2026-09-26，持续更新）

- 已提交 `b12b960 refactor: introduce stage columns and refine vehicle arrays`。该逻辑提交包含模型、Parser、整车 Refine、投影及 Refine 持久化入口；完整工作树验证依赖后续 Detect、Clip、Fitting、回放迁移，不能把工作树通过结果理解为该提交独立完整验收。
- 已完成生产逐点/逐事件类删除及调用迁移；现有权威 XY、lonlat 为 float64 `(N,2)`，来源整数身份与有效性 mask 独立，保留 `id::text` 同时间排序。Refine 采用整车列批、预分配注入工作区及 Run 内统一 subtrack_index；Detect 的配对与拓扑使用同一 partition 身份，Clip/Fitting 复用 XY。
- 独立审查已覆盖来源 0/-1、注入锚点、CSV 原来源 hash/extra、只读交付、批次身份 dtype/列长/数组类型及 JSON 回放字段；发现的问题在原业务规则下修复，不采用新旧产物差分或输出容差作为验收。
- 初始 Pipeline 测试库存为 1082 项（实施前基线与初始修改快照核对）。原 Detect 99 个测试函数逐项映射并恢复明确预期覆盖；删除逐点模型构造接口的测试改为相应列批结构或边界行为测试。完整映射正在最终审查，尚不标记全部验收完成。
- 首轮不忽略任何文件、真实 PostgreSQL 环境的 `make test`：Backend 84 passed；Pipeline/CV 1154 collected，1153 passed、1 failed、零 skipped。唯一失败是 PostGIS 测试残留逐点访问，已改为来源 key/mask 与权威 XY 列断言；其所在真实 PG 受影响组 25 passed。共享非数组列结构入口修复后，列批/Refine/Clip 回放/Detect 边界受影响组 50 passed。
- 本次正式冒烟使用 `services/pipeline/config/pipeline-roadnet-test.toml`，全量使用 `services/pipeline/config/pipeline.toml`；尚未执行时不记录运行成功、耗时或内存达标。最终实测将固定代码 revision，记录新容器 Pipeline cgroup 峰值、完整 CLI 耗时，并单独报告 PostgreSQL 临时文件及磁盘变化，保护源表只读。

- 最终完整 `make test`（无忽略文件，真实 PG）：Backend 84 passed；Pipeline/CV 1181 passed、零 skipped，75.81 秒，8 项既有 Rasterio 弃用提醒。最后完整 patch 的资源释放修复在套件收集后补入，另外执行 25 项受影响测试通过，并经独立弱引用测试 1 passed 确认；两次结果分别归因。
- 已固定 runtime 提交 `10f27a6 refactor: complete columnar pipeline and replay contracts`（93 个文件，完整可导入运行的模型 API 迁移同时覆盖 Backend）。测试覆盖映射见 [Q99 Detect 测试覆盖迁移映射](validation/columnar-detect-test-mapping.md)。外部既有 Makefile、README、Dockerfile、CI 修改未纳入本任务提交。
- 1M 正式冒烟已启动：源 `public.day01`，配置 `pipeline-roadnet-test.toml`，Refine ID `0b31582d-0f2f-4a55-b08e-34dc50e8573e`，runtime revision `10f27a6`。结果尚未完成，不表示冒烟或性能验收通过。


#### 首次 1M 诊断运行结果（runtime `10f27a6`）

- 配置 `pipeline-roadnet-test.toml`；源 `public.day01`，只读源 count 前后均为 1,000,000。Refine `0b31582d-0f2f-4a55-b08e-34dc50e8573e`，Detect `a886bd50-c318-4397-91b7-941bac629f21`。
- CLI 完整耗时 921.782 秒，返回码 1。Detect 计算与写出后，最终事务被旧数据库 completion guard 的同 vehicle 排除规则拒绝；未进入 Clip/Fitting/Network，不属于功能或性能验收成功。
- Fresh Pipeline cgroup memory.peak 1,999,998,976 字节（请求 2,000,000,000 字节，内核按页向下取整），swap 0；memory.max 压力事件 274，OOM 与 oom_kill 均 0。该计量包括 wrapper、Pipeline 子进程与该容器计入的缓存，不等同主进程 RSS。共享 PostgreSQL 的历史 cgroup peak 另采样，不作为本 Run 独占峰值。
- 共享 PG 数据库计数 delta：temp_bytes 2,703,799,187 字节，database_size 644,202,496 字节；临时文件计数 8,288→13,101。这些数据库累计计数可能包括同库并发活动，不声称严格独占归因。
- 现有日志：Refine 读取精炼 47.7 秒、1,598 车辆，kept 614,879、excluded 385,121、injected 999；cross-grid 1,131 个任务；turn 56,178，hotspot 10,105，cross 58,655，candidate crossing 83,947，clusters 393，complete intersections 292（事务完成前计算结果）。
- 本次作为诊断运行进行了隔离 helper 容器的三次短时只读 py-spy stack dump，未向被测容器安装包或修改源码。前两次定位到 CrossCandidateRelation 的 ndarray object→DuckDB register/INSERT 与 importlib 探测，第三次定位到 topology CrossingBatch 构造；因此本次数字不作为修复后新 revision 的最终性能验收。
- 实证收尾项：RefineReader/topology Reader 仍保留整轨 tuple 中间图，改为有序有界驱动批次立即列化与列 chunks；DetectDatasetReader 去掉新引入的每轨查询；Turn/Cross 参数列写入保留独立 NULL/NaN mask，避免 object 扫描。数据库派生校验只排同 Run subtrack，允许同原车不同 partition；保护源表不改。

- 首次失败后的派生数据库 guard 修复：只保留同 Detect Run 的 `subtrack_index` 相同即拒绝，删除原 vehicle 相同 OR 及四个无必要来源表 JOIN。真实 PostgreSQL completion/schema/migration 受影响组 45 passed，新增同原车不同 partition 提交通过、同 partition 拒绝；独立审查确认 endpoint 的 Detect/Run 外键与 owner、连续 segment 校验保留。
- 已向 `roadnet_test` 与 `roadnet` 仅执行该函数的 `CREATE OR REPLACE FUNCTION validate_detect_run_completion()`，没有 reset、没有对源表 DML。新建库与重复 `init-db` 使用相同正式 DDL；物理表 schema version 8 不变。

- 首次诊断后的读取结构修复：RefineReader 与 topology Reader 一条有序 SQL 读取、bounded driver batch 立即列化，只保留跨 batch 的各列 chunks；不再保留整轨 row tuple 图。原 32 函数 43 cases 加跨 batch/readonly/释放证据共 48 passed。DetectReader 恢复单条有序读取，消除重构新引入的按每轨查询；两个 reader 经独立交叉审查无开放 finding。
- DuckDB 关系 I/O 独立微基准（4096 行宽 Cross 全字段、batch 1024、两轮）：原 register 26.195852/24.143485 秒；直接 typed nested-list 参数 32.502882/36.168193 秒，因此未选择该更慢路径；最终 bounded typed-column scalar JSON 参数 `from_json`/`unnest` 0.125571/0.118719 秒。微基准只代表传输边界，不等同全链路性能。
- 最终正式关系写入仍以 ndarray 为 Pipeline 内存模型；每个有界写入批次暂时序列化每列，不构造或保留逐事件 row。所有用户值与列 schema 用驱动参数传递，SQL 标识符来自固定 dataclass 字段。NULL 与有效 NaN 使用独立 mask 恢复，不与业务 valid mask 混用；BIGINT 极值/0/-1、Unicode/空串/NULL、正负 Inf、IEEE signed zero 实证保真，事务失败无前缀写入与计数回滚保留。真实 PostgreSQL 11 个受影响测试文件 259 passed、零 skipped，独立审查闭环，无新增依赖。

- 集中修复后的完整 `make test`（无忽略文件、真实 PG）：Backend 84 passed；Pipeline/CV 1195 passed、零 skipped，71.88 秒，8 项既有 Rasterio 提醒。固定 runtime 修复提交 `e85bc83 fix: stream columns and validate Run partition identity`；新 1M 正式验收已启动，未进行 helper stack 采样干预，Refine ID `b3c526aa-a054-4ff1-bc63-533c9be33162`，完成前不标记成功。

- `e85bc83` 的 1M 运行因只读审查确认尚有重复 XY 投影及必要身份/CSV 边界修复，主动有序终止，属于诊断运行，不是验收成功。只中断本任务 Pipeline 子进程并取消已定位的 Detect COPY 会话，未停止共享服务；计量 wrapper 完整记录 1240.209719 秒、返回 -2、Pipeline 控制组峰值 1,999,998,976 字节、max 631、OOM/OOM kill 均 0，`public.day01` 前后均 1,000,000 行。运行未完成 Detect Parquet 导出，尚未进入 Clip/Network，最终 stats 为空；记录 `columnar-e85bc83-smoke.json`、日志 `columnar-e85bc83-smoke.log`。
- 后续集中收口包含 Refine Parquet 原生 BIGINT 来源 key、Backend 外部字符串 key 对 BIGINT 的参数绑定、实际 CSV 导入入口与非 UTF 编码猜测、Clip 已证实的列参数驱动成本，以及 Detect 导出复用 Refine 权威 XY。独立 PG 回放遗留模块 `query_replay_inputs.py` 的两个函数全仓库无调用或导入，删除该未使用 API，实际 CLI/Backend 回放保留；外部 JSON 的字符串 key、同时间 `id::text` 排序及 v1 指纹文本编码保持。
- 主动终止的 Detect Run `e91895d3-3b14-47bf-ae83-4890ce00ef44` 已仅按正式结束语义标记 failed，避免清理中断留下 running 状态；未删除已记录的诊断派生证据。
- 集中来源身份审计：Refine Parquet schema/export/Reader 与 PG grid 内部 key 统一 BIGINT，注入来源 key 用 NULL 与独立 Refine ID 表达；Backend 外部字符串 key 参数绑定为 BIGINT[]。保留的文本转换仅用于外部 JSON 回放字段、可选 lineage 诊断 JSON 与 v1 来源指纹/同时间排序。实际 CSV 导入恢复 bounded COPY 写入数返回，不建立逐点 ID 映射；编码猜测使用明确 runtime 默认值。真实 CSV Parser→CLI→私有临时 PG 表验证多车辆、extra 与关闭，受影响 Parser/回放/模型组 53 passed；Backend 真实临时 PG key 0/-1 与原 Map 组 16 passed。
- 最后权威 XY 链收口：schema v9 新增 kept 派生 x/y，新 Refine writer 写入算法 track.xy，Refine export 直接读取；v8→v9 仅新增 nullable 派生列，历史数据不 backfill、不重新投影 fallback。两库 `roadnet_test`/`roadnet` 已通过 `init-db --migrate-artifacts` 升级。真实 PG 升级/写出/非投影 XY 导出 25 passed；PG Clip replay reader 同样复用 kept.x/y，并以有界 driver 数组列 chunks 代替整轨 row tuple 图，权威 XY、跨批次释放与早关闭组 13 passed，最终 streaming 组 20 passed。
- 固定集中 runtime 提交 `fbc2c41 fix: preserve native identity and reuse authoritative coordinates`（34 个本任务文件，仅增量 stage Backend 来源键两处 SQL）。最终 Pipeline/CV 完整套件 1210 passed、零 skipped，76.55 秒、8 项既有 Rasterio 提醒。原 `make test` 的 Backend 阶段 84 passed、1 failed：外部并发 centerline 回放新增字段与原 `test_streaming_api_fields` 预期未同步，本任务未修改或提交该外部实现/测试；执行 make 同等 Pipeline/CV 命令完成上述全绿。本任务 Backend 真实 PG/Map 精确组 16 passed。
- 最新 1M 冒烟已按固定 `fbc2c41` 启动，源 `public.day01`、配置 `pipeline-roadnet-test.toml`，Refine ID `49e953ef-4046-490f-8ad9-b7165d86c065`，尚未完成时不记录成功。状态清单与计量路径已落盘，Pipeline 新控制组与共享 PG 分开计量；26M 全量验收待实际运行。

#### 恢复后验证与全量计量（2026-09-27）

- `fbc2c41` 的受控 1M 诊断在 Detect 导出阶段主动终止，耗时 1069.416285 秒、返回 2；源 `public.day01` 前后均 1,000,000。Pipeline cgroup peak 为 1,999,998,976 字节，max 496、OOM/oom_kill 0、swap 0。不是完成验收。独立 DuckDB EXPLAIN 确认 kept 条件与 ID 等值并列的 LEFT JOIN 使用 BLOCKWISE_NL_JOIN；等价地把 kept 条件折入 nullable 左键后使用 HASH_JOIN LEFT。
- 用户另行完成的 1M 日志 `20260926_234453_620938_pid-1_log.txt` 五阶段均 completed，总耗时 1905.410 秒；Refine `eb80a2f6-7105-48b5-bb01-accbbc344356`、Detect `de0cfdd1-1586-4be9-9c2a-e3069a80e31d`、Fit `867af27e-ab31-4c20-9ee2-4536f9f7a238`，Network 292 nodes/556 edges。复用该功能通过证据，不重复整套 1M；它发生在 JOIN 修正前且日志没有 revision/config 路径或 cgroup/swap/OOM 指标，不作为修正后性能或内存验收。
- 最小修复提交 `6e5b2cd fix: hash join authoritative kept coordinates`：仅改 kept 身份 JOIN 的等价表达式，保留非 kept 同 ID 不关联及缺失权威 XY 拒绝。真实 PG 导出精准组 13 passed，追加计划回归所在组 12 passed；独立私有 mixed-kind 256 行 old/new 全字段精确一致，计划从 BLOCKWISE_NL_JOIN 变为 HASH_JOIN，微量 probe 不外推全量收益。
- 恢复后执行 `make test`：Backend 84 passed/1 外部 centerline 字段 fixture failed；没有修改外部契约。随后执行相同完整 Pipeline/CV 命令，1239 passed、零 skipped、72.44 秒、8 项既有 Rasterio 提醒。计划回归在该套件收集后新增，另行精准验证通过，分别归因。
- 26M 全量已启动：源 `public.trajectory_points` 精确 26,000,000 行，配置 `pipeline.toml`，显式 `--refine-resume-last false` 确保 fresh Refine；任务提交 `6e5b2cd` 加工作区用户 reuse 等既有 dirty，不能称纯 commit checkout。测量 wrapper 记录额外 CLI 参数，dirty 摘要独立保存；Pipeline fresh cgroup 请求 2,000,000,000 字节、有效 1,999,998,976、swap 0，PG 共享服务单列。实际结束前不标记 26M 时间/内存验收通过。


### grid 预算修订规划轮（2026-09-27）

用户最新要求（根协调器转述）：本修改先计划，并与用户 grill。完整逐字用户消息
未传给本文维护者，故不伪造引文。当前仅事实查证与文档维护；不继续生产修改、提交
或重启 26M。该轮记录时，5 文件 WIP 尚未提交：取消总 overflow 配对工作量拒绝，
membership 超限复用全 finite overflow 流，及对应测试；35 focused cases 已正常收尾通过，
不构成新方案批准。`6e5b2cd` 的 26M 诊断已安全终止，316.391142 秒、返回 -2，
peak 141,770,752 B、max/OOM 0、swap 0；源前后均 26,000,000 行，不属于完整验收。

#### Q100 — “尽量完成每 grid”的覆盖范围

> A：覆盖配对工作量、membership 索引、整 grid 输入、候选及去重累计四层；超限切换处理策略，而不是因预算直接失败。（推荐）
>
> B：仅覆盖已复现的配对工作量与 membership 两层，其他层保留硬拒绝。

推荐 A；分块或落盘的具体策略依本题答案在下一轮讨论，当前不预先批准实现。

> 用户回答：全部采用推荐

正文关联：第 7.3 节。当前解释：采纳 A，四层均纳入完整处理方案；具体策略及实施授权仍待后续。

#### Q101 — 超过 1 小时的运行策略

> A：继续完成，并明确记录性能目标未达标。（推荐）
>
> B：1 小时时终止，并标记失败。

推荐 A；两项均保留既有 2 GB/swap 0 和精确完整规则，不将超过 1 小时写为性能通过。

> 用户回答：全部采用推荐

正文关联：第 7.2、7.3 节。当前解释：采纳 A，超过 1 小时继续完成，性能目标未达标须明确记录。


### 本轮合并回答 — 对应 Q100–Q101（2026-09-27）

> 用户回答原文：全部采用推荐

采纳 Q100=A、Q101=A；仅确认上述设计选择，不推定恢复实施授权。

### 候选结果与全 Run 去重的只读规划事实（2026-09-27）

以下为建议，不是已确认实施方案。当前 `CrossCandidateRelation` 具有原生字段 schema、
有界 typed-column JSON 参数写入、事务失败计数回滚、sealed 批量读取、临时目录与 close
清理；可以复用业务批次接口，不需要逐事件对象或通用关系框架。当前连接为 `:memory:`，
scheduler 的 worker `found`、全 Run `batches` 与 `seen_pairs` 仍累计，最后 concat/unique/sort；
仅提前 append 到现有关系并不能证明整个结果路径严格有界。

最低改动建议：worker 使用独立磁盘候选关系并分批写入；主关系在受限 DuckDB 运算中按
`from_subtrack_index/to_subtrack_index/from_point_index/to_point_index` 去重和排序，最终连续编号。
重复代表需使用确定的网格顺序与局部顺序，不使用线程完成先后；最终身份顺序保持既有规则。
每 worker 只拥有自己的连接，主线程合并，异常或 early close 关闭 cursor/连接并删除本任务
临时目录，成功关系由阶段所有者消费后关闭。资源失败不发布部分 Run，不能跳过配对。

现有配置可复用：`relation_batch_size=1024`、DuckDB memory_limit/threads、
`duckdb_max_temp_directory_size="50GB"`，及 data_root 下任务独立 duckdb 临时目录。
该 50GB 是 DuckDB 临时文件上限，不自动等同新增磁盘数据库文件或多个 worker 的全 Run
总空间上限；具体容量计量与磁盘空间耗尽时的行为仍需下一轮决策。已定 Q100 四层覆盖、
Q101 超时继续、2GB/swap0、精确完整、不 skip 与普通路径保留，不重复询问。

### 下一轮 — 大 grid 兜底与引擎临时外存预算（2026-09-27）

发问时前提：Q100=A 四层均需完整处理，Q101=A 超过 1 小时继续并记录性能目标未达标；
2 GB/swap 0、全部 grid 完成、精确配对、不 skip、稳定身份与普通路径保持。
本轮只讨论路线及外存容量；当时 WIP 实施暂停，未授权继续。

#### Q102 — 大 grid 的兜底路线

> A：保留普通 grid 快路径；超预算数据交 DuckDB 列式关系，按有界块完整遍历块对并复用原几何 kernel。（推荐）
>
> B：先实现自适应空间子网格削减候选，再以块对兜底；需新增长段跨界覆盖与去重规则。

推荐 A：包含所有跨块配对，保持原精确几何规则；最坏工作量可能很大。
B 增加空间划分及覆盖/去重规则，需要额外验证。用户回答更正显式落盘方向，见下文。

> 用户回答：保留普通快速路径，超预算用duckdb，不要显式调用保存，而是利用duckdb原生的外存特性

正文关联：第 7.3 节。当前解释：普通快路径保留，超预算采用引擎原生外存，
禁止项目显式 save/spool/COPY 落盘流程；具体支持与资源控制待核查。

#### Q103 — 引擎临时外存预算

> A：继续使用现有 50GB 引擎临时外存上限；耗尽明确失败，不自动扩容。（推荐）
>
> B：提高该配置，由用户指定额度。

推荐 A。当前 `duckdb_max_temp_directory_size="50GB"` 仅为引擎 temp 限制，
不覆盖所有 worker 累计或磁盘数据库数据文件，不能称为 Run 级硬 quota。
新增文件及 Run 实际总磁盘峰值需要计量记录，但本轮不引入项目全局配额框架。
目录与恢复粒度沿已有决定，不在本题重复选择。此前草案提出的单 Run 所有工作文件
50GB 硬上限未经验证，已在发问前撤回，不作为用户待选项或已确认约束。

> 用户回答：沿用上限，自动扩容并log内写入扩容事件

正文关联：第 7.3 节。当前解释：50GB 是初始额度；应用自动上调引擎生效
max_temp_directory_size 并记录事件，不声称 DuckDB 原生自动提升该配置。
这里不是物理扩磁盘或修改 Docker 挂载；最终边界、增长规则与重试粒度待讨论。


### 本轮回答原文 — 对应 Q102–Q103（2026-09-27）

> Q102：保留普通快速路径，超预算用duckdb，不要显式调用保存，而是利用duckdb原生的外存特性
>
> Q103：沿用上限，自动扩容并log内写入扩容事件

此回答确认原生外存方向及自动上调要求，不构成恢复编码、提交或重跑授权。
此前“worker 磁盘关系显式保存”建议不是现行决定，须按 Q102 修订；50GB 总 Run
硬配额草案已撤回。只测量实际磁盘占用，不把引擎 temp 限制冒称全 Run 硬 quota。

### 下一轮 — 自动上调的边界与增长规则（待回答）

此前 Spec 未确认对应预留水位、固定最终额度或增长规则。以下只调整 DuckDB 允许的
临时外存上限，不预先占用磁盘，也不扩大物理磁盘。SQL 失败后的重试粒度另待事实核查，
不在本轮预设。

#### Q104 — 自动上调的最终边界

> A：不另设固定最终额度，按临时目录所在文件系统可用空间和预留水位约束上调；达到水位明确失败。（推荐）
>
> B：配置固定最终上限；达到上限失败。

推荐 A。预留水位数值在选择 A 后再讨论，当前不擅定；选择 B 后需明确最终额度。

> 用户回答：最终边检 200G

正文关联：第 7.3 节及 Q103。当前解释：按语义记录最终边界 200GB，
不是采纳预留水位建议。配置作用域待下一轮澄清，不能默认为 Run 全局硬 quota。

#### Q105 — 额度增长规则

> A：额度倍增，50→100→200GB，受 Q104 最终边界约束。（推荐）
>
> B：每次增加 50GB。

推荐 A。这里提高允许的引擎 temp 上限，不预先申请或占用相应磁盘容量。

> 用户回答：倍增

正文关联：第 7.3 节及 Q103/Q104。当前解释：50→100→200GB，达到最终额度
不能再自动增长；不预占磁盘，不修改物理磁盘或 Docker 挂载。

#### Q106 — 提高额度后的重试粒度

> A：保留已提交输入或当前有界批次，必要时 ROLLBACK，提高额度后只重放失败 SQL/批次，不重复交付已完成批次。（推荐）
>
> B：清理当前 grid 中间结果后整 grid 重算。

推荐 A。DuckDB 不能从 operator 内部断点续算，这里是当前 SQL 或有界批次的重放。
普通失败 Run 的整阶段重跑约定保持；额度上调是尚未判定 Run 失败的内部可恢复事件，
不等同自动重跑已失败 Run。Q104/Q105 已回答；Q106 后续确认受限 A，见本题追加回答。

> 用户回答：啥意思，说清楚批次和grid
> 用户追问：我感觉A更合理，但是保留每个batch批次，会不会过度设计了，如果要实现A需要付出什么代价？需要设计什么机制？
> 用户最终回答：同意，只处理duckdb额度耗尽的case

正文关联：第 7.3 节及 Q103–Q105。当前解释：在成本说明后确认受限 A：仅 DuckDB 临时外存额度耗尽触发扩容与安全重放；当前有界输入的短暂持有不等于保存历史批次或持久 checkpoint。


### 本轮回答原文 — 对应 Q104–Q106（2026-09-27）

> Q104：最终边检 200G
>
> Q105：倍增
>
> Q106：啥意思，说清楚批次和grid

Q104 按语义记录最终额度 200GB；Q105 为 50→100→200GB。按文件系统剩余空间与
预留水位设置最终边界的推荐没有被采纳，不保留为用户决定。200GB 的配置作用域仍需
澄清，不能推定为 Run 全局磁盘硬 quota。Q106 当时仅请求解释，后续回答确认受限 A。

#### Q106 的术语与例子说明（后续确认受限 A）

- grid 是一个地理范围的任务；一个 grid 内可包含许多 segment 配对。
- pair batch 是有界的候选 segment 配对计算批，区别于数据库输入 fetch 批，
  也不必与写入事务一一对应。
- 推荐恢复粒度为失败 SQL 或可重放批次事务：保留已提交输入及完成结果，
  失败事务 ROLLBACK 后提高额度并重放，不重复交付已完成批次。

例如某 grid 按 100 个可重放计算/写入批次完成，第 61 批失败：A 保留已经完成的
1–60 批，在扩容后重放失败操作并继续；B 清理该 grid 中间结果，重新计算整个 grid，
也必须避免重复交付。该例只用于理解，不声称实际失败总发生在 pair batch 边界。
一次排序/去重 SQL 可能覆盖整个 grid，失败后需要重放该整条 SQL，不保证仅重算
固定几条 pair，也不能声称从 SQL 内部或 operator 断点继续。

本轮仍在规划：不恢复生产修改、测试、提交或重跑。


### Q106 追问：A 的最小恢复代价（2026-09-28，仅规划）

> 用户追问原文：我感觉A更合理，但是保留每个batch批次，会不会过度设计了，如果要实现A需要付出什么代价？需要设计什么机制？

用户已确认受限 A，仅处理 DuckDB 临时外存额度耗尽。A 不要求保存每个批次副本，不新增持久 checkpoint 或
恢复表。现有 `CrossCandidateRelation.append_batch` 和 `TurnEventRelation.append_batch` 已把
传入的一个 ndarray 业务批次按配置 `relation_batch_size` 切成有界列，每次建立 JSON 参数，
在一次 BEGIN/COMMIT 内写入，失败时 ROLLBACK 并将 `_count` 恢复到调用前值。
列参数当前只活到该次 SQL 调用；调用者在 append 返回前仍持有当前业务批次。
因此单次 append 的最小重放可以保留当前输入批次到提交成功，失败后回滚，提升额度，
对同一个输入重调 append；已成功提交的前序 append 无需缓存或重播。

代价是要在明确的资源所有者处识别“引擎临时外存额度不足”且仅对此扩容；
失败事务必须先回滚、再在连接空闲时调 `max_temp_directory_size` 并记录旧/新额度、
操作、关系或 grid 身份、失败原因和重试次数。需要保证写入前序批次不会重复交付，
`_count`、`ordinal`、`calculation_index` 与唯一约束在回滚后同步，且 50→100→200GB
超过上限仍明确失败。其他 SQL/数据/真实磁盘不足错误原样失败。

不能把这一逻辑当作统一几行 wrapper：`seal` 的聚合校验、去重/排序 SQL 可能处理
整个关系，失败时需保留已提交输入并重放整条 SQL；scheduler 当前还将 worker 的
`found`、主线程 `batches` 和 `seen_pairs` 累计在 Python 内存，需要先改变结果所有权与
确定性去重边界。`append_batch` 事务包含内部多个 1024 行块，若后半块失败，整个
append 回滚并重放该业务批次，而非只重放最后一个内部块。当前 `:memory:` 关系可用
DuckDB 原生 temp 外存，temp 路径由关系单独拥有；失败和阶段结束仍必须 close cursor、
连接并清理目录。额外内存主要是当前有界 ndarray 批次与该次 JSON 参数，外存与重跑
当前 SQL 的计算量可能增加；没有基于代码证据的统一工时数字。

上述机制/成本解释后，用户确认受限 A；仍未授权恢复实施。

### Q106 受限 A 的读取与交付边界补充（已确认，实施暂停）

这里的受限 A 仅针对 DuckDB 临时外存额度耗尽。现有关系 `append_batch` 的事务、
`_count` 回滚和调用者在返回前持有的当前业务 batch 可复用；既不保存历史 batch，
也不新增持久 checkpoint/恢复表。新增机制是识别特定额度错误，事务 ROLLBACK，
在没有活跃 SQL 的连接上将生效额度按 50→100→200GB 调高并记日志，然后重放当前
业务 batch 或失败的整条 SQL。一个业务 batch 内可能有多个 1024 行写入子片；
事务中途失败应重放整个业务 batch，子片不是独立恢复单元。

PostgreSQL 网格输入使用前向 cursor/generator，已经 yield 给下游的数据不能倒带。
读取中的当前有界输入需持有到相应写入 COMMIT；已提交的输入/结果不重放。
去重和排序这类可能触发临时外存额度的 SQL 必须在向下游交付前完成；已输出的
DuckDB result cursor 不能作为任意位置可续传流。利用现有 grid 完成与阶段发布界线，
不新增任意游标续传、跨进程恢复框架，也不声称 SQL operator 能断点续算。

当前 worker 的 `found`、主线程 `batches`/`seen_pairs` 改为引擎外存，是 Q100
四层完整处理的共同工作，无论 Q106 选 A 或 B 都需要，不能算成 A 独有成本。
若某 grid 在完成前已分批提交中间候选，要隔离其未完成结果，确保失败清理或
确定性幂等去重，不能假定 INSERT 重放天然幂等。受限 A 的专属代价是额度错误分类、
事务边界与计数状态同步、额度日志和安全重放。

待验证语义：多子片 append 中途额度失败后不重复候选且计数连续；已提交结果不重放；
到达 200GB 最终额度时明确失败；非额度错误不得误触发扩容或重试。用户已确认
Q106 受限 A，但未授权恢复实施。


### Q106 最终确认原文（2026-09-28）

> 用户回答原文：同意，只处理duckdb额度耗尽的case

当前解释：采纳受限 A，只针对 DuckDB 临时外存额度耗尽，回滚后按 50→100→200GB
上调生效额度并记日志，再重放当前可重放业务批或整条 SQL。其他 OOM、PostgreSQL、
I/O、几何或数据错误不因此自动重试。此设计确认不等于恢复编码、测试或提交授权。

### 200GB 配置作用域的只读核查（2026-09-28）

现有 `max_temp_directory_size` 在各 `duckdb.connect` 配置中独立设置，
不是 Run 全局额度。正式 Detect 同时创建 TurnEventRelation 与 CrossCandidateRelation
两个独立 DuckDB 连接，各有独立 temp 目录；其他 Refine/Detect 导入导出、关系回读、
Clip 输入及制品操作也各建连接。当前网格 worker 使用 PostgreSQL 连接，尚无 DuckDB
每 worker 关系；若 Q100 方案为 worker 建关系，实例数和并发容量将增加。

最小选择是沿引擎原生的每实例 cap：每个发生额度耗尽的实例从 50→100→200GB，
现有连接生命周期与隔离目录可复用，日志逐实例记录。代价是并发连接的实际 temp
占用之和可以超过 200GB；测量需另记录 Run 实际总磁盘峰值。若 200GB 意指 Run
共享总 cap，当前多个独立连接仅分别 SET 200GB 无法达成；实现前可查单 Run
共享 DuckDB 实例、独立游标等引擎原生策略，或另行协调额度，不能预断唯一方案
是自研全局 quota 框架。

Q107 后续确认每实例分别设置，不采用 Run 合计额度；本段保留发问前的只读取舍说明。其他未决项若只是
异常分类、单 SQL 的重试实现、资源清理与测试，按已确认语义由实施者解决，
不另要求用户决定。当前 5 文件预算变更已进入 HEAD；后续实施仍暂停。


### Q107：200GB 的作用域（已回答）

发问时前提：Q100 四层完整处理、Q101 超时继续并标性能未达标、Q102 DuckDB
原生外存、Q103 初始 50GB 自动上调、Q104 最终 200GB、Q105 倍增、Q106 仅
DuckDB 额度耗尽的受限 A 均已确认；本题只确认额度语义，不授权实施。

#### Q107 — 200GB 上限约束哪个范围

> A：每个独立 DuckDB 实例各按 50→100→200GB 增长；复用引擎原生 cap，单 Run 实际合计可能超过 200GB。（推荐）
>
> B：整个 Run 的全部 DuckDB 工作区合计最多 200GB；需要调整共享实例或统一额度协调，不能只给每个现有实例设置 200GB。

推荐 A，保持当前各关系独立 owner/连接边界。当前代码存在多个独立 DuckDB 实例，
其 `max_temp_directory_size` 不能自动形成 Run 总额。若用户选 B，须再核可否使用
单 Run 共享 DuckDB 实例、独立游标等引擎原生策略；不预断必须自研全局 quota。
block size、SQL、内存切分与重试实现是工程细节，不再作为本题用户语义选择。

> 用户回答：按推荐，每个实例分别设置临时额度，不要合计

当前解释：采纳 A。每个独立 DuckDB 实例从 50GB 增至 100GB、200GB 封顶；
不设置 Run 合计配额。逐 owner 记录扩容事件，Run 实际总磁盘峰值仍测量报告，
不将其用作 200GB 总量拒绝。完整实施方案与恢复实施授权仍待最终确认，
当前 5 文件预算变更已进入 HEAD；后续实施暂停。
正文关联：第 7.3 节及 200GB 配置作用域的只读核查。


### Q107 回答原文（2026-09-28）

> 用户回答原文：按推荐，每个实例分别设置临时额度，不要合计

采纳每实例 DuckDB 原生 temp cap；50→100→200GB 每个实例独立生效，
不设单 Run 合计拒绝。总磁盘实际占用只作测量与报告，不当作共享硬额度。

## Q100–Q107 现行实施计划（2026-09-28，已获恢复授权）

本节是实施时优先遵循的现行正文，后文 QA 与独立审核保留历史过程。用户已确认
Q100–Q107、提前切换、不统一内存安排、精确 DBSCAN 减少冗余列/复制、删除两处
`event_identity` UNIQUE ART，并授权恢复实施。5 文件预算变更已在历史提交
`3c777b6`，仅覆盖部分预算层；本节和第 7.3/7.2 节共同定义当前目标与验收。

### 行为和资源边界

1. 普通 grid 快路径及原几何 kernel、交叉角/直线规则、同 Run subtrack 身份排除、
   交点精确计算、规范 pair 方向和稳定最终排序保持。网格状态仍需全部完成才可结束
   Detect；不跳过困难网格，不以已有派生结果或旧输出容差作为正确性 oracle。
2. 配对总工作量是计算量，不是驻留内存：超过原 20M 工作量阈值继续使用现有
   有界 pair patch 流，保留估计与进度记录；membership 索引超驻留预算时不分配
   oversized 索引，改由全 finite segment 的有界精确配对流处理，保留同轨排除、
   overflow/overflow 一次性去重及非有限段原处理规则。
3. 整 grid 输入超驻留预算时，在当前 grid 生命周期内将输入列交 DuckDB TEMP
   关系，按有界 segment 块读回，完整枚举块对（包含不同块），复用同一几何 kernel；
   不持有完整 grid 的 Python row/segment 图，不因预算跳过配对。普通小 grid 不额外
   走关系路径。必须在旧 `grid_segments` concat/unique 超额分配前、同一尚未关闭的
   PostgreSQL 前向 cursor 中早切；按 segment identity 做完整 grid 有界去重，不能
   先触发旧 guard 后重开逐轨/逐块全表扫描。
4. 候选与全 Run 去重超过驻留预算时，候选列批进入 DuckDB TEMP 关系，由引擎
   对业务 identity 去重、按既有身份规则稳定排序、最后赋连续 calculation_index，
   不保留 worker `found`、主线程 `batches`/`seen_pairs` 的无界 Python 图。worker
   的部分 grid 结果在该 grid 完成前不得作为最终结果对外交付；跨 grid 重复代表
   必须依据确定的 grid 序和局部序，不依赖线程完成先后。worker→主线程以有界
   批次/背压交接，不堆积 future 的整个 grid 候选。暂存与最终 Cross/Turn 关系均
   不创建 `event_identity(calculation_index)` UNIQUE ART；暂存先按 pair 身份去重
   稳定排序，再用 SQL `row_number` 生成最终连续编号，最终关系 `seal` 独立校验
   非 NULL、唯一、连续与业务有效性，成功后才供下游按编号查询。
5. DuckDB 使用 `:memory:`/TEMP 表及引擎原生外存。项目不新增显式 save/spool/COPY
   文件流程、不新建通用任务/配额框架。每个独立实例的初始 temp cap 为 50GB，
   仅识别 DuckDB 临时额度耗尽后，在连接空闲且必要事务已回滚时提升至 100GB、
   200GB，200GB 封顶；逐 owner 记录实例/阶段/grid或关系、旧新额度、失败原因、
   重放操作与次数。增加的是允许额度，不预占磁盘或改变物理磁盘与 Docker 挂载。
   非额度 OOM、PostgreSQL、I/O、几何及数据错误不自动扩容或重试。实例各自有
   上限，不设 Run 合计 200GB 拒绝；实际总磁盘峰值另计量。
6. 额度失败可恢复时，保留调用者当前有界输入直到 COMMIT；ROLLBACK 后同步恢复
   关系计数与 ordinal，再重放失败的整个业务批次或整条 SQL。单业务批次内 1024
   行写入子片不单独 checkpoint；已提交批次不重播。排序/去重等 SQL 在对外发布
   前完成；已向下游 yield 的前向 cursor 与 SQL operator 均不支持任意断点续传。
   不保存历史 batch 副本，不设持久 checkpoint/恢复表。200GB 用尽仍失败，
   不发布部分 Run。
7. 保留现有精确 G*/DBSCAN、空间网格按需邻域及 `eps`/`min_samples`；减少
   `_coordinates` 中不必要 source 列与 pieces→concat 重复数组，并按实际候选规模
   核 labels/gridlinks 等线性状态及 2GB 父控制组峰值。不改用 ELKI/近似/抽样。
   用户拒绝统一内存分配/并发准入，保持当前 worker 默认数与每实例 1GB 配置，
   不预称 2GB 性能目标已满足。

### 依赖顺序与 review 单元

1. 核对历史提交 `3c777b6` 中已纳入的 5 文件预算变更与当前其他 dirty ownership；
   复核其有界 overflow 工作量修复与
   membership fallback，在相同输入的 known geometry、same vehicle 不同 subtrack、
   同 subtrack 排除、非有限段、跨 patch 唯一性、patch 上限及稳定方向测试后形成
   第一逻辑提交；若实现与本文语义不符则修订 WIP，不直接视 35 focused passed
   为最终验收。
2. 实现单 grid 大输入在 concat 前、同 cursor 早切的 DuckDB TEMP 列关系、
   全局 segment identity 有界去重与跨块完整配对，维持输入 cursor
   与 worker/关系 owner 的 close/异常清理。小 grid 快路径和大 grid 精确结果按
   独立已知几何断言验证，不以历史产物做 oracle；形成第二逻辑提交。
3. 将 worker 候选与全 Run 去重/排序/连续编号移入 TEMP 关系，移除两处
   `event_identity` UNIQUE ART，以 `seal` SQL 保持发布前唯一/连续性；删除无界
   `found`/`batches`/`seen_pairs` 累计；跨 worker 与跨 grid 的确定性代表和部分
   结果隔离、事务失败回滚/资源清理测试通过后形成第三逻辑提交。
4. 在每个 DuckDB 资源 owner 处加入仅 temp cap 错误触发的受限扩容及日志，
   对当前有界业务批或安全整 SQL 做重放。构造多子片中途失败、已提交不重播、
   200GB 上限、其他异常不误重试、异常 close/临时目录清理测试；形成第四提交。
   实际代码依赖可使步骤 2/3 与 owner 控制交织，但可 review 提交必须维持可运行。
5. 减少 `_coordinates`/G* 的重复列与数组副本，保留精确算法；用实际输入规模与
   2GB 父控制组核结果。geometry 对算法身份/配对覆盖、原生 spill 与额度重放
   边界做独立只读 review；
   所有具体 finding 由相应 owner 修复。统一执行 `make test` 及真实 PG 受影响组，
   说明外部并发 Backend fixture 若仍失败的独立原因，不删除旧测试覆盖。
6. 固定可运行 runtime revision 与工作区 dirty 摘要，使用
   `services/pipeline/config/pipeline-roadnet-test.toml` 跑 fresh 1M 全链路功能冒烟；
   然后用 `services/pipeline/config/pipeline.toml` 对精确 26,000,000 源行做 fresh
   Refine→Network 全链路计量。保护 `trajectory_points`/`day01` 源表只读 count
   前后相同；派生可重建。记录 Run IDs、每阶段/子阶段耗时、完整 CLI 墙钟时间、
   Pipeline 新 cgroup peak/max/OOM/swap、DuckDB 每实例额度事件与临时目录/Run
   总磁盘峰值、PG temp/disk 单列。Pipeline cgroup peak <2,000,000,000 B、
   swap 0/OOM 0 且完整耗时 <3600s 才可标性能达标；超过 1 小时仍继续完成，
   明确写性能目标未达标，不自动终止作业。功能完整与性能目标分别报告。

### 本轮实施与验证状态（2026-09-28）

四层预算路径已按上述现行计划接入：普通 grid 保留原快路径，超额输入在同一
PostgreSQL 前向流中转入 DuckDB TEMP 列关系，块对仍调用原精确几何 kernel；
worker 通过有界队列交付候选，主线程暂存、按稳定身份去重和编号，失败 attempt
在重试前删除。Cross/Turn 的两处 `event_identity` UNIQUE ART 已移除，`seal`
负责最终编号校验。独立 DuckDB owner 使用 50→100→200GB 的受限 temp 额度
恢复；G* 坐标读取省去不用的 source 列。实现仍须以真实 1M 与 26M 全链路运行
检验，当前不将 2GB、1 小时或完整功能验收写为已达标。

验证已完成：真实 PostgreSQL Compose 环境的 Pipeline/CV 完整套件
`1273 passed`、零失败；受影响组 `57 passed`；grid 路径生产镜像
`46 passed`，其中含真实 DuckDB 低内存 TEMP spill 与额度重试；关系/额度
受影响组 `118 passed`。`make test` 的 Backend 部分 `108 passed, 1 failed`，
失败为现有 `test_store_clip_reads_resolved_crossings_and_returns_between_clip`
夹具对 source fingerprint 查询返回错误列形状，本轮没有修改 Backend 生产或
夹具文件；`make test` 因此未整体通过。完整 Pipeline/CV 随后按 Makefile
同等命令单独运行并通过。

固定 revision `ff4b6e9` 的 fresh 1M 冒烟使用
`pipeline-roadnet-test.toml`、`public.day01`、显式禁用 Refine 复用。全链路
正常退出（1556.028 秒），源表前后均为 1,000,000 行；产生 35,004 clips、
1,506 centerlines、556 network edges。独立 Pipeline 容器的有效
`memory.max=1,999,998,976 B`，`memory.peak=1,999,998,976 B`、swap 0、
`memory.events max` 新增 470、OOM/kill 0。此为功能成功，不能说内存有明显余量。
完整指标：`/data/tmp/road-network/logs/columnar-ff4b6e9-1m.json`。

同一 revision 的 fresh 26M 全量使用 `pipeline.toml`、
`public.trajectory_points`、显式禁用 Refine 复用。Refine 完成 26,000,000
行（41,948 车，2434.494 秒），Detect 跨轨迹网格最后记录为 52/1131 完成、
4 个运行中；随后 Pipeline 子进程被 cgroup OOM kill，wrapper 返回 -9，
单调墙钟 5795.968 秒，Clip/Fitting/Network 未执行。源表前后均为
26,000,000 行。有效 `memory.max=1,999,998,976 B`、
`memory.peak=2,000,834,560 B`、swap 0、`memory.events max` 新增
67,924、OOM/kill 各新增 1。故该 revision 的 26M 完整功能、2GB 峰值及
1 小时目标均未通过；不能以 1M 结果替代。PG 共享数据库的 temp bytes
增量 13,378,749,586 B、数据库大小增量 7,283,900,416 B，与 Pipeline
cgroup 分开记录。完整指标：
`/data/tmp/road-network/logs/columnar-ff4b6e9-26m.json`。目前尚无
可证的本次超额 TEMP 切换/额度上调事件；低内存回归测试证明代码路径可运行，
不等于此次 26M 已完成所有 grid。OOM 内存生命周期正在据实际代码定位，
不得把任何单点发现未经复验写作唯一根因。

后续提交 `edf842a` 仅修正 `open_grid_segments` contextmanager 在交给 worker
消费 grid 时仍强持有最后一批输入、拼接/去重工作数组和排序前结果的生命周期；
未改几何规则、grid 预算、worker 数或 DuckDB 内存配置。fast/TEMP 两条路径
的弱引用生命周期回归通过；生产镜像针对性 grid/config 组 `26 passed`。
一组无数据库的同输入 120,000 segment 局部 RSS 探针，旧版交付时
187,199,488 B、修正版 178,737,152 B；各测一次，仅证局部强引用下降，
不能外推全链峰值或宣称这就是 26M OOM 的唯一原因。修复后的
`make test` 仍由同一 Backend 外部夹具失败阻断（`108 passed, 1 failed`）；
随后按 Makefile 的 Pipeline/CV 命令、真实 PostgreSQL 环境完整运行，
`1275 passed`、零失败。修复后的 fresh 26M 全链功能与资源仍**未验收**。

本计划不改变既有 2GB 硬 cap、数据保护、前后端 JSON/CLI/回放契约。用户已
确认上述最终方案并授权恢复实施；功能与性能达标仍以实际检查/运行结果为准。


### Sol high 独立设计审核（2026-09-28，历史实施前审核）

以下审核记录保留当时的未闭环状态；其后用户采纳的修订及实现见
“Q100–Q107 现行实施计划”和“本轮实施与验证状态”，不能把本段的
“当前”理解为现行代码状态。26M 功能及性能仍须以真实运行结果判定。

独立审核结论：当前计划**不能直接实施**。以下是必须补足的设计与证据，
不是已修复项；Q100–Q107 的用户决策保持有效，不重复询问内部实现细节。

1. `grid_segments` 现先 concat/unique 再检查预算，超限前已经分配；`finally`
   会关闭 PostgreSQL 前向 cursor。大 grid 兜底必须在超额分配之前将同一 cursor
   后续有界输入交给 DuckDB TEMP，并在完整 grid 范围按 segment identity 有界去重。
   不能先触发旧 guard、关闭 cursor 后才试图切换或重读全部源。
2. `CrossCandidateRelation` 当前对 `calculation_index` 建 UNIQUE ART 索引；
   DuckDB 原生外存不能据此保证 ART 索引内存有界，而且在暂存阶段预置唯一编号
   与最终全 Run 去重、稳定编号可能冲突。需明确不依赖 ART 的 staging/最终关系设计，
   或拿出该索引在 2GB 父控制组下的实测预算证据，才能称候选路径有界。
3. 候选到聚类的后续内存仍需核查：`_coordinates` 累计全部 pieces 并 concat
   XY、indices、source，DBSCAN 等阶段也可能全量驻留。计划应涵盖完整
   候选→聚类峰值及超额时保持精确规则的处理方式，或以测量证据证明既有预算足够；
   不能预设必须重写全部 DBSCAN，也不能无证据宣称 26M 必然 OOM。
4. 多个 DuckDB 实例各自 `memory_limit=1GB`，而 Pipeline 父控制组硬上限为 2GB。
   每实例 200GB temp cap 是磁盘配置，不解决并发驻留内存。需定义并发连接
   的内存分配/准入和峰值验证方案；这不是 Run 磁盘总 quota 决策。

审核还指出需具体落实 worker→主线程有界批与背压、关系 owner 清理、稳定重复代表；
限定 DuckDB temp cap 重放不能误改 Q74 的普通可恢复错误重试规则，同时避免
200GB 封顶后的重复循环与已完成批次再次交付。这些是实现边界，不自动形成
新的用户语义问题。

当前 Git 核对：HEAD 为 `3c777b6`，工作树 Spec 修改仍未提交；此前 5 文件预算
WIP 已在 HEAD 中，不能继续标成“未提交”。仓库还存在其他人的 staged/untracked
文件，不能从当前状态推定该提交的作者或混入本任务后续提交。审核项待修订详细方案
与再次独立复核；本节不将其标为通过，也不恢复编码、测试或全量运行。

### 对 Sol high 审核的用户反馈及索引事实（2026-09-28，规划中）

本轮根协调器转述的用户原话片段：

> 1：那就早点呗
>
> 2：什么索引？用在哪里？
>
> 4：不要整理内存安排

当前解释：第 1 点接受在超额 concat/unique 分配之前判断，并在仍打开的同一
PostgreSQL 前向 cursor 上切换至 DuckDB TEMP；这修订计划，尚非代码已实现或审核闭环。
第 4 点撤回 Sol high 建议的统一内存分配/全局准入计划，保留既有 worker 数和每实例
`duckdb_memory_limit` 默认设置，不暗改配置。原 2GB Pipeline 父 cgroup 的计量与验收
约束未被用户明确取消，继续实际测量，不能事先保证已满足。第 3 点 DBSCAN 的
代码与资料事实由独立审查者另核，本记录不抢先定论。用户提及 ELKI、近似或调整
`eps` 的论证并未授权换语言/库、放宽精确规则或改既有阈值。

用户问的不是空间索引。`CrossCandidateRelation._create_schema` 在建立 `events` 后，
创建 `event_identity ON events(calculation_index)` UNIQUE 索引；`TurnEventRelation` 有
同名同字段索引。它约束单个关系内“事件编号”唯一，并可能服务按编号选取的查询；
不按候选两端 segment identity 去重，也不按 XY 做空间搜索。当前 grid 先在 Python
按双方 subtrack/point index 去重、排序，再赋连续 `calculation_index`，随后才写入
`CrossCandidateRelation`。因此现有索引不能替代未来 TEMP staging 的业务 pair 去重。

`CrossCandidateRelation.iter_batches(indices=...)` 将外部传入的 int64 calculation
indices 注册为临时 selected 关系，再 `JOIN events USING(calculation_index)` 并按
`ordinal` 输出；SQL 不显式要求索引，优化器是否使用该索引需另证。`seal` 还独立检查
`count(DISTINCT calculation_index)`、首末 ordinal、ordinal distinct 及
`calculation_index=ordinal`；即使移除 UNIQUE 索引，语义校验仍有代码路径，
但重复编号在 append 时的即时拒绝会改变。测试覆盖按索引切片读取、篡改后的
seal 拒绝、资源关闭；不能盲删现有最终关系索引或改公开的 `iter_batches(indices)`
契约。规划建议仅在**新增 TEMP staging** 中不预置该 UNIQUE ART 索引，
先完成稳定 pair 去重和最终编号；现有最终关系保持接口和校验，是否无索引或延后
建索引需在资源实测/行为测试后决定，尚未批准任何代码删除。

高优先级审核项目前仍有未闭环部分：提前切换尚待实现；索引 staging/最终关系的
内存证据未补；候选→聚类峰值尚待独立事实；用户拒绝统一内存安排后仍需按原 2GB
父控制组实测。worker→主线程有界背压、稳定代表及额度重试边界仍是详细设计工作。
本轮只修改计划，不运行生产/测试、不提交，也不宣称已经可以直接实施。

### DBSCAN 与索引审核的事实补充（2026-09-28，仅规划）

独立核查确认，当前 DBSCAN 为项目自研的精确实现，输入是 G* 转向热点与 cross
候选，不是 26M 原始点；`n>256` 时已有空间网格索引，邻域按需计算，核心点计数
达到 `min_samples` 即止，没有全 N×N 距离矩阵。成功的 1M 运行对应输入约
10,105 个转向热点与 58,655 个 cross 候选，DBSCAN 相关两段计时约 0.2/0.8 秒；
这只说明该次输入，并不能推断 26M 聚类的候选数、峰值或耗时，因为 26M 此前在
聚类前失败。计划先测真实候选规模，并检查 `_coordinates` 的全量 pieces/concat XY、
indices 与不必要 source 列，以及 labels/gridlinks 等线性状态的峰值。保留现有精确
算法、`eps`、`min_samples`，不把用户技术论证误读成改用 ELKI、近似搜索、抽样或
调整阈值的授权，也不预设要重写 DBSCAN。

`event_identity(calculation_index)` 是最终候选关系的事件编号唯一性约束，
不是空间索引；segment pair 去重另以两端 subtrack/point 身份 key 执行。新增
候选 TEMP staging 不应预建该 ART 索引，但仅这么做并未解决最终关系保留索引时的
驻留峰值。最终关系是否延后建索引、以无索引的 SQL 唯一性校验替代，或用实测证明
索引在 2GB 父控制组内可承受，仍需给出明确方案与证据，不把索引审核标为闭环。

用户已拒绝 Sol high 的统一内存分配/并发准入建议：实施计划撤回该建议，保持现有
每实例 memory_limit 与 worker 默认设置。该取舍不等于已证明内存足够；原 2GB
Pipeline 父 cgroup 指标仍需测量和如实报告。这里没有新增待用户决定的算法选项。

### 用户对四项审核的本轮收敛与索引推荐（2026-09-28，实施仍暂停）

用户经根协调器转述的原话：

> ok，14已经收敛了把，2：你给个推荐方案，3按照推荐完成

当前解释：第 1 点提前切换至同一前向 cursor 的 DuckDB TEMP 路径，已是确认的计划
修订；第 4 点不做全局内存分配/统一准入、不擅改默认 worker 或每实例内存配置，
已收敛，但原 2GB Pipeline 父控制组仍需实测，不能宣称已达标。第 3 点采纳此前推荐：
保留现有精确 G*/DBSCAN、`eps` 与 `min_samples`，先减少 `_coordinates` 中不必要
source 列和 pieces→concat 重复数组，测实际候选规模、labels/gridlinks 等线性
状态及父控制组峰值；不换 ELKI/近似/采样，不预设重写整个 DBSCAN。这里“按照推荐
完成”是方案采纳，不在第 2 点索引方案尚待确认时推定全局恢复实施授权。

第 2 点给用户的最小推荐方案是**整条生产链删除两处 `event_identity` UNIQUE ART**，
不是只让大数据 staging 无索引、最终关系照旧重建可能占用内存的 ART：

- 新的 cross 候选 TEMP staging 存双方 subtrack/point 身份及确定的 grid/local 顺序，
  不预分配最终事件编号，不建 `calculation_index` ART，也不使用空间索引代替业务
  pair 去重。引擎 SQL 先按业务 pair 身份选确定性首次代表，再按既有规范顺序排序，
  最后 `row_number` 产生连续 `calculation_index`。
- `CrossCandidateRelation` 与 `TurnEventRelation` 最终关系均删除
  `CREATE UNIQUE INDEX event_identity ON events(calculation_index)`。保留现有列 schema、
  按编号 `iter_batches(indices)` 的 selected JOIN、原 JSON/lineage/stable IDs 与
  `seal` 的 SQL 检查（非 NULL、`count(DISTINCT calculation_index)`、ordinal 首末/唯一、
  `calculation_index=ordinal`）；只在验证通过后发布关系。索引不是空间索引，也不是
  grid pair 去重：前者以前约束事件编号，后者本来依赖两端业务身份。
- 内部错误时点会变：重复 `calculation_index` 从 append 时 ART 即时拒绝，转为
  seal 时 SQL 拒绝且关闭关系，不保留不必要的内部错误时点兼容层。生产调用者
  顺序写入并在 seal 后消费；公开查询字段和结果语义不变。现有测试中人为另外
  创建的 source/vehicle UNIQUE 索引用于注入事务失败，保留该测试；单行篡改、
  非连续 ID 的 seal 测试保留，并新增真实两行重复编号在 seal 阶段拒绝且无发布。

这是一份待用户确认的推荐，不是已实施的修改。移除 ART 后仍需验证无索引的
`iter_batches(indices)` 大批量查询、DuckDB SQL 去重/排序的原生外存行为和 2GB
父控制组峰值；不能仅凭删除索引宣称内存审核闭环。用户其他已确认的 Q100–Q107
语义保持，仍不修改生产、测试、提交或启动全量作业。


### 最终索引方案确认与恢复实施授权（2026-09-28）

> 用户回答原文：ok，可以

当前解释：用户确认两处最终关系及暂存关系不建 `event_identity` UNIQUE ART 的
最小方案，并结合此前明确实施请求授权恢复 Q100–Q107 方案执行。历史“待确认”
段落保留原问答状态，不再作为当前禁止实施的指令；实施以“现行实施计划”正文为准。

## 2026-09-28 性能调查与新一轮设计问题（草稿，未获实施授权）

本节只记录当前代码的只读核查、独立运行的性能证据和待讨论方案。用户本轮明确要求先讨论、不修改实现或配置；此前 Q100–Q107 的历史设计确认及授权原文不被本节改写，也不表示本轮优化已获授权。Q71 曾拒绝额外数据指纹复核，本轮 Q109–Q110 重新讨论变化检测范围，回答前不更改其含义。

### 实测口径与热点

`20260928_081003_401589_pid-7_log.txt` 与 `20260928_1615_day01_scalene-profile-all.json` 对应同一次 `public.day01` 完成运行：日志总耗时 1347.559 秒，Scalene 捕获跨度 1348.210 秒。日志分段为 Detect 计算 438.874 秒、Detect 写出 106.424 秒、Clip 组装 308.039 秒、Fitting 计算 267.755 秒、Fitting 持久化 225.996 秒；Network 0.414 秒。`resolved_sequence_s=34.244` 包含在 Detect 写出中，不能另加。Fitting 持久化计时包括从 Clip 临时关系按来源 ID 逐次取回片段、建立清单、数据库写入和最终提交，不能称为纯 PostgreSQL 等待：`stages/fitting.py:65-98` → `db/pipeline_store.py:390-419` → `db/write_fitting_results.py:130-160` → `clips/clip_input.py:228-236,275-309`。日志仅见 44,972 行 `centerline_source_clips` 写入约 0.8 秒的进度观测，不代表此前约 220 秒的片段取回与准备均属数据库写入。

`20260928_1511_day01_pyspy_hotspots.svg` 对应另一轮 `public.day01` 完成运行，总耗时 1378.501 秒。py-spy 的函数样本占比为包含式调用栈，不是阶段耗时，父子栈不得相加；其中拓扑构建 6349/137157 样本（4.63%），Clip `get_by_clip_id` 23206/137157（16.92%）。Scalene 中 `clips/clip_input.py` 文件 CPU 时间占比 9.5826%，`threading` 的大比例包含等待，不能当作业务 CPU。两轮 profile、日志分段及包含式采样不交叉相加。更早的 `20260927_002759_039888_pid-7_log.txt` 是 `public.trajectory_points` 26M 输入的中途记录，截止约 619 万行，没有完成终态，不作为上述 day01 的时间证据。Scalene 内嵌的 `clip_input.py` 全 338 行与本轮工作树该文件一致，但未提供整个仓库的提交身份。

当前 Clip 关系每片段开启事务，写一次元数据，再按 `relation_batch_size` 将内点转为六列 JSON 并由 DuckDB `from_json`/`unnest` 插入；当前代码不是历史的 `executemany`。读取先取片段，再逐片段单独查内点并构造 NumPy 数组；Fitting 的合并与双向三变体重复遍历关系，每个配对拟合至少两次读取片段，采样块较多时还会继续重读。定位：`clips/clip_input.py:82-119,275-309`，`centerlines/fit_centerlines.py:69-124,148-151,234-274`。在独立新建、无网络和运行数据挂载、单 CPU/512 MB 的容器中，800 片段 × 24 内点的合成对照显示：当前逐片段 JSON 写入 2.509 秒，有界每 128 片段 JSON 列批写入 0.075 秒；逐片段取点 0.760 秒，按 128 片段范围取点 0.020 秒。点数 19,200、ID 和及字段关系相同；读端只核对点 ID 汇总，尚未重构和验证完整 `TrackClip`，这些比值不等于全 Pipeline 加速。直接绑定 Python typed-list 的批写为 24.278 秒，反而较慢；当前镜像没有 `pyarrow`，Polars→DuckDB 注册原型不能运行。第一候选是沿用现有 JSON 格式做有界跨片段批量写入与读取，保持片段顺序、事务失败回滚和父控制组内存预算；该候选尚非决定。

拓扑验证在 G* 过滤和聚类后只调用一次，提取中的逐 selection 和逐 cluster 循环均串行，不是 G* 前后各做一轮。Refine Parquet 读取器用 bbox 查询命中窗口的完整子轨迹；不同窗口命中同一子轨迹时可能重复输出。`extent()` 的全点 min/max 查询在 Detect 路径有多次调用。定位：`intersections/detect_intersections.py:303-307,390-398`，`intersections/extract_topology.py:146-169`，`trajectories/results/read_parquet.py:146-149,224-273`。当前显式 Python 线程池用于 cross-track grid；不能据此称拓扑按簇并行，也不能从仓库配置推定 PostgreSQL 或 NumPy 原生线程的运行时数量。

源数据指纹、制品 SHA-256 和业务校验目的不同：Refine/Detect 导出前后各扫描一次 Refine 引用的源行并计算指纹；发布制品时登记文件 SHA、Parquet footer/schema/行数；当前消费配置 `verify_artifact_sha256=false`，仍检查结构及 footer。定位：`db/source_table.py:237-290`，`stages/refine.py:118-145`，`stages/detect.py:142-173`，`artifacts/parquet_manifest.py:68-91,111-160`，`config/pipeline.toml:112`。现有 profile 未将源指纹两次扫描、SHA 和 SQL 业务校验可靠拆分成独立耗时，不能将所有“校验”费用归于 SHA。均匀抽样会漏掉未抽中的删除或修改；`max(id)`、行数或最后写入时间也不能单独证明内容未变。已确认的 Q27–Q28/Q57 和本文件第 4.3 节要求完成 Run 的 Parquet 制品不可变，当前 `artifacts/publish_dataset.py:162-199` 将文件设为只读并原子发布。Q109 仅澄清用户此次“只会 append”是指原始输入还是意图变更已完成阶段的既有语义，不将封存重新包装为新提案。若决定采用低成本写入版本，还须确认原始 PostgreSQL 表的所有 INSERT、UPDATE、DELETE（含直接 SQL/COPY）能否受同一事务内版本登记约束；否则不能承诺 O(1) 完整变化检测。

本轮用户短消息原文：

> 当前先别修改，我们在讨论阶段，你先看看code，必要时候可以做一些实测，最后结果出来之后，用@grill me和我讨论热点性能提升的方向

本轮调查输入的用户长消息原文如下。原文中的 HTML 实体、转义下划线及历史自我更正保留，不将其中的初步推测视为已核实结论：

> astra，你是我的助理，而不是执行者，详细的repo detect、coding和review都交给subagent(gpt-6-sol-low)，并且允许subagent唤起(gpt-6-luna-high)进行必要的并行任务   &#x20;
>
>
>
>
>
> 根据log文件，/data/tmp/road-network/logs/20260927\_002759\_039888\_pid-7\_log.txt，性能报告和代码探索结果，给我找5个性能热点，我感觉拓扑验证挺消耗时间，帮我看看是不是并行的
>
> 我感觉clip构建也很慢，帮我看看到底怎么修复
>
> 我感觉指纹校验也很耗时间，我想改为别的方式保证数据，因为postgres和parquet大多数时候都不会更新，我们可以假设数据是稳定的stable，指纹是否能用抽样的方式进行，比如ls=（1，2，3，4，5，6，7，8，9，10，11）我们只要均匀抽样，如果ls[2]=3,但是ls[4]=5,ls[6]=8,那么我们大概能知道ls[4]之后，ls[6]之前发送了删除或者变更
>
> 还有更简单的方案，当我们认为这些数据不会update而只会append，只要核对当前stage\_id的最后一次写入，是否等于每次stage\_result记录下来（新增特性），就能判断是否有更改
>
> - **第一次：py-spy。** Pipeline 完成，约 13,785 秒？更正：约 **1,378.5 秒**。火焰图显示耗时主要分布在 Detect、Clip 和 Fitting 的调用栈；Clip 片段处理和 Fitting 的计算、结果写入值得优先查看。报告：[py-spy 火焰图](/data/tmp/road-network/logs/20260928_1511_day01_pyspy_hotspots.svg) 。py-spy 在目标程序结束后的清理阶段报了 `No child process`，但 Pipeline 已完成且 SVG 已成功保存。
> - **第二次：Scalene。** 前两次尝试没有得到完整、可用的逐行报告：第一次因 DuckDB 达到容器约 1GB 内存限制而中止；另一次虽然跑完，但分析范围过滤得太窄。调整为全模块采样后，最终这轮完整完成，耗时约 **1,347.6 秒**。可用 `scalene view --cli` 查看 [Scalene 逐行报告](/data/tmp/road-network/logs/20260928_1615_day01_scalene-profile-all.json) 。JSON 约 131 MB。
>
> 目前最值得跟进的是 Clip 数据读写。Scalene 把 `clip_input.py` 归为约 9.6% 的运行时间，其中包含逐片段执行 SQL 的读写；例如片段迭代时逐个查询点数据的代码在 [clip\_input.py (line 284)](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/clip_input.py:284) ，逐个插入片段的代码在 [clip\_input.py (line 89)](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/clips/clip_input.py:89) 。这适合作为后续检查批量读写的候选点。报告中 `threading.py` 也占了很大比例，主要归在 native 等待和线程入口；不要把这个比例直接解读成业务 Python 函数的耗时。
>
> 这两份剖析结果的总耗时接近，但 profiler 会增加运行开销；它们适合找热点，不应直接当作基准成绩。

### 本轮待回答 QA — 优化优先级与变化检测语义（2026-09-28）

发问前提：以上为调查事实和候选方案，不修改 Q71 等历史决定；Q108–Q110 均未回答，推荐不代表采纳。先确认顺序及数据封存/写入边界，再考虑具体实现与验收。

#### Q108 — 优化顺序

❓ **Q108 — 优化顺序**：第一轮优先收敛哪条路线？A. 先解决 Clip/Fitting 的批量读写和重复取数，再处理 Detect 内部热点；B. 先推进拓扑验证并行；C. 先改数据变化校验机制。
➡️ 推荐 A。当前 day01 的 Clip 与 Fitting 合计约占六成耗时，批量写入已有小规模对照证据；拓扑并行和校验机制分别继续设计，本轮仍不实施。

用户回答：待回答。正文关联：本节“实测口径与热点”。当前解释：待确认。

#### Q109 — 追加发生在哪一层

❓ **Q109 — 追加发生在哪一层**：你提出的“只会 append”，是 A. 原始输入可以追加，已完成的 stage_id 结果及已发布 Parquet 继续封存；还是 B. 也希望允许向已经完成的同一个 stage_id 追加结果？
➡️ 推荐 A，沿用按运行批次发布结果的方式；新增输入或重新计算形成新的阶段结果。若选择 B，再讨论如何让写入版本、阶段状态和 Parquet 一起更新，不能只给 stage_result 加一个时间戳。

用户回答：待回答。正文关联：本节“源数据指纹、制品 SHA-256 和业务校验”。当前解释：待确认。

#### Q110 — 变化检测覆盖哪些写入

❓ **Q110 — 变化检测覆盖哪些写入**：原始 PostgreSQL 数据的后续追加或修改，能否统一经过会登记版本的导入/写入入口？A. 可以，以受控写入版本为准；B. 仍需支持直接 SQL/COPY 等入口，并自动识别它们造成的变化。
➡️ 推荐 A，用与数据写入同事务更新的版本记录实现低成本判断。若选择 B，下一轮再比较数据库侧变更登记与接受漏检的抽样；不把最后一行或抽样相同当作完整内容未变的证明。

用户回答：待回答。正文关联：本节“源数据指纹、制品 SHA-256 和业务校验”。当前解释：待确认。

本轮检查：Q108–Q110 仅发问前记录，尚无回答；此前 Q1–Q107 与历史实施授权保持原貌，本轮不新增实施授权。两条用户调查输入均已记录原文。

### 2026-09-29 22:34（Asia/Shanghai）资源边界再更正

开发人员 @reid 要求不设置 Pipeline 容器硬内存上限，只记录 6,000,000,000 B
观测阈值。超过阈值写 `ERROR`，作业继续；记录实际 `memory.max`、
`memory.swap.max`、`memory.peak` 与事件计数。此前 4GB 硬上限与 swap 0
只对应已失败的历史运行。现行条款已在第 6.1、7.2、7.3 节原位置更新。

### 本轮补充 — 跨轨迹几何夹角与列式计算（2026-09-30）

用户修正授权原文：

> 如果要符合预期，确实的用端点向量计算夹角，就按照这样改，并且记得把方向分桶也改为复用端点向量计算夹角

用户后续消息原文：

> [$grill-me](/home/reid/.codex/skills/skills/grill-me/SKILL.md) 这个计算过程最好也能用列式计算，应该能实现把

当前解释：前一条已授权几何角度及分桶修正；后一条发起列式计算设计访谈。既有网格
NumPy 路径是第 3.1 节已确认的局部计算路径；是否将其迁移到 DuckDB 不能仅由
“列式计算”四字推定。已有 DuckDB 路径的角度一致性修复属于原语义修正范围。

#### Q113 — 本轮列式实现范围

> 这次“列式计算”是否接受保留 NumPy 主路径：按列批量预计算端点方向，让分桶、候选筛选和内核复用；已有 DuckDB 分支同步几何口径？
>
> A. 保留 NumPy 主路径，批量计算并复用几何方向，已有 DuckDB 分支同步修正。
>
> B. 将主网格的方向计算、候选配对和精确求交整体迁移到 DuckDB SQL。
>
> 推荐 A：当前坐标与候选已经是数组，能在现有路径直接复用按列计算的方向；B 还需重设计空间配对执行计划并验证性能。本轮保持既有直线与速度条件。

用户回答原文：

> 保留numpy主路径，同步修正duckdb路径

正文关联：第 4.7 节。当前状态：选择 A，本轮列式实现范围已确认；结合此前修正授权执行，无新增引擎迁移待决项。
本轮检查：保留已有 Q1–Q112 及其他工作的未提交内容；Q113 编号连续，问题、选项、
推荐及用户两条需求原文均已保存；QA 仍位于文末。没有生成新的导出数据。


### Q113 实施与验证记录（2026-09-30）

按用户选择保留 NumPy 主路径，同步修正已有 DuckDB 前置角度筛选。分桶、候选过滤、
精确内核复用端点几何方向；原直线条件、速度条件和来源属性保持。
`make test` 通过：Compose 18 passed，Backend 107 passed/2 skipped，Pipeline
1266 passed/72 skipped。新增角度路径专项 72 passed。
固定 grid00 的 86321 历史 2,586 条交点记录在几何角度条件下全部被剔除；关闭速度筛选
重算全格为 1,750,089 条，逐批独立点积验证全部满足 80–100°，其中 86321 为 0 条。
速度阈值 20 时全格仍为 0 条。复测未重新导出 CSV；主机负载影响未隔离，不作为性能验收。
详情与实际配置见 [验证记录](validation/cross-track-endpoint-angle-20260930.md) 。
本轮设计范围、实施和功能验证已完成；不改变其他工作流的状态。
