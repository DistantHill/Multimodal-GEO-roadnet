# PostgreSQL / PostGIS

这里只保存数据库定义、初始化和迁移逻辑。PostgreSQL runtime data 由 Compose 的 `postgres_data` named volume 管理，禁止在此目录或仓库内保存数据目录。

`init/001-enable-postgis.sql` 在首次创建 named volume 时启用 PostGIS。原项目的业务 DDL、兼容性迁移和索引创建仍由 `services/pipeline/src/track/v2/cli/init_db.py` 管理，可通过 `make pipeline-init-db` 执行。本次迁移不改用 Alembic，也不复制两份可能漂移的 Schema 定义。
