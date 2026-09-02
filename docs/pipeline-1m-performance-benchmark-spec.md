# Pipeline 100 万行性能优化与基准 Spec

状态：已验证

基准日期：2026-09-01

适用范围：`services/pipeline/src/track/v2/` 当前运行架构

> 该结果只验证 100 万行。2,000 万行的线性规模推演和 3 GB 内存合同见
> [`Pipeline 20M memory budget Spec`](pipeline-20m-memory-budget-spec.md)。当前
> eager Detect/Clip 架构尚未通过该合同，不能把本页 1.204 GiB 峰值外推为达标证据。

## 1. 结论

在 `test.public.day01` 的 1,000,000 行真实轨迹数据上，当前 Pipeline 已从
Cleaner 完整运行到 Network，退出码为 0，单调时钟阶段耗时合计 120.588s
（CLI 显示 120.6s）。Pipeline 容器 cgroup 内存高水位为 1,292,587,008
bytes，即 1,232.7 MiB / 1.204 GiB；运行前后宿主机均未使用 swap。

相对 2026-08-31 的历史失败运行：

- Detect 检测计算（含工作区 DB 读取，不含派生产物落库）从 633.628s 降到
  49.951s，观察到约 12.7× 加速；
- 跨车空间计算从 348.775s 降到 26.925s，观察到约 13.0× 加速；
- 聚类流程从 150.468s 降到 4.512s，观察到约 33.3× 加速；
- 跨车 DBSCAN 从 143.9s 降到约 2.0s，观察到约 72× 加速；
- 拓扑验证从 120.388s 降到 2.071s，观察到约 58.1× 加速；
- 新运行还完成了历史失败运行没有进入的 Clip、Fitting 和 Network。

历史运行在 675.872s 后失败退出；当前完整 CLI 记录为 120.6s 并成功退出，观察
耗时缩短约 5.6×，而且完成了更多 Stage。由于完成边界不同，这个值只说明整体
工程效果，不作为严格端到端加速比。

历史运行和当前运行的跨车候选数不同，且历史运行在 Detect 落库时失败，
因此以上是同一真实数据集上的工程观察值，不是严格的逐输出微基准。本 Spec
不承诺字节级输出等价，也不要求同一 Run ID 的重复持久化完全幂等。

## 2. 基准契约

### 2.1 代码与配置标识

- Git branch：`main`；
- HEAD：`425509b5e82604b2e48cba565d05b34763e9009e`；
- 基准包含尚未提交的 Pipeline 优化工作树；
- `services/pipeline/src/track/v2/` diff SHA-256：
  `d5e71e3aba312683d10eb94ec3d5bffdd00ecab3a1a6e04146925d2af103c322`；
- `services/pipeline/src/track/v2/config/pipeline.toml` SHA-256：
  `67153b221dfac0f782a40b09e3954423f15da62b1942199d7a797ec18c42b5b3`。

仅 branch 或 HEAD 不能唯一标识本次被测代码；复测时必须同时记录工作树或使用
包含这些改动的提交。

### 2.2 数据集

| 项目 | 值 |
|---|---:|
| 数据库与表 | `test.public.day01` |
| 精确行数 | 1,000,000 |
| 车辆数 | 1,598 |
| 源 ID 范围 | 6,241,001–21,747,000 |
| 表及索引 | 527 MB |
| Cleaner 保留点 | 614,879 |
| Cleaner 注入点 | 996 |
| Detect 工作轨迹数 | 34,281 |

日志中的总行数 999,829 来自 `pg_class.reltuples`，只是进度估算；本次输入实际
读取行数及数据库精确计数均为 1,000,000。

### 2.3 机器与数据库

| 项目 | 值 |
|---|---|
| CPU | AMD Ryzen 5 5600H，12 个逻辑 CPU |
| Docker 内存配额 | 约 6.698 GiB |
| Swap | 2.0 GiB，运行前后使用量均为 0 |
| PostgreSQL | 16.4 |
| `shared_buffers` | 128 MiB |
| `work_mem` | 4 MiB |
| 运行前负载 | 约 0.5–0.8；无其他数据库会话 |
| 运行后负载 | 0.13 / 0.42 / 0.32 |

