# Schema v6 migrations

`track.v2` 数据库结构（schema）v4 将 Clean 统一改名为 Refine。v3 可执行
`python -m pipeline init-db --database roadnet --migrate-refine`，在一个事务内
保留数据、改名并依次升级至 v6。v4/v5 使用 `--migrate-artifacts` 升至 v6。
执行前停止相关读写服务，完成后一起部署 Pipeline、Backend 和客户端。具体步骤见
[迁移说明](../../../docs/refine-migration-and-performance.md)。

无迁移选项时初始化器只接受空库或已登记为 v6 的库。v1、v2 或未版本化
Pipeline 表仍需另行处理；`--reset` 会破坏性重建。v4 业务表的权威数据定义语言
（data definition language，DDL）位于
`services/pipeline/src/track/v2/cli/init_db.py`。

## 流式任务账本扩展

`004_patch_ledger.sql` 是独立任务账本的显式安装定义，不是历史业务数据迁移，
不改变 v3 业务表，也不由现有初始化入口自动执行。文件序号不表示已升级至 schema v4。
由 `track.v2.db.patch_ledger.install_ledger` 将定义安装到调用方指定、已存在的数据库结构；
不得直接把模板内容作为普通 SQL 文件运行。该接口需要独占、空闲且开启自动提交的连接，
安装操作自身在一个事务内执行；应先在隔离测试结构验证，再于业务接入时显式部署。

任务状态与业务运行状态是两个边界：任务全部完成不能绕过清单封闭与阶段发布检查，
当前扩展不会自动更新 `refine_runs`、`detect_runs` 或前后端字段。
实现进度、限制与验证证据见[实施任务卡](../../../docs/pipeline-streaming-implementation.md)。
