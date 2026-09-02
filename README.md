# Multimodal GEO Roadnet

## 当前状态

当前仓库采用单 Repository、多 Docker Service 的结构。轨迹重建 Pipeline 最初从宿主机源码仓库的 `codex/pipe-06a-integration@75fa562` 迁入 `services/pipeline/src/track/v2/`；初始迁入保留了包名、阶段顺序和 lineage 意图。当前实现已在此基线上重构 Detect、Clip、持久化和 Run 生命周期，不承诺与来源版本逐行输出、顺序或失败重放完全等价。

源码 provenance 与初始迁入边界见 [`services/pipeline/SOURCE.md`](services/pipeline/SOURCE.md)，当前执行架构和取舍见 [`docs/performance-optimization-review.md`](docs/performance-optimization-review.md)，2026-09-01 的完整 100 万行基准、逐项优化收益和内存口径见 [`docs/pipeline-1m-performance-benchmark-spec.md`](docs/pipeline-1m-performance-benchmark-spec.md)。2,000 万行输入的规模推演、全量物化审计、3 GB 内存合同和外存化改造门槛见 [`docs/pipeline-20m-memory-budget-spec.md`](docs/pipeline-20m-memory-budget-spec.md)；当前实现尚未通过该合同。

## 目录职责

- `apps/frontend`：独立前端开发容器；仅负责 UI 和 Backend API 调用。
- `apps/backend`：独立 FastAPI API 容器；提供健康检查，以及只读、限量的地图图层、Replay 和 Clip GeoJSON API。
- `services/pipeline`：Pipeline 独立运行单元，包含 Cleaner、Detect、Clip、Fitting、Network、持久化、回放和数据库初始化逻辑。Detect 的 DB 模式只构建一次 Clean 轨迹 workspace，并由转向/正交检测、跨车求交和拓扑验证共享；Backend request process 不执行这些耗时阶段。
- `services/vision`：可选 YOLOv8 CUDA 11.7 离线推理/跟踪运行单元，通过 Compose `vision` profile 启用。
- `db/postgres`、`db/qdrant`：数据库定义、初始化和迁移逻辑，不保存运行时数据。
- `infra/docker`：开发和生产 Compose 文件。

Detect 内部的视觉判别能力仍可作为 Pipeline 内部逻辑。独立 YOLOv8 GPU Runtime 已迁入 `services/vision`，但不强制改造成网络微服务，也不由 Backend request process 执行。没有稳定的跨服务共享语义时不创建 `libs/`。

## 开发环境

```sh
cp .env.example .env
docker compose -f infra/docker/compose.dev.yml up -d --build
```

服务入口：

- Frontend：`http://localhost:${FRONTEND_PORT:-5173}`
- Backend：`http://localhost:${BACKEND_PORT:-8000}/health`
- Backend 依赖检查：`/health/dependencies`
- 地图图层目录：`/api/v1/map/layers`
- PostgreSQL 主机映射默认是 `15432`，容器内服务名和端口仍是 `postgres:5432`。
- Qdrant 主机映射默认是 `16333`，容器内服务名和端口仍是 `qdrant:6333`。

宿主机端口映射与容器内连接配置分开：`POSTGRES_PORT`/`QDRANT_PORT` 是容器内端口，`POSTGRES_HOST_PORT`/`QDRANT_HOST_PORT` 仅用于开发机发布端口。

前端源码通过 bind mount 热更新，`frontend_node_modules` 使用 Docker named volume。Backend 和 Pipeline 也分别使用独立源码挂载和独立 Docker build context。

## Web GIS 与 QGIS 插件

打开 Frontend 后可在左侧图层树切换业务图层，在地图工具栏选择平移、点选或框选。Replay 接受轨迹点主键或 WGS84 框选范围；Clip 要求同一 Detect Run 中恰好两个交叉口。两者的返回值只绘制到内存 `tmp` 图层，不写数据库或中间文件。地图窗口显示 EPSG:3857，Backend GeoJSON 固定为 EPSG:4326，前端负责投影转换。

QGIS 3.44 LTR 插件源码位于 `apps/qgis-plugin/roadnet_tools/`。开发机可将该目录链接到 QGIS 用户插件目录，或按 [插件说明](apps/qgis-plugin/README.md) 打包 ZIP 安装。启用 **Roadnet Tools** 后，在点图层中使用 QGIS 自带点选/框选，随后从 **Vector → Roadnet Tools** 执行 Replay 或 Clip；后端 URL 默认为 `http://localhost:8000`，可在插件设置中修改。成功响应会替换插件管理的内存图层 `tmp`，请求或解析失败会保留上一次成功结果。

地图 API：

- `GET /api/v1/map/layers`：图层目录及默认 Detect/Fit 上下文。
- `GET /api/v1/map/layers/{layer_id}`：读取有上限的 EPSG:4326 GeoJSON，可传 `bbox`、`detect_id`、`fit_id` 和 `limit`。
- `POST /api/v1/map/replay`：按 `point_ids` 或 `bbox`（二选一）回放轨迹。
- `POST /api/v1/map/clip`：按 `detect_id` 与两个 `intersection_ids` 截取交叉口间轨迹；ID 值是 PostgreSQL `intersections.id`，不再接受 Detect 运行时的 cluster 计算序号。

这些 API 使用只读 PostgreSQL 事务、语句超时和请求数量上限；不会在 Backend 请求进程中启动完整 Pipeline 作业。

## 配置与运行时数据

真实 `.env` 不提交 Git。复制 `.env.example` 后按需修改；生产环境必须设置非示例的 `POSTGRES_PASSWORD` 和外部 `DATA_ROOT`。

