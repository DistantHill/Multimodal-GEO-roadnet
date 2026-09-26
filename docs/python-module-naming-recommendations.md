# Python 源码模块命名审查与建议

审查基线：提交 `f3e214f406a0ef6e04b41d86305689d22a367e0d`（2026-09-24）；该提交在 `apps/backend/src` 与 `services/pipeline/src` 下共有 129 个 `.py` 文件。原逐文件审查对应此快照，不包含测试目录、已删除文件和工作树副本。原审查检查了文件名、模块说明、文件头注释、顶层函数与类的名称及其说明；对职责混合的文件进一步读取了源码。截至 2026-09-26 终验，当前两个源码根共有 139 个 `.py` 文件（Backend 17，Pipeline 122），Pipeline 数含拆分新增模块和包初始化文件。基线后新增的数据库迁移模块原名 `source_row_key_migration.py`，后经单独审查并改为 `migrate_source_row_key.py`，不纳入原逐文件审查。基线 129 个文件中的 31 个手工工具已从主工作树删除，不纳入后续命名迁移；忽略的独立旧工作树副本按 Q11 保留。其余 98 个基线项中，67 个已迁移、拆分或核实移除，31 个按职责证据保留，逐行状态见下表。原审查时未移动源码或改变导入；后续批次及其状态见逐文件表和 Spec QA 记录。本清单的旧路径继续标识审查基线来源，实施状态另行注明。Q9 只适用于需表达目标格式的转换，对数据库 schema 迁移不适用，因此未添加 `to_bigint`。

本文采用项目术语：后端服务（Backend）、数据处理流水线（Pipeline）、精炼阶段（Refine）、检测阶段（Detect）、片段提取阶段（Clip）、拟合阶段（Fitting）、路网构建阶段（Network）、命令行接口（CLI）。后文保留英文阶段名作为代码层级和标识符。

## 外部依据与认可度

