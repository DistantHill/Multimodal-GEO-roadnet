# Multimodal GEO Roadnet

## 当前状态

当前仓库采用单 Repository、多 Docker Service 的结构。轨迹重建 Pipeline 最初从宿主机源码仓库的 `codex/pipe-06a-integration@75fa562` 迁入 `services/pipeline/src/track/v2/`；初始迁入保留了包名、阶段顺序和 lineage 意图。当前实现已在此基线上重构 Detect、Clip、持久化和 Run 生命周期，不承诺与来源版本逐行输出、顺序或失败重放完全等价。

源码 provenance 与初始迁入边界见 [`services/pipeline/SOURCE.md`](services/pipeline/SOURCE.md)。当前执行依据是 [Pipeline 列式存储与计算架构 Spec](docs/pipeline-columnar-architecture-spec.md)，部署和故障处置见 [Pipeline 列式架构运维指南](docs/pipeline-columnar-operations.md)。2026-09-01 的完整 100 万行历史基准、逐项优化收益和内存口径见 [`docs/pipeline-1m-performance-benchmark-spec.md`](docs/pipeline-1m-performance-benchmark-spec.md)。精确 26M、默认 2 GB 和一小时目标尚未完成最终验收。

## 目录职责

- `apps/frontend`：独立前端开发容器；仅负责 UI 和 Backend API 调用。
- `apps/backend`：独立 FastAPI API 容器；提供健康检查，以及只读、限量的地图图层、Replay 和 Clip GeoJSON API。
- `services/pipeline`：Pipeline 独立运行单元，包含 Refine、Detect、Clip、Fitting、Network、列式制品、持久化、回放和数据库初始化逻辑。PostgreSQL 保存权威状态，Refine/Detect 发布不可变 Parquet，DuckDB 承担主要列式计算；Backend request process 不执行这些耗时阶段。
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

宿主机端口映射与容器内连接配置分开：`POSTGRES_HOST_PORT` 和
`QDRANT_HOST_PORT` 仅用于开发机发布端口；容器内主机、端口、数据库名和用户均由
各服务的 TOML 配置。

前端源码通过 bind mount 热更新，`frontend_node_modules` 使用 Docker named volume。Backend 和 Pipeline 也分别使用独立源码挂载和独立 Docker build context。

## Web GIS 与 QGIS 插件

打开 Frontend 后可在左侧图层树切换业务图层，在地图工具栏选择平移、点选或框选。Replay 接受来源行键、Detect resolved point ID、交叉口或事件 ID；框选仅提交已加载要素的准确 ID；Clip 要求同一 Detect Run 中恰好两个交叉口。两者的返回值只绘制到内存 `tmp` 图层，不写数据库或中间文件。地图窗口显示 EPSG:3857，Backend GeoJSON 固定为 EPSG:4326，前端负责投影转换。

QGIS 3.44 LTR 插件源码位于 `apps/qgis-plugin/roadnet_tools/` 。开发机可将该目录链接到 QGIS 用户插件目录，或按 [插件说明](apps/qgis-plugin/README.md) 打包 ZIP 安装。启用 **Roadnet Tools** 后，在轨迹点、事件或交叉口图层中使用 QGIS 自带点选/框选，随后从 **Vector → Roadnet Tools** 执行 Replay 或 Clip；后端 URL 默认为 `http://localhost:8000`，可在插件设置中修改。成功响应会替换插件管理的内存图层 `tmp`，请求或解析失败会保留上一次成功结果。

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

真实 `.env` 不提交 Git。复制 `.env.example` 后设置密码、宿主机挂载、端口及两个服务
配置路径；生产环境必须设置非示例的 `POSTGRES_PASSWORD` 和外部 `DATA_ROOT`。

Pipeline 的完整非敏感配置位于 `services/pipeline/config/pipeline.toml`，字段定义位于
`services/pipeline/config/field_specs.toml`；Backend 配置位于 `apps/backend/config/backend.toml`。
TOML 是非敏感参数的唯一权威来源，`.env` 只保存秘密、Compose 宿主机值和
`PIPELINE_CONFIG_PATH` / `BACKEND_CONFIG_PATH`。每个进程只在启动时加载一次；修改
TOML 后必须重新创建或重启对应服务，运行中的 Pipeline Run 不会热重载。不支持
旧的逐项环境变量、`TRACK_PG_*` 别名或 CLI 配置覆盖。

