# Multimodal GEO Roadnet

## 当前状态

当前仓库采用单 Repository、多 Docker Service 的结构。轨迹重建 Pipeline 已从宿主机源码仓库的 `codex/pipe-06a-integration@75fa562` 迁入 `services/pipeline/src/track/v2/`；迁移保留原包名、阶段顺序、lineage/run ID 和数据库业务语义。来源与迁移边界见 `services/pipeline/SOURCE.md`。

## 目录职责

- `apps/frontend`：独立前端开发容器；仅负责 UI 和 Backend API 调用。
- `apps/backend`：独立 FastAPI API 容器；当前提供健康检查和数据库/Qdrant 连通性检查骨架。
- `services/pipeline`：Pipeline 独立运行单元，包含 Cleaner、Detect、Clip、Fitting、Network、持久化、回放和数据库初始化逻辑。Backend request process 不执行这些耗时阶段。
- `db/postgres`、`db/qdrant`：数据库定义、初始化和迁移逻辑，不保存运行时数据。
- `infra/docker`：开发和生产 Compose 文件。

Detect 中的现有视觉判别能力仍作为 Pipeline 内部逻辑；当前没有独立 GPU Runtime 或扩缩容需求，因此不创建 `services/vision`。没有稳定的跨服务共享语义时也不创建 `libs/`。

## 开发环境

```sh
cp .env.example .env
docker compose -f infra/docker/compose.dev.yml up -d --build
```

服务入口：

- Frontend：`http://localhost:${FRONTEND_PORT:-5173}`
- Backend：`http://localhost:${BACKEND_PORT:-8000}/health`
- Backend 依赖检查：`/health/dependencies`
- PostgreSQL 主机映射默认是 `15432`，容器内服务名和端口仍是 `postgres:5432`。
- Qdrant 主机映射默认是 `16333`，容器内服务名和端口仍是 `qdrant:6333`。

宿主机端口映射与容器内连接配置分开：`POSTGRES_PORT`/`QDRANT_PORT` 是容器内端口，`POSTGRES_HOST_PORT`/`QDRANT_HOST_PORT` 仅用于开发机发布端口。

前端源码通过 bind mount 热更新，`frontend_node_modules` 使用 Docker named volume。Backend 和 Pipeline 也分别使用独立源码挂载和独立 Docker build context。

## 配置与运行时数据

真实 `.env` 不提交 Git。复制 `.env.example` 后按需修改；生产环境必须设置非示例的 `POSTGRES_PASSWORD` 和外部 `DATA_ROOT`。

Raw trajectory、Parquet、中间文件、导出文件和模型权重属于 Runtime Data。开发/生产通过 `DATA_ROOT` 挂载到容器 `/data`，例如宿主机 `/data/road-network` 对应容器 `/data`；代码仓库不创建 `data/raw` 或 `data/parquet`。

Pipeline 日志默认写入容器内 `/data/logs/pipeline.log`，即宿主机 `${DATA_ROOT}/logs/pipeline.log`。可用 `PIPELINE_LOG_PATH` 修改容器内位置，但不要指向源码目录。

PostgreSQL 使用 `postgres_data:/var/lib/postgresql/data`，Qdrant 使用 `qdrant_data:/qdrant/storage`。这两个 named volume 不绑定到仓库目录。

Pipeline 的数据库配置优先读取进程中的 `TRACK_PG_*`（兼容旧项目），其次读取标准 Compose 变量 `POSTGRES_HOST`、`POSTGRES_PORT`、`POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`。代码不再提供默认数据库密码。

## Pipeline 运行

首次创建或更新业务表结构时执行原有 Python 初始化/迁移入口：

```sh
make pipeline-init-db
```

查看完整参数或从外部数据目录运行 CSV：

```sh
make pipeline-help
docker compose -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline run --csv /data/parquet-or-csv/input.csv
```

也可使用 `--pg-table TABLE` 从 PostgreSQL 读取，或用 `--clean-id UUID` 复用已完成的 Clean Run。Pipeline 是独立批处理进程，不经由 Backend API request process 启动。

## 常用命令

```sh
make dev-config
make dev-up
make dev-logs
make test
make pipeline-check
make pipeline-init-db
make pipeline-help
make dev-down
```

`make test` 会运行 Backend 与 Pipeline 的 pytest 套件。源项目的历史测试基线并非全绿，迁移报告中会区分既有失败与迁移新增回归。
