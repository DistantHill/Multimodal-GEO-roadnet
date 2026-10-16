# 项目 SQL 空间索引检查 hook

配置位于 `.codex/hooks.json` ，实现位于 `scripts/hooks/sql_spatial_index.py` 。
保留已有 grilling hook；新增的 handler 需要在 Codex 中审核并信任才会执行。

## 触发与限制

- `SessionStart` 保存会话开始时的 SQL 内容基线。
- 不在 `PreToolUse` 注册 SQL 审查，确保发现待审 SQL 后仍能读取源码、检查执行计划和修复审查记录。脚本兼容旧的 `PreToolUse` 调用，但项目配置不再注册它。
- 每次 `PostToolUse` 比较项目文件，检测新增、修改的 SQL；包括 shell 写入和
  `apply_patch` 写入，不依赖工具名称猜测是否写入。
- 缺少检查记录、缺少原因或计划证据不符时返回 `decision: block`，要求 agent 修复。
- `Stop` 复查尚未通过的 SQL，要求完成检查后再结束任务。
- SQL 内容哈希变化后必须重新检查；删除待检查的 SQL 会清除对应待办。
- 现有未提交修改作为初始基线，不要求追溯检查历史 SQL。

写入后 hook 无法撤销已经落盘的修改；它拦截工具结果并要求补齐检查。
这不是操作系统文件写入拦截器：编辑器、外部进程和未启用 hook 的会话不受其控制。

检测覆盖 Git 管理或未忽略的新文件中的 SQL 文件、Python SQL 字符串、f-string、
以 SQL 字符串开头的拼接，以及 JS/TS 字符串和 shell heredoc。
运行时完全动态生成的 SQL、查询构造器、被 Git 忽略的文件不保证自动识别；仍须遵守
项目 SQL 空间索引要求。对动态 SQL，应将实际生成的 SQL 纳入检查记录或显式验证。

状态按项目和 session_id 隔离，存放于系统临时目录；文件锁保护并发工具事件。
脚本只读项目代码和计划文件，不连接数据库、不自动执行 SQL。

## 提交检查记录

缺少检查时，hook 给出 `review key`，由相对文件名与 SQL 内容 SHA-256 组成。
也可在项目根目录执行以下命令列出当前 SQL 标识：

```sh
python3 scripts/hooks/sql_spatial_index.py --list
```

将检查结果写入 `docs/sql-index-reviews.json` ，以 hook 给出的完整 key 为键。
空间查询必须记录实际使用的索引、坐标系和计划文件：

```json
{
  "relative/file.py#<hook给出的sha256>": {
    "status": "uses-index",
    "indexes": ["idx_trajectory_points_geom"],
    "crs": "EPSG:4326",
    "plan": "docs/validation/sql-plans/example.json"
  }
}
```

对实际参数化查询执行 `EXPLAIN (FORMAT JSON)`，把返回的原生 JSON 计划保存到对应
证据文件。项目内历史证据继续兼容；外部证据必须位于项目 `DATA_ROOT` 下的
` codex-artifacts/ ` 目录；未配置时使用 ` /data/tmp/<项目名>/codex-artifacts/ ` 。
目录来源解析与用户级运行产物规划器相同，解析符号链接后仍须位于允许目录内。
外部计划在审查记录中使用绝对路径，并提供 `plan_sha256`，内容变化后必须重新核验。
计划中必须有声明索引对应的 `Index Scan`、`Index Only Scan` 或
`Bitmap Index Scan` 节点，并具有空间 `Index Cond` 或空间排序条件。
`Filter` 中出现空间函数、仅引用索引名称、顺序扫描计划均不能作为使用空间索引的证据。
数据库支持只读的 `EXPLAIN` 时优先使用；`EXPLAIN ANALYZE` 会执行语句，应仅在
合适的数据和事务环境中使用。

不适用空间索引或有充分理由不采用时，记录具体原因，例如运行元数据主键查询：

```json
{
  "relative/file.py#<hook给出的sha256>": {
    "status": "not-applicable",
    "reason": "按 run_id 查询运行元数据，不涉及空间数据。"
  }
}
```

hook 核验记录与 SQL 内容哈希、计划节点和索引条件；它不能替代数据库执行计划分析，
不能自动证明计划来源与参数、索引类型及 CRS 声明真实，也不能判断豁免原因是否合理。
记录者必须使用对应 SQL 的真实计划，确认现有空间索引定义，并保留查询结果语义。

## 验证

```sh
python3 -m unittest discover -s scripts/hooks/tests -v
```

