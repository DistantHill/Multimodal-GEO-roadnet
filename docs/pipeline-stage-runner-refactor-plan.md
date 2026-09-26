# Pipeline 阶段运行器重构计划

状态：已实施。用户授权按纯结构重构一次完成并以单个提交交付。

本文遵循[项目术语与写作规范](terminology.md)，说明如何把当前集中在
`track.v2.orchestration.run_pipeline.run_pipeline()` 中的阶段实现提取为独立阶段运行器，使
`orchestration/run_pipeline.py` 只保留数据处理流水线（Pipeline）的顺序编排、阶段选择和最终结果汇总。

## 1. 背景

当前 `run_pipeline()` 同时承担以下职责：

- 校验 Pipeline 输入并建立 PostgreSQL 连接；
- 新建或复用精炼阶段（Refine）运行批次（Run）；
- 执行解析阶段（Parser）与 Refine 的逐车流式处理；
- 控制 Refine PostgreSQL 写入、来源指纹核对和 Parquet 发布；
- 执行检测阶段（Detect）的计算、持久化、Parquet 发布和失败收敛；
- 执行片段提取阶段（Clip）并管理其延迟关闭资源；
- 执行拟合阶段（Fitting）的计算、持久化和 Run 生命周期；
- 执行路网构建阶段（Network）；
- 汇总全部统计字段并发送阶段事件。

算法实现已经分别位于 `trajectories/refine_trajectories.py`、`intersections/detect_intersections.py`、
`intersections/extract_topology.py`、`clips/extract_clips.py`、`centerlines/fit_centerlines.py` 和 `road_network/build_road_network.py`，但阶段生命周期仍内联在 `orchestration/run_pipeline.py`。这使 Pipeline 编排器同时依赖
算法、数据库、文件发布、阶段进度和失败恢复细节，增加了审查、测试和后续修改的耦合。

## 2. 范围说明

当前正式 Pipeline 包含五个业务阶段：

1. Refine，其中包含 Parser 的数据源读取；
2. Detect；
3. Clip；
4. Fitting；
5. Network。

因此，Refine 样板完成后剩余四个阶段运行器。Pipeline 最后的统计汇总、资源收尾和完成
消息属于编排职责，不定义为额外业务阶段。

本次计划包含：

- 建立五个正式阶段运行器；
- 建立阶段间强类型引用和结果；
- 保持现有事务、Run 状态和 Parquet 发布顺序；
- 将 `run_pipeline()` 收缩为阶段选择与顺序调用；
- 迁移阶段生命周期测试和 Pipeline 编排测试。

本次计划不包含：

- 修改 Refine、Detect、Clip、Fitting 或 Network 算法；
- 修改 PostgreSQL Schema 或 Run 状态模型；
- 修改 Parquet Schema、文件布局或生命周期事件；
- 修改配置字段及其语义；
- 修改 CLI 参数、统计字段或用户可见输出语义；
- 将 Parser 拆为独立的第六个业务阶段；
- 引入通用工作流框架、依赖注入框架或新的插件机制。

## 3. 目标与非目标

### 3.1 目标

1. `orchestration/run_pipeline.py` 只负责：
   - 输入校验；
   - PostgreSQL 连接和数据库结构版本校验；
   - 新建或复用 Refine 的选择；
   - 五个阶段的顺序调用；
   - 阶段结果到兼容 `PipelineResult.stats` 的汇总；
   - Pipeline 总耗时和完成消息。
2. 每个 `*_stage.py` 负责本阶段的：
   - Run 生命周期；
   - 算法入口调用；
   - 持久化和事务边界；
   - 制品发布；
   - 进度与报告；
   - 失败收敛；
   - 阶段私有资源关闭。
3. 阶段间通过不可变数据类传递身份和结果，不共享可变 `stats` 字典。
4. 阶段运行器不改变现有算法模块的职责。
5. 每一步重构均保持测试可运行，避免一次性迁移全部阶段。

### 3.2 非目标

- 不要求把 `run_pipeline()` 压缩成单屏代码；目标规模约为 120–180 行。
- 不把所有阶段统一成同一个抽象基类。
- 不用通用异常处理器覆盖 Refine、Detect 和 Fitting 不同的失败语义。
- 不让阶段运行器直接读取未经集中配置解析的环境变量。
- 不让阶段运行器隐式创建新的 PostgreSQL 主连接。

## 4. 目标文件结构

```text
services/pipeline/src/track/v2/
├── orchestration/run_pipeline.py
├── stages/types.py                # 阶段引用、结果及 Clip 资源所有权
├── stages/support.py              # 仅包含真正通用的事件和计时辅助函数
├── stages/refine.py
├── stages/detect.py
├── stages/clip.py
├── stages/fitting.py
├── stages/network.py
│
├── trajectories/refine_trajectories.py # Refine 算法
├── intersections/detect_intersections.py # Detect 交叉口检测
├── clips/extract_clips.py       # Clip 算法
├── centerlines/fit_centerlines.py # Fitting 中心线算法
├── road_network/build_road_network.py # Network 算法
├── orchestration/events.py
└── db/
    └── pipeline_store.py
```

`services/pipeline/generated/refine_stage.py` 只作为审查样板，位于被 Git 忽略的目录，
不得成为正式运行时导入路径。实施时应根据当前接口修正后迁入
`services/pipeline/src/track/v2/stages/refine.py`。

## 5. 阶段间数据模型