`DATA_ROOT` 仅作为 Compose 宿主机挂载根目录，保存可丢弃的 Pipeline 工作区、中间文件和运行产物；当前宿主机使用
`/data/tmp/road-network`，并挂载到容器 `/data`。原始或公共数据集放在
`/data/datasets`，最终交付物放在 `/data/export`，均不混入临时工作区。模型权重是
独立持久资产，使用 `/data/service-data/vision/models`，不进入代码仓库。

`.env` 的目录配置均为宿主机路径，容器内目录由 Compose 固定，通过绑定挂载（bind mount）连接，不使用跨容器软链接。

| 宿主机目录配置 | 默认宿主机目录 | Pipeline 容器内目录 |
| --- | --- | --- |
| `DATA_ROOT` | 开发环境 `/data/tmp/road-network`；生产环境必填 | `/data` |
| `PARQUET_ROOT` | `${DATA_ROOT}/parquet` | `/data/parquet` |
| DuckDB 临时目录（从 `DATA_ROOT` 派生） | `${DATA_ROOT}/duckdb` | `/data/duckdb` |
| Polars 临时目录（从 `DATA_ROOT` 派生） | `${DATA_ROOT}/polars` | `/data/polars` |
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

Pipeline 和 Backend 的数据库主机、端口、数据库名、用户及超时等非敏感值从各自 TOML
读取；只有 `POSTGRES_PASSWORD` 由根目录 `.env` 注入。缺少密码、仍为占位值或配置文件无效时，
服务在启动阶段直接失败。

## Pipeline 运行

