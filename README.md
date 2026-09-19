# Multimodal GEO Roadnet

## 当前状态

当前仓库采用单 Repository、多 Docker Service 的结构。轨迹重建 Pipeline 最初从宿主机源码仓库的 `codex/pipe-06a-integration@75fa562` 迁入 `services/pipeline/src/track/v2/`；初始迁入保留了包名、阶段顺序和 lineage 意图。当前实现已在此基线上重构 Detect、Clip、持久化和 Run 生命周期，不承诺与来源版本逐行输出、顺序或失败重放完全等价。

源码 provenance 与初始迁入边界见 [`services/pipeline/SOURCE.md`](services/pipeline/SOURCE.md)，当前执行架构和取舍见 [`docs/performance-optimization-review.md`](docs/performance-optimization-review.md)，2026-09-01 的完整 100 万行基准、逐项优化收益和内存口径见 [`docs/pipeline-1m-performance-benchmark-spec.md`](docs/pipeline-1m-performance-benchmark-spec.md)。2,000 万行输入的规模推演、全量物化审计、3 GB 内存合同和外存化改造门槛见 [`docs/pipeline-20m-memory-budget-spec.md`](docs/pipeline-20m-memory-budget-spec.md)；当前实现尚未通过该合同。

## 目录职责

- `apps/frontend`：独立前端开发容器；仅负责 UI 和 Backend API 调用。
- `apps/backend`：独立 FastAPI API 容器；提供健康检查，以及只读、限量的地图图层、Replay 和 Clip GeoJSON API。
- `services/pipeline`：Pipeline 独立运行单元，包含 Refine、Detect、Clip、Fitting、Network、持久化、回放和数据库初始化逻辑。Detect 的 DB 模式只构建一次 Refine 轨迹 workspace，并由转向/正交检测、跨车求交和拓扑验证共享；Backend request process 不执行这些耗时阶段。
- `services/vision`：可选 YOLOv8 CUDA 11.7 离线推理/跟踪运行单元，通过 Compose `vision` profile 启用。
- `db/postgres`、`db/qdrant`：数据库定义、初始化和迁移逻辑，不保存运行时数据。
- `infra/docker`：开发和生产应用 Compose 文件；PostgreSQL 与 Qdrant 的独立 Compose 位于宿主机 `/data/services`。

Detect 内部的视觉判别能力仍可作为 Pipeline 内部逻辑。独立 YOLOv8 GPU Runtime 已迁入 `services/vision`，但不强制改造成网络微服务，也不由 Backend request process 执行。没有稳定的跨服务共享语义时不创建 `libs/`。

## 开发环境

```sh
cp .env.example .env
make services-up
docker compose --env-file .env -f infra/docker/compose.dev.yml up -d --build
```

`make dev-up` 会先执行 `make services-up`。数据库栈可独立使用 `make services-up`、
`make services-logs`、`make services-down` 管理；应用与数据库通过外部 Docker 网络
`roadnet-services` 通信。

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
  `intersection_id`（三选一）回放。Replay 拒绝 `bbox`；前端框选只提交已加载要素的准确 ID。
  Detect 产物使用内部 `resolved_point_ids` 并要求
  `detect_id`；原始/Refine 来源使用字符串化的 `source_row_keys`。
  点 ID 先定位命中子轨迹，再有界读取其来源点；交叉口 ID 沿候选交叉口及事件外键读取构成点。
  Replay 不再全量读取 Refine 源表计算指纹，仅读取所选溯源链，并检查读取到的来源记录是否缺失。
  这不等价于全库不可变性校验：与选择无关的源行变更不会在 Replay 中检测。
- `POST /api/v1/map/clip`：按 `detect_id` 与两个 `intersection_ids` 截取交叉口间轨迹；ID 值是 PostgreSQL `intersections.id`，不再接受 Detect 运行时的 cluster 计算序号。

这些 API 使用只读 PostgreSQL 事务、语句超时和请求数量上限；不会在 Backend 请求进程中启动完整 Pipeline 作业。

## 配置与运行时数据

真实 `.env` 不提交 Git。复制 `.env.example` 后按需修改；生产环境必须设置非示例的 `POSTGRES_PASSWORD` 和外部 `DATA_ROOT`。

`DATA_ROOT` 保存可丢弃的 Pipeline 工作区、中间文件和运行产物；当前宿主机使用
`/data/tmp/road-network`，并挂载到容器 `/data`。原始或公共数据集放在
`/data/datasets`，最终交付物放在 `/data/export`，均不混入临时工作区。模型权重是
独立持久资产，使用 `/data/service-data/vision/models`，不进入代码仓库。

`.env` 的目录配置均为宿主机路径，容器内目录由 Compose 固定，通过绑定挂载（bind mount）连接，不使用跨容器软链接。

| 宿主机目录配置 | 默认宿主机目录 | Pipeline 容器内目录 |
| --- | --- | --- |
| `DATA_ROOT` | 开发环境 `/data/tmp/road-network`；生产环境必填 | `/data` |
| `PARQUET_ROOT` | `${DATA_ROOT}/parquet` | `/data/parquet` |
| `PIPELINE_LOG_DIR` | `${DATA_ROOT}/logs` | `/data/logs` |