以下阶段引用、结果及 `ClipStageResult` 统一定义于 `stages/types.py`。该模块只依赖
现有模型、制品描述符及持久化结果类型，不导入 `orchestration/run_pipeline.py` 或任何阶段运行器。
`PipelineInput`、`PipelineResult` 留在 `orchestration/run_pipeline.py`，阶段运行器不依赖这两个编排类型，
避免循环导入。`stages/support.py` 不承担类型容器职责。

### 5.1 持久化阶段引用

```python
@dataclass(frozen=True, slots=True)
class RefineStageRef:
    refine_id: str
    source_table: str
    spatial_context: SpatialContext


@dataclass(frozen=True, slots=True)
class DetectStageRef:
    detect_id: str
    refine: RefineStageRef


@dataclass(frozen=True, slots=True)
class FittingStageRef:
    fit_id: str
    detect: DetectStageRef
```

阶段引用只描述稳定身份和下游必需上下文，不持有数据库游标、读取器、临时目录或大型计算
结果。

### 5.2 阶段结果

```python
@dataclass(frozen=True, slots=True)
class RefineStageResult:
    ref: RefineStageRef
    source_point_count: int
    kept_point_count: int
    excluded_point_count: int
    injected_point_count: int
    vehicle_count: int
    artifact: ArtifactDescriptor
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class DetectStageResult:
    ref: DetectStageRef
    artifact: ArtifactDescriptor
    persisted: DetectPersistenceResult
    compute_elapsed_s: float
    persist_elapsed_s: float
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class FittingStageResult:
    ref: FittingStageRef
    bundle: CenterlineFitBundle
    centerline_ids: tuple[int, ...]
    compute_elapsed_s: float
    persist_elapsed_s: float
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class NetworkStageResult:
    result: NetworkResult
    elapsed_s: float
```

Clip 的结果包含延迟读取资源，单独采用可关闭对象，见 8.3。

新建与复用分支统一向下游提供 `refine_ref: RefineStageRef`；只有新建分支产生
`refine_result: RefineStageResult | None`，复用时其值为 `None`。不让同一个变量有时
表示引用、有时表示结果，也不为复用分支伪造计数、制品描述符或本次计算耗时。

## 6. 公共阶段辅助边界

建议新增 `stages/support.py`，第一版只容纳以下无状态辅助函数：

```python
def emit_message(emit: EventSink, message: str) -> None: ...
def emit_report(emit: EventSink, title: str, report: Mapping[str, object]) -> None: ...
def log_stage_start(stage: str, **run_ids: object) -> float: ...
def log_stage_finish(stage: str, started_at: float, **run_ids: object) -> float: ...
```

以下内容不得进入通用辅助层：

- Refine 的 staging 放弃和失败 Run 收敛；
- Detect 的制品发布后 orphan 语义；
- Fitting 的完成报告；
- Clip 的延迟资源关闭；
- 任一阶段的算法调用或持久化调用。

这些行为具有不同状态机，过早统一会隐藏事务差异。

## 7. 目标 Pipeline 编排

目标代码形态如下，参数与第 8 节的阶段签名保持一致。新建与复用选择直接保留在
`orchestration/run_pipeline.py`；新建分支也在此由 `config.projection` 创建 `SpatialContext`，复用分支
始终采用历史 Run 的空间上下文，不使用本次投影配置覆盖它。

```python
def run_pipeline(
    pipeline_input: PipelineInput,
    config: PipelineConfig,
    *,
    emit: EventSink | None = None,
) -> PipelineResult:
    sink = _noop_event_sink if emit is None else emit
    _validate_pipeline_input(pipeline_input)
    parameters = config.snapshot_parameters()
    started_at = time.perf_counter()

    with pg_connect(config.postgres) as conn:
        require_current_schema(conn)
        store = PostgresPipelineStore(conn)
        refine_result: RefineStageResult | None = None
        if pipeline_input.refine_id is not None:
            record = store.get_refine_run(pipeline_input.refine_id)
            if record.status != "completed":
                raise ValueError(f"Refine Run {record.id} is not completed")
            refine_ref = RefineStageRef(
                refine_id=record.id,
                source_table=record.source_table,
                spatial_context=record.spatial_context,
            )
            # 保持现有复用消息；这些零表示本次未执行 Refine，不写入 stats。
            emit_message(sink, "  排除 / Excluded: 0")
            emit_message(sink, "  注入 / Injected: 0")
        else:
            assert pipeline_input.source_table is not None
            spatial_context = create_spatial_context(
                config.projection,
                processing_srid=config.projection.processing_srid,
            )
            refine_result = run_refine_stage(
                store=store,
                source_table=pipeline_input.source_table,
                spatial_context=spatial_context,
                postgres_config=config.postgres,
                parser_config=config.parser,
                refine_config=config.refine,
                runtime_config=config.runtime,
                paths_config=config.paths,
                parameters=parameters,
                emit=sink,
            )
            refine_ref = refine_result.ref

        detect = run_detect_stage(
            store=store,
            postgres_config=config.postgres,
            refine=refine_ref,
            detect_config=config.detect,
            runtime_config=config.runtime,
            paths_config=config.paths,
            verify_sha256=config.resources.verify_artifact_sha256,
            parameters=parameters,
            emit=sink,
        )

        with run_clip_stage(
            store=store,
            detect=detect.ref,
            clip_config=config.clip,
            runtime_config=config.runtime,
            paths_config=config.paths,
            verify_sha256=config.resources.verify_artifact_sha256,
            emit=sink,
        ) as clip:
            fitting = run_fitting_stage(
                store=store,
                detect=detect.ref,
                clip=clip,
                clip_config=config.clip,
                fitting_config=config.fitting,
                parameters=parameters,
                emit=sink,
            )

            network = run_network_stage(
                fitting=fitting,
                intersections=clip.result.intersections,
                config=config.network,
                emit=sink,
            )

        emit_message(sink, "\n  提交 / Committed.")

    # 总耗时包含 Clip 资源关闭及 PostgreSQL 上下文退出。
    elapsed_s = time.perf_counter() - started_at
    result = _build_pipeline_result(
        refine_ref=refine_ref,
        refine_result=refine_result,
        detect=detect,
        clip=clip,
        fitting=fitting,
        network=network,
        elapsed_s=elapsed_s,
    )
    emit_message(sink, f"\n── 完成 / Done ({elapsed_s:.1f}s) ──")
    return result
```

