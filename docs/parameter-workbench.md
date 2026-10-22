# 参数试验台设计说明

本文说明当前实现的数据、试验和资源边界，安装与命令见[根 README](../README.md)，实际验证记录见[验收记录](validation/parameter-workbench.md)。术语遵循[项目术语与写作规范](terminology.md)。

## 目标与活动模块

唯一配置入口是选定的 `pipeline.toml`。命令行接口（command-line interface，CLI）仅选择动作和配置文件；输入、坐标系、算法参数、搜索空间、评估口径及资源预算均来自该文件。连接密钥和分配器预加载属于运行环境，不覆盖算法参数。

活动 Python 实现集中在 `services/pipeline/src/pipeline/` 的单层模块；原生实现位于 `services/pipeline/native/`，通过 pybind11 暴露为 `pipeline._native`。

| 模块 | 职责与执行边界 |
| --- | --- |
| `__main__.py`、`config.py`、`runner.py` | 命令分派、配置校验、五阶段顺序执行、隔离计算进程、运行清单 |
| `source.py`、`artifacts.py` | 输入快照、坐标转换、外排分组、自包含字段、原子文件发布与摘要 |
| `refine.py`、`detect.py`、`clip.py`、`fit.py` | 准备数组、组织有界分组、调用原生算子、保存结果及血缘 |
| `network.py` | 依据已提取的交叉口和中心线主键组织节点、边与度数 |
| `labels.py`、`evaluation.py`、`tune.py` | 标注校验与静态预览、区域评估、Optuna 搜索/剪枝/续跑 |
| `resources.py`、`database.py` | 资源计量编排、协作式预算检查、可选最终成果发布 |
| `native/core.cpp` | 精炼、转向/穿越候选、密度聚类、拓扑验证、圆域裁切、弧长重采样与中位数拟合 |
| `native/profiling.cpp` | 独立线程采样、原生时钟与进程指标、gperftools 接口 |

精炼阶段（Refine）、检测阶段（Detect）、片段提取阶段（Clip）、拟合阶段（Fitting，目录 `fit`）的轨迹与几何核心均由 C++17 执行；缺少扩展会直接失败。Python 保留数据读写、坐标投影、属性转换与路网构建阶段（Network）的主键关联。

当前检测采用转向/穿越候选、带噪声的基于密度的空间聚类（Density-Based Spatial Clustering of Applications with Noise，DBSCAN）和臂数/车辆支撑验证。历史局部热点统计量（G*）、HDBSCAN、视觉判别尚未迁入，当前实现不保证与旧算法逐项等价。

## 数据身份、坐标与产物发布

源数据行键在所有输入文件内必须全局唯一。`source_point_id` 由 `dataset_id` 与源键派生；每次运行另建 `run_id`，各阶段标识及其输出键由运行身份、阶段和成员身份派生。跨运行比较应使用源键和几何/指标，不能要求新的运行复用原阶段主键。

输入通过字段映射归一化为不可变 Parquet 快照，保留时间、速度、航向、坐标和完整 `attributes_json`。文件内容及输入配置参与指纹，缓存复用前校验文件摘要；同一缓存的检查、构建和发布由进程锁保护。重复源键、车辆编码碰撞及不可用输入会明确失败，非法记录另存 `rejected.parquet`。

坐标参考系（coordinate reference system，CRS）有三个边界：`source_crs` 解释输入；`processing_crs` 必须为适合区域的米制投影，供全部距离、半径和长度计算；展示用 GeoJSON 和数据库成果转换为 EPSG:4326。GeoParquet 保存计算坐标及 CRS 元数据，不以文件扩展名猜测坐标系。

