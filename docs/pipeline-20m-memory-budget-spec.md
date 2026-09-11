# Pipeline 2,000 万行内存预算与外存化 Spec

> 历史容量审计：本文基于 2026-09-02 的 pre-v3 对象生命周期。Schema v3 已将
> Clip 改为读取最终 resolved p/i/c 序列，并重构 Detect 持久化；文中的旧结构
> 数字不能作为当前实现验收结果。20M 验收前必须按
> [schema-v3 benchmark](schema-v3-cutover-benchmark.md) 重新建立基线。

状态：容量审计完成，当前实现不达标，20M 实测待外存化改造后执行

日期：2026-09-02

基线：[2026-09-01 100 万行完整基准](pipeline-1m-performance-benchmark-spec.md)

适用路径：PostgreSQL 大表输入的 Cleaner → Detect → Clip → Fitting → Network

## 1. 内存合同与当前结论

本 Spec 把“内存不超过 3 GB”定义为：

- fresh Pipeline parent cgroup 设置 `memory.max = 3,000,000,000 bytes`，所有
  Stage child cgroup 都位于其下；parent `memory.peak` 还必须小于该值，即约
  2,861 MiB / 2.794 GiB；
- 运行前设置 `memory.swap.max = 0`，结束时 `memory.swap.current = 0`；
- 使用新建 cgroup，或记录 `memory.events` 前后差值，要求本次运行新增的 `oom = 0`、
  `oom_kill = 0`；`memory.peak` 同样必须来自新建/已重置计量边界；
- PostgreSQL 是独立容器，单独报告内存、临时文件和磁盘，不计入上述 Pipeline
  3 GB。如果合同要求 Pipeline 与 PostgreSQL 合计小于 3 GB，必须另行收紧
  PostgreSQL cgroup、`work_mem`、并行 worker 和 Pipeline 软预算；
- 2,000 万行是 Cleaner 的精确输入行数；所有下游数量仅按 100 万行基准乘 20
  做容量规划，不是算法输出承诺。

当前代码不能满足这个合同，不能进入 20M 验收签字：

- Cleaner 的生产 PG CLI 基本是流式的，按分布保持的 20× 输入预计约
  0.1–0.25 GiB，但单个超长车辆仍没有硬上限；
- Detect 会把 12,317,500 个有效点全部保留为 Python 对象；只计两个模型对象、
  empty `extra` 和一个 list 槽的浅层下限已经约 3.304 GiB，容器 allocation 代理
  则约 6.83 GiB；
- Clip 会再次全量物化这 12,317,500 个点，保守结构下限约 4.54 GiB，计入数值、
  字符串和客户端查询缓冲后预计约 7–8 GiB；
- Detect、Clip、Fitting 的大对象生命周期还在 Pipeline 函数中重叠。

因此，本报告的 20M 数字是 projected，不是 verified。1M 的 1.204 GiB cgroup
峰值也不能简单乘 20：流式结构接近常量内存，全量对象结构才随行数近似线性增长。

### 1.1 当前内存报告

下表是本次容量审计的最终结论。1M 列来自 2026-09-01 cgroup/`docker stats`
观测；20M 列是按本 Spec 规模模型做的对象与工作集推算，不是 20M RSS 实测。

| Stage | 1M Pipeline 抽样峰值 | 20M 当前架构工作集推算 | 3 GB 结论 |
|---|---:|---:|---|
| Cleaner | 92.6 MiB（迟到抽样） | 分布保持时约 0.1–0.25 GiB；超长单车未硬约束 | 条件可控，尚不能签字 |
| Detect | 1,049.6 MiB | 12.3175M-point workspace 浅层下限约 3.304 GiB、容器 allocation 代理约 6.83 GiB；turn/cross output 另有独立风险代理 | **不达标** |
| Clip | 1,226.8 MiB | 全量输入结构下限约 4.54 GiB，预计约 7–8 GiB；Clip output point graph 下限另约 2.31 GiB | **不达标** |
| Fitting | 1,220.6 MiB | 自身新增约 0.35–0.40 GiB，但继承 Detect/Clip 大对象及 allocator 高水位 | **不达标** |
| Network | 未采到（Stage 12ms） | 独立加载约数 MiB至数十 MiB；当前同进程仍持有上游结果 | 算法可控，当前全链路不达标 |
| 完整 Pipeline | cgroup `memory.peak` 1.204 GiB | 无可信的去重后峰值推算；多个 O(N) 单项代理已分别违约 | **不达标，20M 未实测** |