两个低 CPU 的无关视觉容器保持运行，合计占用约 734 MiB。数据计数和准备查询已
读取源表，因此这是 warm-cache 基准。目标数据库运行前约 962 MB，已有索引和
历史 Run；本次是向已有索引的数据库追加一个新 Run，不是空库导入。

### 2.4 命令与计时

Pipeline 镜像在计时前构建。被测命令为：

```sh
python -m pipeline run --pg-table public.day01 --database test
```

这是完整 CLI 路径；`--clean-id` 复用 Cleaner 的局部执行不能替代本基准。
阶段用时以 `[stage-finish]`、`[stage-phase]` 和 `[perf]` 中的单调时钟为准。
数据库 `started_at`/`finished_at` 以及容器墙钟只用于定位，不用于计算耗时。

内存口径：

- Pipeline 总峰值采用 cgroup `memory.peak`，它是容器内存记账高水位，不等同于
  单个 Python 进程 RSS；
- 阶段峰值来自约 3.3 秒一次的 `docker stats` 抽样，只能视为近似值；
- PostgreSQL 单独列出，不并入 Pipeline 容器峰值；
- 各优化条目的结构内存是数量级估算，结构共享且生命周期重叠，不能相加得到总峰值。

## 3. 完整 Pipeline 结果

### 3.1 阶段耗时

| Stage | 用时 | 总耗时占比 | 状态 |
|---|---:|---:|---|
| Cleaner | 33.406s | 27.7% | completed |
| Detect（计算和持久化） | 63.667s | 52.8% | completed |
| Clip | 19.309s | 16.0% | completed |
| Fitting | 4.194s | 3.5% | completed |
| Network | 0.012s | <0.1% | completed |
| 合计 | 120.588s | 100.0% | exit 0 |

Detect 的 49.951s 是四个 PerfTracker 子阶段合计，包含共享工作区的 DB 读取，
不含派生产物落库。Stage 与它相差的 13.716s 是未被这四个子阶段覆盖的综合区间，
还包括开头的空间 extent 查询、阶段间逻辑、日志/结果构建、持久化、ID 映射、
Run 收尾与 commit。这个差值不是连续的“计算后尾部”，只能作为持久化耗时上限。

Cleaner 的历史观察值为 30.4s，本次为 33.406s，单次运行约慢 9.9%。Cleaner
不是这一轮架构优化的目标，且两次数据库/cache 状态不同，因此不把该差异解释为
确定的性能回退。

### 3.2 Detect 内部

| 子阶段 | 当前用时 | Detect 计算占比 | 历史值 | 观察提升 |
|---|---:|---:|---:|---:|
| 轨迹流式检测 | 16.442s | 32.9% | 13.997s | 观察慢约 17.5%；差异未隔离 |
| 全局跨车空间计算 | 26.925s | 53.9% | 348.775s | 13.0× |
| 聚类流程 | 4.512s | 9.0% | 150.468s | 33.3× |
| 拓扑索引与验证 | 2.071s | 4.1% | 120.388s | 58.1× |
| Detect 计算合计 | 49.951s | 100.0% | 633.628s | 12.7× |

补充观测：

- 全局跨车网格：2,904,079 个 memberships，1 个 overflow segment，
  58,669 个候选，没有触发容量回退；
- 跨车 DBSCAN：58,669 点 → 341 簇，约 2.0s；
- 转向 DBSCAN：10,082 点 → 79 簇，约 0.1s；
- 拓扑索引构建 439.9ms，394 个 route window 共 778.4ms；
- 最终得到 56,170 个转向事件、10,082 个热点事件、394 个候选簇和
  175 个完整交叉口。

### 3.3 下游输出

| 输出 | 数量 |
|---|---:|
| `tracks_cross` | 58,669 |
| `turn_events` | 56,170 |
| candidate clusters | 394 |
| candidate intersection events | 68,438 |
| complete intersections | 175 |
| entry/exit points | 8,243 |
| clips | 31,826 |
| clip points | 494,646 |
| combined centerlines | 310 |
| directed centerlines | 530 |
| centerline manifests | 28,588 |
| network nodes / edges / isolated | 175 / 310 / 10 |

