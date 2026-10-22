# Repository Guidelines

## Project Structure

- The active product is the standalone parameter workbench. Keep Refine, Detect, Clip, Fit, Network, evaluation and experiment orchestration in flat modules under `services/pipeline/src/pipeline/`.
- `services/pipeline/native/` contains the C++17 operators and resource sampler. Python orchestrates these operators; no Python geometry-algorithm fallback is part of the new entry point.
- Root `pipeline.toml` is the sole active parameter/input authority. Intermediate artifacts stay in external Parquet/GeoJSON files; PostgreSQL publication is optional and only stores final intersections and centerlines.
- `apps/`, `services/vision/`, `infra/docker/`, `db/`, and `services/pipeline/src/track/v2/` remain legacy reference material. Frontend/backend changes are outside the current workbench scope. Historical deployments require their matching baseline checkout; see `docs/legacy-runtime.md`.
- Runtime trajectories, Parquet, exports, logs, build wheels and model files stay outside the repository. Never run long jobs in Backend request handlers.

## Build, Test, and Development Commands

```sh
CC=gcc CXX=g++ make install  # Install locked dependencies and compile the native wheel
make test                   # Run the core workbench tests without PG/UI
make pipeline-check         # Validate root pipeline.toml and native/label dependencies
make pipeline-run           # Run the five stages
make pipeline-preview       # Export a static label preview
make pipeline-tune          # Run Optuna and replay the best completed configuration
```

`PYTHON`, `BUILD_ROOT` and `PIPELINE_CONFIG` select the interpreter, external build destination and one TOML file. They do not override individual algorithm parameters. The current Pipeline Dockerfile builds the standalone core; it is not a drop-in image for historical Compose commands.

## Coding Style and Naming

Use Python 3.12+, C++17, four-space indentation, type hints where practical, and `snake_case` for modules, functions, and variables. Keep packages under each service’s `src/`. Move code to `libs/` only when stable and shared by at least two runtime units.

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

Core workbench tests use `pytest` under `services/pipeline/tests/workbench/`. Name files `test_*.py` and test behavior, not implementation details. Run `make test` before submitting. Add regression coverage for path, configuration, API, or migration changes; no threshold is configured. Legacy tests remain available but are not presented as workbench acceptance.

当前参数试验台只以根 `pipeline.toml` 或明确选定的一份同格式 TOML 运行。测试自己生成
合成数据与临时 TOML；报告中注明实际配置、数据规模和是否命中输入缓存，不把合成验收
称为真实数据全量验收。历史运行的冒烟配置 `services/pipeline/config/pipeline-roadnet-test.toml`
和全量配置 `services/pipeline/config/pipeline.toml` 只适用于原基线；引用旧记录时仍需区分。

全量测试启动后，不在 agent session 内循环等待、轮询进程或反复读取日志。
使用独立后台脚本 `scripts/watch_pipeline_log.py` 每 10 分钟记录一次本次日志的
末尾内容；监控记录保存到仓库外。启动方式见 README 的“全量测试日志监控”。
按用户请求或任务完成后一次性读取记录，不用会话内轮询脚本来替代日志轮询。

## 运行产物与临时文件（项目记忆）

- 测试运行证据 JSON、验证结果、轮询及监控记录、运行日志、截图、导出数据和开发
  中间文件默认保存在仓库外，不随意写入当前工作目录或 Git 工作区。
- 优先读取项目 `DATA_ROOT`：进程环境变量优先，其次项目根目录的 ` .env ` 。
  在该目录下使用 ` codex-artifacts/<项目标识>/<会话标识>/ ` ，按 `evidence`、`logs`、
  `metrics`、`screenshots`、`exports`、`scratch` 等用途分类，需要时才创建。
- 没有配置 `DATA_ROOT`，或其路径落在仓库内部时，使用
  ` /data/tmp/<项目名>/codex-artifacts/<项目标识>/<会话标识>/ ` ，不要退回仓库保存。
- 每轮开始先结合会话任务判断可能产生的运行产物，规划对应目录；结束时记录实际
  使用的路径。源码、测试代码、正式文档和应纳入版本管理的配置照常保留在仓库。
- SQL 审查登记及其受专门 hook 约束的计划文件仍遵循上方 SQL 审查流程；本条不免除
  审查，也不自动迁移已有证据。HTML 产物按预览 hook 规则保存在已登记的仓库外保存区；源码模板保留在仓库。

## Commits and Pull Requests

Use concise imperative messages, preferably Conventional Commit style (for example, `refactor: build native parameter workbench`). PRs should explain architectural impact, list validation commands and their scope, and identify configuration or data-directory changes. Never commit `.env`, credentials, runtime data, model weights, database files, or Qdrant storage.

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