上述各行不能直接相加：有些临时结构串行出现，有些对象共享底层引用；但 Detect
workspace、Detect eager output 和跨 Stage 保留确实重叠，workspace 浅层下限已足以
否定 3 GB 合同。当前不提供一个看似精确但重复计算共享对象的 20M 总峰值。

## 2. 规模合同

### 2.1 推演方法

Cleaner 原始输入固定为精确 20,000,000 行。其他数量使用：

```text
20M planning count = 2026-09-01 1M observed count × 20
```

这个模型等价于复制数据分布而不是把 20 份完全重叠的轨迹放在同一空间。若复制
数据用于实测，每份必须使用唯一主键和车辆 ID，并做足够远的空间平移；直接重叠
会使跨车 segment pair 接近平方增长，不再满足用户指定的线性下游假设。

### 2.2 规划数量

| 数据项 | 1M 实测 | 20M 规划值 | 口径 |
|---|---:|---:|---|
| Cleaner 原始输入 | 1,000,000 | **20,000,000** | 20M 必须精确校验 |
| 原始车辆 | 1,598 | 31,960 | 20× 推演 |
| kept points | 614,879 | 12,297,580 | 20× 推演 |
| injected points | 996 | 19,920 | 20× 推演 |
| Detect active points | 615,875 | 12,317,500 | kept + injected |
| tracks/subtracks | 34,281 | 685,620 | 20× 推演 |
| turn events | 56,170 | 1,123,400 | 20× 推演 |
| hotspot events | 10,082 | 201,640 | 20× 推演 |
| cross-track candidates | 58,669 | 1,173,380 | 20× 推演 |
| global grid memberships | 2,904,079 | 58,081,580 | 20× 推演 |
| candidate clusters | 394 | 7,880 | 20× 推演 |
| candidate events | 68,438 | 1,368,760 | 20× 推演 |
| candidate crossing points | 33,808 | 676,160 | 20× 推演 |
| complete intersections | 175 | 3,500 | 20× 推演 |
| complete entry/exit observations | 8,243 | 164,860 | 20× 推演 |
| clips | 31,826 | 636,520 | 20× 推演 |
| eligible clips | 14,294 | 285,880 | 20× 推演 |
| boundary clips | 17,532 | 350,640 | 20× 推演 |
| points in clips | 494,646 | 9,892,920 | 20× 推演 |
| combined centerlines | 310 | 6,200 | 20× 推演 |
| directed centerlines | 530 | 10,600 | 20× 推演 |
| centerline manifests | 28,588 | 571,760 | 20× 推演 |
| network nodes / edges / isolated | 175 / 310 / 10 | 3,500 / 6,200 / 200 | 20× 推演 |

100 万行源表及索引的 PostgreSQL pretty-size 报告值为 527 MB；按其二进制单位
机械外推约 10,540 MiB / 10.3 GiB。1M 新 Run 的数据库报告值约增长 400 MB，
同口径外推约 7.8 GiB。它们只是磁盘容量量级，索引页、TOAST、数据分布和 vacuum
状态会改变实际值。磁盘表大小本身不会直接决定 Pipeline 内存，读取方式才是
决定因素。

## 3. 表规模与内存关系

需要区分四类结构：