本次 Run ID：

- Clean：`580c273d-bb96-4120-bf29-adac33d424c5`；
- Detect：`320e2d6d-a6c6-4440-99dd-87c44381aca9`；
- Fit：`0551f0e3-2219-4225-8ae6-3588d8c53689`。

### 3.4 当前剩余热点

- Cleaner 为 33.406s，是当前第二大 Stage；
- Detect 内 `cross_track_grid_cells` 为 21.309s，占全局跨车计算的 79.1%；
- Detect 未被四个 PerfTracker 子阶段覆盖的综合区间约 13.716s，下一轮应先增加
  extent、结果构建和持久化的细分计时，再决定优化 ID-dependent INSERT、COPY
  还是 commit；
- Clip 为 19.309s，且 `docker stats` 抽样最高点出现在该阶段。继续优化时应同时
  控制全量 point/clip 生命周期，不能只看 intersection grid。

## 4. 内存与数据库资源

### 4.1 容器内存

| 阶段 | Pipeline 抽样峰值 | PostgreSQL 抽样峰值 | 说明 |
|---|---:|---:|---|
| Cleaner | 92.6 MiB | 705.3 MiB | 抽样在 Stage 开始约 18s 后才启动，可能漏掉早期峰值 |
| Detect | 1,049.6 MiB | 984.0 MiB | 共享轨迹、跨车索引和 Detect 结果共同驻留 |
| Clip | 1,226.8 MiB | 949.1 MiB | 本次 Pipeline 抽样最高点 |
| Fitting | 1,220.6 MiB | 943.5 MiB | 大部分是 Clip/结果对象继续驻留，并非 Fitting 独占 |
| Network | 未采到 | 未采到 | Stage 仅 12ms，低于抽样周期 |

Pipeline cgroup 精确峰值为 1,232.7 MiB。Pipeline 抽样峰值时 PostgreSQL
约为 943.6 MiB，两容器合计约 2.12 GiB；这是同一抽样时刻的近似值，不是
两个独立峰值相加。运行后宿主机仍有约 4.4 GiB available memory，swap 为 0。

### 4.2 数据库增量

本次向已有数据库追加结果后，`test` 从约 962 MB 增长到约 1,362 MB。基于
`pg_stat_database` 的前后差值，观察到约 519.3 MiB 临时文件、871,750 行插入，
以及约 2.12 GiB 的 block read。统计包含少量基准前后核验查询，仅用于容量规划，
不作为单个优化条目的归因证据。

## 5. 性能优化条目

### PERF-01：Detect 共享轨迹工作区

- 原因：旧 DB 路径在 Method 1/2、跨车候选和拓扑验证之间重复读取轨迹，且后续
  逻辑依赖大量按窗口数据库查询；数据库往返和重复对象构建成为主耗时。
- 改动：`_stream_extract_method_1` 在流式检测时构建一次
  `dict[vehicle_id, list[TrajectoryPoint]]`，由转向/正交检测、全局跨车计算和拓扑
  验证共享。实现见
  [`intersect2.py`](../services/pipeline/src/track/v2/intersect2.py)。
- 效果：它是 PERF-02 和 PERF-04 的架构前提。组合后 Detect 计算由 633.628s
  降到 49.951s；该收益不能与全局网格、DBSCAN、拓扑索引拆开单独归因。
- 内存：常驻 615,875 个有效点和 34,281 个轨迹列表，是 Detect 的主要内存来源，
  数量级为数百 MiB；所有 Detect 结构共同作用下的抽样峰值为 1,049.6 MiB。
  这项改动明确采用“内存换数据库往返”的架构。

### PERF-02：一次性全局跨车 Segment Grid

- 原因：历史实现遍历 8,541 个空间窗口，窗口计算耗时 348.775s；相邻窗口会
  重复处理 segment，并可能在窗口边缘漏掉端点不同时落入同一窗口的长 segment。
- 改动：一次构造全局 segment grid，按 canonical owner cell 去重，只生成跨车辆
  segment pair，并以 NumPy 批量完成精确求交。设置 8,000,000 memberships 和
  20,000,000 overflow pair-space 两个全局路径预算；超限时进入不发起逐窗口 SQL
  的内存 point-window 回退路径，该回退自身没有独立的硬容量上限。
  实现见 [`cross_track.py`](../services/pipeline/src/track/v2/cross_track.py) 和
  [`intersect2.py`](../services/pipeline/src/track/v2/intersect2.py)。