hook 测试独立运行，不接入 Pipeline、Compose 或 Backend 测试目标。
仅修改 Codex hook 配置或脚本时，运行上述 hook 测试及配置检查即可。

## 多项目 HTML 预览工作台

开发源码位于 `scripts/hooks/html_preview.py` ，首页位于 `scripts/hooks/preview_home.html` 。
运行副本安装在 `~/.codex/hooks/html-preview/` ，用户级配置位于 `~/.codex/hooks.json` 。
用户级命令仅引用用户目录中的脚本，仓库搬迁不影响其他项目；各项目无需自行添加配置。
项目级只保留 HTML 结果生成规则，预览服务登记统一使用用户级 handler；SQL 与 grilling 仍保持项目级。
更新源码后需将脚本和首页同步到用户目录；运行目录与已有产物登记保持不变。
新增用户级 handler 需在 Codex 的 `/hooks` 中审核并信任；这是
[官方 hook 文档](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks)要求。

hook 根据事件工作目录定位 Git 项目根目录；非 Git 目录则以事件工作目录为根目录。
`SessionStart` 建立初始基线，`PostToolUse` 与 `Stop` 检测新增或内容变化的
`.html` / `.htm` 文件。包含被 Git 忽略的产物，排除隐藏目录、虚拟环境、
Node 依赖、首页模板和项目外的符号链接。外部进程写入后，下一次工具事件才会检测。
已有 HTML 只要带有 `codex-preview=artifact` 的 meta 标记，即使内容未变化，也会在首次会话扫描时自动登记。
没有标记的旧文件可用以下命令主动登记并补齐标记；另一个项目只需指定自己的根目录：

```sh
python3 ~/.codex/hooks/html-preview/html_preview.py --root /absolute/project --publish reports/example.html
```

所有项目共用监听 `127.0.0.1:18765` 的后台 HTTP 服务，启动锁保证并发事件只启动
一个服务。项目根目录的哈希作为项目标识，同名项目、同名 HTML 不会覆盖。
浏览器链接形如 `http://localhost:18765/p/<项目标识>/reports/example.html` 。
HTML 内的相对资源链接保持项目内路由；从网站根目录开始的资源链接需改为相对路径。
旧版无项目标识的 HTML 链接仅在恰好一个项目拥有该文件时重定向。

已迁出仓库的项目可在持久登记记录中设置 `storage_root`，指向外部 HTML 保存区。
项目的 `root` 和项目标识仍用于生成原有链接；扫描、文件读取和静态资源访问改用
`storage_root`。`--publish` 的相对路径相对于该保存区，绝对路径也必须位于其中。
本项目现有产物已迁至 ` /data/tmp/codex/html-preview/Multimodal-GEO-roadnet-f76ba09062e0da40/ ` ，
保留原有文件相对目录，因此旧报告及关联 JSON 的链接不变。

Windows 仅需建立一次 SSH 本地端口转发，所有项目和所有文件均使用该连接：

```powershell
ssh -N -L 18765:127.0.0.1:18765 reid@100.120.6.3
```

转发建立后打开 <http://localhost:18765/> 。工作台支持项目筛选、标题与文件搜索、
按更新时间或标题排序、复制链接、新标签页打开、30 秒自动同步及窄屏布局。
页面显示实际文件的标题、更新时间和大小。删除文件后从列表移除。
hook 不连接 Windows 的 SSH 22 端口，也不验证 Windows 转发状态。
不会自动启动 Windows 浏览器或复制产物；更新 HTML 后刷新预览页即可。

服务仅提供已登记 HTML，以及这些 HTML 所在目录及子目录中的 CSS、JS、JSON、
图片、字体和 CSV 静态资源；不提供目录列表、隐藏文件或 Python 等源代码。
同目录 JSON/CSV 也可通过预览端口读取。HTML 引用登记目录之外的资源不自动放行。
登记和变更基线按项目隔离，浏览器请求不接受项目根目录参数。

产物清单持久保存在 ` ~/.local/state/codex-html-preview/ ` （设置了 `XDG_STATE_HOME` 时使用该根目录）。
其中 ` projects/<项目标识>/state.json ` 保存项目根目录、变更基线及已发布文件。
临时运行目录仍为 ` /tmp/roadnet-html-preview-<用户ID>/ ` ，仅保存进程心跳、启动锁、
会话首次检查标记及旧版日志；systemd 管理后的服务日志位于用户 journal。
清理临时目录或服务重启不会删除持久清单。
HTTP 服务独立于会话存活，SSH 转发在 Windows 客户端连接断开后停止。
启动失败保留待发布记录，下一次事件重试；本机端口被其他服务占用时报告故障，
不停止其他服务。会话恢复或压缩不清除待发布记录。
停止预览时，用运行目录中的 PID 停止 HTTP 进程，再关闭 Windows 转发连接。