| 模式 | 近似公式 | 对 20M 的含义 |
|---|---|---|
| 流式 | `M ≈ base + fetch_batch + max_group` | 总表变大主要增加时间，不应线性增加内存 |
| 固定批次 | `M ≈ base + bytes_per_row × batch_limit` | 必须按字节而非只按行数设上限 |
| 全量物化 | `M ≈ base + bytes_per_row × N` | 12.3M Python 对象必然突破 3 GB |
| 跨 Stage 保留 | `M ≈ Σ live_stage_objects` | 单 Stage 看似可接受，叠加后仍会 OOM |

Python 对象的 `bytes_per_row` 不只是 dataclass 浅尺寸，还包括：

- `PointRef`、唯一 row key、vehicle/source 字符串；
- 每个数值字段的 Python float/int 对象；
- `extra` dict、tuple/list/dict/set 的槽位和扩容余量；
- DB client result buffer、NumPy scratch 和 CPython allocator 碎片。

cgroup `memory.peak` 还会记账 Pipeline cgroup 内的文件页缓存。因此把对象写到本地
temp file 并不自动等于零内存；spool 必须顺序访问、限制 mmap/window 大小，并在
消费后主动释放页缓存，或者优先放入单独计量的 PostgreSQL staging。最终仍以
cgroup 指标验收，不能只看进程 RSS。

只有明确标为“浅层下限”的结构数字用于证明最低占用；allocation proxy 只表示风险
量级，两者都不能当成精确 RSS 预测。最终验收权威值只能是 20M 容器的 cgroup
`memory.peak`。

### 3.1 全量物化清单

| Stage | 当前全量物化点 | 与 20M 规模关系 | 处理决定 |
|---|---|---|---|
| Cleaner | 公共 `clean()` 的 active/excluded/subtracks；生产 PG 路径不走它 | O(raw/active rows) | DB Pipeline 禁用 eager API；单车状态也要 spill |
| Detect | `tracks` dict、turn/ortho/cross lists、segment/grid、G*/DBSCAN、topology point arrays、`IntersectResult` | O(active points + events + memberships) | DB staging + tile/halo + shard；只返回 compact summary |
| Detect persistence | 全量 row tuple、`_with_stage_id` 第二份 list、百万级临时候选归属 map | O(output rows) | generator COPY + 分批/外存映射 |
| Clip | unnamed cursor result、`grouped_rows`、全部 `TrajectoryPoint`、`ClipInput`、完整 `ClipResult` | O(active points + clips + clip points) | named cursor，一次一个 subtrack，eligible clip 写 spool |
| Fitting | 全部 oriented coordinate tuples、pair NumPy stack、manifest rows | O(eligible clip points + manifests) | 一次一个 pair，sample matrix spill，generator manifest |
| Network | 自身图很小，已只读取 Clip 携带的 compact intersections，但 runner 仍保留上游结果与 `fit_bundle` | O(intersections + centerlines)，另继承上游 | 从持久化窄表加载，Stage 进程隔离 |

### 3.2 对象代理的证据等级

本次在现有 Pipeline 镜像内使用
`tracemalloc.get_traced_memory()[0]` 前后差除以样本数，样本量均为 100,000；每个
样本创建独立 row key、字符串、数值和 empty dict。环境和结果为：

| 项目 | 值 |
|---|---:|
| 镜像 ID | `sha256:57094894992a8c07918095aff867a2d9c0ed088471e8202bfe7f83029914b37e` |
| Python | 3.12.14 |
| `PointRef` shallow size | 104 B |
| `TrajectoryPoint` shallow size | 112 B |
| empty `extra` / one list slot | 64 B / 8 B |
| point workspace | 595.16414 B/item |
| standalone turn | 659.01750 B/item |
| standalone cross candidate + cluster wrapper | 1,022.79826 B/item |

