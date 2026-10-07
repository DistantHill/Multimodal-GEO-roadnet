# PostgreSQL / PostGIS

这里只保存数据库定义、初始化和迁移逻辑。PostgreSQL 由 `/data/services/compose.yml` 独立编排，运行数据绑定挂载到 `/data/service-data/postgres`；禁止在此目录或仓库内保存数据目录。

`init/001-enable-postgis.sql` 只在空数据目录首次初始化时启用 PostGIS。业务
Schema 的唯一权威入口是 `services/pipeline/src/track/v2/cli/init_db.py`。当前版本为
schema v9：v4 至 v8 使用 `init-db --migrate-artifacts` 升级；v3 及更早版本
不再支持保留数据的 CLI 迁移，`init-db --reset` 会全量重建已知 Pipeline 表。
不要混跑依赖不同版本字段的服务。

`refine_runs` 唯一保存外部 `source_table`，kept/excluded 的
`source_row_key` 为 `BIGINT`。仓内实体关系使用 `BIGINT` 主键。Detect 在最终事务中写入
`resolved_subtrack_points`，每行恰好引用 kept、injected 或完整交叉口的 candidate
crossing 之一。Clip 只消费 completed Detect 的最终顺序；Centerline manifest
仅保存起止 crossing 血缘。

点产物在首次写入时保存 SRID 4326 的几何快照：`kept_trajectory_points` 和
`excluded_points` 从当前批次的经纬度写入 `geom`；`resolved_subtrack_points`
复制所引用保留点、注入点或穿越点的几何。`entry_exit_points` 的一行包含一对
进出点，因此其 `geom` 为 `geometry(MultiPoint, 4326)`，按入口、出口顺序保存
两个点；其余点表使用 `geometry(Point, 4326)`。原有来源键和序列保持不变。
已有 schema v9 再次执行 `init-db` 会补齐这些可空列，但不会回填历史数据。

术语严格区分：`tracks_cross` 的 track cross 是两条轨迹线段之间的交点；
`candidate_entry_exit_points` 及所有 `crossing_*` 字段表示轨迹穿越交叉口边界的
entry/exit crossing。两者不是同一种实体。

新运行的历史字段 `source_fingerprint` 保存运行 UUID 元数据：Refine 保存
`refine_id`，Detect 制品保存 `refine_id` 与 `detect_id`，不再计算源数据内容摘要。
上下游归属校验使用制品的运行 UUID；已有旧指纹元数据无需回填。

当前不使用 Alembic，也不在此目录复制第二份可能漂移的业务 Schema。

Detect 在同一结果写入事务内，按当前 `detect_id` 将转向事件物化到
`turn_evnets_gemo`（沿用既有表名）。该表保存 `turn_events` 的全部字段及
`geometry(Point, 4326)` 类型的非空 `geom`：kept 点通过当前 Refine 的
`source_table` 获取几何，injected 点直接取 `injected_points.geom`。
事件 ID 保持不变，删除源事件时级联删除副本；completed Detect 的副本同样不可修改。

已有 schema v9 执行 `init-db` 即可添加此表；此前手动物化的同名表会保留数据，
并补充非空、外键和索引约束。初始化不自动回填其他历史 Detect 批次；
新执行的 Detect 会自动物化，复用已完成 Detect 不会重新写入。