| 阶段/数据 | 主键与血缘 | 自包含内容 |
| --- | --- | --- |
| 输入 `points.parquet` | `source_point_id`、源键、车辆、`source_seq` | 原始属性、时间、运动字段、投影坐标、经纬度与自身几何 |
| Refine `points` / `excluded` | `point_id` / `source_point_id`；`run_id`、`refine_id`、子轨迹及插入点父键 | 保留/注入点及剔除记录各自保存属性、坐标、几何与原因；注入点保留两侧来源属性 |
| Refine `segments` | `segment_id`、两端 `point_id` | 两端坐标/时间/运动字段、包围盒、长度和线几何 |
| Detect `candidates` / `intersections` | `event_id` / `id`；源点/线段键、`refine_id`、`detect_id` | 候选位置与类型；交叉口几何、半径、候选数、车辆数及臂方向 |
| Detect `segment_tiles` | `segment_id + tile_x + tile_y` | 完整线段记录及所覆盖网格，支持文件范围筛选 |
| Clip `clips` | `clip_id`、`clip_stage_id`、子轨迹及两端交叉口键 | 点键数组、点属性数组、完整坐标数组、方向、类型与线几何 |
| Fit `centerlines` / `memberships` | 中心线 `id` / `centerline_id + clip_id`；`fit_id`、片段阶段与交叉口键 | 中心线、支撑片段数与成员键；成员记录自身带片段几何和支撑属性 |
| Network `nodes` / `edges` | 节点/边 `id`、`network_id`、交叉口/中心线键 | 节点位置与度数；边几何、端点身份及中心线属性 |

主键用于关联和审计，读取任一数据集的几何不需要回查原始 `trajectory_points`。所有阶段数据保存在运行目录，数据库不承接这些中间计算产物。

Parquet 先写 `.partial`，成功关闭后替换目标文件；空输出也有完整类型定义。阶段最后发布 `manifest.json`，记录 `stage_id`、`parents`、配置摘要及各数据集的主键、行数、字节数、列和 SHA-256。清单采用 JavaScript 对象表示法（JavaScript Object Notation，JSON），同样先写临时文件再替换。

原子性针对单个文件；整个阶段目录不是一个文件系统事务。失败可能留下已完成的部分文件，消费者应以阶段及运行清单的 `COMPLETE` 状态确认成功。当前不提供阶段断点自动接续；重新执行使用新运行身份。文件摘要用于内容校验，不代表掉电持久性保证。

## 空间计算与规模边界

源数据按车辆和时间外排，跨 Arrow 批次保持分组。区域运行读取区域包围盒及 `context_halo_m` 范围；空间过滤造成的 `source_seq` 断裂会切开轨迹，避免连接未观测的区域外行程。

区域结果受上下文范围影响。DBSCAN 连通簇可能跨越有限缓冲区，拟合也可能缺少远端交叉口，因此区域运行不保证与全域结果逐项等价；最佳参数的完整复跑应另行核查这些差异。

Detect 将每条合格线段放入其包围盒覆盖的网格，先检查总归属记录数量，再在网格内调用 C++ 求交。穿越点由唯一归属网格输出，边界候选不会因重复网格成员而重复计数。拓扑验证先通过网格和米制包围盒筛选，再执行原生精确计算；DuckDB 的 Parquet 谓词下推不等同于数据库空间索引。

**当前只拟合两端均关联已检测交叉口的道路。** `between_intersection` 片段进入 Fit；边界片段可保留作诊断，但不会自动成为终端道路。拟合按交叉口对归一方向、弧长重采样并聚合，输出 `combined`，可另导出两个方向的变体。

Network 只以 `combined` 构造无向物理道路，方向变体不重复成为边。同一交叉口对仅支持一条物理道路，不支持自环，也不能据此区分共享端点的多条平行道路。连接通过交叉口主键表达；线几何止于路口圆域边界，不吸附到路口中心。二维穿越无法可靠区分高架与地面道路。

批量读取和外排降低全量物化成本，但峰值仍取决于最大车辆组、最密网格、最大交叉口对片段组和候选总量。交叉口、候选和中心线列表仍会驻留内存，不能仅按输入总行数推算内存需求。

## 标注、评价与 Optuna

GeoPackage（GPKG）明确指定图层；Shapefile（SHP）需完整配套文件。标注加载校验 CRS、主键、几何及区域配置，输出静态 SVG、GeoJSON、GeoParquet 供预览。交叉口支持点或面，中心线支持线；无需引入前后端。

评估区域必须显式声明 `reviewed_complete=true`，表示区域内正例和遗漏已完整审查。区域内部不能重叠，共边要素唯一归属。`tune` 区域用于选择参数，留出区域（holdout）仅在选定最佳已完成试验后评估；未配置留出区域时报告 `UNAVAILABLE`，不声称完成独立验证。