命令通过一次性 `docker compose run --rm --no-deps pipeline` 执行，容器挂载当前源码；
没有读取数据库。它未覆盖 NumPy、libpq、native allocator、文件页缓存，脚本与输出
也尚未作为长期制品归档。因此它仍只是实验性 Python allocation 代理，不能当作
cgroup RSS 实测或严格下限。若后续要把精确 bytes/row 作为长期容量基线，必须把
harness 纳入仓库并归档命令、输出和代码哈希。

## 4. 各 Stage 审计

### 4.1 Parser + Cleaner

当前生产 PG CLI 的主要工作集：

- [`PostgresTrajectorySourceHandle.iter_points`](../services/pipeline/src/track/v2/sources.py)
  使用 named cursor，fetch size 为 10,000；
- 当前 PG source query 仍可能是 `SELECT *`；10,000 只限制行数，不限制超宽
  JSON/`extra` 等字段的 fetch bytes；
- `StreamVehicleSource` 一次保留一个完整车辆；
- [`clean_vehicle`](../services/pipeline/src/track/v2/cleaner.py) 在一个车辆内同时创建
  gap subtracks、stationary set、kept/removed 和 injected lists；
- [`run_pipeline`](../services/pipeline/src/track/v2/pipeline.py) 的 pending Cleaner
  输出在约 100,000 points 后、且只在完整车辆边界 flush。

生产 PG 路径的内存关系近似为：

```text
M_cleaner ≈ base
          + fetch_rows × fetched_row_bytes
          + pending_rows × persisted_row_bytes × batch_copy_factor
          + max_vehicle_rows × cleaner_state_bytes
```

它不是 O(20M)。在“车辆数和每车点数分布保持、只把总体放大 20×”的假设下，
根据 1M Cleaner 迟到抽样峰值 92.6 MiB，规划值约 0.1–0.25 GiB，建议 Stage
预算为 0.5 GB。

但当前仍不能做硬保证：

- flush 只能在完整车辆之后发生，单个超长车辆可绕过 100k 阈值；
- 公共 [`clean()`](../services/pipeline/src/track/v2/cleaner.py) 会累积所有 active、
  excluded 和 subtracks，12,317,500 active points 粗估约 4.5–7 GiB，禁止用于
  20M DB Pipeline；
- [`PostgresPipelineStore.persist_clean_batch`](../services/pipeline/src/track/v2/db/pipeline_store.py)
  当前直接清空 `pending_excluded` 而没有持久化；1M 的 zero-exclusion 数据恰好掩盖
  了这个正确性问题；
- Cleaner flush 还会创建全批 `assigned_subtracks` wrapper list；injected writer 会先
  创建全批 rows，再由 `_with_stage_id` 创建第二份 tuple list。这些受当前 100k
  软批次约束，但仍是必须纳入预算的批内物化；
- CSV 路径还会保留 raw insert ID map；本 Spec 的主合同只覆盖 PG 大表输入。

达标改造要求：

- PG 查询改为只投影 parser、排序和 source reference 需要的窄列；同时记录
  `max_fetched_row_bytes` / `max_fetch_batch_bytes`，不能只依赖 10k 行阈值；
- 用有状态的固定行/字节 reader 替换或绕过
  `StreamVehicleSource.iter_vehicle_tracks()` 的整车物化，再把 chunk 交给 Cleaner；
  reader 只保留 gap、stationary run 和 orthogonal injection 所需的最小
  look-behind；
- 超过单车硬上限的 undecided stationary run 写入外存，不允许继续扩大 Python list；
- kept/injected/excluded 使用生成器直接 COPY，禁止构造第二份 stage-ID row list；
- 真正写入 `excluded_points`；非零 exclusion 必须有持久化
  回归测试；
- 计时外 `COUNT(*)` 必须等于 20,000,000；流结束后 `ReadReport.total_rows` 也必须
  等于 20,000,000，且 `loaded_rows + skipped_rows = total_rows`。本合同要求 Cleaner
  收到精确 20M 有效输入，因此还要求 `skipped_rows = 0` 和 `total_raw = 20,000,000`；
  `pg_class.reltuples` 只能用于进度。

