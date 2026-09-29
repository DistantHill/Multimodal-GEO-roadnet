# Schema v9 migrations

`track.v2` 数据库结构（schema）当前为 v9。v4 至 v8 使用
`python -m pipeline init-db --migrate-artifacts` 逐级升级至 v9。
v3 的保留数据迁移 CLI 已删除；早期迁移经过见
[迁移说明](../../../docs/refine-migration-and-performance.md)。
执行迁移前停止相关读写服务，完成后一起部署 Pipeline、Backend 和客户端。

无迁移选项时初始化器只接受空库或已登记为 v9 的库。v3 及更早版本或未版本化
Pipeline 表需要重建；`--reset` 会删除已知 Pipeline 表及其数据。业务表的权威数据定义语言
（data definition language，DDL）位于
`services/pipeline/src/track/v2/cli/init_db.py`。

## 已撤回的 patch ledger 扩展

该可选账本从未由数据库初始化器自动安装，相关 Python 实现与 DDL 模板已撤回。
新环境不需要执行任何账本安装或卸载操作。

若某环境曾手工安装该扩展，必须先停止所有相关调用并确认无账本作业运行，
再将下列 `target_schema` 替换为经核对的目标数据库结构名称，以显式限定名称严格按顺序手工执行：

```sql
DROP TABLE IF EXISTS target_schema.pipeline_patch_attempts;
DROP TABLE IF EXISTS target_schema.pipeline_patch_tasks;
DROP TABLE IF EXISTS target_schema.pipeline_patch_executions;
DROP TABLE IF EXISTS target_schema.pipeline_patch_runs;
```

不得使用 `CASCADE`，不得删除 `refine_runs`、`detect_runs` 或其他业务表。本仓库仅提供上述
手工操作说明，初始化、迁移和测试入口均不执行这些 `DROP TABLE` 语句。
