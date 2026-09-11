# PostgreSQL / PostGIS

这里只保存数据库定义、初始化和迁移逻辑。PostgreSQL runtime data 由 Compose 的 `postgres_data` named volume 管理，禁止在此目录或仓库内保存数据目录。

`init/001-enable-postgis.sql` 只在首次创建 named volume 时启用 PostGIS。业务
Schema 的唯一权威入口是 `services/pipeline/src/track/v2/cli/init_db.py`。当前只支持
破坏性 schema v3：旧库必须在核对连接目标后使用 `init-db --reset` 全量重建，
不存在 v1/v2 原地迁移或滚动混跑。

`clean_runs` 唯一保存外部 `source_table`，kept/excluded 使用字符串化的
`source_row_key`。仓内实体关系使用 `BIGINT` 主键。Detect 在最终事务中写入
`resolved_subtrack_points`，每行恰好引用 kept、injected 或完整交叉口的 candidate
crossing 之一。Clip 只消费 completed Detect 的最终顺序；Centerline manifest
仅保存起止 crossing 血缘。

术语严格区分：`tracks_cross` 的 track cross 是两条轨迹线段之间的交点；
`candidate_entry_exit_points` 及所有 `crossing_*` 字段表示轨迹穿越交叉口边界的
entry/exit crossing。两者不是同一种实体。

当前不使用 Alembic，也不在此目录复制第二份可能漂移的业务 Schema。
