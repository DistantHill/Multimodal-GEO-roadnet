# Schema v3 cutover

本目录不再包含可执行的历史迁移。`track.v2` 数据库结构（schema）v3 是一次明确的破坏性
接口与数据库结构切换：停止写入器（writer），核对数据库、用户和数据库结构，执行
`python -m pipeline init-db --database roadnet --reset`，再一起部署 v3 Pipeline、
Backend、Frontend 和 QGIS 客户端。

无 `--reset` 时初始化器只接受空库或已登记为 v3 的库。检测到 v1、v2 或未版本化
Pipeline 表会失败，不补列、不回填，也不猜测历史血缘。v3 业务表的权威数据定义语言
（data definition language，DDL）位于
`services/pipeline/src/track/v2/cli/init_db.py`。

## 流式任务账本扩展

`004_patch_ledger.sql` 是独立任务账本的显式安装定义，不是历史业务数据迁移，
不改变 v3 业务表，也不由现有初始化入口自动执行。文件序号不表示已升级至 schema v4。
由 `track.v2.db.patch_ledger.install_ledger` 将定义安装到调用方指定、已存在的数据库结构；
不得直接把模板内容作为普通 SQL 文件运行。该接口需要独占、空闲且开启自动提交的连接，
安装操作自身在一个事务内执行；应先在隔离测试结构验证，再于业务接入时显式部署。

任务状态与业务运行状态是两个边界：任务全部完成不能绕过清单封闭与阶段发布检查，
当前扩展不会自动更新 `clean_runs`、`detect_runs` 或前后端字段。
实现进度、限制与验证证据见[实施任务卡](../../../docs/pipeline-streaming-implementation.md)。