交叉口使用距离阈值内的一对一匹配计算精确率、召回率和 F1。中心线使用区域内按长度计算的几何覆盖 F1，评分物理道路时避免重复计入方向变体；`combined_f1` 是两类 F1 的调和平均。中心线覆盖不验证拓扑正确性，当前没有路网拓扑优化目标。

调参遵循以下顺序：

1. 固定输入快照、标注、区域顺序和随机种子，再验证搜索空间。参数必须存在且能影响目标；例如交叉口 F1 不允许搜索 Fit 参数，当前也拒绝 Network 参数。
2. 每个试验（trial）按相同区域顺序执行完整五阶段。每个区域尝试启动新的计算进程，结束后退出，减少分配器保留内存对后续试验的影响。
3. 每完成一个区域，汇总当前区域前缀的匹配计数/长度，再计算累计指标；不直接平均区域 F1，也不比较不同阶段的分数。
4. 调用 Optuna `report()` / `should_prune()`。自定义 `RegionMedianPruner` 比较相同前缀上已完成试验的中位数，并遵守 `startup_trials`、`warmup_steps`。
5. 质量剪枝实际跳过剩余区域计算；最后一个区域结束后不再做质量剪枝。只有一个调参区域时没有中途节省计算的机会。
6. 仅 `COMPLETE` 试验参与最终排序，同分优先较早编号。保存 `best.pipeline.toml`，评估留出区域，CLI 再用最佳配置对完整来源复跑。

资源预算触发单独记录 `reason_kind=resource_budget` 并将试验标为 `PRUNED`；输入和基础设施异常标为 `FAIL` 并停止本次调用，不能伪装成较差质量分数。“最佳”仅表示本研究已完成试验的最佳观察值。

当前是单机、单协调者、顺序试验，研究目录有排他锁；没有并行试验调度或分布式执行。`n_trials` 是包含失败/剪枝试验的总预算。`tuning.timeout_s` 在启动下一个试验前检查，不抢占正在执行的试验，也不限制后续留出评估和最佳配置复跑。

### 续跑身份

`optuna.journal` 保存 Optuna 事件；`identity.json` 固定输入指纹、标注内容与区域顺序、语义配置、顶层 Python 文件摘要、原生扩展摘要及已登记依赖版本。`trials.json` 和每个试验的清单保留状态、参数、区域指标和产物路径。

增大 `n_trials` 或调整 `timeout_s` 可续跑；`output.root` 不参与语义身份，但移动研究仍需完整保留原研究目录。数据、标注、搜索空间、资源配置、算法代码等身份内容改变时拒绝混入原研究，需使用新 `study_name`。中断留下的 `RUNNING` 试验转为失败并记录 `INTERRUPTED`，仍消耗原试验预算。

文件来源的缓存和跨调用续跑已验证。**`input.kind=postgres` 每次调用都会重新导出只读快照并生成 `snapshot_nonce`，因此跨 CLI 调用会改变研究身份。** 需要续跑时，应先固定并校验文件快照，以该文件作为来源建立研究；不能依靠表名相同推断数据库内容未变。

## 运行时间与内存测量

`resources.json` 同时保存整体和阶段指标。独立 C++ 线程采样不依赖 Python 全局解释器锁（Global Interpreter Lock，GIL），可观测原生算子运行期间的分配。Linux 已验证；macOS 的自身内存采样分支尚未验收，进程树和控制组（cgroup）统计依赖 Linux。