空库直接执行 `init-db`。已有 v3 数据库使用 `--migrate-refine` 保留数据并依次完成 Refine
改名、制品登记与完成态约束升级；v4、v5 或 v6 使用 `--migrate-artifacts` 依次升级制品
登记和 Refine/Detect completed Run 的制品约束。
仅在明确需要丢弃旧业务结果时，停止 Pipeline 写入、核对连接目标后执行全量重建：

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline init-db --reset
```

初始化器创建 `track.v2` schema version 7。无迁移选项时，它只接受空库或
已经标记为 v7 的数据库；检测到未版本化表、v1 或 v2 会失败。Pipeline 与
Backend 启动时同样校验版本。`--reset` 会删除全部 Pipeline 业务数据，因此每次
执行前都要核对输出的 database、user 与 schema。

Refine 改名涉及数据库、CLI、配置和前后端字段；升级步骤、兼容入口与性能验证见
[Refine 迁移与加速说明](docs/refine-migration-and-performance.md)。

地图 Clip 请求的值语义也随之改变：仓内 Frontend 与 QGIS 插件已改传 `intersections.id`，仍传旧 `cluster_id` 的外部客户端必须同步升级。

查看完整参数或从 PostgreSQL 来源表运行：

```sh
make pipeline-help
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline run --source-table public.day01
```

输入使用 `--source-table` 或 `--refine-id` 二选一。来源表接受
`public.day01` 这类 schema-qualified 名称，并必须提供唯一非空的 `id` 以及
`vehicle_id/timestamp/lon/lat/heading/speed/geom`；`extra` 可选。Pipeline 不再把
CSV 当作运行入口，需要时先用独立 importer 导入 PostgreSQL。
独立 importer 支持 `--extra=true|false`，默认 `true` 保留原有额外字段解析行为；
`--extra=false` 只解析并导入映射的 `vehicle_id/timestamp/lon/lat/heading/speed`，
不解析额外字段，也不写入 `extra` 列（已有列使用数据库默认值）。
例如：`python -m pipeline import-trajectories --csv input.csv --extra=false`。
`--refine-id UUID`
只复用已完成 Refine Run；上下游产物按 `refine_id`、`detect_id` 的 UUID 关联，
Refine 后及 Detect 前后不扫描或比较源数据指纹。每次始终创建新的 Detect Run；Detect
完成后再创建 Fit Run。Pipeline 是独立批处理进程，不经由 Backend 请求启动。

`make test` 运行 pytest 测试套件。`pipeline-smoke-test` 则执行真实流水线，
使用 `pipeline-roadnet-test.toml` 冒烟测试配置，连接 `roadnet_test` 并提交运行结果。
可指定来源表，按需设置 `REFINE_RESUME_LAST`；省略 Bool 时由终端询问，`true` 自动复用最新已完成结果，`false` 新建 Refine：

```sh
make pipeline-smoke-test SOURCE_TABLE=public.day01
make pipeline-smoke-test SOURCE_TABLE=public.day01 REFINE_RESUME_LAST=true
make pipeline-smoke-test REFINE_RESUME_LAST=true
```

也可直接复用指定批次：

```sh
make pipeline-smoke-test REFINE_ID=00000000-0000-0000-0000-000000000000
```

`pipeline-full-test` 使用 `pipeline.toml` 全量测试配置，连接 `roadnet` 并提交运行结果，
支持同样的 `SOURCE_TABLE`、`REFINE_ID` 与 `REFINE_RESUME_LAST` 选择方式：

```sh
make pipeline-full-test SOURCE_TABLE=public.day01
make pipeline-full-test SOURCE_TABLE=public.day01 REFINE_RESUME_LAST=false
make pipeline-full-test REFINE_RESUME_LAST=true
```

### 全量测试日志监控

启动全量测试后，使用独立后台脚本 `scripts/watch_pipeline_log.py` 监控本次运行
的日志，不在 agent session 内循环等待或轮询日志、进程。脚本启动时记录一次
日志末尾，此后每 10 分钟记录一次，默认每次 20 行并附带时间戳。

将 `run_log` 设置为本次 Pipeline 输出的日志文件对应的宿主机路径；默认宿主机
日志目录为 `DATA_ROOT/logs`，设置了 `PIPELINE_LOG_DIR` 时以该挂载目录为准。
使用明确的文件名，避免跟随其他并行任务的日志。监控输出也保存在仓库外：

```sh
run_log=/data/tmp/road-network/logs/本次运行日志.txt
monitor_log=/tmp/roadnet-full-monitor.log
nohup python3 scripts/watch_pipeline_log.py --log "$run_log" --output "$monitor_log" \
  > /tmp/roadnet-full-monitor-launch.log 2>&1 < /dev/null &
