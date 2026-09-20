# Schema v7 migrations

`track.v2` 数据库结构（schema）v4 将 Clean 统一改名为 Refine。v3 可执行
`python -m pipeline init-db --database roadnet --migrate-refine`，在一个事务内
保留数据、改名并依次升级至 v7。v4/v5/v6 使用 `--migrate-artifacts` 升至 v7。
执行前停止相关读写服务，完成后一起部署 Pipeline、Backend 和客户端。具体步骤见
[迁移说明](../../../docs/refine-migration-and-performance.md)。

无迁移选项时初始化器只接受空库或已登记为 v7 的库。v1、v2 或未版本化
Pipeline 表仍需另行处理；`--reset` 会破坏性重建。v4 业务表的权威数据定义语言
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
