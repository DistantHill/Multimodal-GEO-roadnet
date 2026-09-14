# Schema definitions

业务 Schema 的唯一权威定义位于
`services/pipeline/src/track/v2/cli/init_db.py`。Schema v4 支持空库初始化或对
既有 v4 结构补建幂等对象；v3 使用 `--migrate-refine` 在事务内保留数据升级，
v1/v2 仍需 `--reset` 破坏性重建。

当前 Run lineage 是 `refine_runs → detect_runs → fit_runs`。后续若拆分为独立
SQL，必须保持 v4 的列、索引、约束、版本检查和事务边界。不要把 PostgreSQL
数据文件、备份或 runtime volume 放入仓库。