Pipeline 日志文件名为 `<timestamp>_pid-<pid>_log.txt`，每个进程使用独立文件；`PIPELINE_LOG_DIR` 可指向 `DATA_ROOT` 之外的宿主机目录。Compose 不再读取 `PIPELINE_LOG_PATH`。旧配置中若将 `PIPELINE_LOG_DIR` 写成容器路径 `/data/logs`，需删掉该项使用默认值，或改成所需宿主机目录。挂载变更对新建容器生效；常驻服务需重新创建容器，已有日志不会自动搬移。

每个 stage 都记录带关联 run ID 的 `[stage-start]` 和 `[stage-finish]`，结束记录中的 `elapsed_s` 是单调时钟测得的阶段耗时。Fitting 另记录 `phase=fit_compute` 与 `phase=fit_persist` 的 `[stage-phase]`；Detect 会记录全局空间索引的 membership、overflow 和候选数量，用于区分计算、持久化和资源回退耗时。

YOLO 模型通过 `MODEL_ROOT` 挂载到容器 `/models`，当前宿主机目录为
`/data/service-data/vision/models`。输入和运行输出分别使用 `/data/vision/input` 与
`/data/vision/output`，因此属于 `DATA_ROOT` 下的临时运行数据。

PostgreSQL 与 Qdrant 不再由项目 Compose 创建。独立编排文件为
`/data/services/compose.yml`，运行数据通过绑定挂载分别保存到
`/data/service-data/postgres` 和 `/data/service-data/qdrant`。数据库配置及本机凭据位于
`/data/services`，其中 `.env` 必须保持为本机私有文件；项目 Compose 只通过外部网络
`roadnet-services` 使用服务名 `postgres` 和 `qdrant` 访问数据库。

Pipeline 的数据库配置优先读取进程中的来源兼容变量 `TRACK_PG_*`，其次读取标准 Compose 变量 `POSTGRES_HOST`、`POSTGRES_PORT`、`POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`。这只是环境变量别名兼容，不代表旧执行架构仍受支持；代码不再提供默认数据库密码。

## Pipeline 运行

空库直接执行 `init-db`。已有 v3 数据库使用 `--migrate-refine` 保留数据升级。
仅在明确需要丢弃旧业务结果时，停止 Pipeline 写入、核对连接目标后执行全量重建：

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline init-db --database roadnet --reset
```

初始化器创建 `track.v2` schema version 4。无迁移选项时，它只接受空库或
已经标记为 v4 的数据库；v3 使用 `init-db --migrate-refine` 保留数据升级，
检测到未版本化表、v1 或 v2 会失败。Pipeline 与
Backend 启动时同样校验版本。`--reset` 会删除全部 Pipeline 业务数据，因此每次
执行前都要核对输出的 database、user 与 schema。

Refine 改名涉及数据库、CLI、配置和前后端字段；升级步骤、兼容入口与性能验证见
[Refine 迁移与加速说明](docs/refine-migration-and-performance.md)。

地图 Clip 请求的值语义也随之改变：仓内 Frontend 与 QGIS 插件已改传 `intersections.id`，仍传旧 `cluster_id` 的外部客户端必须同步升级。

查看完整参数或从 PostgreSQL 来源表运行：

```sh
make pipeline-help
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline run --source-table public.day01 \
  --processing-srid 32650
```

输入使用 `--source-table` 或 `--refine-id` 二选一。来源表接受
`public.day01` 这类 schema-qualified 名称，并必须提供唯一非空的 `id` 以及
`vehicle_id/timestamp/lon/lat/heading/speed/geom`；`extra` 可选。Pipeline 不再把
CSV 当作运行入口，需要时先用独立 importer 导入 PostgreSQL。`--refine-id UUID`
只复用已完成且来源指纹仍一致的 Refine Run，并始终创建新的 Detect Run；Detect
完成后再创建 Fit Run。Pipeline 是独立批处理进程，不经由 Backend 请求启动。

当前 Run lineage 为 `refine_runs → detect_runs → fit_runs`。Detect 的完整
`resolved_subtrack_points` 序列与所有 Detect 子产物在一个最终事务中落盘；失败
会回滚子表并单独标记 Run 为 `failed`。只有 completed Detect 可供 Clip/Replay
消费。同一 Detect/Fit Run 不支持重复落库，重新计算应创建新 Run。

`32650` 仅是北京所在 UTM 分区的命令示例，不是项目默认值。新 Refine Run 必须按完整数据范围明确选择米制投影；也可在自定义 `pipeline.toml` 的 `[projection]` 中配置。使用 `--refine-id UUID` 时会继承原 Run 的投影，不能覆盖。选择与迁移规则见 [Run 级投影上下文](docs/run-projection-context.md)。

## 常用命令

交叉口 CV 的原始点统计、五组灰度归一化和交互预览，见
[灰度归一化预览](docs/cv-normalization-preview.md)。入口为
`python -m scripts.cv_human_check.cv_preview`，产物写入 `DATA_ROOT`，不启动模型训练。

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
