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
docker compose --env-file .env -f infra/docker/compose.dev.yml up -d --build
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

打开 Frontend 后可在左侧图层树切换业务图层，在地图工具栏选择平移、点选或框选。Replay 接受来源行键、Detect resolved point ID 或 WGS84 框选范围；Clip 要求同一 Detect Run 中恰好两个交叉口。两者的返回值只绘制到内存 `tmp` 图层，不写数据库或中间文件。地图窗口显示 EPSG:3857，Backend GeoJSON 固定为 EPSG:4326，前端负责投影转换。

QGIS 3.44 LTR 插件源码位于 `apps/qgis-plugin/roadnet_tools/`。开发机可将该目录链接到 QGIS 用户插件目录，或按 [插件说明](apps/qgis-plugin/README.md) 打包 ZIP 安装。启用 **Roadnet Tools** 后，在点图层中使用 QGIS 自带点选/框选，随后从 **Vector → Roadnet Tools** 执行 Replay 或 Clip；后端 URL 默认为 `http://localhost:8000`，可在插件设置中修改。成功响应会替换插件管理的内存图层 `tmp`，请求或解析失败会保留上一次成功结果。

地图 API：

- `GET /api/v1/map/layers`：图层目录及默认 Detect/Fit 上下文。
- `GET /api/v1/map/layers/{layer_id}`：读取有上限的 EPSG:4326 GeoJSON，可传 `bbox`、`detect_id`、`fit_id` 和 `limit`。
- `POST /api/v1/map/replay`：按 `source_row_keys`、`resolved_point_ids` 或
  `bbox`（三选一）回放轨迹。Detect 产物使用内部 `resolved_point_ids` 并要求
  `detect_id`；原始/Clean 来源使用字符串化的 `source_row_keys`。
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

Schema v3 是破坏性切换，不提供旧数据迁移。首次切换必须先停止 Pipeline 写入，
核对连接目标，然后显式全量重建：

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline init-db --database roadnet --reset
```

初始化器只创建 `track.v2` schema version 3。无 `--reset` 时，它只接受空库或
已经标记为 v3 的数据库；检测到未版本化表、v1 或 v2 会立即失败。Pipeline 与
Backend 启动时同样校验版本。`--reset` 会删除全部 Pipeline 业务数据，因此每次
执行前都要核对输出的 database、user 与 schema。

地图 Clip 请求的值语义也随之改变：仓内 Frontend 与 QGIS 插件已改传 `intersections.id`，仍传旧 `cluster_id` 的外部客户端必须同步升级。

查看完整参数或从 PostgreSQL 来源表运行：

```sh
make pipeline-help
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline run --source-table public.day01 \
  --processing-srid 32650
```

输入使用 `--source-table` 或 `--clean-id` 二选一。来源表接受
`public.day01` 这类 schema-qualified 名称，并必须提供唯一非空的 `id` 以及
`vehicle_id/timestamp/lon/lat/heading/speed/geom`；`extra` 可选。Pipeline 不再把
CSV 当作运行入口，需要时先用独立 importer 导入 PostgreSQL。`--clean-id UUID`
只复用已完成且来源指纹仍一致的 Clean Run，并始终创建新的 Detect Run；Detect
完成后再创建 Fit Run。Pipeline 是独立批处理进程，不经由 Backend 请求启动。

当前 Run lineage 为 `clean_runs → detect_runs → fit_runs`。Detect 的完整
`resolved_subtrack_points` 序列与所有 Detect 子产物在一个最终事务中落盘；失败
会回滚子表并单独标记 Run 为 `failed`。只有 completed Detect 可供 Clip/Replay
消费。同一 Detect/Fit Run 不支持重复落库，重新计算应创建新 Run。

`32650` 仅是北京所在 UTM 分区的命令示例，不是项目默认值。新 Clean Run 必须按完整数据范围明确选择米制投影；也可在自定义 `pipeline.toml` 的 `[projection]` 中配置。使用 `--clean-id UUID` 时会继承原 Run 的投影，不能覆盖。选择与迁移规则见 [Run 级投影上下文](docs/run-projection-context.md)。

## 常用命令

交叉口 CV 的原始点统计、五组灰度归一化和交互预览，见
[灰度归一化预览](docs/cv-normalization-preview.md)。入口为
`python -m pipeline cv-preview`，产物写入 `DATA_ROOT`，不启动模型训练。

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