| 字段/分项 | 口径与边界 |
| --- | --- |
| `wall_s` | 原生单调时钟的直接经过时间；整体包含嵌套阶段，不能再把各层计时相加 |
| `process_cpu_user_s` / `process_cpu_system_s` | 自身进程全部线程的中央处理器（central processing unit，CPU）时间；不含子进程 CPU 时间 |
| `phases.read / compute / write` | 不重叠的输入/输出（input/output，I/O）和计算区间，可聚合重复批次；每项含调用数及墙钟/CPU 时间 |
| `orchestration_wall_s` | 阶段墙钟时间减去已记录分项的时间，表示该阶段其余编排开销 |
| `rss_sampled_peak_bytes` | 当前跨度内常驻内存集大小（resident set size，RSS）的采样峰值，含边界采样，可能遗漏短暂尖峰 |
| `process_tree_rss_sampled_peak_bytes` | 自身进程和采样时仍存活的后代 RSS 之和的峰值；共享页可能重复计算，短命子进程可能遗漏 |
| `process_lifetime_peak_rss_bytes` | 操作系统（operating system，OS）记录的进程生命周期高水位，不是阶段局部峰值 |
| `cgroup_current_sampled_peak_bytes` | 当前控制组 `memory.current` 的采样峰值，可能包含其他任务、文件页缓存及后代控制组 |
| 快照中的 `cgroup_lifetime_peak_bytes`、`cgroup_limit_bytes`、`cgroup_swap_bytes`、`cgroup_events` | 来自 `memory.peak`、`memory.max`、`memory.swap.current`、`memory.events`；历史峰值可能被外部重置，不能当作本次试验独占峰值 |
| `process_count_peak` / `thread_count_peak`、`samples` | 观测到的进程/自身线程数量及采样次数；不可据此保证捕获每个短暂进程 |

内存字段为字节，`soft_memory_mb` 使用十进制 MB，时间为秒。缺失读数或未设内存限额可表示为 `null`，不能解释为零占用；`cgroup_scope=shared_or_unknown` 不推断控制组为本运行独占。`sample_interval_ms` 为 1–60000；阶段和整体边界读取完整进程树/控制组，批次分项和预算检查使用较便宜的自身进程采样，后台线程继续按间隔采样。

`include_children=true` 时，内存预算（memory budget）比较已记录的进程树 RSS，并至少计入自身 RSS；关闭时只比较自身 RSS。`max_wall_s` 检查当前测量运行的墙钟时间。预算在安全边界协作检查，不是 OS 强制内存上限或抢占式超时，无法中断执行中的 C++ 算子。

整体计时覆盖该 Pipeline 测量区间，不包括进程启动、最终遥测序列化及隔离进程结果文件交接/父进程读取。阶段清单计算文件摘要计入写入分项。调参前的共享输入准备单独记录在 `input-resources/`，每个区域运行注明输入已在外部准备；不能将单个区域指标称为整个研究的总资源。

计算规模保护阈值包括：`max_vehicle_points`、`max_tile_segments`、`max_candidates`、`max_pair_checks`、`max_grid_memberships`、`max_fit_points`。DuckDB 的 `duckdb_memory_mb` 只约束其自身预算，必须为 Python、Arrow、C++ 数组和文件缓冲保留空间。

### gperftools 原生堆分析

可选的开源 gperftools 通过完整 `libtcmalloc` 记录分配器管理的堆；它不等于总 RSS，也不覆盖所有独立内存映射。工作台检查实际分配的归属，避免只发现符号就误判分析器已经接管分配。

在 `pipeline.toml` 设置 `resources.heap_profile=true`，安装完整库，并在 Python 启动前预加载。以下 Linux 路径仅作示例，以实际安装位置为准：

```sh
env -u HEAPPROFILE LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc.so.4 \
  python -m pipeline check-config --config pipeline.toml
env -u HEAPPROFILE LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc.so.4 \
  python -m pipeline run --config pipeline.toml
```