`run_refine_stage()` 只执行新 Run，不复用历史 Run。下游制品存在性和来源验证继续由
Detect 执行。`_build_pipeline_result()` 只读取关闭后仍有效的报告、身份与耗时，不能再次
遍历 `clip.result.clips`，也不执行数据库或文件读取。

## 8. 各阶段设计

### 8.1 Refine Stage

建议接口：

```python
def run_refine_stage(
    *,
    store: PipelineStore,
    source_table: str,
    spatial_context: SpatialContext,
    postgres_config: PostgresConfig,
    parser_config: ParserConfig,
    refine_config: RefineConfig,
    runtime_config: RuntimeConfig,
    paths_config: PathsConfig,
    parameters: dict[str, object],
    emit: EventSink,
) -> RefineStageResult:
    ...
```

职责：

1. 验证源表并构造 `PostgresSource`；
2. 新建 Refine Run 并保存完整配置快照；
3. 使用 Parser 配置打开源数据；
4. 按车辆流式读取并调用 `refine_vehicle()`；
5. 按既有阈值批量写出剔除点、保留点和注入点；
6. 验证运行边界与坐标参考系（coordinate reference system，CRS）；
7. 计算来源指纹并提交 PostgreSQL 权威结果；
8. 创建 staging，导出和校验 Refine Parquet；
9. 在独立视图中复核来源指纹；
10. 原子发布 Parquet；
11. 登记制品并完成 Refine Run；
12. 失败时放弃未发布 staging，并将仍为 `running` 的 Run 收敛为 `failed`。

当前生成样板需要修正：

- `parameters` 应使用 `PipelineConfig.snapshot_parameters()` 的完整结果；
- `get_database()` 应接收 `parser_config.fields`；
- `export_refine_parquet()` 应接收 `data_root`；
- `export_refine_parquet()` 应接收
  `postgres_extension_path=paths_config.duckdb_postgres_extension_path`；
- 路径必须来自 `PathsConfig`，不直接解析环境变量；
- 正式模块必须使用包内相对导入。

复用已有 Refine Run 时设置 `refine_ref`，并保留 `refine_result=None`。当前实现仅将
复用分支的局部计数设为零，并发送排除、注入为零的消息；它已经省略三个 Refine 统计键，
并未在 `PipelineResult.stats` 中用零表示历史数据量。本次保持这一键存在性及消息行为，
不增加历史计数查询；历史 Run 的 `RefineRunRecord` 也没有完整计算计数摘要。

### 8.2 Detect Stage

建议接口：

```python
def run_detect_stage(
    *,
    store: PipelineStore,
    postgres_config: PostgresConfig,
    refine: RefineStageRef,
    detect_config: IntersectConfig,
    runtime_config: RuntimeConfig,
    paths_config: PathsConfig,
    verify_sha256: bool,
    parameters: dict[str, object],
    emit: EventSink,
) -> DetectStageResult:
    ...
```

职责：

1. 新建 Detect Run；
2. 查询并校验已登记的 Refine Parquet；
3. 建立 `RefineDatasetReader`；
4. 构造 `DetectRuntimeContext`；
5. 执行 `detect_intersections()`；
6. 关闭读取器并结束计算侧数据库快照；
7. 持久化 Detect 结果并提交 PostgreSQL 权威行；
8. 立即关闭 `IntersectResult` 并解除运行器对结果的引用；
9. 导出和校验 Detect Parquet；
10. 复核来源指纹；
11. 原子发布 Detect Parquet；
12. 登记制品并完成 Detect Run；
13. 失败或取消时关闭尚未释放的 `IntersectResult`；
14. 失败时放弃未发布 staging，将未完成 Run 标记为 `failed`。

必须保持的顺序：

```text
Detect 计算
→ PostgreSQL 结果持久化和提交，Run 仍为 running
→ 立即关闭 IntersectResult，释放计算暂存关系
→ Detect Parquet staging 导出与校验
→ 来源指纹复核
→ Parquet 原子发布
→ 制品登记和 Run completed 同一事务提交
```

发布完成后若数据库登记失败，文件成为不可见 orphan，由既有对账流程处理，不得尝试回滚
文件系统原子发布。

提前释放是强制约束，不能只在运行器最外层 `finally` 中等到导出或登记结束后释放。
成功路径将结果引用清空；异常路径仅关闭仍被持有的结果，保持当前释放时点。

### 8.3 Clip Stage

建议接口：

```python
def run_clip_stage(
    *,
    store: PipelineStore,
    detect: DetectStageRef,
    clip_config: ClipConfig,
    runtime_config: RuntimeConfig,
    paths_config: PathsConfig,
    verify_sha256: bool,
    emit: EventSink,
) -> ClipStageResult:
    ...
```

