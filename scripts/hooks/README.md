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

对实际参数化查询执行 `EXPLAIN (FORMAT JSON)`，把返回的原生 JSON 计划保存到上述
项目内文件。计划中必须有声明索引对应的 `Index Scan`、`Index Only Scan` 或
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