Raw trajectory、Parquet、中间文件、导出文件和模型权重属于 Runtime Data。开发/生产通过 `DATA_ROOT` 挂载到容器 `/data`，例如宿主机 `/data/road-network` 对应容器 `/data`；代码仓库不创建 `data/raw` 或 `data/parquet`。

Pipeline 日志默认写入容器内 `/data/logs/<timestamp>_pid-<pid>_log.txt`，即宿主机 `${DATA_ROOT}/logs/`。每个进程使用独立文件；每个 stage 都记录带关联 run ID 的 `[stage-start]` 和 `[stage-finish]`，结束记录中的 `elapsed_s` 是单调时钟测得的阶段耗时。Fitting 另记录 `phase=fit_compute` 与 `phase=fit_persist` 的 `[stage-phase]`；Detect 会记录全局空间索引的 membership、overflow 和候选数量，用于区分计算、持久化和资源回退耗时。可用 `PIPELINE_LOG_DIR` 修改目录；仅在确实需要共享固定文件时设置 `PIPELINE_LOG_PATH`，且不要指向源码目录。

YOLO 模型在容器内挂载到 `/models`，输入和输出分别使用 `/data/vision/input` 与 `/data/vision/output`。本地迁移权重位于被 Git 忽略的 `models/yolo/yolov8n.pt`；生产环境必须通过外部 `MODEL_ROOT` 挂载。

PostgreSQL 使用 `postgres_data:/var/lib/postgresql/data`，Qdrant 使用 `qdrant_data:/qdrant/storage`。这两个 named volume 不绑定到仓库目录。

Pipeline 的数据库配置优先读取进程中的来源兼容变量 `TRACK_PG_*`，其次读取标准 Compose 变量 `POSTGRES_HOST`、`POSTGRES_PORT`、`POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`。这只是环境变量别名兼容，不代表旧执行架构仍受支持；代码不再提供默认数据库密码。

## Pipeline 运行

首次创建或更新业务表结构时执行当前权威 Python 初始化/迁移入口：

```sh
make pipeline-init-db
```

非 `--reset` 初始化在一个事务内处理当前建表、自动补列、显式结构迁移和索引同步；已有 named volume 升级代码后也必须执行。它会为旧表补齐 `turn_events.tp_source_id` 和 `turn_events.tp_source_row_key`，并删除不再持久化的事件及候选簇算法序号。事件唯一使用 `turn_events.id`；候选路口和完整路口分别使用 `candidate_intersections.id` 与 `intersections.id`，Centerline 端点也引用完整路口数据库 ID。运行时的事件和簇计算序号均不落库。只有数据库仍存在早期单表 Run 注册表 `pipeline_runs` 且 Clean 父子关系无法确定时，才需要通过 `--legacy-clean-map` 提供精确的 Detect UUID → Clean UUID 映射。`--reset` 会删除全部 Pipeline 业务数据，不能用于普通升级。

删除这些计算序号列的版本不支持新旧 writer 滚动混跑。升级已有数据库时，先停止 Pipeline 写入，执行 `make pipeline-init-db`，再启动新版本 Pipeline。迁移会先校验旧的簇引用、回填 Centerline 及其 Clip manifest 的 `BIGINT` 路口 ID，再删除旧列；任何映射冲突或历史 combined Centerline 端点次序与数据库 ID 次序不一致都会中止并回滚，不会静默交换端点。

地图 Clip 请求的值语义也随之改变：仓内 Frontend 与 QGIS 插件已改传 `intersections.id`，仍传旧 `cluster_id` 的外部客户端必须同步升级。

查看完整参数或从外部数据目录运行 CSV：

```sh
make pipeline-help
docker compose -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline run --csv /data/parquet-or-csv/input.csv \
  --processing-srid 32650
```

输入使用 `--csv`、`--pg-table` 或 `--clean-id` 三选一。`--pg-table` 接受 `public.day01` 这类 schema-qualified 名称；进度总行数通过 `to_regclass` 解析 `pg_class.reltuples`，因此是估算值。`--clean-id UUID` 只复用已完成的 Clean Run，并始终创建新的 Detect Run；Detect 完成后再创建新的 Fit Run。Clip 没有独立 Run 注册表，Network 当前也不单独持久化。Pipeline 是独立批处理进程，不经由 Backend API request process 启动。

当前 Run lineage 为 `clean_runs → detect_runs → fit_runs`，早期的 `pipeline_runs` 只属于一次性数据库迁移输入，不是当前执行模型。Detect 的 append-only 派生产物使用 PostgreSQL COPY，并假定写入新建的 Run ID；同一 Detect/Fit Run 的重复落库不保证幂等，重新计算应创建新 Run。普通 Detect 异常会回滚派生产物，并把 Run 标为 `failed`；进程被强制终止时仍可能需要运维侧清理长期 `running` 记录。

`32650` 仅是北京所在 UTM 分区的命令示例，不是项目默认值。新 Clean Run 必须按完整数据范围明确选择米制投影；也可在自定义 `pipeline.toml` 的 `[projection]` 中配置。使用 `--clean-id UUID` 时会继承原 Run 的投影，不能覆盖。选择与迁移规则见 [Run 级投影上下文](docs/run-projection-context.md)。

## 常用命令

```sh
make dev-config
make dev-up
make dev-logs
make test
make frontend-test
make qgis-plugin-test
make pipeline-check
make pipeline-init-db
make pipeline-help
make vision-build
make vision-check
make vision-run
make dev-down
```

`make test` 会运行 Backend 与 Pipeline 的 pytest 套件。提交或性能报告必须列出实际执行的验证命令和结果，并逐项说明已知失败；不要用来源仓库或“legacy 基线”笼统替代当前验证状态。