`ClipStageResult` 应拥有 `ClipResult` 的资源生命周期：

```python
class ClipStageResult:
    result: ClipResult
    assembly_elapsed_s: float
    elapsed_s: float

    def close(self) -> None: ...
    def __enter__(self) -> "ClipStageResult": ...
    def __exit__(self, *args: object) -> None: ...
```

Pipeline 使用：

```python
with run_clip_stage(...) as clip:
    fitting = run_fitting_stage(...)
    network = run_network_stage(...)
```

Clip 不拥有独立 Run，但其读取资源必须持续到 Fitting 和 Network 完成，不能在
`run_clip_stage()` 返回前关闭。

这里的关闭对象是 `ClipResult.clips` 中可关闭的 `ClipRelation`；`ClipResult` 自身没有
`close()`。内存元组形式的 `clips` 无须关闭。`ClipStageResult.close()` 必须幂等，关闭后
报告、空间上下文和交叉口摘要仍可供统计映射读取，不得再访问延迟片段。

资源移交规则：`store.load_clip()` 成功后，`run_clip_stage()` 立即接管关闭责任；只有
完成报告、阶段完成事件及结果对象构造并成功返回时，责任才移交给调用者的上下文。
取得资源后至返回前任意异常（包括 `KeyboardInterrupt`）都必须在运行器内关闭资源并
重新抛出。外层 `with` 尚未建立，不能替代这一保护。`load_clip()` 自身失败时仍由现有
加载实现关闭其已创建资源；阶段运行器不对尚未取得的资源重复关闭。

### 8.4 Fitting Stage

建议接口：

```python
def run_fitting_stage(
    *,
    store: PipelineStore,
    detect: DetectStageRef,
    clip: ClipStageResult,
    clip_config: ClipConfig,
    fitting_config: FittingConfig,
    parameters: dict[str, object],
    emit: EventSink,
) -> FittingStageResult:
    ...
```

职责：

1. 新建 Fit Run 并保存完整配置快照；
2. 执行 `fit_centerline_variants()`；
3. 持久化中心线变体；
4. 构造 combined、a_to_b、b_to_a 报告；
5. 完整性校验并完成 Fit Run；
6. 失败时回滚并将未完成 Run 标记为 `failed`；
7. 返回 `bundle` 和 `centerline_ids` 供 Network 和统计汇总使用。

Fitting 返回前不能丢弃 `CenterlineFitBundle`，因为 Network 需要
`bundle.combined`。

### 8.5 Network Stage

建议接口：

```python
def run_network_stage(
    *,
    fitting: FittingStageResult,
    intersections: Iterable[ClipIntersection],
    config: NetworkConfig,
    emit: EventSink,
) -> NetworkStageResult:
    ...
```

职责：

1. 发送阶段开始事件；
2. 调用 `build_network()`；
3. 输出 Network 报告；
4. 发送阶段完成事件；
5. 返回 `NetworkResult` 和阶段耗时。

Network 当前不创建独立 Run，也不应被迫依赖 `PipelineStore`。如果未来增加 Network
持久化，再单独扩展接口。

`Iterable` 从 `collections.abc` 导入；当前传入值是
`clip.result.intersections: tuple[ClipIntersection, ...]`，与 `build_network()` 的
接口一致，无须为了阶段包装转换成列表。

## 9. 配置传递原则

阶段接口只接收其需要的配置分组，不直接接收完整 `PipelineConfig`，防止阶段无边界地访问
其他阶段配置。

允许的配置依赖：

| 阶段 | 配置依赖 |
| --- | --- |
| Refine | `PostgresConfig`、`ParserConfig`、`RefineConfig`、`RuntimeConfig`、`PathsConfig` |
| Detect | `PostgresConfig`、`IntersectConfig`、`RuntimeConfig`、`PathsConfig`、SHA-256 校验开关 |
| Clip | `ClipConfig`、`RuntimeConfig`、`PathsConfig`、SHA-256 校验开关 |
| Fitting | `ClipConfig`、`FittingConfig`、完整配置快照 |
| Network | `NetworkConfig` |

`parameters` 是要写入 Run 表的完整、无密钥配置快照，不等同于阶段算法配置。

## 10. 统计汇总原则

为兼容 CLI 和现有测试，保留 `PipelineResult.stats`。阶段运行器不得直接修改共享
`stats` 字典，而应返回强类型结果，由 Pipeline 集中映射：

```python
stats = {
    "refine_id": refine_ref.refine_id,
    "detect_id": detect.ref.detect_id,
    "fit_id": fitting.ref.fit_id,
    "source_srid": refine_ref.spatial_context.source_srid,
    "processing_srid": refine_ref.spatial_context.processing_srid,
    "detect_compute_s": round(detect.compute_elapsed_s, 6),
    "detect_persist_s": round(detect.persist_elapsed_s, 6),
    "resolved_sequence_s": round(detect.persisted.resolved_sequence_elapsed_s, 6),
    "tracks_cross": detect.persisted.tracks_cross_count,
    "resolved_subtrack_points": detect.persisted.resolved_point_count,
    "turn_events": detect.persisted.turn_event_count,
    "candidate_intersections": detect.persisted.candidate_intersection_count,
    "candidate_entry_exit_points": detect.persisted.candidate_entry_exit_point_count,
    "intersections": detect.persisted.intersection_count,
    "entry_exit_points": detect.persisted.entry_exit_point_count,
    "clip_assembly_s": round(clip.assembly_elapsed_s, 6),
    "centerlines": len(fitting.centerline_ids),
    "centerline_ids": list(fitting.centerline_ids),
    "_elapsed_s": round(elapsed_s, 6),
}
if refine_result is not None:
    stats.update({
        "source_points": refine_result.source_point_count,
        "kept_trajectory_points": refine_result.kept_point_count,
        "injected_points": refine_result.injected_point_count,
    })
for prefix, report in (
    ("clip", clip.result.report),
    ("fitting", fitting.bundle.combined.report),
    ("network", network.result.report),
):
    stats.update({
        f"{prefix}_{name}": value
        for name, value in report.as_dict().items()
        if isinstance(value, int)
    })
```