- 效果：跨车空间计算从 348.775s 降到 26.925s，约 13.0×。当前路径发现
  58,669 个候选，历史窗口路径为 53,549 个；候选簇由 390 变为 394，完整
  交叉口仍为 175。候选增加与更完整的窗口边界覆盖一致，但未逐候选归因；当前
  路径不是等价输出回放。
- 内存：2,904,079 个 membership 仅按 64 位引用下限计算约 22.2 MiB；segment
  对象、cell list、owner 数组及几何 NumPy buffer 还会占用数十到上百 MiB。
  这些对象与 PERF-01 工作区重叠，已包含在 Detect 约 1.025 GiB 抽样峰值中。
  显式预算用于阻止异常长轨迹造成无界网格增长。

### PERF-03：精确网格 DBSCAN 与 Active Frontier

- 原因：历史大输入路径已经有精确均匀网格和 epoch 去重，但在密集簇中仍会为
  core 判定反复物化、排序和遍历完整邻居集合；53,549 个跨车点的 DBSCAN
  单独耗时 143.9s。
- 改动：保留网格候选和精确距离语义，core 判定达到 `min_samples` 后短路；另为
  当前簇维护 active frontier，点入队时即从可用 cell set 删除，后续扩张只取得
  尚未入队的邻居，避免密集簇内重复物化和遍历。小输入及不能安全索引的非有限
  边界输入继续使用扫描路径。
  实现见 [`intersect2.py`](../services/pipeline/src/track/v2/intersect2.py)。
- 效果：跨车 DBSCAN 在候选增加到 58,669 点的情况下约 2.0s，观察到约 72×
  加速；包含 G*、两类 DBSCAN、合并和消融的聚类流程由 150.468s 降到
  4.512s，约 33.3×。
- 内存：新增/保留的 labels、core flags、point-to-cell、grid 和 active set 均为
  O(N) 状态。对 58,669 点，临时结构约为数 MiB 到低十几 MiB；聚类结束即可
  释放，未单独形成容器峰值。这里的 O(N) 只描述额外内存，最坏运行时间仍取决于
  点分布和配置。

### PERF-04：一次构建的拓扑点索引

- 原因：历史拓扑验证对每个候选簇重复查询和整理窗口内轨迹，390 个历史候选的
  总耗时为 120.388s。
- 改动：在共享轨迹上按跨度更大的坐标轴排序一次，保存坐标数组和点引用数组；
  每个窗口通过二分查找缩小范围，再执行精确 bbox 过滤。一次只物化当前窗口，
  同时间戳点保持流式轨迹顺序。
- 效果：拓扑索引和验证合计 2.071s，约 58.1×；其中索引构建 439.9ms，
  route window 查询合计 778.4ms。
- 内存：两个长度为 615,875 的引用数组，其 64 位引用下限约 9.4 MiB，另有列表
  与当前窗口分组开销。点对象复用 PERF-01 工作区，不再复制全量轨迹。

### PERF-05：Detect Append-only 表使用 COPY

- 原因：大量参数化 `INSERT VALUES` 会重复构造大 SQL 和参数数组，并增加协议解析
  与往返；历史运行还因 Schema 缺列在最后落库阶段整体回滚。
- 改动：`tracks_cross`、`turn_events` 和 `entry_exit_points` 改为 PostgreSQL
  COPY。确实需要生成 ID 的父表仍使用批量
  `INSERT ... RETURNING`；`turn_events.id` 是唯一持久化事件 ID，运行时计算索引
  不写入数据库，因此 COPY 后不再读取 ID map。
  实现见 [`db_writer.py`](../services/pipeline/src/track/v2/db_writer.py)。
- 效果：本次 191,520 条主要 append-only Detect 行成功持久化，完整 Detect
  Stage 为 63.667s。四个 PerfTracker 子阶段之外的 13.716s 还包含 extent 查询、
  阶段间逻辑、其他父子表写入、报告和 commit，因此只能作为持久化耗时
  上限，不能与历史失败的约 9.1s 直接计算加速比。
