# Migrations

原有自动列迁移、结构迁移和 legacy run 迁移仍由 `services/pipeline/src/track/v2/cli/init_db.py` 与 `track.v2.db.legacy_migration` 执行。此次重构不引入 Alembic，也不改变 migration 语义。
