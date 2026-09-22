# Pipeline 列式架构运维指南

当前设计依据：[Pipeline 列式存储与计算架构 Spec](pipeline-columnar-architecture-spec.md)。
实施状态见[Pipeline 列式架构分阶段实施任务卡](pipeline-columnar-implementation.md)。
术语遵循[项目术语与写作规范](terminology.md)。

## 存储与路径

Pipeline 的非敏感运行配置集中在 `services/pipeline/config/pipeline.toml`，
字段定义集中在 `services/pipeline/config/field_specs.toml`。运行入口只在启动时读取
`PIPELINE_CONFIG_PATH` 指向的完整 TOML；不支持多文件合并、逐项环境变量覆盖或
CLI 覆盖。修改配置后必须重新创建或重启 Pipeline 进程；运行中的 Run 保持原配置
快照，不会热重载。数据库密码仅由根目录 `.env` 中的 `POSTGRES_PASSWORD` 注入；
旧 `TRACK_PG_*` 与逐项 `POSTGRES_*` 非敏感配置不再兼容。

PostgreSQL 是源数据、Run 状态、制品登记和最终业务结果的权威存储。Refine 与 Detect
成功后各发布不可变 Parquet 数据集，作为下游的大表输入；Parquet 不是可修改的第二套
业务数据库。不得覆盖、修补或重新生成已发布 Run 的单个 part 文件，重算必须使用新的
Run ID 和新路径。

`DATA_ROOT` 是运行时根目录。`PARQUET_ROOT` 可显式设置，未设置时为
`${DATA_ROOT}/parquet`；DuckDB 和 Polars 临时目录分别派生为
`${DATA_ROOT}/duckdb`、`${DATA_ROOT}/polars`。容器默认对应 `/data/parquet`、
`/data/duckdb`、`/data/polars`。DuckDB 使用内存 catalog，其临时目录及外存溢写由
DuckDB 引擎管理；`.duckdb` 文件不是持久权威状态。Polars 目录用于引擎临时文件或缓存。

Parquet staging 与最终发布目录必须位于同一文件系统，以目录原子改名完成发布。发布
后文件及目录只读，消费者只读打开。不要把 staging 放在另一个挂载点，也不要绕过发布
和登记流程移动数据集。

## 失败与重跑

失败 Run 永久保持 `failed`。清理或对账该 Run 的 staging 和引擎临时目录后，以新 Run
ID 从最近的 completed 上游 Parquet 检查点重跑整个失败阶段。系统不支持旧 patch、
单文件或阶段内部检查点续跑，也不得把失败 Run 改回 `completed`。

## 制品对账与清理

默认宽限期为 7 天。当前成功血缘引用的制品受保护；失败 Run 制品和未登记目录只有在
超过宽限期且计划判定可清理时才会成为 apply 候选。先运行默认 dry-run：

```sh
python -m pipeline artifacts reconcile
```

核对 JSON 计划、数据库目标和 `PARQUET_ROOT` 后，才可显式执行：

```sh
python -m pipeline artifacts reconcile --apply
```

`--apply` 会删除合格的受控目录，并为仍登记为 `present` 的制品追加 `deleted` 事件。
计划含 critical 项时 CLI 拒绝 apply；其中包括非法路径、非法登记和登记制品缺失。此时
应先调查 PostgreSQL 登记与文件系统状态，不得手工修改已发布 Parquet 来消除告警。
可用 `--grace-days` 临时覆盖宽限期；生产清理应保留默认值，除非已有明确、可审计的
处置决定。该命令不由后台线程自动调度。

曾手工安装旧 patch ledger 的环境，仅按
[PostgreSQL 迁移说明](../db/postgres/migrations/README.md)核对并手工清理；新环境不需要
安装或卸载该结构。不要让程序扫描或删除未知数据库结构。

## 验收状态

列式架构已经接入，不等于最终容量与性能目标通过。精确 26M 输入、默认 2 GB 硬上限、
无 swap 和 Refine 至 Network 小于一小时的 C13 验收尚未执行；运维记录不得把单元测试、
小样本或提高内存上限的运行写成该验收已通过。

2026-09-22 的冻结业务快照 `7e170a5` 已在 1M 输入上完成 Refine 至 Network，墙钟
`1,135.014419538` 秒，`memory.peak=1,999,998,976 B`，swap/OOM/OOM kill 均为 0。
该峰值几乎触及 `2,000,000,000 B` 内存上限，不代表容量余量充足。运行容器为
`roadnet-c12g-full-1m-20260922-145233`，保留根目录为
`/data/tmp/road-network/c12g_full_1m_20260922-145233_yPGnIn` ；不得将该目录当作可覆盖的
日常输出路径，也不得在验收完成前清理其日志、统计和已发布制品。

验证器从 `e2130d7` 演进到 crossing key 内联规划 `78dbd2a`；正式差分已有十张持久化表
通过，但 `detect.entry_exit` 在原计划下超时。局部关闭 nested loop 的只读诊断在
`8.499` 秒内确认第 `1,009` 个业务键存在 `exit_bearing` 差异
`8.161123332683928e-09`，所以该历史 baseline 与新 Run 的严格差分未通过。C12h3 只在
`detect.entry_exit` 的 DECLARE 规划期间应用并恢复该提示，让验证器能及时报告真实差异；
它不放宽数值容差，也不代表十四张持久化表和八张 Network controlled replay 已完成。

反例来自单个正交注入点的最低位坐标差异。当前镜像回放支持依赖环境差异这一候选解释，
但历史 baseline 没有保存最初射线交点 XY、精确镜像或依赖版本，不能把候选解释记录成
已证明根因。不得修改旧 Run、舍入结果或自动替换 baseline；如需建立可比较的新 baseline，
应先取得用户确认，再在固定环境中使用旧源码生成新的隔离 Run。PostgreSQL 单列前后统计
还包含小 fixture 测试污染，不得将全部数据库变化归因于该 1M Run。完整证据与阶段耗时见
实施任务卡的“C12 2026-09-22 冻结快照 1M 全链路运行”章节。
