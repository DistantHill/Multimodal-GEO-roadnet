# Schema definitions

业务 Schema 的唯一权威定义位于
`services/pipeline/src/track/v2/cli/init_db.py`。Schema v9 支持空库初始化；v4 至 v8
使用 `--migrate-artifacts` 在事务内保留数据升级，v3 及更早版本需使用 `--reset`
重建已知 Pipeline 表。

当前 Run lineage 是 `refine_runs → detect_runs → fit_runs`。后续若拆分为独立
SQL，必须保持 v9 的列、索引、约束、版本检查和事务边界。不要把 PostgreSQL
数据文件、备份或 runtime volume 放入仓库。
