# PostgreSQL / PostGIS

这里只保存数据库定义、初始化和迁移逻辑。PostgreSQL runtime data 由 Compose 的 `postgres_data` named volume 管理，禁止在此目录或仓库内保存数据目录。

`init/001-enable-postgis.sql` 只在首次创建 named volume 时启用 PostGIS。当前业务 Schema、兼容性列迁移、显式结构迁移和索引创建的唯一权威入口是 `services/pipeline/src/track/v2/cli/init_db.py`，通过 `make pipeline-init-db` 执行。已有 named volume 升级代码后也要运行该命令，例如它会补齐 `turn_events.tp_source_id` 和 `turn_events.tp_source_row_key`。

当前不使用 Alembic，也不在此目录复制第二份可能漂移的业务 Schema。普通升级不要使用 `--reset`；该参数会删除全部 Pipeline 业务数据。早期 `pipeline_runs` 注册表的兼容升级属于一次性迁移，不代表当前 Pipeline 仍使用旧 Run 模型。