以上映射不执行算法或数据库访问。完整公共键清单如下；“两种路径”指成功执行的新建
Refine 路径和复用 Refine 路径，异常时仍抛出错误，不返回部分统计。

| 公共键 | 值来源 | 存在条件 |
| --- | --- | --- |
| `refine_id` | `refine_ref.refine_id` | 两种路径 |
| `detect_id` | `detect.ref.detect_id` | 两种路径 |
| `fit_id` | `fitting.ref.fit_id` | 两种路径 |
| `source_srid` | `refine_ref.spatial_context.source_srid` | 两种路径 |
| `processing_srid` | `refine_ref.spatial_context.processing_srid` | 两种路径 |
| `source_points` | `refine_result.source_point_count` | 仅新建；复用省略 |
| `kept_trajectory_points` | `refine_result.kept_point_count` | 仅新建；复用省略 |
| `injected_points` | `refine_result.injected_point_count` | 仅新建；复用省略 |
| `detect_compute_s` | `detect.compute_elapsed_s` | 两种路径 |
| `detect_persist_s` | `detect.persist_elapsed_s` | 两种路径 |
| `resolved_sequence_s` | `detect.persisted.resolved_sequence_elapsed_s` | 两种路径 |
| `tracks_cross` | `detect.persisted.tracks_cross_count` | 两种路径 |
| `resolved_subtrack_points` | `detect.persisted.resolved_point_count` | 两种路径 |
| `turn_events` | `detect.persisted.turn_event_count` | 两种路径 |
| `candidate_intersections` | `detect.persisted.candidate_intersection_count` | 两种路径 |
| `candidate_entry_exit_points` | `detect.persisted.candidate_entry_exit_point_count` | 两种路径 |
| `intersections` | `detect.persisted.intersection_count` | 两种路径 |
| `entry_exit_points` | `detect.persisted.entry_exit_point_count` | 两种路径 |
| `clip_assembly_s` | `clip.assembly_elapsed_s` | 两种路径 |
| `clip_total_vehicles` | `clip.result.report.total_vehicles` | 两种路径 |
| `clip_total_subtracks` | `clip.result.report.total_subtracks` | 两种路径 |
| `clip_total_clips` | `clip.result.report.total_clips` | 两种路径 |
| `clip_between_intersection_clips` | `clip.result.report.between_intersection_clips` | 两种路径 |
| `clip_boundary_start_clips` | `clip.result.report.boundary_start_clips` | 两种路径 |
| `clip_boundary_end_clips` | `clip.result.report.boundary_end_clips` | 两种路径 |
| `clip_full_track_clips` | `clip.result.report.full_track_clips` | 两种路径 |
| `clip_total_points_in_clips` | `clip.result.report.total_points_in_clips` | 两种路径 |
| `clip_skipped_degenerate_count` | `clip.result.report.skipped_degenerate_count` | 两种路径 |
| `centerlines` | `len(fitting.centerline_ids)` | 两种路径 |
| `centerline_ids` | `list(fitting.centerline_ids)` | 两种路径 |
| `fitting_input_clip_count` | `fitting.bundle.combined.report.input_clip_count` | 两种路径 |
| `fitting_eligible_clip_count` | `fitting.bundle.combined.report.eligible_clip_count` | 两种路径 |
| `fitting_connected_pair_count` | `fitting.bundle.combined.report.connected_pair_count` | 两种路径 |
| `fitting_fitted_centerline_count` | `fitting.bundle.combined.report.fitted_centerline_count` | 两种路径 |
| `fitting_skipped_clip_count` | `fitting.bundle.combined.report.skipped_clip_count` | 两种路径 |
| `fitting_skipped_pair_count` | `fitting.bundle.combined.report.skipped_pair_count` | 两种路径 |
| `network_input_centerline_count` | `network.result.report.input_centerline_count` | 两种路径 |
| `network_node_count` | `network.result.report.node_count` | 两种路径 |
| `network_edge_count` | `network.result.report.edge_count` | 两种路径 |
| `network_isolated_node_count` | `network.result.report.isolated_node_count` | 两种路径 |
| `network_skipped_centerline_count` | `network.result.report.skipped_centerline_count` | 两种路径 |
| `_elapsed_s` | Pipeline 总耗时 | 两种路径 |

耗时保留秒单位及 `round(value, 6)`，测量边界不因阶段提取变化：

| 字段 | 测量边界 |
| --- | --- |
| `detect_compute_s` | 查询 Refine 制品、打开读取器、执行 Detect 至读取器退出；不含随后关闭数据库快照的 `rollback()` |
| `detect_persist_s` | 从 Detect 结果持久化前开始，包含结果验证与提交、计算结果关闭、指纹计算、Parquet 导出/复核/发布、制品登记和最终事务提交；不含完成后报告输出 |
| `resolved_sequence_s` | 沿用 `DetectPersistenceResult` 返回的已解析序列持久化子阶段耗时，不在运行器重新计时 |
| `clip_assembly_s` | `store.load_clip()` 调用前至调用后及资源接管，不含报告输出、Fitting、Network 或最终关闭 |
| `_elapsed_s` | 从输入与配置快照准备后、建立主连接前开始，至 Clip 关闭和 PostgreSQL 上下文退出后结束；不含最终 Done 消息 |

