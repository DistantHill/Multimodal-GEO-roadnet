# Refine 复用选择实施与验证报告

日期：2026-09-26。

实施范围：按已确认的 `docs/refine-reuse-selection-spec.md` 第 2–9 节实现 CLI 与 Make Refine 选择。未修改 Spec 或其中 QA。直接调用 Pipeline 运行函数的语义保持不变。

## 改动文件

- ` services/pipeline/src/track/v2/cli/run_pipeline.py `：新增 `--refine-resume-last true|false` 三态解析，选择候选、交互提示、显式 UUID 历史摘要、冲突报错与失败 UUID 输出。
- ` services/pipeline/src/track/v2/cli/refine_selection.py `：新增按精确 `source_table` 文本查询最新 completed Refine 的 CLI 元数据读取，以及历史参数和空间上下文摘要。
- ` Makefile `：smoke/full 目标支持 `SOURCE_TABLE` 或 `REFINE_ID` 与可选 `REFINE_RESUME_LAST`，显式空值/非法 Bool/组合冲突先于 `services-up` 校验。
- ` README.md `：记录 Make/CLI 选择方式、分别使用的配置及候选发现不增加当前来源数据扫描。
- ` infra/docker/tests/test_make_pipeline_commands.py `：补充 Make 参数组合、未传 Bool 保留、smoke/full 配置和非法参数不调用 Docker 的回归测试。该测试文件在实施前已是未跟踪用户文件，本次在其原内容上增补。
- ` services/pipeline/tests/v2/test_refine_selection_cli.py `：新增 CLI 参数、选择路由、交互、无终端/EOF、历史摘要、查询失败和失败 UUID 行为测试。
- ` services/pipeline/tests/v2/test_run_cli.py `、` services/pipeline/tests/v2/test_clip_cli.py `：为既有显式 UUID CLI 测试补上历史摘要读取打桩，保留原断言。

## 验收标准对应情况

- CLI 与两个 Make 入口支持来源表或显式 UUID 二选一。Make 对来源/UUID、Bool 空值、非法值、多词 Bool 及 UUID+Bool 冲突在依赖服务启动前失败；CLI 也拒绝 Bool 与 UUID 组合。
- `true` 自动复用最新候选；`false` 不查询候选并新建；省略 Bool 时只在终端交互。回车、y/yes 复用，n/no 新建，其他输入继续提示。无终端、EOF 或 Ctrl+C 时取消，不自行选择。
- 最新候选只从 `refine_runs` 元数据查询：来源文本精确匹配、状态为 completed、按 `started_at DESC, id DESC` 固定排序。候选查询不扫描 kept/injected/excluded 点表或来源表。
- 无候选时提示并继续新建；显式 UUID 不查询 latest、不提示。查询异常退出；所选记录元数据解析、投影校验或下游复用失败时错误包含 Refine UUID，不回退更旧结果或自动重算。
- 复用摘要展示 UUID、来源表、开始/完成时间、历史 Refine 数据类别、真实历史 Parser/Refine 生成参数和完整历史投影字段。缺失的历史参数显示“历史记录未保存”。摘要说明 Parser/Refine 跳过，Detect/Clip/Fitting/Network 使用当前启动配置，不把整份历史快照描述为继承配置。
- Make 冒烟配置名为 `pipeline-roadnet-test.toml`；Make 全量配置名为 `pipeline.toml`。本次只做 Make 命令生成与参数验证，没有启动真实 smoke/full Pipeline。

## 验证命令与结果

| 命令 | 退出码 | 结果 |
| --- | ---: | --- |
| `python3 -m unittest discover -s infra/docker/tests -v` | 0 | 14 项通过，包括 Docker stub 断言非法参数时 services-up 未被调用。 |
| `docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline python -m pytest tests/v2/test_refine_selection_cli.py tests/v2/test_run_cli.py tests/v2/test_clip_cli.py -q` | 0 | 36 项通过。 |
| `docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline python -m pytest tests scripts/cv_human_check/tests` | 0 | Pipeline 完整套件：1167 passed、72 skipped、8 个 rasterio PendingDeprecationWarning。容器 Python 3.12.14。 |
| `python3 -m py_compile services/pipeline/src/track/v2/cli/run_pipeline.py services/pipeline/src/track/v2/cli/refine_selection.py services/pipeline/tests/v2/test_refine_selection_cli.py services/pipeline/tests/v2/test_run_cli.py services/pipeline/tests/v2/test_clip_cli.py infra/docker/tests/test_make_pipeline_commands.py` | 0 | 修改的 Python 源码及测试文件语法编译通过。 |
| `make test`（提升权限执行） | 2 | Compose 单元测试 14 项通过，PostgreSQL/Qdrant 健康；Backend pytest 收集 85 项，83 passed、1 skipped、1 failed，Make 因失败未继续 Pipeline 阶段。失败项见下文。 |

