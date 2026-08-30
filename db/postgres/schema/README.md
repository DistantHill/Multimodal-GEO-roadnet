# Schema definitions

业务 Schema 的当前权威定义位于 `services/pipeline/src/track/v2/cli/init_db.py`，以保持原有 Python 初始化行为。后续若拆分为独立 SQL，必须保持该入口兼容并增加迁移测试。不要把 PostgreSQL 数据文件、备份或 runtime volume 放入仓库。
