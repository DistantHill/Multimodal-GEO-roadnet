# Migrations

当前数据库升级由 `services/pipeline/src/track/v2/cli/init_db.py` 统一执行，不使用 Alembic。非 `--reset` 模式在一个事务中按以下顺序运行：

1. 如果存在早期 `pipeline_runs` 注册表，通过 `track.v2.db.legacy_migration` 原子拆分为 `clean_runs` 与 `detect_runs`。
2. 创建当前缺失的业务表。
3. 对比权威 DDL 与 `information_schema`，自动增加缺失列。
4. 执行不能仅由列差异表达的结构迁移，例如约束、可空性和冗余索引清理。
5. 创建或同步当前索引。
6. 任一步失败时回滚整笔升级。

这里的 *legacy* 只指确实存在旧 `pipeline_runs` 表的数据库，不是当前 Pipeline 运行模式。当前 lineage 是 `clean_runs → detect_runs → fit_runs`。如果旧 Detect 与父 Clean 的关系无法从历史参数确定，迁移会在任何写操作前失败，必须使用 `--legacy-clean-map` 提供完整、精确的 Detect UUID → Clean UUID 映射；迁移成功并校验产物后会删除 `pipeline_runs`。

自动补列会为已有 `turn_events` 补齐 `tp_source_id` 和 `tp_source_row_key`。Generated/SERIAL 列不会走通用自动补列；没有默认值的新 `NOT NULL` 列会先按 nullable 加入，复杂约束必须写成显式结构迁移。普通升级不要使用会清空业务数据的 `--reset`。