### 4.2 Detect

#### 全量轨迹 workspace

[`_stream_vehicle_tracks_from_db`](../services/pipeline/src/track/v2/db/queries.py) 本身
是 named cursor，但
[`_stream_extract_method_1`](../services/pipeline/src/track/v2/intersect2.py)
把每个 track 存入全局 `tracks` dict，直到 cross-track 和 topology 都结束才释放。

Pipeline Python 3.12 容器内，`PointRef + TrajectoryPoint + empty extra + 一个 list
slot` 的不重复浅层尺寸为 `104 + 112 + 64 + 8 = 288 B/point`：

```text
12,317,500 × 288 B ≈ 3.304 GiB > 2.794 GiB contract
```

它还没有计算每行字符串、float/int、track list/dict 头和 allocator 碎片，已经违反
合同。同一容器的 100k `tracemalloc` allocation 代理测得约 595.16 B/point：

```text
12,317,500 × 595.16414 B ≈ 6.83 GiB
```

这个数还不含 685,620 个 track list、外层 dict、turn/cross events 和算法 scratch，
单 workspace 已超过合同两倍。

#### Cross-track guard 与 fallback

1M 全局网格为 2,904,079 memberships；20× 规划为 58,081,580，必然超过当前
8,000,000 guard。但 guard 在 `_prepare_segments` 全量构造 Segment 对象之后才
检查，可能在进入 fallback 前已经 OOM。

当前 memory-window fallback 也不是外存路径：它把全量点引用复制到所有重叠
window buckets，处理后不立即 `pop`，而且原始 `tracks` 仍然驻留。因此调低 guard
或进入 fallback 都不能证明 3 GB。

#### G*、DBSCAN、Topology 与 eager result

20× 时还会同时出现：

- 1,123,400 turn events 的 G* bounds、grid、local counts 和 z-score map；
- 1,173,380 cross candidates 的 `xs`、`ys`、labels、core flags、point-cell tuples、
  grid、active sets 和 frontier；
- 12,317,500 points 的 topology `points` 与 `coordinates` 两个引用数组；
- `IntersectResult` 中全部 turns、cross candidates、1,368,760 cluster memberships、
  676,160 crossing points 和 164,860 observations。

standalone 对象代理估算中，turn 输出约 0.69 GiB，cross candidate 加 cluster
包装约 1.12 GiB。它们只表示 eager output 的另一组风险量级，不是 RSS 或严格
下限，也不能再与 6.83 GiB workspace 代理相加。

#### Detect 持久化

COPY 协议本身逐行，但 writer 调用方先创建全量 row tuple，`_with_stage_id` 再复制
一次。20× 转换期浅层临时量级：

| 写入 | 当前转换期临时量级 |
|---|---:|
| `tracks_cross` | 约 385 MiB |
| `turn_events` | 约 454 MiB |
| candidate events | 约 282 MiB |
| candidate crossings | 约 232 MiB |
| complete entry/exit | 约 42 MiB |

这些值是原 rows list 与 `_with_stage_id` 新 tuple list 转换瞬间的 CPython 浅层
载荷，未计字段对象和 libpq buffer；五个批次顺序发生，不能彼此相加，转换返回后
原 list 可释放，但每批都与完整 `IntersectResult` 共存。百万级 turn/crossing
候选归属 map 只在 Detect 持久化期间临时存在；`DetectPersistenceResult` 已只返回
计数，不再把这些 map 传给后续 Stage。

#### Detect 结论

当前 Detect 含多个随 active points/events 线性增长的 O(N) 结构，workspace 的
不重复浅层下限已经超过 3 GB，足以判定不达标。不能只通过添加 `slots`、`del`、
`gc.collect()` 或调整 membership guard 修复，必须采用外存分区执行器：