`make test` 的 Backend 失败为 `tests/test_streaming_api_fields.py::test_centerline_fields_keep_pair_direction_and_metre_units`：旧断言预期 properties 仅含既有七个字段，当前结果额外含 `centerline_id` 与 `fit_id: None`。实施前的状态记录已显示 Backend `map_contracts.py`、`map_layers.py` 等用户改动；本次未修改任何 Backend 文件。Pipeline 完整测试因此单独运行，不能将 `make test` 报告为全绿。

首次在默认 sandbox 中执行 `make test` 因 Docker socket 权限被拒绝而退出 2。用户已授权跑测后通过提升权限执行；不需要绕过审批。宿主 Python 为 3.10，低于 Pipeline `pyproject.toml` 声明的 Python 3.12 下限，Pipeline pytest 使用 Compose 容器 Python 3.12.14 执行。

## 测试日志与基线

- ` /tmp/refine-reuse-selection-make-test-escalated.log `：提升权限运行的完整 `make test` 日志，含 Backend 失败断言。
- ` /tmp/refine-reuse-selection-make-unittest.log `：Make/Compose unittest 日志。
- ` /tmp/refine-reuse-selection-cli-tests.log `：36 项 CLI 相关测试日志。
- ` /tmp/refine-reuse-selection-baseline/git-status.txt ` 与 ` /tmp/refine-reuse-selection-baseline/git-diff.patch `：修改前工作区状态和已存在差异记录。
- ` /tmp/refine-reuse-selection-baseline/tree/ `：所有本次编辑过的原文件快照；其中包括实施前已修改的 Makefile/README，以及原先未跟踪的 Make 测试文件。

## 未执行项与限制

未运行真实冒烟或全量 Pipeline，避免触发持久化数据处理。Make 的配置验证为 `pipeline-roadnet-test.toml` 和 `pipeline.toml`；实际测试套件使用的是 Pipeline pytest，不使用这两个业务运行配置。候选选择不比较当前来源表完整内容与历史运行输入；这是 Spec 明确接受的范围，复用后仍沿用现有产物和已保存来源指纹校验。


## 主 session 恢复与验收记录（2026-09-27）

中断位置：指定 gpt-6-luna（high）子代理已完成实现、测试和上述文本报告，最后因使用额度限制停止；主 session 尚未完成验收记录。本节由主 session 进行只读验收与文档收尾，不代替子代理修改实现或跑测。

验收核查：

- 对照实施前备份审查 CLI、Make 和 README 增量；直接调用运行函数、Pipeline 编排、存储接口和生命周期测试与备份一致。
- 候选 SQL 仅读取 `refine_runs`，按精确来源文本、completed、`started_at DESC, id DESC` 选择；无点表统计或新增来源扫描。
- 静态核对三态 Bool、交互接受/拒绝、无终端/EOF、冲突拒绝、选中 UUID 错误信息和继承展示。最新记录的解析错误包含 UUID，交互接受后明确输出选中的 UUID。
- 实读 CLI 测试日志：36 passed；Make unittest 日志：14 项 OK。
- Pipeline 完整套件 1167 passed、72 skipped 来源为子代理报告和中断前进度消息；未发现单独保存的完整 Pipeline 原始日志，不将该结果称为本 session 重新跑测。
- 实读 `make test` 日志：Backend 83 passed、1 skipped、1 failed，失败是 `test_centerline_fields_keep_pair_direction_and_metre_units`，实际 properties 多出 `centerline_id`、`fit_id`。该功能未编辑 Backend；不能宣称仓库全部测试通过。

用户补充的百万行真实冒烟日志： [/data/tmp/road-network/logs/20260926_234453_620938_pid-1_log.txt](/data/tmp/road-network/logs/20260926_234453_620938_pid-1_log.txt) 。

日志确认数据库为 `roadnet_test`、来源为 `public.day01`；本次新建 Refine，然后 Refine/Detect/Clip/Fitting/Network 五阶段均 completed，最后 Committed 和 Done。源点为 1,000,000 行，总耗时 1905.410 秒（约 31 分 45 秒），输出 292 个路网节点及 556 条边。

- Refine UUID：`eb80a2f6-7105-48b5-bb01-accbbc344356`。
- Detect UUID：`de0cfdd1-1586-4be9-9c2a-e3069a80e31d`。
- Fit UUID：`867af27e-ab31-4c20-9ee2-4536f9f7a238`。

用户称该运行是 smoke-test；项目 Make 冒烟入口固定使用 `pipeline-roadnet-test.toml`。该日志没有打印配置文件路径，实际配置归属依据用户所述入口与项目约定，不能仅凭日志独立核验当时的环境变量。该日志证明新建 Refine 的真实全流程成功，不证明本次执行了历史复用；复用选择分支证据来自代码和 CLI 回归。

结论：本次 Refine 选择功能范围内验收通过，无需用户再手动重复验收。仓库整体 `make test` 仍有上述 Backend 单测失败，单独保留为未通过项；未扩大本任务去修改 Backend。恢复后未重复执行耗时流水线，未提交 Git。