Fitting 的 `compute_elapsed_s`、`persist_elapsed_s` 仍服务于现有 `fit_compute`、
`fit_persist` 日志与事件，不新增 `fit_compute_s` 或 `fit_persist_s` 公共统计键。
各阶段 `elapsed_s` 保持阶段边界日志、完成事件的现有含义，不自动加入 `stats`。
复用时省略三个 Refine 键已经是当前行为，本次不查询历史真实计数，不制造零值结果。

## 11. 事务和失败语义

### 11.1 通用原则

- `PipelineStore` 方法继续不做隐式提交或回滚；
- 阶段运行器明确拥有本阶段事务边界；
- 失败 Run 永久保持 `failed`，不恢复为 `completed`；
- 下游只能读取已完成且制品状态为 `present` 的上游 Run；
- 已发布但未登记的文件是 orphan，不是可消费制品；
- 进度或报告发送失败不能把已经原子完成的 Run 改回 `failed`。

### 11.2 Refine

- PostgreSQL Refine 行提交后，Run 仍为 `running`；
- Parquet 发布并登记成功后，Run 才能完成；
- staging 尚未发布时失败，应安全放弃 staging；
- 文件已发布后登记失败，不删除已发布文件。

### 11.3 Detect

- Detect 计算只读事务在持久化前结束；
- PostgreSQL Detect 行先提交，Run 保持 `running`；
- Parquet 发布、登记和 Run 完成遵循现有顺序；
- `IntersectResult` 在所有成功和失败路径关闭。
- 成功路径必须在结果行提交后、Parquet 导出前关闭 `IntersectResult`。

### 11.4 Fitting

- 中心线持久化、完成校验和 Run 完成属于同一成功路径；
- 失败时回滚结果写入，再单独将仍为 `running` 的 Fit Run 标记为 `failed`。

### 11.5 异常捕获范围保持现状

上述失败收敛指当前受保护的代码区间和捕获范围，不能解释为新增通用的全异常处理器。

| 阶段 | 当前捕获行为 | 本次迁移约束 |
| --- | --- | --- |
| Refine | 主执行块捕获 `Exception`；失败收敛辅助函数尽力回滚并只更新仍为 `running` 的 Run；流式读取资源由 `finally` 关闭 | 保留捕获类型、保护区间与原始错误优先原则，不擅自扩展为 `BaseException` |
| Detect | 执行块捕获 `(Exception, KeyboardInterrupt)`；最终释放尚未关闭的结果；完成标记保护已提交 Run | 保留取消前后不同 Run 状态；阶段开始事件及创建 Run 的既有保护边界不移动 |
| Fitting | 执行块捕获 `(psycopg.Error, RuntimeError, ValueError)`；完成标记保护已提交 Run | 不将 `KeyboardInterrupt`、任意其他异常新增为失败状态收敛条件；保留 Run 创建与阶段开始事件的既有保护边界 |
| Clip | 算法内部以 `BaseException` 保护已创建关系；取得结果后当前由 Pipeline 的 `ExitStack` 清理 | 新运行器取得结果到成功移交之间以 `BaseException` 保护资源，移交后由 `ClipStageResult` 关闭；这是恢复原有资源覆盖范围，不新增 Run 状态行为 |
| Network | 没有阶段级异常捕获 | 原样传播，由外层 Clip/连接上下文收尾 |

如需统一取消行为、扩大失败收敛异常类型或改变清理错误优先级，应另列行为修复并取得实施
授权，不能夹带在本次结构重构中。

## 12. 资源所有权

| 资源 | 创建者 | 关闭者 |
| --- | --- | --- |
| PostgreSQL 主连接 | `run_pipeline()` | `run_pipeline()` 上下文 |
| Refine 源数据 handle | `run_refine_stage()` | stream 创建成功后由 `stream.close()` 关闭；否则直接关闭 handle，避免重复关闭 |
| `StreamVehicleSource` | `run_refine_stage()` | `run_refine_stage()` 的 `finally` |
| Refine staging | `run_refine_stage()` | 发布或失败放弃 |
| `RefineDatasetReader` | `run_detect_stage()` | `run_detect_stage()` |
| `IntersectResult` | `run_detect_stage()` | 结果行提交后立即关闭；异常时关闭尚未释放的结果 |
| Detect staging | `run_detect_stage()` | 发布或失败放弃 |
| Clip 延迟读取资源 | `run_clip_stage()` 调用加载器取得 | 成功返回前由运行器异常保护；返回后由 `ClipStageResult.close()` 幂等关闭 |
| `CenterlineFitBundle` | `run_fitting_stage()` | Pipeline 返回前自然释放 |

## 13. 实施顺序

### C1：建立阶段类型和公共辅助函数

- 在 `stages/types.py` 新增阶段引用和结果数据类，避免反向导入 Pipeline；
- 新增最小 `stages/support.py`；
- 不改变 `run_pipeline()` 行为；
- 同步调整 `test_pipeline_events.py` 的报告辅助函数导入并保留格式断言；
- 添加类型和事件辅助函数单元测试。

