# Migrations

当前数据库升级由 `services/pipeline/src/track/v2/cli/init_db.py` 统一执行，不使用 Alembic。非 `--reset` 模式在一个事务中按以下顺序运行：

1. 如果存在早期 `pipeline_runs` 注册表，通过 `track.v2.db.legacy_migration` 原子拆分为 `clean_runs` 与 `detect_runs`。
2. 创建当前缺失的业务表。
3. 对比权威 DDL 与 `information_schema`，自动增加缺失列。
4. 执行不能仅由列差异表达的结构迁移，例如约束、可空性和冗余索引清理。
5. 创建或同步当前索引。
6. 任一步失败时回滚整笔升级。

这里的 *legacy* 只指确实存在旧 `pipeline_runs` 表的数据库，不是当前 Pipeline 运行模式。当前 lineage 是 `clean_runs → detect_runs → fit_runs`。如果旧 Detect 与父 Clean 的关系无法从历史参数确定，迁移会在任何写操作前失败，必须使用 `--legacy-clean-map` 提供完整、精确的 Detect UUID → Clean UUID 映射；迁移成功并校验产物后会删除 `pipeline_runs`。

自动补列会为已有 `turn_events` 补齐 `tp_source_id` 和 `tp_source_row_key`，也会先以 nullable 形式增加 Centerline 与 Clip manifest 的 `BIGINT` 路口 ID 列。显式结构迁移会校验旧簇引用，按 `intersections.id` 回填端点，建立新约束，再删除 `turn_events.event_id` 以及所有持久化的 `cluster_id`/cluster endpoint 计算序号列与旧索引。Generated/SERIAL 列不会走通用自动补列；没有默认值的新 `NOT NULL` 列会先按 nullable 加入，复杂约束必须写成显式结构迁移。普通升级不要使用会清空业务数据的 `--reset`。

迁移还会把 `candidate_entry_exit_points` 中可验证的旧注入点来源别名 `injected_points` 规范化为 `public.injected_points`。其 row key 必须能精确匹配同一 Detect 所属 Clean Run、同一 vehicle 的 `injected_points.id`；迁移不会猜测或替换不明确的来源。

这次删列不支持新旧 writer 滚动混跑：迁移前新 writer 无法满足旧列的 `NOT NULL`，迁移后旧 writer 会引用已删除列。已有数据库必须先停止 Pipeline 写入，执行 `make pipeline-init-db`，再启动新版本。旧数据存在冲突、缺失端点或 combined Centerline 的新数据库 ID 次序不满足约束时，迁移会失败并回滚，不会通过选 `MIN` 或静默交换端点修复。

## Versioned SQL

`20260902_candidate_membership_on_events.sql` 将候选路口成员归属下沉到
`turn_events` 和 `tracks_cross`。脚本先验证并回填旧
`candidate_intersection_events` 数据，建立同 Detect Run 的复合外键与索引，
最后删除旧成员表。发现 A/C 事件跨候选重复归属或旧 A/C 成员无法映射时会中止
并回滚。仅存在于内存中的 Method 2 `orthogonal` 没有 Detect 级 A/C 记录，因而
不会写入反向成员关系。