- 内存：COPY 协议逐行写入，协议缓冲近似常量；调用方仍保留事件和 row tuple，
  额外转换列表大致为低十几到数十 MiB。COPY 不负责降低 Detect 主工作区内存。

### PERF-06：Clean Point 分组后本地排序

- 原因：Clip 读取 kept 与 injected 点时，PostgreSQL 对数十万条宽 UNION row 做
  全局 `ORDER BY`，容易产生大量临时文件；真正需要的顺序只在单个 subtrack 内。
- 改动：数据库返回无全局排序的行，Python 按 `(vehicle_id, subtrack_index)` 分组，
  再在小组内按 `(anchor_sequence, point_kind, row_id)` 排序，保持既有顺序语义。
  实现见 [`queries.py`](../services/pipeline/src/track/v2/db/queries.py)。
- 效果：当前完整 Clip 为 19.309s。历史 84.811s 的 Clip 来自资源争用运行，不能
  用来宣称受控加速比；本条的直接收益是把全局数据库 sort/spill 变为可预测的局部
  排序。
- 内存：排序工作从 PostgreSQL 转移到 Pipeline。615,875 个四元组、分组 list 和
  排序临时键估计约 50–80 MiB，点对象本身另计。Clip 的 Pipeline 抽样峰值达到
  1,226.8 MiB，是全流程最高阶段。

### PERF-07：Clip 固定网格与有界 Overflow

- 原因：基线已为一次 Clip 复用一个 grid，但 cell size 由最大交叉口直径决定；
  一个大半径交叉口会把全局网格放得过粗，使普通 segment 命中过多候选。
- 改动：把全局 cell size 固定为 100m。由于小 cell 会让大圆占用过多 cells，
  单交叉口 bbox 超过 4,096 cells 时改放入有界 overflow collection；长 segment
  在枚举 cells 与扫描全部交叉口之间选择较便宜路径，最终始终执行精确保守
  bbox/圆相交判断。每次 Clip 复用一个 grid 的行为保持不变。
  实现见 [`clip.py`](../services/pipeline/src/track/v2/clip.py)。
- 效果：175 个交叉口、34,281 条轨迹生成 31,826 个 clips 和 494,646 个 clip
  points，Stage 用时 19.309s。由于缺少同资源、同代码其余部分不变的旧 Clip
  对照，本条不单列加速倍数。
- 内存：grid 未做独立 allocation 测量；每个普通交叉口最多进入 4,096 cells，
  超出的交叉口转入 overflow，因此结构规模受此阈值和交叉口数约束。它包含在
  Clip 的 1,226.8 MiB 抽样峰值中，不能从该总值分离。

### PERF-08：Fitting Centerline 批量 RETURNING 与 Manifest COPY

- 原因：历史路径为每条 centerline 及其 manifest 分别发起写操作，本地数据库
  往返也会成为 Fitting 主耗时；本数据集会产生 840 条 centerline 和 28,588 条
  manifest。
- 改动：三个 variant 的 centerline 合并为一次批量
  `INSERT ... RETURNING` 并建立 ID map；所有 `centerline_source_clips` 使用一次
  COPY。Fitting 另外记录 compute 和 persist 两个独立 phase。
  实现见 [`db_writer.py`](../services/pipeline/src/track/v2/db_writer.py) 和
  [`pipeline.py`](../services/pipeline/src/track/v2/pipeline.py)。
- 效果：本次 compute 3.447s、persist 0.677s、Stage 合计 4.194s。相对先前可复现
  的约 17.8s 持久化观测值，当前约为 26×；它是跨运行观察值，严格微基准仍需
  固定 PostgreSQL cache 和数据库大小。
- 内存：28,588 个 16 字段 manifest tuple 的引用载荷下限约 3.5 MiB；计入 tuple
  header、list 与少量新值后，临时开销约 5–10 MiB。Fitting 约 1.192 GiB 的抽样
  值主要继承自 Clip 结果，不应归因给批量写入。

## 6. 正确性与可观测性修复

以下改动是本次完整基准能够成立的必要条件，但不计入性能加速条目：