- 逐车生成 turn、ortho、segment staging rows，并按固定字节批次直接 COPY；
- segment 按空间 tile + 精确 halo 查询，canonical owner tile 去重；高密 tile 按
  segment 数/估算 bytes 递归细分，长 segment 进入带 GiST bbox 的 overflow 表；
- G* 按 home cell + radius halo 两遍处理，DB 聚合 mean/variance；
- DBSCAN 的 core/edge 按 tile 生成，跨 tile component 用紧凑 union-find 或外存
  union edge；border point 必须定义稳定归属；
- cluster membership 写表，不再把 CrossTrackCandidate 再包装成 TurnEvent；
- topology 按 cluster + vehicle hash shard 查询 segment staging；
- Detect 直接持久化，只返回 counts 和约 3,500 个 compact intersections。

### 4.3 Clip

[`fetch_clean_points`](../services/pipeline/src/track/v2/db/queries.py) 当前用 unnamed
client cursor 读取全部 Clean 点；`_group_points` 同时保留：

- 12,317,500 个 `TrajectoryPoint`、`PointRef` 和 `extra` dict；
- 每点一个 `(anchor_sequence, point_kind, row_id, point)` tuple；
- grouped dict/list 与最终 point lists；
- libpq 可能缓存的完整查询结果。

仅 `PointRef + TrajectoryPoint + empty extra + 一个容器引用` 的低估值约
3.304 GiB；加 grouped tuple 和返回结构的保守下限约 4.54 GiB，计入 floats、
strings 和 DB client buffer 后预计约 7–8 GiB。

随后 `extract_clips` 又累积 636,520 个 TrackClip、1,273,040 endpoints，以及约
8.62M interior point references。Clip output 保守 point graph 估值约 2.31 GiB；
其中 interior tuple 引用复用输入的 `TrajectoryPoint`，不是在 4.54/7–8 GiB 之上
再创建一套点对象，但会让这些点继续存活，因此两组数字不能机械相加。

达标改造要求：

- 使用 server-side named cursor，按 `(vehicle_id, subtrack_index, order)` 流式归并
  kept/injected，一次只保留一个 subtrack；
- Clip reader 使用窄 point，只带 timestamp/lon/lat/order，不加载 heading、speed、
  extra 和 PointRef；
- boundary clip 只更新计数，不保存对象；eligible clip 连续写入 run-scoped、按
  pair 建索引的 PostgreSQL/temp-file spool；
- PostgreSQL spool 缓存归 PostgreSQL 容器单独计量；本地 spool 的 page cache、
  readahead 和 mmap window 必须计入 Clip Pipeline cgroup 预算；
- intersection grid 设置全局 membership bytes 上限；单交叉口 4,096-cell cap
  不能替代全局预算；
- 记录 `max_subtrack_points`、`max_live_clip_points`、`clip_grid_memberships`、
  `spool_rows` 和 `spool_bytes`。

### 4.4 Fitting

当前 Fitting 在完整 `ClipResult` 常驻时又执行：

- 为全部 285,880 eligible clips 创建 oriented lon/lat tuples；按平均点数估算约
  4.44M coordinate tuples，仅 tuple 和外层引用约 271 MiB；
- 最大 connected pair 一次性构造 NumPy sample stack，没有字节预算；
- 持久化先建 `clips_by_id`，再物化 571,760 个 16-field manifest rows，约
  109–115 MiB，最后才进入流式 COPY。

Fitting 自身新增量不是当前最大风险，问题是它与完整 Detect、Clip 对象共存。

达标改造要求：

- 从 clip spool 按 `(from_intersection_id, to_intersection_id)` 一次读取一个 pair；
- 同一遍生成 combined、A→B、B→A，避免三次全量遍历；
- 对 `max_clips_per_pair` 和 `max_pair_sample_matrix_bytes` 设置硬预算，超限使用
  memmap/DB spill，而不是依赖平均 pair 大小；
- 本地 memmap 的 resident pages 仍计入 Fitting Pipeline cgroup；记录 peak resident
  window，并用有界 window/顺序访问/reclaim 控制 page cache；