本次升级已迁移旧版项目登记，并替换此前启动的单项目服务。
初始化持久清单时，会导入旧临时目录中的所有项目记录，并保留旧文件；不会用旧记录覆盖新的持久记录。
已丢失临时记录的旧报告，可以通过补齐 HTML 标记或显式发布来恢复。

## 首次对话：项目 HTML 结果生成与用户级登记

用户级 `UserPromptSubmit` handler 运行安装副本中的 `html_project_bootstrap.py` 。
它按项目根目录与 `session_id` 记录首次检查状态；恢复同一会话不重复检查，
新会话会重新检查。检查项目 ` .codex/hooks.json ` 与 ` .codex/config.toml ` 中的
`UserPromptSubmit` 生成规则。可识别标准 policy 脚本或显式标注 HTML 生成的 handler；
无法从任意脚本名字推断语义，未标注的自定义生成 hook 应提供清晰的生成说明。

缺少规则时，保留已有 handler，在项目 JSON 配置中追加 `html_result_policy.py` 的
命令 handler。配置写入使用临时文件替换与项目锁，避免并发会话覆盖；不修改信任记录。
已有生成 hook 时不重复追加。首次检查不生成业务 HTML，也不执行已有 hook 命令。
配置损坏或不可写时返回诊断，保留原配置，并在下一次提示词重试检查。

用户级 bootstrap 在每次提示词中继续注入生成规则，使新项目 handler 尚未加载时
当前会话仍收到要求。项目级 policy 指示 agent 为每次实质性结果生成有用的可交互
HTML；具体交互应服务结果，不编造数据。用户明确指定其他格式时遵从本次要求。
这是一项 agent 输出指令，不是自动写 HTML 的文件转换器，也不静态证明页面有交互。

HTML 及其配套 JSON、图片、CSS/JS 保存于仓库外的 ` /data/tmp/codex/html-preview/ ` ，
使用项目持久登记的 `storage_root` 保存区，禁止把生成产物写入 pwd 或 Git 工作区。
项目尚未配置外部保存区时，先完成登记，不得回退到仓库。
用户级预览 handler 的 `PostToolUse` / `Stop` 将产物登记至共享服务。
如需立即登记，`--root` 指定原项目根目录，`--publish` 指定外部 HTML 的绝对路径。
最终回复应给出
登记后的含项目标识的浏览器链接；所有项目仍共用一次 Windows 本地端口转发。

新增用户级 bootstrap 和自动补齐的项目级 policy 需要 Codex 审核并信任其定义；
没有通过审核的 handler 不保证自动执行。项目级生成规则无需在其他项目手动复制，
首次检查会按授权补齐；已有项目的 SQL、grilling 等规则保持原配置。

## 预览服务自动启动与沙箱检查（2026-10-15）

统一预览服务由用户级 systemd 单元 `html-preview.service` 管理。
单元源码位于 ` scripts/hooks/html-preview.service ` ，安装于
` ~/.config/systemd/user/html-preview.service ` 。登录启动用户服务管理器后自动启动；
异常退出后自动重启。当前用户未启用 linger，因此这里不宣称无人登录时开机启动。

在 Ubuntu 普通终端中手动启动或检查：

```sh
systemctl --user start html-preview.service
systemctl --user status html-preview.service --no-pager
journalctl --user -u html-preview.service -n 30 --no-pager
```

hook 在 `SessionStart`、无 HTML 变化的工具事件及有产物变化时都会检查服务。
已安装 systemd 单元时通过用户服务管理器启动，不再抢占该单元的端口；
没有安装单元的环境保留独立后台进程启动方式。

曾确认的故障是 Codex 沙箱禁止创建网络 socket，进程日志报告
`PermissionError: [Errno 1] Operation not permitted`，并非本次检测到了端口占用。
旧实现把所有启动失败都显示为端口占用提示，并且没有新 HTML 时不会尝试启动。

服务成功绑定端口后，在共享运行目录写入 `server.ready.json`，每 2 秒更新心跳。
当健康检查被明确的 EPERM/EACCES 拒绝时，hook 验证服务标识、PID、进程启动时刻
及实际命令行；若沙箱还隔离了 PID 命名空间，则要求系统启动标识一致，且心跳
在最近 10 秒内更新。过期心跳、旧 PID、前次系统启动记录均不接受。
普通连接拒绝、超时等错误不能使用此替代检查判定健康。
该检查表示就绪服务进程存在或近期有心跳，不等同于本次 HTTP 请求成功。
状态文件位于系统临时目录，不写入仓库。