### C2：迁入并接入 Refine Stage

- 将生成样板迁入正式包；
- 适配当前集中配置和 Parquet 导出参数；
- 保持新建与复用 Refine 的语义；
- 删除 Pipeline 中 Refine 私有辅助函数和导入；
- 同提交迁移 Refine 生命周期及故障测试、复用分支键存在性测试，并更新相关配置接线断言。

### C3：提取 Network Stage

- 提取纯计算、事件和报告逻辑；
- 验证强类型阶段结果模式；
- 保持现有 Network 统计字段；
- 同提交将 `test_pipeline_config_wiring.py` 中 Network 算法 patch 移到阶段模块。

### C4：提取 Clip Stage

- 提取 Detect Parquet 消费和 Clip 组装；
- 建立 `ClipStageResult` 上下文管理器；
- 移除 Pipeline 的 Clip 资源回调；
- 同提交迁移 Clip 配置接线断言；覆盖正常、返回前报告异常、`KeyboardInterrupt`、
  Fitting/Network 失败和重复关闭，证明资源只关闭一次。

### C5：提取 Fitting Stage

- 提取 Fit Run 生命周期；
- 提取中心线持久化和报告；
- 同提交迁移 Fitting 配置接线及生命周期断言；覆盖现有捕获类型内的计算失败、
  持久化失败、提交失败和成功完成测试。

### C6：提取 Detect Stage

- 提取 Refine Parquet 读取；
- 提取 Detect 计算和持久化；
- 提取 Detect Parquet 发布；
- 同提交迁移 Detect 配置接线断言及 `test_turn_event_wiring.py` 中阶段相关用例，解除
  对 `RunPipelineLifecycleTest.setUp()` 及旧 Pipeline 内部 patch 的依赖；
- 完整覆盖来源变化、staging 放弃、orphan、Run 状态、取消，以及导出前提前关闭结果。

### C7：收缩 Pipeline 和汇总统计

- 删除阶段内部导入和私有辅助函数；
- Pipeline 只调用阶段运行器；
- 集中构造兼容 `PipelineResult.stats`；
- 校验 CLI 输出和公共 API 不变；
- 保留非默认配置经真实阶段包装传至算法/Store 的轻量接线测试，避免只 mock 阶段入口。

### C8：全量验证和文档同步

- 运行阶段单元测试；
- 运行 Pipeline 测试；
- 运行真实 PostgreSQL 与 Parquet 集成测试；
- 运行 `make test`；
- 更新与 `orchestration/run_pipeline.py` 旧行号或旧职责描述相关的技术文档。

## 14. 测试计划

新增：

```text
services/pipeline/tests/v2/test_refine_stage.py
services/pipeline/tests/v2/test_detect_stage.py
services/pipeline/tests/v2/test_clip_stage.py
services/pipeline/tests/v2/test_fitting_stage.py
services/pipeline/tests/v2/test_network_stage.py
```

### 14.1 阶段单元测试

每个阶段按其实际职责验证：

- 输入正确传递给算法入口；
- 阶段事件顺序；
- 返回结果字段；
- 阶段私有资源关闭（存在资源时）；
- 算法或输出失败的传播行为。

仅 Refine、Detect、Fitting 验证 Run 创建、提交、完成顺序，当前捕获范围内的失败回滚和
状态收敛，以及已完成 Run 不被后续报告异常改写。Clip 和 Network 不创建 Run，不能为
套用测试清单引入事务或状态模型。

Clip 须覆盖加载成功后、运行器返回前报告或事件抛错以及 `KeyboardInterrupt` 的关闭；
Detect 须覆盖结果行提交后、Parquet 导出前关闭暂存关系，计算或提交取消时标记失败，
已提交完成后输出取消时保留完成状态。Refine/Fitting 只固定第 11.5 节现有行为，不趁
测试迁移扩大异常捕获范围。

### 14.2 Pipeline 编排测试

`test_run_pipeline_lifecycle.py` 应逐步转为 mock 五个阶段运行器，只验证：

- 新建 Refine 路径；
- 复用 Refine 路径；
- 阶段调用顺序；
- 上游引用传给下游；
- Clip 上下文进入和退出；
- 最终统计映射；
- 任一阶段异常时后续阶段不运行。

阶段内部的数据库调用、Parquet 发布和失败状态断言迁入对应 `test_*_stage.py`。

测试迁移必须随所属阶段同提交完成，具体归属如下：

| 现有测试 | 迁移与保留方式 |
| --- | --- |
| `test_run_pipeline_lifecycle.py` | 将阶段故障注入和事务断言迁至对应阶段测试；保留新建/复用、顺序、上下文及完整统计映射的编排断言 |
| `test_pipeline_config_wiring.py` | 分阶段更新算法、读取器和发布函数的 patch 位置；保留至少一条真实阶段运行器串联、底层依赖替换的非默认配置接线测试 |
| `test_pipeline_events.py` | 报告辅助函数改从 `stages/support.py` 导入；保留整数格式、事件适配器和 CLI 报告行为测试 |
| `test_turn_event_wiring.py` | Detect 暂存释放、提交和取消用例改用阶段测试 fixture；算法内部关系测试保留原归属，移除对旧生命周期测试 fixture 的依赖 |
| `test_pipeline_api.py` | 保留公开导出、冻结数据类、CLI 调用签名与失败时 reporter 关闭测试 |

不得为了迁移后继续 patch 旧符号而在 `orchestration/run_pipeline.py` 保留无用兼容导入。完整统计测试必须
比较第 10 节的键集合和值，验证复用时仅省略三个 Refine 键，不能只检查 Run ID。