不能使用 `libtcmalloc_minimal`，也不能在 Python 已启动后设置 `LD_PRELOAD` 补救。工作台拥有分析器启停权，按阶段和运行结束输出 `.heap`；每份文件描述分析器本次启用以来跟踪的分配及当前存活堆，不代表阶段独占堆峰值。已由 `HEAPPROFILE` 启动的分析器会被拒绝，未正确预加载时明确失败。堆分析会改变分配行为并增加开销，应单独记录，不能与未开启分析的速度结果直接混合比较。[gperftools 官方说明](https://gperftools.github.io/gperftools/heapprofile.html)

控制组指标仅诊断，代码不会创建独占控制组、重置系统峰值或写入 `memory.max`。需要强制上限及独占总量时，应由部署环境建立隔离边界，并记录内存不足（out of memory，OOM）事件；当前采样报告不能替代该验收。[Linux cgroup v2 文档](https://docs.kernel.org/admin-guide/cgroup-v2.html)

## 数据库与迁移

数据库默认关闭，文件结果足以独立运行和评估。`[database] enabled=true` 才启用最终发布，连接字符串从 `dsn_env` 指定的环境变量读取，不写入运行清单或错误消息。来源连接与成果连接分别配置，PostgreSQL 来源只做只读快照。

成果 schema 默认 `roadnet_workbench`，只发布 `intersections(Point)`、`centerlines(LineString)` 两张表，几何均为 EPSG:4326。每行包含主键、`run_id/refine_id/detect_id/fit_id` 等适用血缘、完整 `properties JSONB` 和非空几何；交叉口尚无拟合阶段时 `fit_id` 可空。

两张表各建通用搜索树索引（Generalized Search Tree，GiST）几何索引。标识符安全引用、值参数化；创建结构和两层插入处于同一事务，主键冲突失败并回滚，不覆盖已有结果。PostGIS 必须预先安装，发布模块不会执行扩展安装。

试验和留出运行强制关闭成果发布，只有按用户配置执行的独立运行及最佳配置完整复跑可以发布。点、线段、候选、片段、成员关系、试验日志和资源数据均留在文件中，不建立对应数据库表。

历史表、服务和 `track.v2` 保留作迁移参考，不在新默认入口加载，也不会自动删除。迁移应先独立验证新 schema 和文件结果，再由维护者明确处理旧依赖与数据保留。真实 PostGIS 在线发布和旧库迁移尚未验收；当前测试验证了连接替身下的事务、字段和安全标识符行为。

## 真实数据性能验收方案

合成测试证明了原生算子、血缘、调参流程及采样机制的行为，不能据此确认大规模真实数据精度与速度。历史 26M 点、2 GB、一小时等目标尚无本次实测证据。

建议按以下顺序确定验收标准（acceptance criteria），并把每组配置保存为完整 TOML：

1. 固定输入文件及摘要、标注版本、投影、原生二进制和依赖版本；记录硬件、CPU 配额、内存/交换空间、存储和冷/热缓存状态。先完成小范围标注精度验收。
2. 建立逐级放大的真实数据集，同时保留最长车辆、最密路口、最大交叉口对片段组；分别记录行数、车辆数、线段数、网格归属数、候选数和最大分组，避免只验证均匀抽样。
3. 先测一次正常全流程，再对固定数据重复运行；区分首次导入和缓存复用。报告总体、阶段、读/算/写时间及吞吐量，不把归一化成本从首次运行中隐去。
4. 在每次独立进程中采集阶段 RSS、生命周期高水位与溢写目录峰值。需要验证硬内存上限时，由外部建立独占控制组并记录 `memory.peak`、交换空间、`oom/oom_kill`；另测研究协调者和结果交接的总开销。
5. 验证预算触发后清单状态、部分产物识别和下一次干净运行；检查主键唯一、血缘可追溯、几何自包含及 CRS 正确。提高规模保护阈值前先确认实际密度和剩余预算。
6. 固定区域顺序比较无质量剪枝与启用剪枝的计算量、完成/剪枝数和留出指标；将采样间隔及 gperftools 开关纳入记录，不把分析开销混入普通基准。

验收报告应给出实测值、目标值、输入规模和未覆盖场景。对于未达到要求的最密分组，应先定位外排、原生候选或全局列表占用，再决定分区/并行策略；当前不承诺内存随输入规模保持恒定。

## 实现与上游资料

- 数据契约：[artifacts.py](../services/pipeline/src/pipeline/artifacts.py)、[source.py](../services/pipeline/src/pipeline/source.py)。
- 调参语义：[tune.py](../services/pipeline/src/pipeline/tune.py)、[Optuna Trial](https://optuna.readthedocs.io/en/stable/reference/generated/optuna.trial.Trial.html)、[JournalStorage](https://optuna.readthedocs.io/en/stable/reference/generated/optuna.storages.JournalStorage.html)。
- 资源实现：[resources.py](../services/pipeline/src/pipeline/resources.py)、[profiling.cpp](../services/pipeline/native/profiling.cpp)、[gperftools 分配归属接口](https://github.com/gperftools/gperftools/blob/gperftools-2.15/src/gperftools/malloc_extension_c.h)。
- 数据库边界：[database.py](../services/pipeline/src/pipeline/database.py)、[结构化查询语言（Structured Query Language，SQL）审查登记](sql-index-reviews.json)、[历史运行边界](legacy-runtime.md)。