| 证据 | 可用于本项目的结论 | 认可度及限制 |
| --- | --- | --- |
| [PEP 8 在 GitHub 的原文](https://github.com/python/peps/blob/main/peps/pep-0008.rst) | 模块名短、全小写；下划线确实提升可读性时使用。公开名称应反映用途；既有代码还要考虑内部一致性。 | Python 官方规范，优先级最高；它没有规定模块必须用动词。 |
| [Python Guide 的模块结构建议](https://github.com/realpython/python-guide/blob/master/docs/writing/structure.rst) | 用包层级承载命名空间，避免把所有上下文拼进一个长文件名。 | GitHub 上维护的指南；页面本身不是投票，不能当作社区投票数。 |
| [Google Python Style Guide](https://github.com/google/styleguide/blob/gh-pages/pyguide.md) | 文件名应描述用途，使用可导入的小写下划线名；相关类型与函数可以在同一模块中。 | GitHub 上的组织规范；不是针对本项目的评审结论。 |
| [Stack Overflow：Python 模块命名](https://stackoverflow.com/questions/711884/python-naming-conventions-for-modules) | 高票回答赞成简短的领域名，并引用 PEP 8；评论指出一类一文件和超大文件是两个极端，应按相关职责拆分。 | 最高回答 133 票；其余相关回答 47、30 票，评论存在不同意见。 |
| [Stack Overflow：包目录命名](https://stackoverflow.com/questions/52827722/folder-naming-convention-for-python-projects) | 小写与必要的下划线普遍可接受。 | 首位回答 84 票。 |
| [Stack Overflow：同名模块可处在不同包](https://stackoverflow.com/questions/44221225/python-same-module-name-convention) | 完整导入路径提供语境，子包中的短文件名可以重用。 | 首位回答 6 票，属于辅助证据。 |
| [Stack Overflow：包名用动词还是名词](https://stackoverflow.com/questions/70755029/python-package-naming-verb-or-noun) | 动词可用于执行动作的模块，但没有公认的“所有包和模块都用动词”规范。 | 唯一回答 5 票，证据较弱；本项目的动词规则应视为项目约定。 |

综合判断：包路径先表达运行单元中的阶段或数据域；确实跨阶段共用的设施再进入公共包。模块根据内聚职责采用动作名或名词名。外部资料支持分层、简短和描述性命名；对象、动作及必要的输入/输出信息属于本项目约定，不能声称是 PEP 8 或社区硬性标准。投票反映回答的认可度，不能证明某个具体目录方案的质量。

## 建议规则

1. 完整导入路径共同表达运行单元、阶段/数据域和职责。阶段专属代码优先归入对应阶段或数据域；只有确实被多个阶段共同使用的设施才放入公共包。不得先按通用技术分类，再把阶段名留给文件名补足。例如 Detect 专属导出候选为 `track.v2.intersections.results.export_to_parquet`；跨阶段的清单设施才考虑 `track.v2.artifacts.parquet_manifest`。子包只在职责边界实际需要时建立。
2. 单一操作或转换模块用能说明对象的动作名；类型、契约、配置和内聚策略模块用名词。除第 3 条的目标格式转换外，不要求固定的“来源＋动作＋结果”词序。完整路径须能辨认处理对象和主要动作；来源存储或结果格式只在能区分相邻模块时加入。校验、编排等没有独立输出文件的职责不强加结果词。
3. 当转换模块需要在名称中表达目标格式时，采用“动词＋处理对象＋`to`＋目标格式”，例如 `export_detect_results_to_parquet.py`。若父包已明确对象，基名可省略重复部分；目标格式之前的对象不必再次写入完整阶段路径。一般读取动作如 `read_detect_parquet.py` 不属于此转换词序规则。点号不用于表示格式扩展名，因为点号会改变 Python 导入语义。
4. **定义数据类型、协议或一组紧密相关策略**的模块使用名词，例如 `models.py`、`errors.py`、`grid.py`、`manifest.py`；阶段专属执行边界归入对应阶段上下文。CLI 入口函数可保留 `main()`，由命令名和入口模块提供语境。供业务代码直接导入调用的公共函数名应说明动作和对象。不把 `define_`、`do_`、`process_` 机械加到所有文件名前。
5. 路径与文件名共同承担语义，避免重复成 `track.v2.intersections.detect_detect_results.py`。一个文件若同时有两个独立的主要动作，如导出和读取，应先拆分再命名；单纯增长文件名不能解决职责混杂。
6. 名称只使用小写英文单词、必要的下划线和 `.py`；保留 Python 约定的 `__init__.py`、`__main__.py`。`_` 前缀只用于确实内部使用的模块。消除 `manual_`、`bench_` 等已由目录表明的重复前缀，以及 `pari` 这种无法由职责解释的缩写。
7. 新模块先写一句职责说明，再检查完整导入路径能否说明其领域和主要职责。类名说明表示的对象、结果、配置或角色；常量名说明固定值的业务含义，必要时包含单位或适用范围。职责独立变化、调用或复用时再拆分模块；不要把版本号、物理存储、算法名同时堆在文件名里，除非区分现存并行实现确实需要。

## 推荐层级

```text
backend/
  api/                 HTTP 路由、请求与响应结构
  stores/              只读地图查询与目录访问
  replay/              回放选择和几何要素生成
track/v2/
  candidates/          交叉口候选、跨轨迹配对与候选任务块
  intersections/      Detect 产生的交叉口、拓扑与结果
  trajectories/       Parser/Refine 处理的轨迹与点数据
  clips/              Clip 数据与提取
  centerlines/        Fitting 产生的中心线
  road_network/       Network 产生的节点与边
  artifacts/           确实跨阶段共用的清单、发布与核对设施
  db/                  数据库读写、查询和版本迁移；按数据对象或操作拆模块
    migrations/        按版本顺序执行的 schema 迁移
  stages/              各阶段运行器及共享契约、支持代码
  raster/              独立栅格领域；阶段专属输入/输出命名其所属阶段
  validation/          已完成结果的只读比较和回放
  cli/                 命令入口
  diagnostics/         参考算法、诊断、核验和报告，按用途设子包
```

这些是按实际职责建包的示意，不是固定目录模板或立即整体搬迁的指令。新建的 `candidates`、`intersections`、`trajectories`、`clips`、`centerlines`、`road_network` 等数据域名与当前根模块不冲突；每个包仍需有成组职责和清楚边界。只有跨阶段共享的清单才归入 `track.v2.artifacts`。`pipeline`、`models`、`errors`、`config` 等跨域核心文件仍可位于 `track.v2` 根层。类、常量和 CLI `main()` 遵循其成员语义，不套用模块动作名规则。

### 导入路径冲突与实施顺序

最初的冲突核对基于 `f3e214f` 快照；当时根层存在 `detect.py`、`refine.py`、`clip.py`、`fitting.py`、`network.py`、`parser.py`、`replay.py`。此后 Detect、Clip、Replay、Fitting、Network、Parser、Refine 根模块已迁入 `intersections`、`clips`、`validation`、`centerlines`、`trajectories` 数据域，旧根路径删除。该快照中的同名路径当时不可直接建立，不能将其称为可直接迁移；若后续采用其他同名阶段包，必须先将对应根模块及其调用点同批或提前迁走，再建立子包。当前根层已无上述同名冲突模块；其余映射按现行树复核。

当前 Pipeline 包含既有的 `cli`、`config`、`db`、`raster`、`validation`，以及实施中按职责创建的 `artifacts`、`candidates`、`intersections`、`trajectories`、`clips`、`centerlines`、`road_network`、`stages`、`telemetry`、`diagnostics`、`orchestration`、`migrations` 等包；Backend 另有 `api`、`stores`、`replay`。核查确认 `track.v2.config/` 是包且没有 `track.v2.config.py`，因此 `track.v2.config.field_mapping` 没有路径冲突。尚未创建独立的 `relations` 包，因为当前没有适合共同迁移的稳定模块组。新包需由成组内聚模块或明确领域边界支撑，不为单个空包增加层级。

实施顺序：先迁移到现有包或无冲突的数据域包，并在同一批移动其所有现行引用。若后续决定采用其他同名阶段包，先迁走基线中冲突的根模块及其现行引用；确认旧模块路径已删除后，才建立子包并迁入对应职责模块。Detect、Clip、Replay、Fitting、Network、Parser、Refine 根模块均已分别迁入无冲突的数据域路径。根模块可与包及子模块在一个批次内整体迁移，也可先单独迁移，但不能先建立冲突目录。共享 `stages`、`artifacts` 等包只接纳确实跨阶段的契约或设施；只有存在成组且边界清楚的关系职责时才建立 `relations` 包。每批先核对固定路径和源码哈希证据，再改路径；历史证据留在原位置。

## 优先处理的命名和职责

| 当前模块 | 观察到的主要职责 | 建议目标 | 方式 |
| --- | --- | --- | --- |
| `track.v2.detect_parquet_export` | PostgreSQL 中已提交的 Detect 结果导出为待发布的 Parquet | 本批已迁移至 `track.v2.intersections.results.export_to_parquet` | 与读取器同批建立结果数据域包 |
| `track.v2.detect_parquet_reader` | 读取并校验 Detect Parquet，供 Clip 使用 | 本批已迁移至 `track.v2.intersections.results.read_parquet` | 与交叉口结果导出同批建立 `intersections.results` |
| `track.v2.refine_parquet` | 同一文件含 Refine Parquet 导出与 DuckDB 读取器 | 本批已拆分至 `track.v2.trajectories.results.export_to_parquet`、`read_parquet`、`parquet_schema`；共享 `ParquetColumn`、SQL literal helper 迁至 `track.v2.artifacts.parquet_schema`、`parquet_sql` | Refine 专属 schema 留在轨迹结果域；跨阶段 helper 放 artifacts，避免 Detect 依赖 Refine |
| `track.v2.artifact_parquet` | 构建、校验 Parquet 清单和文件元数据 | 本批已迁移至 `track.v2.artifacts.parquet_manifest` | 跨 Detect/Refine 共用清单职责，保留名词 |
| `track.v2.artifact_publication` | 待发布目录与正式目录的安全发布 | 本批已迁移至 `track.v2.artifacts.publish_dataset` | 跨 Detect/Refine 共用发布设施，动作明确 |
| `track.v2.artifact_reconcile` | 扫描产物并制定/执行核对计划 | 本批已迁移至 `track.v2.artifacts.reconcile_datasets` | CLI 核对 Detect/Refine 共用产物；动作明确 |
| `track.v2.detect_candidates` | 调度转向和跨轨迹候选任务块并合并 | 本批已迁移至 `track.v2.candidates.collect_candidates` | 候选是稳定数据域；已与候选构建模块成组迁移 |
| `track.v2.grid_cross_track_candidates` | 网格划分、线段装载、穿越检测及候选任务执行 | 本批已拆分为 `track.v2.candidates.grid`、`grid_segments`、`detect_grid_crossings` | 已成组建立无冲突的 `candidates` 包 |
| `track.v2.candidate_patches` | 轨迹和跨轨迹线段对的任务块构建 | 本批已迁移至 `track.v2.candidates.build_patches` | 与同数据域候选模块同批迁移 |
| `track.v2.cross_track` | 跨轨迹线段准备、精确相交与候选构建 | 本批已迁移至 `track.v2.candidates.detect_cross_track_pairs` | 若保留单文件，名称覆盖主动作；后续可拆准备与相交 |
| `track.v2.turn_events` | 在一个轨迹任务块内检测转向事件 | 本批已迁移至 `track.v2.candidates.detect_turn_events` | 归入无冲突的候选数据域包 |
| `track.v2._cross_track_legacy` | 历史 Method 3 基准参考实现 | 本批已迁移至 `track.v2.diagnostics.reference_cross_track_candidates` | 历史参考实现单独存放 |
| `track.v2.clip_relation` | Clip/Fitting 交接的 DuckDB 临时关系 | 本批已迁移至 `track.v2.clips.clip_input` | 与 Clip 算法入口组成 Clip 到 Fitting 的数据边界 |
| `track.v2.cross_candidate_relation` | 跨轨迹候选的 DuckDB 归一化关系 | 本批已迁移至 `track.v2.candidates.cross_track_relation` | 与候选算法按数据域归属 |
| `track.v2.turn_event_relation` | 转向事件的 DuckDB 归一化关系 | 本批已迁移至 `track.v2.candidates.turn_event_relation` | 与候选算法按数据域归属 |
| `track.v2.writer_relation` | Detect 写入前的原始来源关联 | 本批已迁移至 `track.v2.intersections.relations.source_associations` | 交叉口检测的数据域包 |
| `track.v2.refine_stage`、`detect_stage`、`clip_stage`、`fitting_stage`、`network_stage` | 各阶段执行与资源边界 | 本批已迁移至 `track.v2.stages.refine`、`stages.detect`、`stages.clip`、`stages.fitting`、`stages.network`；共享 `support`、`types` 同批归入 `stages` | 每阶段运行器拥有稳定执行边界，共享阶段契约与支持代码同域 |
| `track.v2.db_writer` | Refine、Detect、Fitting 等运行记录和结果写入 | 本批已拆分至 `track.v2.db.write_refine_results`、`write_detect_results`、`write_fitting_results`、`insert_trajectory_points`、`insert_batches` | 按结果域及通用插入操作留在数据库层，不将持久化代码误归算法域 |
| `track.v2.db.queries` | 空间窗口/扫描迭代、Detect/Clip 结果、Refine 点流读取和 Replay 快照查询 | 本批按 Q4/Q5 拆为 `track.v2.candidates.query_source_windows`、`track.v2.intersections.query_results`、`track.v2.trajectories.query_refine_points`、`track.v2.validation.query_replay_inputs`；`SpatialWindow` 类型移至 `track.v2.projection` | 按实际调用数据域拆分查询；跨 reader 共享的窗口值类型靠近 QueryWindow；Replay snapshot reader 暂未被当前运行路径调用 |
| `backend.map_api` | 路由、地图查询、回放和 Clip 投影并存 | 本批已按职责拆为 `backend.api.map_routes`、`backend.stores.map_layers`、`backend.replay.build_clip_features`，并新增共享 `map_errors`、`map_features`、`replay.track_points` | 路由、存储查询、投影各自独立；共享 helper 不重复放入单侧模块 |
| `backend.map_schemas` | bbox、Replay/Clip 请求与 GeoJSON 响应 | 本批已迁移至 `backend.api.map_contracts` | 请求与响应契约位于 API 包 |
| `backend.database_catalog` | FastAPI 只读数据库与表目录端点 | 本批已迁移至 `backend.api.database_catalog` | 路由是主要职责；计数函数是内部辅助函数，见下方映射修订 |
| `backend.intersection_replay` | 选取交叉口事件成员点 | 本批已迁移至 `backend.replay.select_intersection_members` | 保持回放领域边界 |
| `backend.replay_segments` | 把相邻来源线段分类为回放要素 | 本批已迁移至 `backend.replay.build_segment_features` | 与交叉口成员选择同属回放功能 |
| `track.v2.downstream` | 只有尚未实现的 `detect_intersections` 占位函数 | 已核实并删除：无现行引用的未实现占位模块 |
| `track.v2.manual_thin_benchmark._init_` | 两行占位文件，但拼写不是 Python 包初始化文件 | 按 2026-09-25 用户决定删除，不纳入命名迁移 | 此行仅为 `f3e214f` 基线记录 |
| `track.v2.manual_thin_benchmark.manual_bench_clip_pari` | 固定 Detect Run 的片段对打印诊断 | 按 2026-09-25 用户决定删除，不纳入命名迁移 | 此行仅为 `f3e214f` 基线记录 |

本表中的文件路径表示 `f3e214f` 审查基线，不表示这些模块仍以原路径存在。例如 Detect Parquet 导出当前位于 `track.v2.intersections.results.export_to_parquet`，Detect 算法已拆分到 `track.v2.intersections.detect_intersections` 与 `track.v2.intersections.extract_topology`；各行的现行状态和目标见建议列。实施中同步更新现行导入、测试、CLI 入口、脚本和使用文档并删除旧模块；`track.v2.cli.init_db` 仍由项目命令入口直接引用。

实施记录：逐文件表中 129 个基线项已完成核对：31 个 `manual_*` 文件按用户决定从主工作树删除并退出候选，余下 98 项中 67 项已迁移/拆分/核实移除，31 项按职责证据保留。主要实施批次及验证见 Spec QA。`delete_run.py`、`projection.py`、`db.schema` 和各入口/契约模块的保留依据见逐文件表。`database_catalog.py` 的初始 `stores` 候选根据实际职责修订为 `api.database_catalog`：其主要接口是 FastAPI router 与目录端点，计数函数只服务该路由，未拆出单次使用的 store 层。`map_api.py` 按路由、地图存储查询、Clip 投影拆成 `api.map_routes`、`stores.map_layers`、`replay.build_clip_features`，并抽出共享 `map_errors`、`map_features`、`replay.track_points`。DB 查询职责按实际调用方拆为四个数据域模块，`SpatialWindow` 放在 `projection.py`。`pipeline.py`、`pipeline_events.py` 已迁入 `orchestration`；Parser/Refine 迁入 `trajectories`，Fitting 迁入 `centerlines`。基线时的同名根模块冲突均已解除；冲突清单只说明历史实施顺序，不表示当前仍有阻塞。`manual_*` 递归忽略规则生效，忽略的独立旧副本依 Q11 保留；历史路径/哈希证据不改写。

## 逐文件审查清单

下表路径以 `f3e214f` 为命名审查基线；“职责线索”来自文件头说明或顶层公开对象；“保留”表示该基线文件名在原包层级内可解释，并不表示文件内部已无其他维护问题。建议目标是目标模块路径，非现有文件路径。手工工具表项已按后续决策标注删除，不再是迁移候选。

### Track 包根（1 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/track/__init__.py` | Python 包初始化 | 保留：现名与层级匹配 |

### Backend（9 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `apps/backend/src/backend/__init__.py` | Backend API service. | 保留：现名与层级匹配 |
| `apps/backend/src/backend/config.py` | 公开对象：ConfigError, PostgresConfig, QdrantConfig | 保留：Backend 配置类型与加载 |
| `apps/backend/src/backend/database.py` | 公开对象：configure_database, postgres_connection_settings, verify_runtime_schema | 保留：数据库连接与当前结构验证；以后增长再拆分 |
| `apps/backend/src/backend/database_catalog.py` | Read-only catalog browsing with exact counts for small or unmeasured tables. | 本批已迁移至 `backend.api.database_catalog`；该文件以 FastAPI router 和目录端点为主，`table_counts` 是路由内 helper，因此修订原 `backend.stores.database_catalog` 候选，不拆成单用 store 模块 |
| `apps/backend/src/backend/intersection_replay.py` | Bounded event-member selection using Pipeline's existing point replay helper. | 本批已迁移至 `backend.replay.select_intersection_members`；此行记录审查基线中的旧路径 |
| `apps/backend/src/backend/main.py` | 公开对象：lifespan, health, dependency_health | 保留：ASGI 入口与健康检查 |
| `apps/backend/src/backend/map_api.py` | Read-only map, replay, and Clip projections for the current schema. | 本批已拆分：`backend.api.map_routes`、`backend.stores.map_layers`、`backend.replay.build_clip_features`，并抽出共享 `backend.map_errors`、`backend.map_features`、`backend.replay.track_points`；此行保留审查基线旧路径 |
| `apps/backend/src/backend/map_schemas.py` | 公开对象：validate_bbox, parse_bbox, ReplayRequest | 本批已迁移至 `backend.api.map_contracts`；此行记录审查基线中的旧路径 |
| `apps/backend/src/backend/replay_segments.py` | Classify only source segments adjacent to selected points as participants. | 本批已迁移至 `backend.replay.build_segment_features`；此行记录审查基线中的旧路径 |

### Pipeline 入口（2 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/pipeline/__init__.py` | Independent runtime boundary for the road network pipeline. | 保留：Python 包初始化 |
| `services/pipeline/src/pipeline/__main__.py` | 公开对象：check_config, serve, main | 保留：Python 标准运行入口 |

### Pipeline 核心及阶段（49 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/track/v2/__init__.py` | track.v2 — Step-based trajectory pipeline. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/_cross_track_legacy.py` | Historical Method 3 vehicle-pair oracle used by equivalence benchmarks. | 本批已迁移至 `track.v2.diagnostics.reference_cross_track_candidates`；此行保留基线路径 |
| `services/pipeline/src/track/v2/_geometry.py` | Shared geometry helpers for v2 pipeline stages. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/_perf.py` | Lightweight performance timing framework for the v2 detect pipeline. | 本批已迁移至 `track.v2.intersections.performance_timing`；此行保留基线路径 |
| `services/pipeline/src/track/v2/artifact_parquet.py` | Build and verify immutable Parquet artifact manifests. | 本批已迁移至 `track.v2.artifacts.parquet_manifest`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/artifact_publication.py` | Safe filesystem publication for immutable stage datasets. | 本批已迁移至 `track.v2.artifacts.publish_dataset`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/artifact_reconcile.py` | Read-only reconciliation planning for immutable artifact datasets. | 本批已迁移至 `track.v2.artifacts.reconcile_datasets`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/candidate_patches.py` | Partition tracks and spatial segment pairs without running detection kernels. | 本批已迁移至 `track.v2.candidates.build_patches`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/clip.py` | Clip resolved Detect sequences with one active-set state machine. | 本批已迁移至 `track.v2.clips.extract_clips`；此行保留基线路径 |
| `services/pipeline/src/track/v2/clip_relation.py` | DuckDB-managed temporary columnar relation for Clip/Fitting handoff. | 本批已迁移至 `track.v2.clips.clip_input`；此行保留基线路径 |
| `services/pipeline/src/track/v2/clip_stage.py` | Execution and resource boundary for the Clip stage. | 本批已迁移至 `track.v2.stages.clip`；此行保留基线路径 |
| `services/pipeline/src/track/v2/cross_candidate_relation.py` | DuckDB-managed normalized relation for transient cross-track candidates. | 本批已迁移至 `track.v2.candidates.cross_track_relation`；据 Q4 修订原 intersection relation 目标 |
| `services/pipeline/src/track/v2/cross_track.py` | Cross-track segment preparation and exact detection for one pair patch. | 本批已迁移至 `track.v2.candidates.detect_cross_track_pairs`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/db_writer.py` | Schema-v3 PostgreSQL writers. | 本批已拆分至 `track.v2.db.write_refine_results`、`write_detect_results`、`write_fitting_results`、`insert_trajectory_points` 与 `insert_batches`；分别按结果域或通用 INSERT/COPY 职责归入现有 DB 层 |
| `services/pipeline/src/track/v2/dbscan_storage.py` | Compact index storage for the existing exact DBSCAN expansion order. | 本批已迁移至 `track.v2.candidates.dbscan_index`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/delete_run.py` | Explicit, confirmation-gated deletion of Refine and Detect Runs. | 经 Q7 复核后保留：唯一 Run 删除服务，当前仅由 CLI 调用；单独建立 `runs` 包没有同域邻居 |
| `services/pipeline/src/track/v2/detect.py` | Detect — Intersection candidate detection and topology extraction. | 本批已拆分至 `track.v2.intersections.detect_intersections` 与 `track.v2.intersections.extract_topology`；此行记录基线中的旧路径 |
| `services/pipeline/src/track/v2/detect_candidates.py` | Run scheduled candidate patches and merge results before global clustering. | 本批已迁移至 `track.v2.candidates.collect_candidates`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/detect_parquet_export.py` | Export committed results of one still-running Detect Run to staged Parquet v1. | 本批已迁移至 `track.v2.intersections.results.export_to_parquet`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/detect_parquet_reader.py` | Read one immutable Detect Parquet v1 dataset for Clip. | 本批已迁移至 `track.v2.intersections.results.read_parquet`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/detect_stage.py` | Database-backed execution boundary for the Detect stage. | 本批已迁移至 `track.v2.stages.detect`；此行保留基线路径 |
| `services/pipeline/src/track/v2/downstream.py` | 公开对象：detect_intersections | 已核实并移除：未实现且无现行引用的 Detect 占位模块 |
| `services/pipeline/src/track/v2/errors.py` | 公开对象：V2Error, FatalDataError, ConfigError | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/exceptions.py` | Backward-compatible exception exports for older v2 imports. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/field_config.py` | 公开对象：load_field_config, build_field_mapping | 本批已迁移至 `track.v2.config.field_mapping`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/fitting.py` | Fitting stage — aggregate between-intersection clips into centerlines. | 本批已迁移至 `track.v2.centerlines.fit_centerlines`；此行保留基线路径 |
| `services/pipeline/src/track/v2/fitting_stage.py` | Database-backed execution boundary for the Fitting stage. | 本批已迁移至 `track.v2.stages.fitting`；此行保留基线路径 |
| `services/pipeline/src/track/v2/grid_cross_track_candidates.py` | In-memory closed XY grids for one Detect cross-track task. | 本批已拆分为 `track.v2.candidates.grid`、`grid_segments`、`detect_grid_crossings`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/heartbeat.py` | Periodic operation status while the caller is blocked in SQL or a loop. | 本批已迁移至 `track.v2.telemetry.heartbeat`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/logger.py` | Logger module: rich-powered terminal output + timestamped log mirror + Live panels. | 本批已迁移至 `track.v2.telemetry.console_logging`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/member_sequences.py` | Disk-free helpers for immutable, repeatable member sequences. | 本批已迁移至 `track.v2.intersections.member_sequences`；此行记录审查基线中的旧路径 | 现行消费者均属 Detect；据 Q4 归入交叉口数据域，不归共享关系包 |
| `services/pipeline/src/track/v2/models.py` | Unified base class for all v2 output models. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/network.py` | Network stage — assemble intersection nodes and centerline edges. | 本批已迁移至 `track.v2.road_network.build_road_network`；此行保留基线路径 |
| `services/pipeline/src/track/v2/network_stage.py` | Execution boundary for the Network stage. | 本批已迁移至 `track.v2.stages.network`；此行保留基线路径 |
| `services/pipeline/src/track/v2/output.py` | Shared JSON output helpers for v2 command-line adapters. | 本批已迁移至 `track.v2.cli.json_output`；此行保留基线路径 |
| `services/pipeline/src/track/v2/parser.py` | Parser stage: source reading, field parsing and vehicle streaming. | 本批已拆分至 `track.v2.trajectories.read_source` 与 `track.v2.trajectories.parse_rows`；此行保留基线路径 |
| `services/pipeline/src/track/v2/pipeline.py` | Thin orchestration for the database-backed track.v2 pipeline. | 本批已迁移至 `track.v2.orchestration.run_pipeline`；此行保留基线路径 |
| `services/pipeline/src/track/v2/pipeline_events.py` | Typed events emitted by the pipeline core. | 本批已迁移至 `track.v2.orchestration.events`；此行保留基线路径 |
| `services/pipeline/src/track/v2/projection.py` | Run-scoped coordinate transforms for metric pipeline calculations. | 经 Q7 复核后保留：该模块跨 Detect、Refine、Clip、Fitting 及相关读写/编排代码复用，根路径职责清楚；单独建立 `spatial` 包没有同域邻居 |
| `services/pipeline/src/track/v2/refine.py` | Refine stage: split trajectories, exclude stationary points and derive points. | 本批已迁移至 `track.v2.trajectories.refine_trajectories`；此行保留基线路径 |
| `services/pipeline/src/track/v2/refine_parquet.py` | Export one running Refine Run from PostgreSQL into staged Parquet v1. | 本批已拆分至 `track.v2.trajectories.results.export_to_parquet`、`read_parquet`、`parquet_schema`；Refine 专属 `REFINE_PARQUET_V1` 随结果域；共享 `ParquetColumn` 与 SQL literal helper 分别迁至 `track.v2.artifacts.parquet_schema`、`artifacts.parquet_sql` |
| `services/pipeline/src/track/v2/refine_stage.py` | Database-backed execution boundary for the Refine stage. | 本批已迁移至 `track.v2.stages.refine`；此行保留基线路径 |
| `services/pipeline/src/track/v2/replay.py` | Semantic replay over schema-v4 source and resolved-point identities. | 本批按职责拆分至 `track.v2.validation.replay_lineage` 与 `track.v2.validation.resolve_point_references`；此行保留基线路径 |
| `services/pipeline/src/track/v2/resource_budget.py` | Read-only, credential-free run environment snapshots. | 本批已迁移至 `track.v2.telemetry.snapshot_run_environment`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/stage_support.py` | Shared event and timing helpers for Pipeline stage runners. | 本批已迁移至 `track.v2.stages.support`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/stage_types.py` | Typed references and results exchanged by Pipeline stage runners. | 本批已迁移至 `track.v2.stages.types`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/turn_event_relation.py` | DuckDB-managed normalized relation for transient Detect turn events. | 本批已迁移至 `track.v2.candidates.turn_event_relation`；据 Q4 修订原 intersection relation 目标 |
| `services/pipeline/src/track/v2/turn_events.py` | Turn-event geometry for one track patch; no scheduling or progress output. | 本批已迁移至 `track.v2.candidates.detect_turn_events`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/writer_relation.py` | DuckDB relations for the first, source-association phase of Detect writing. | 本批已迁移至 `track.v2.intersections.relations.source_associations`；此行记录审查基线中的旧路径 |

### CLI（11 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/track/v2/cli/__init__.py` | track.v2 CLI — command-line entry points for v2 pipeline operations. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/cli/_startup.py` | Single configuration boundary shared by Pipeline command-line entrypoints. | 本批已迁移至 `track.v2.cli._load_config`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/cli/artifacts.py` | Explicit, audit-friendly reconciliation of immutable Parquet artifacts. | 本批已迁移至 `track.v2.cli.reconcile_artifacts`；此行保留基线路径 |
| `services/pipeline/src/track/v2/cli/delete_run.py` | Delete one Refine or Detect Run after preview and two confirmations. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/cli/import_trajectories.py` | Import CSV trajectory data into the ``trajectory_points`` table. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/cli/init_db.py` | Create track.v2 schema v7 or migrate an existing v3/v4/v5/v6 schema in place. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/cli/pipeline_reporter.py` | Console adapter for typed pipeline events. | 本批已迁移至 `track.v2.cli.report_pipeline_events`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/cli/rasterize.py` | Rasterize one completed Refine Run. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/cli/replay.py` | Replay one persisted schema-v4 result back to verified source rows. | 本批已迁移至 `track.v2.cli.replay_run`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/cli/run.py` | Schema-v3 CLI for the database-backed track.v2 Pipeline. | 本批已迁移至 `track.v2.cli.run_pipeline`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/cli/semantic_diff.py` | CLI for bounded, read-only lineage comparison. | 本批已迁移至 `track.v2.cli.compare_run_lineages`；此行记录审查基线中的旧路径 |

### 数据库（12 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/track/v2/db/__init__.py` | Database infrastructure layer for track.v2 pipeline stages. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/db/artifact_migration.py` | Transactional, additive track.v2 schema v4 to v5 migration. | 本批已迁移至 `track.v2.db.migrations.artifacts_v5`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/db/artifact_registry.py` | Strict, transaction-neutral registry API for immutable columnar artifacts. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/db/completion_migration.py` | Transactional track.v2 schema v5 to v6 Refine completion migration. | 本批已迁移至 `track.v2.db.migrations.refine_completion_v6`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/db/cursors.py` | Scoped planning for server-side cursors whose results are fully consumed. | 本批已迁移至 `track.v2.db.plan_full_scan_cursor`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/db/detect_completion_migration.py` | Transactional track.v2 schema v6 to v7 Detect completion migration. | 本批已迁移至 `track.v2.db.migrations.detect_completion_v7`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/db/grid_points.py` | Read one Detect grid's Refine points and their subtrack neighbours. | 本批已迁移至 `track.v2.db.fetch_refine_points_in_grid`；Q89 内容差异保留 |
| `services/pipeline/src/track/v2/db/pipeline_store.py` | Stage-level persistence seam for the v2 pipeline. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/db/queries.py` | Database queries for Pipeline intersection, Clip, and Replay stages. | 本批按现行消费者拆分为 `track.v2.candidates.query_source_windows`、`track.v2.intersections.query_results`、`track.v2.trajectories.query_refine_points`、`track.v2.validation.query_replay_inputs`；`SpatialWindow` 移至 `track.v2.projection` |
| `services/pipeline/src/track/v2/db/refine_migration.py` | Transactional, data-preserving schema v3 → v4 Refine rename. | 本批已迁移至 `track.v2.db.migrations.refine_v4`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/db/schema.py` | Runtime schema version contract and guard for the destructive track.v2 API. | 保留 `track.v2.db.schema`；除 runtime guard 外导出共享 `SCHEMA_COMPONENT`、`SCHEMA_VERSION` 契约常量，多个服务和迁移模块直接使用；`schema_guard` 会缩窄职责 |
| `services/pipeline/src/track/v2/db/source_table.py` | Validation and immutable-source fingerprints for schema-v4 source tables. | 保留：现名与层级匹配 |

### 栅格（9 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/track/v2/raster/__init__.py` | Rasterize a persisted Refine Run's kept and injected point stream. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/raster/aggregation.py` | Conservative integer point counts. | 本批已迁移至 `track.v2.raster.aggregate_point_counts`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/raster/artifacts.py` | GeoTIFF and JSON outputs for one raster run. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/raster/grid.py` | Fixed-anchor metric grid and deterministic boundary ownership. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/raster/models.py` | Configuration and result types for point rasterization. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/raster/normalization.py` | Global nonzero-cell Q95 and uint8 sqrt rendering. | 本批已迁移至 `track.v2.raster.render_counts_to_uint8`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/raster/point_source.py` | Read exactly the Detect kept-plus-injected input for one Refine Run. | 本批已迁移至 `track.v2.raster.read_refine_points`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/raster/runner.py` | One read of Refine points followed by verified, isolated raster publication. | 本批已迁移至 `track.v2.raster.run_rasterization`；CLI 调用已更新 |
| `services/pipeline/src/track/v2/raster/tiling.py` | Padded RGB tiles, validity masks, and geographic index. | 本批已迁移至 `track.v2.raster.write_rgb_tiles`；此行记录审查基线中的旧路径 |

### 校验（3 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/track/v2/validation/__init__.py` | Read-only validation tools for completed Pipeline runs. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/validation/network_replay.py` | Controlled Network replay from bounded persisted Fitting inputs. | 本批已迁移至 `track.v2.validation.replay_network_from_fitting`；此行记录审查基线中的旧路径 |
| `services/pipeline/src/track/v2/validation/semantic_diff.py` | Bounded semantic comparison of two completed Pipeline lineages. | 本批已迁移至 `track.v2.validation.compare_run_lineages`；此行记录审查基线中的旧路径 |

### 基线手工工具与诊断（31 项，已删除）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/track/v2/manual_thin_benchmark/_init_.py` | 文件头未说明职责；按顶层定义核查 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/_thin_bench_helpers.py` | 薄壳测试脚本共享 helper —— 统一默认连接到 track_test 数据库。 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_attest_windows_gate_v1_legacy.py` | Create or verify the write-once attestation for legacy Windows Gate v1. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_clip.py` | Thin in-memory benchmark for schema-v4 resolved active-set Clip assembly. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_clip_pari.py` | 公开对象：main | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_count_vehicles.py` | Diagnose COUNT(DISTINCT vehicle_id) performance on trajectory_points (20M rows). | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_cross_track_inner.py` | Verify and time the complete Method 3 spatial-window stage. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_dbscan.py` | Strict baseline/optimized DBSCAN comparison for PERF-DBSCAN-001. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_fitting_variants.py` | Thin wall-clock benchmark for fixed combined/A->B/B->A fitting variants. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_hotspots.py` | Reproducible Getis-Ord G* equivalence and performance gate. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_iter_vehicle_tracks.py` | 薄壳脚本：量化 iter_vehicle_tracks() 各环节性能 + SQL 层诊断。 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_perf_hooks.py` | manual_bench_perf_hooks.py — 验证 _perf.py 性能计时框架的正确性与开销. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_point_objects.py` | Synthetic Parser object profile and Refine copy benchmark; no database I/O. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_read_refine.py` | Read-only v4 benchmark for the PostgreSQL source → Refine hot loop. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_schema_v4.py` | Read-only A65 audit for the track.v2 schema-v4 cutover. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_seed_1m.py` | 薄壳脚本：从 postgres.day01 取前 N 行覆盖 v3 来源表。 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_spatial_windows.py` | 薄壳脚本：测试 SpatialWindow 滑窗循环各环节耗时。 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_windows.py` | Strict archive-vs-worktree benchmark for cross-track spatial windows. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_windows_floor.py` | Isolated import-plus-frozen-input RSS floor diagnostic (no intersection algorithm). | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_windows_phase.py` | Render the non-gating Phase 0 view from validated R7 benchmark evidence. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_diag_hotspots_phases.py` | Phase-0 hotspot timing diagnostic with fail-closed provenance. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_diag_turn_events.py` | 手动诊断转向事件检测 —— 逐车统计每个过滤条件的拦截量。 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_diag_windows_hybrid_threshold.py` | Fixed, DB-free threshold diagnostic for bounded cross-track hybrid dispatch. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_diagnose_dbscan_phases.py` | Exclusive DBSCAN phase diagnostic for D-DB-PHASE-01. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_restore_windows_gate_v1.py` | Safely restore the frozen Windows Gate v1 source selection. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_setup_test_db.py` | 薄壳脚本：创建 track_test 独立测试数据库环境。 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_test_cross_track.py` | 手动测试 _extract_cross_track_windows 的薄壳脚本。 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/manual_verify_dbscan_wrappers.py` | Archive-to-worktree strict wrapper snapshot gate for PERF-DBSCAN-001. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/perf_windows_gate.py` | Freeze the immutable inputs for PERF-WINDOWS-STRICT-001. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/projection_difference_report.py` | Auditable legacy-to-fixed projection difference reports and approvals. | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |
| `services/pipeline/src/track/v2/manual_thin_benchmark/visualize_replay.py` | 可视化 replay 溯源链路：空间分布 + 溯源 DAG。 | 按 2026-09-25 用户决定删除，且不纳入命名迁移；此行仅保留 f3e214f 基线审查记录 |

### Pipeline 配置（2 个）

| 当前文件 | 职责线索 | 建议 |
| --- | --- | --- |
| `services/pipeline/src/track/v2/config/__init__.py` | Pipeline configuration types and strict TOML loader. | 保留：现名与层级匹配 |
| `services/pipeline/src/track/v2/config/pipeline_config_loader.py` | Strict, single-source Pipeline TOML configuration loader. | 保留：现名与层级匹配 |
