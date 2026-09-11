# Schema v3 cutover

本目录不再包含可执行的历史迁移。`track.v2` schema v3 是一次明确的破坏性
API/Schema 切换：停止 writer，核对 database/user/schema，执行
`python -m pipeline init-db --database roadnet --reset`，再一起部署 v3 Pipeline、
Backend、Frontend 和 QGIS 客户端。

无 `--reset` 时初始化器只接受空库或已登记为 v3 的库。检测到 v1、v2 或未版本化
Pipeline 表会失败，不补列、不回填，也不猜测历史血缘。权威 DDL 始终位于
`services/pipeline/src/track/v2/cli/init_db.py`。
