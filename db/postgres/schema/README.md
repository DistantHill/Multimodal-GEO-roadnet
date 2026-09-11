# Schema definitions

业务 Schema 的唯一权威定义位于
`services/pipeline/src/track/v2/cli/init_db.py`。Schema v3 只支持空库初始化或对
既有 v3 结构补建幂等对象；v1/v2 必须用 `--reset` 破坏性重建，不做自动补列或
兼容迁移。

当前 Run lineage 是 `clean_runs → detect_runs → fit_runs`。后续若拆分为独立
SQL，必须保持 v3 的列、索引、约束、版本检查和事务边界。不要把 PostgreSQL
数据文件、备份或 runtime volume 放入仓库。