- centerline 按固定 pair 批次 `INSERT ... RETURNING`；manifest 用一次性 generator
  直接 COPY，禁止完整 `manifest_rows`；
- issue 只保留计数和有界样本。

### 4.5 Network

20× 规划只有约 3,500 nodes 和 6,200 combined edges，Network 自身预计为数 MiB
到几十 MiB，不是主要风险。

当前问题是 Network 为取得 intersections 而迫使完整 `iresult` 活到 Pipeline 最后。
`build_network` 已支持 `Iterable[ClipIntersection]`，应从持久化 intersections 加载
轻量对象；combined centerlines 也从持久化结果读取。Detect/Fit 大对象不得跨入
Network Stage。另记录 `total_centerline_points`、`max_centerline_points` 和
`max_geometry_bytes`，防止异常超长 centerline 绕过 node/edge 数量预算。

### 4.6 跨 Stage 生命周期

当前 [`run_pipeline`](../services/pipeline/src/track/v2/pipeline.py) 在同一 Python
函数和进程中保留 `iresult`、`clip_result`、`fit_bundle`，即使局部变量 `del`，
CPython arena 也不保证把 RSS 归还给 OS。

硬合同要求小型 orchestrator 分别启动 Cleaner、Detect、Clip、Fitting 和 Network
子进程。每个子进程只通过 Run ID/数据库/spool 传递状态；进程退出由 OS 回收全部
heap 和 native allocator 高水位。`gc.collect()`、`malloc_trim()` 只能是辅助，
不能作为 3 GB 证明。

## 5. 目标内存预算

五个 Stage 在独立子进程中顺序执行，以下预算不相加：

以下 MB 均为十进制 MB，与 3,000,000,000-byte 硬门槛口径一致。
软上限指该 Stage 运行时整个 Pipeline cgroup 的工作集，包含 orchestrator、Python
heap、NumPy/libpq native buffer、allocator 保留和文件页缓存，不是算法对象净大小。

| Stage | 目标软上限 | 硬门槛前余量 | 当前状态 |
|---|---:|---:|---|
| Cleaner | 500 MB | 2.5 GB | 预计可控，但缺超长车辆 spill |
| Detect | 2,500 MB | 500 MB | 不达标，必须外存化 |
| Clip | 1,500 MB | 1.5 GB | 不达标，必须流式 + spool |
| Fitting | 1,500 MB | 1.5 GB | 不达标，必须按 pair + spill |
| Network | 256 MB | 2.744 GB | 算法可控，需切断上游引用 |

Detect 内部互斥工作区预算：

| 子阶段 | 最大工作集 |
|---|---:|
| 逐车 decode / staging COPY | 256 MB |
| 一个 segment tile + halo | 512 MB |
| G* / DBSCAN / union scratch | 512 MB |
| 一个 topology shard | 512 MB |
| COPY output batch | 128 MB |
| runtime、allocator、page-cache 预留 | 至少 768 MB |

触及 2.25–2.5 GB Pipeline 软水位时必须 split/spill，不能继续分配并等待 3 GB
cgroup OOM。预算必须以 bytes 和实测 high-water 校准，不只配置“多少行”。

## 6. 改造验收顺序

### Phase A：消除隐式复制

- 给 `TrackModel` 增加空 slots，使 `slots=True` 子类真正没有实例 dict；
- `_copy_insert` 接受一次性 iterable，stage ID 由 generator 附加；
- 修复 Cleaner exclusion self-copy/跨 subtrack 累积，并持久化 exclusions；
- Detect 完成后只保留 compact intersections，并及时释放临时候选归属 maps；
- 新增精确输入行数断言和所有 high-water telemetry。

这些改动会降低常数，但单独完成 Phase A 仍不能宣称 20M 达标。

### Phase B：Detect 外存化