monitor_pid=$!
echo "$monitor_pid"
```

按需一次性查看 `monitor_log`，不要在会话中继续轮询监控脚本。脚本只记录日志，
不判断测试是否成功，也不启动、重启或终止 Pipeline。确认测试结束后，用
`kill "$monitor_pid"` 停止监控，再结合测试退出码和最终日志确认结果。

单独传 `REFINE_RESUME_LAST=true` 时，从当前配置连接的数据库中选择最近完成的 Refine Run；
没有已完成批次则报错。与 `SOURCE_TABLE` 同传时只在该来源表的批次中选择。

这两个命令执行 Refine、Detect、Clip、Fitting 和 Network，不会在结束时清理业务结果。
`--rm` 只移除本次临时容器；PostgreSQL 记录、挂载目录中的 Parquet 和日志仍保留。
当前 Network 阶段在内存中组装路网；持久化内容以各阶段已有的数据库和制品写入逻辑为准。

CLI 也支持 `--refine-resume-last true|false`。显式 `--refine-id` 与来源表二选一，且不能与该 Bool 同时使用。单独传 `--refine-resume-last true` 复用当前数据库最近完成的 Refine。省略 Bool 时，有候选且终端可交互则默认复用；非交互环境请显式传 `true` 或 `false`。复用时输出历史 Refine 数据类别、历史 Parser/Refine 生成参数和投影上下文；后续 Detect、Clip、Fitting、Network 使用当前启动配置。

当前 Run lineage 为 `refine_runs → detect_runs → fit_runs`。Detect 的完整
`resolved_subtrack_points` 序列与所有 Detect 子产物在一个最终事务中落盘；失败
会回滚子表并单独标记 Run 为 `failed`。只有 completed Detect 可供 Clip/Replay
消费。同一 Detect/Fit Run 不支持重复落库，重新计算应创建新 Run。

不可变制品的对账默认仅生成 dry-run 计划。只有核对计划、数据库目标和
`PARQUET_ROOT` 后才可使用 `--apply`；计划包含 critical 项时命令会拒绝执行删除：

```sh
python -m pipeline artifacts reconcile
python -m pipeline artifacts reconcile --apply
```

完整的 7 天宽限期、失败 Run 整阶段重跑和处置规则见
[Pipeline 列式架构运维指南](docs/pipeline-columnar-operations.md)。禁止手工修改已发布
Parquet。曾手工安装旧 patch ledger 的环境仅参考
[PostgreSQL 迁移说明](db/postgres/migrations/README.md)清理。

新 Refine Run 使用 `pipeline.toml` 中的 `[projection]` 配置，仓库配置为 EPSG:32649；区域变化时应替换
完整 TOML，不能用 CLI 覆盖。使用 `--refine-id UUID` 时会继承原 Run 的投影，不能覆盖。
选择与迁移规则见 [Run 级投影上下文](docs/run-projection-context.md)。

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
## 交叉口实验评价入口

实验入口管理仓库外单份 TOML、命名案例、批量参数方案和实验历史。`ARGS` 是传给实验命令的参数：`init` 初始化、`run` 运行、`list` 查看历史。下面的命令可在任意目录执行，查看历史且不启动算法：

```sh
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='list'
```

当前机器已初始化，本地配置为 /data/tmp/road-network/experiments/experiment.toml 。填写案例时间和区域后，将 `ARGS='list'` 改为 `ARGS='run'` 即可运行，无需重新初始化。首次安装、配置示例和其他操作见 [实验工作台](docs/experiment-workspace.md) 。

`pipeline-full-test` 默认限定北京时间 2023 年 3 月 1 日全天，仍使用全量配置；例如 `make pipeline-full-test SOURCE_TABLE=trajectory_points`。这项默认时间限制不应用于冒烟测试。

复用 Refine 时可选择其时间范围内的任意子区间，无需与历史范围完全一致。
`time_start` 包含，`time_end` 不包含；两者均为空时使用 Refine 的全部内容。
单侧为空时沿用 Refine 的对应边界。显式时间必须带时区，且不能超出 Refine
保存的来源时间范围。没有保存时间范围的历史 Refine，按其 kept/injected 点的
实际时间边界检查；空结果不能指定时间切片。

```sh
# 从七天 Refine 中选取北京时间 3 月 1 日
make pipeline-full-test REFINE_ID=你的UUID TIME_START=2023-03-01T00:00:00+08:00 TIME_END=2023-03-02T00:00:00+08:00
# 使用全部 Refine 内容；兼容 FULL_TEST_TIME_START / FULL_TEST_TIME_END
make pipeline-full-test REFINE_ID=你的UUID TIME_START= TIME_END=
# 冒烟入口也支持相同时间参数
make pipeline-smoke-test REFINE_ID=你的UUID TIME_START=2023-03-01T00:00:00+08:00 TIME_END=2023-03-02T00:00:00+08:00
```

Pipeline 命令行和栅格化命令均支持 `--time-start` / `--time-end`。验证案例的
`time_start` / `time_end` 可设为 `""` 或省略；显式 `--refine-id` 使用已完成 Refine
及其历史投影和 Parser/Refine 结果，Detect 使用当前方案参数。空时间的新建验证
案例处理区域内全部时间数据；普通 Pipeline 新建 Refine 时需同时提供两个时间
边界，或同时省略。

时间筛选始于 schema v10，当前 Pipeline 与 Backend 要求 schema v11。已有 v9/v10 数据库使用 `python -m pipeline init-db --migrate-artifacts`
进行迁移，保留已有 Refine 与 Parquet 制品。迁移须连接到
实际运行使用的数据库。本次测试只在隔离 schema 中验证迁移，不自动修改业务数据库。
