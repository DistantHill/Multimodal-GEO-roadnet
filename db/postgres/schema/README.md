# Schema definitions

业务 Schema 的当前权威定义位于 `services/pipeline/src/track/v2/cli/init_db.py`。它同时服务新库初始化和已有数据库兼容升级：`CREATE TABLE IF NOT EXISTS` 与自动补列处理常规差异，复杂约束和索引变化由显式结构迁移维护。

当前 Run lineage 是 `clean_runs → detect_runs → fit_runs`；`pipeline_runs` 仅可能作为早期数据库的一次性迁移输入。后续若拆分为独立 SQL，必须保持当前列、索引、约束、lineage 和迁移测试，而不是只复刻初始来源仓库的行为。不要把 PostgreSQL 数据文件、备份或 runtime volume 放入仓库。