### 14.3 集成测试

保留并重新归属以下行为：

- Refine PostgreSQL 行与 Refine Parquet 差分；
- Detect PostgreSQL 行与 Detect Parquet 差分；
- 来源指纹前后变化拒绝发布；
- staging 发布前故障清理；
- 文件发布后数据库登记失败产生 orphan；
- completed Run 必须关联 `present` 制品；
- Clip 延迟读取资源在 Fitting 和 Network 完成前保持有效；
- Detect 计算暂存关系在 Parquet 导出开始前已经释放。

## 15. 验收标准

1. `run_pipeline()` 不直接调用以下算法或制品函数：
   - `refine_vehicle()`；
   - `detect_intersections()`；
   - `fit_centerline_variants()`；
   - `build_network()`；
   - `export_refine_parquet()`；
   - `export_detect_results_to_parquet()`；
   - `create_staging()`、`publish()` 或 `abandon_staging()`。
2. `run_pipeline()` 不包含阶段私有 `try/except`、批量刷新循环或来源指纹逻辑。
3. 五个阶段分别具有独立运行器和强类型结果。
4. Refine 与 Detect 的 PostgreSQL、Parquet 和 Run 完成顺序不变；Detect 结果行提交后、
   Parquet 导出前立即关闭 `IntersectResult`。
5. Clip 成功移交后在 Fitting 和 Network 完成或失败时准确关闭一次；移交前异常及取消
   由阶段运行器关闭，重复关闭无副作用。
6. Fitting 失败路径保持现有回滚和 Run 状态语义。
7. `PipelineResult.stats` 的键、值来源及耗时边界与第 10 节一致；复用 Refine 继续省略
   三个计算计数键，不伪造结果，不额外查询历史计数。
8. CLI 参数和用户可见阶段顺序不变。
9. 单元测试、Pipeline 测试和真实 PostgreSQL/Parquet 集成测试通过。
10. `make test` 通过。

## 16. 风险与控制措施

| 风险 | 影响 | 控制措施 |
| --- | --- | --- |
| 移动代码时改变提交顺序 | Run 或制品出现不一致状态 | 每个阶段先补调用顺序测试，再移动实现 |
| Detect 发布后异常处理错误 | 已完成 Run 被改为 failed，或误删已发布文件 | 显式区分 `published` 与 `completed` 状态 |
| Clip 过早关闭 | Fitting 或 Network 读取失败 | 由 `ClipStageResult` 统一拥有上下文生命周期 |
| Clip 返回前输出异常 | 外层上下文尚未接管，关系泄漏 | 运行器保护取得资源到成功移交的完整区间，包括取消 |
| Detect 只在最终清理时关闭结果 | 导出时叠加计算暂存与导出资源 | 结果行提交后立即释放，测试断言发生于导出前 |
| 阶段结果携带大型对象过久 | 增加内存占用 | 引用与结果分离，Detect 内部关闭 `IntersectResult` |
| 传入完整 `PipelineConfig` | 阶段间重新耦合 | 只传阶段所需配置分组 |
| 测试继续 patch `orchestration/run_pipeline.py` 内部符号 | 重构后测试脆弱 | 阶段行为测试迁入对应模块，Pipeline 测试只 patch 阶段入口 |
| 配置接线测试全部改为 mock 阶段 | 实际参数遗漏无法检出 | 保留真实阶段运行器串联的非默认配置接线测试 |
| 生成目录样板被当作正式源码 | 文件不进入 Git 或部署镜像 | 正式实现迁入 `src/track/v2`，删除或保留样板但禁止导入 |

## 17. 原建议提交顺序

```text
refactor: define pipeline stage types and migrate event helper tests
refactor: extract refine stage runner and migrate lifecycle tests
refactor: extract network stage runner and migrate wiring tests
refactor: extract clip stage runner and verify resource handoff
refactor: extract fitting stage runner and migrate lifecycle tests
refactor: extract detect stage runner and migrate cancellation tests
refactor: reduce pipeline orchestration and verify complete stats mapping
docs: update pipeline stage ownership
```

原计划建议按上述顺序拆分提交。实施时用户明确授权将本次纯结构重构合并为单个提交；阶段
边界、测试迁移和验证仍在该提交内一次完成。

## 18. 审查重点

修订后采用以下设计，复审时检查其一致性：

1. Parser 继续作为 Refine 的读取部分，不增设第六阶段；
2. Pipeline 保留新建/复用选择与空间上下文创建；阶段运行器不依赖 `PipelineInput`；
3. 使用 `refine_ref` 与可选 `refine_result`，复用时延续三个计算计数键省略的现状；
4. 阶段运行器仅接收所需配置分组，类型集中在 `stages/types.py`；
5. Clip 明确返回前异常保护与返回后上下文所有权，Detect 明确导出前释放结果；
6. Detect 最后提取，全部相关生命周期、配置接线及取消测试随阶段同步迁移；
7. `stages/support.py` 严格限制为无状态事件与计时辅助函数；
8. 公共统计、CLI 消息、计时边界和异常捕获范围保持现状。

更改复用时排除/注入零消息的措辞、增加历史真实计数查询或统一各阶段取消收敛行为，均不在
本次结构重构内；若用户希望实施这些行为变化，应另行明确范围。本计划不以这些可选改进
作为开始结构重构的前置条件。

审查通过仅表示设计确认，不代表实施授权。实施应由用户另行明确授权。
