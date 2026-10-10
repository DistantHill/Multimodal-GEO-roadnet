# Repository Guidelines

## Project Structure

- `apps/frontend/` contains the standalone Node UI and production Nginx image.
- `apps/backend/` contains the FastAPI service, with `src/backend/` and `tests/`.
- `services/pipeline/` is the independent Pipeline runtime. Keep Cleaner, Detect, Clip, Fitting, Network, persistence, and replay stages here; do not run long jobs in Backend request handlers.
- `services/vision/` contains the optional YOLOv8 GPU batch runtime and tracking export tools. Keep model weights under the ignored `models/` path or an external `MODEL_ROOT`.
- `db/postgres/` and `db/qdrant/` contain only initialization, schema, migration, and collection definitions. PostgreSQL and Qdrant run from `/data/services`; runtime database data belongs under `/data/service-data`.
- `infra/docker/` contains Compose files. Runtime trajectory, Parquet, exports, and model files stay outside the repository and mount through `DATA_ROOT`.

## Build, Test, and Development Commands

```sh
cp .env.example .env
make dev-up          # Build and start the development stack
make dev-logs        # Follow service logs
make test            # Run Backend and Pipeline pytest suites in containers
make pipeline-check  # Validate Pipeline runtime configuration and data mount
make dev-config      # Validate the development Compose file
make dev-down        # Stop the development stack
```

Check frontend production assets with `docker run --rm roadnet-dev-frontend npm run build`. Use Compose service names (`postgres`, `qdrant`, `backend`), not container IPs.

## Coding Style and Naming

Use Python 3.11+, four-space indentation, type hints where practical, and `snake_case` for modules, functions, and variables. Keep packages under each service’s `src/`. Use ESM JavaScript, two-space indentation, `camelCase` variables/functions, and `PascalCase` UI components. Move code to `libs/` only when stable and shared by at least two runtime units.

## SQL 空间索引要求

所有 SQL 查询在设计和优化时，必须优先考虑使用空间索引，不论用于过滤、查询
还是计算。涉及空间数据时，应检查现有空间索引及其坐标参考系（CRS），使空间
谓词或计算表达式与索引定义匹配，并通过 `EXPLAIN` 或 `EXPLAIN ANALYZE` 确认
实际执行计划。能通过空间索引缩小数据范围的，应先筛选候选数据，再进行后续
查询、排序、窗口计算或空间计算；同时保留业务所需的上下文和结果语义。
不适用或无法使用空间索引的查询，应说明具体原因。

项目 Codex hook 在每次工具执行后检查新增或修改的 SQL，并在结束任务前复查。
修改 SQL 后，必须在 `docs/sql-index-reviews.json` 中按该 SQL 的内容哈希记录
空间索引检查结果：实际使用索引时提供索引名、CRS 与对应的 `EXPLAIN (FORMAT JSON)`
计划文件；不适用或有充分理由不采用时提供具体原因。SQL 再次变化必须重新检查，
不得用不对应当前 SQL 的计划或不真实的豁免原因通过检查。
触发范围、证据格式及限制见 `scripts/hooks/README.md` 。


## Testing Guidelines

Backend and Pipeline tests use `pytest`. Name files `test_*.py` and test behavior, not implementation details. Run `make test` before submitting. Add regression coverage for path, configuration, API, or migration changes; no threshold is configured.

本项目的 Pipeline 冒烟测试使用 `services/pipeline/config/pipeline-roadnet-test.toml`；Pipeline 全量测试使用 `services/pipeline/config/pipeline.toml`。提到“冒烟测试”或“全量测试”时按此配置区分，并在测试记录中注明实际使用的配置。

全量测试启动后，不在 agent session 内循环等待、轮询进程或反复读取日志。
使用独立后台脚本 `scripts/watch_pipeline_log.py` 每 10 分钟记录一次本次日志的
末尾内容；监控记录保存到仓库外。启动方式见 README 的“全量测试日志监控”。
按用户请求或任务完成后一次性读取记录，不用会话内轮询脚本来替代日志轮询。

## Commits and Pull Requests

This checkout has no valid Git history, so no convention can be inferred. Use concise imperative messages, preferably Conventional Commit style (for example, `feat: add pipeline run endpoint`). PRs should explain architectural impact, list validation commands, identify configuration or data-directory changes, and include UI screenshots when relevant. Never commit `.env`, credentials, runtime data, model weights, database files, or Qdrant storage.

## Security and Configuration

Copy `.env.example` to `.env` and replace placeholder secrets locally. Keep `POSTGRES_PASSWORD` out of Git. Treat `/data` as runtime-only and verify bind-mount paths before starting production Compose.

## Windows SSH 与产物推送（项目记忆）

已于 2026-10-07 验证 Ubuntu 主动通过 SSH 密钥登录 Windows，并通过 SCP 推送产物。
用户要求向 Windows 传输产物时，复用此配置。

- Windows：Tailscale 主机名 `tailscale-win`，IP `100.118.40.110`，SSH 端口 `22`，登录用户 `Reid`；`whoami` 返回 `laptop-c7d1dfpu\reid`。
- Ubuntu：Tailscale 主机名 `tailscale-ubuntu-home`，IP `100.120.6.3`。
- Ubuntu 私钥： `/home/reid/.ssh/id_ed25519` ，只在 Ubuntu 使用，不复制私钥或将其内容写入仓库。
- Windows 主机指纹文件： `/home/reid/.ssh/windows_tailscale_known_hosts` ，已通过既有认证连接取得；连接时显式指定并启用严格校验。
- Windows 默认产物接收目录： `D:\User\reid\Downloads` ，SCP 远端路径使用 `D:/User/reid/Downloads/` 。
- Windows 使用独立 MSI 安装的 Microsoft Win32-OpenSSH；`sshd` 已运行并设为自动启动，TCP 22 入站规则已启用。
- Ubuntu 公钥已配置在 Windows 的 `C:\ProgramData\ssh\administrators_authorized_keys` ，该文件仅 SYSTEM 和 Administrators 拥有完全控制权限，继承已禁用。

从 Ubuntu 检查免交互登录：

```sh
ssh -i ~/.ssh/id_ed25519 -o UserKnownHostsFile=/home/reid/.ssh/windows_tailscale_known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=10 Reid@100.118.40.110 whoami
```

从仓库根目录推送目录示例：

```sh
scp -r -i ~/.ssh/id_ed25519 -o UserKnownHostsFile=/home/reid/.ssh/windows_tailscale_known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=10 services/pipeline/generated/myLegacy Reid@100.118.40.110:D:/User/reid/Downloads/ # 推送目录
```

该示例已传输 20 个文件、76,240,620 字节至 `D:\User\reid\Downloads\myLegacy` ，逐文件大小及 SHA-256 均一致。此为当次验证记录，后续以实际源文件为准。
传输完成后核对文件大小及 SHA-256。若执行环境报 `socket: Operation not permitted`，应按工具权限流程申请沙箱外网络执行，不据此判断 Windows SSH 服务故障。

## 术语与写作

编写或修改本项目文档及技术说明时，读取并遵循 [项目术语与写作规范](docs/terminology.md)。

## Grilling → Spec Hook

用户调用 grilling 或继续其 QA 时，先检索 `docs/`：有相关 Spec 就复用，没有才新建。
创建或更新前读取 [Spec 模板](docs/templates/grilling-spec.md)，按模板逐轮更新正文并记录全部 QA。
仅讨论 hook 或引用调用示例不触发访谈。文档维护已获授权，实施仍遵循用户授权与 grilling 流程。