Windows 的 SSH 本地转发仍需要客户端保持连接；Ubuntu 服务自启动不代替 Windows
客户端转发。入口仍为 <http://localhost:18765/> 。


## HTML 产物标记与清单恢复（2026-10-15）

每份需要保留在“全部产物”中的报告，应在 HTML 的 head 中包含：

```html
<meta name="codex-preview" content="artifact">
```

生成规则已要求 agent 写入该标记。显式 `--publish` 或自动登记新增、修改的 HTML
时，预览脚本也会补齐标记，保持重复登记幂等。标记使用 HTML 解析器识别，属性
顺序与单双引号均可；注释及 script 字符串中的示例不算标记。

每次会话开始或工具事件，扫描到带标记的旧文件也会合并进持久清单，不依赖文件
相对初始基线发生变化。因此即使持久清单需要重建，首次项目扫描仍能恢复标记文件。
没有标记、未发生变化、也未显式发布的历史源码或 HTML 模板不会在初始基线阶段
自动登记。文件删除后移出显示列表，重新出现的标记文件会重新收录。

“保存”指保存登记信息和外部产物文件中的标记，不生成历史版本快照。
实际 HTML 文件需要继续存在于登记的仓库外保存区；项目根目录保留身份信息。
项目标识和现有浏览器链接格式保持一致。

本次已给本项目现有的验证报告补齐标记，并迁移尚存的旧登记记录；旧记录已经丢失
且文件没有标记的其他项目，需要在该项目中补标记或执行一次显式发布。

## 每轮非 HTML 运行产物目录规划（2026-10-15）

用户级 `UserPromptSubmit` handler 使用 ` artifact_routing.py ` ，运行副本独立安装于
` ~/.codex/hooks/artifact-routing/artifact_routing.py ` 。每轮提示词都重新规划，
不局限于首次对话，也不依赖 HTML 预览服务。它不移动既有文件，不调整 HTML 规则。

目录优先级如下：

1. 进程环境变量 `DATA_ROOT`；兼容小写 `data_root`。
2. 项目根目录 ` .env ` 中的同名配置；仅读取对应键，不执行 dotenv 中的命令。
3. 未配置时使用 ` /data/tmp/<项目名>/ ` 。配置指向项目仓库内部、含未解析表达式时，
   也使用该仓库外备用目录，并在规划结果中说明原因。

运行文件位于选定根目录的 ` codex-artifacts/<项目标识>/<会话标识>/ ` 。
标识从项目绝对路径和 session_id 分别计算，避免同名项目或不同会话互相覆盖。
同一项目、同一会话的目录保持稳定。无论根目录来自 DATA_ROOT 还是备用目录，
都按 `evidence`、`logs`、`metrics`、`screenshots`、`exports`、`scratch` 分类。
目录由实际执行任务的 agent 在需要时创建；hook 自身不写产物或创建输出目录。

规划器通过当前提示词识别测试、验证、轮询、日志、性能、截图、导出等任务词，
建议相应类别，并要求 agent 根据完整会话与实际工具操作补充判断。
默认保留 scratch 建议供调试和中间文件使用。此处“判断”是关键词建议加 agent
上下文判断，不在 hook 内单独调用模型，也不是文件系统写入拦截器。

hook 会提醒 agent 将运行证据 JSON、轮询记录、日志和开发临时文件保存在仓库外；
源码、测试代码、正式文档及版本管理配置仍在项目中。SQL 审查等已有专门流程继续
生效，不以目录规划为由绕过证据核验。当前已有 HTML 保存与预览流程保持不变。

用户级配置新增 handler 后需在 Codex 的 ` /hooks ` 中审核并信任该定义；
已有 handler 的顺序和命令保持不变。项目记忆同步维护于 ` AGENTS.md ` 。

## 本项目 HTML 保存区（2026-10-16）

本项目已登记的 ` storage_root ` 为
` /data/tmp/codex/html-preview/Multimodal-GEO-roadnet-f76ba09062e0da40/ ` 。
验证报告产物已迁出仓库，保留原相对目录与项目标识，既有预览链接继续有效。
迁移时逐文件核对内容；前端页面、图像比较工具和预览工作台模板属于源码，继续纳入版本管理。