- 当前 Schema 初始化为 `turn_events` 提供 `tp_source_id` 和
  `tp_source_row_key`，非 reset 升级也会为旧表补列；
- Detect 普通异常会先回滚派生事务，再在新事务中把 Run 标为 `failed`；
- schema-qualified source 通过 `to_regclass(source_id)` 获取 planner estimate，
  `public.day01` 不再误报 0；
- Fitting 分别输出 `fit_compute` 和 `fit_persist`，避免把数据库阻塞误判为几何计算。

进程遭遇 `SIGKILL`、宿主机掉电或数据库不可达时，仍可能来不及把 `running` Run
标为失败；这属于运维 stale-run 清理范围。

## 7. 输出语义与取舍

- 全局 segment broad phase 比历史按点窗口策略覆盖更完整，允许候选数和最终候选
  簇发生变化；本次完整交叉口仍为 175。
- COPY 假设 Detect/Fit 使用新创建的 Run ID。同一 Run ID 的重复落库可能触发唯一
  约束，不承诺幂等；重算必须创建新 Run。
- 共享工作区显著减少 SQL 往返，同时引入常驻内存；本次 Pipeline 峰值达到约
  1.20 GiB。对当前 6.7 GiB Docker 配额可接受，且全程无 swap。
- 全局跨车网格超出显式预算时会回退到内存 point-window 路径。当前 1M 数据没有
  触发；极端长 segment 在该回退路径上的覆盖弱于全局 segment grid。

## 8. 建议回归门槛

同机器、同配置、warm cache、已有索引数据库上，建议把以下值作为人工回归门槛，
人为预留约 25% headroom 并按可读值取整；本次只有一个样本，没有测得耗时抖动
分布，这些值不作为跨机器 SLA：

| 指标 | 当前值 | 建议警戒线 |
|---|---:|---:|
| 完整 CLI | 120.6s | 150s |
| Detect 计算 | 49.951s | 62s |
| 全局跨车计算 | 26.925s | 34s |
| 聚类流程 | 4.512s | 5.7s |
| 拓扑验证 | 2.071s | 2.6s |
| Pipeline cgroup 峰值 | 1.204 GiB | 1.50 GiB |

每次回归还必须校验：退出码为 0，Clean/Detect/Fit 状态均为 `completed`；在未
声明语义变更时，完整交叉口为 175、clips 为 31,826、centerlines 为 840、
manifest 为 28,588、network nodes/edges 为 175/310。若有意改变算法语义，必须
评审并更新本 Spec 的期望计数。仅用耗时通过不能替代结果完整性检查。

## 9. 原始证据

- 历史 Before 数字见
  [`2026-08-31 1M validation (historical)`](performance-optimization-review.md#2026-08-31-1000000-row-validation-historical)。
  当时报告指向的旧 `/tmp` 日志已不在当前文件系统，未保留可重新校验的哈希；
  因此旧值只用于工程观察对比，不能升级为严格可复现的微基准；
- Benchmark metadata（包含命令、代码/配置标识、cgroup 原始值、环境、Run ID、
  阶段值及 `pg_stat_database` 前后快照）：
  `/tmp/roadnet-benchmark-day01-1m-20260901-1120/benchmark-metadata.txt`；
- Pipeline log：
  `/tmp/roadnet-benchmark-day01-1m-20260901-1120/logs/pipeline-benchmark-day01-1m-20260901.log`；
- Docker stats：
  `/tmp/roadnet-benchmark-day01-1m-20260901-1120/docker-stats.tsv`；
- Benchmark metadata SHA-256：
  `486679a00e12b37e71c48f7a7b79e46f26ab26057cc93ddb07becafb4205b30b`；
- Pipeline log SHA-256：
  `a455808c1092190173e5ea573fd2944d26dbb6bc022b39ced729eaa778a2248c`；
- Docker stats SHA-256：
  `45c59bdb410729927b3dde6656a143ae749aeb2c293f6ff338b44cf249954a3c`。

`/tmp` 证据不会随 Git 提交持久保存；长期归档时应把日志和 stats 移到外部制品
存储并保留上述哈希，不应把真实数据或运行时数据库写入 Repository。