- 建立 run-scoped event/segment/cluster membership staging；
- 完成 tile/halo、overflow、G* 两遍、跨 tile DBSCAN union 和 topology shard；
- 保留当前 eager `IntersectResult` 仅作为小输入/单测 API；DB Pipeline 使用 bounded
  executor。

### Phase C：Clip/Fitting 外存化

- 一次一个 subtrack 生成 Clip；
- eligible clip 写 pair-indexed spool；
- 一次一个 pair 拟合三个 variant；
- centerline 与 manifest 边算边持久化。

### Phase D：进程隔离与 20M 验收

- 独立 Stage 子进程；
- fresh Pipeline parent cgroup `memory.max=3,000,000,000` bytes、
  `memory.swap.max=0`；每个 Stage child cgroup 单独计量；
- 执行精确 20M 实测并归档 stage/cgroup/PostgreSQL 证据。

## 7. 20M 最终内存报告格式

完成改造后的正式报告必须包含：

| 指标 | 必填值 |
|---|---|
| exact source rows | 必须为 20,000,000 |
| planned / actual downstream counts | 同时报告，解释偏差 |
| 每 Stage child cgroup `memory.peak` | Cleaner、Detect、Clip、Fitting、Network；`VmHWM` 仅作辅助 |
| Pipeline parent `memory.max` / `memory.peak` | 3,000,000,000 bytes / bytes、MiB、GiB |
| `memory.swap.max` / `memory.swap.current` | 必须为 0 / 0 |
| `memory.events` | 新 cgroup 或前后差值：`oom=0`、`oom_kill=0` |
| max group | `max_vehicle_points`、`max_subtrack_points` |
| Detect partition | max tile rows/bytes、overflow segments、union edges |
| Clip spool | rows、bytes、max live points、grid memberships |
| Fitting pair | max clips/pair、max sample matrix bytes、spill count |
| PostgreSQL | cgroup `memory.peak`（或注明采样周期的近似峰值）、temp files/bytes、database growth |

验收条件：

1. source `COUNT(*)`、`ReadReport.total_rows`、`loaded_rows` 和 `total_raw` 均为
   20,000,000，`skipped_rows=0`；
2. Cleaner、Detect、Clip、Fitting、Network 全部完成；
3. fresh Pipeline parent cgroup 的 `memory.max=3,000,000,000`、
   `memory.peak < 3,000,000,000 bytes`、`memory.swap.max=0`，且无 swap/OOM；
4. 所有分区/单组 hard limit 均有 telemetry，发生超限时进入 split/spill 而不是
   累积或失败；
5. 输出完整性通过。若算法语义有意变化，必须更新对应差分基线，不能只看行数。

在 Phase B、C、D 完成并跑过真实 20M 前，最终内存占用栏必须写“未验证”，不能
用本 Spec 的 projected 数字代替实测。

## 8. 测试要求

- poison/lazy iterator 证明 COPY writer 不会预消费全量 rows；
- 非零 exclusion 跨多个 subtrack 后必须完整持久化，不能 self-copy 或静默丢弃；
- 超宽 JSON/`extra` source row 必须被窄投影隔离，fetch batch bytes 有硬上限；
- 单个超长车辆、stationary run 和 subtrack 必须触发 spill；
- segment 跨 tile 边/角、长 segment overflow、同车过滤和 canonical owner 去重；
- G* halo、DBSCAN 跨至少三个 tile 的 chain、noise→border、多簇 border；
- topology 的 vehicle shard、时间块边界、等 timestamp；
- Clip endpoint、clip ID/order、三个 centerline variant 和 manifest 顺序与小输入
  eager oracle 差分一致；
- 一个超大 connected pair 必须触发 sample matrix spill；
- 一个超长 centerline 必须触发 Network geometry hard limit 或有界流式路径；
- Stage 子进程结束后，下一 Stage 不保留前一 Stage Python 对象；
- 1×/2×/4× 分区测试的峰值随最大 partition 而非总 N 增长；
- 20M cgroup 硬限制集成测试。
