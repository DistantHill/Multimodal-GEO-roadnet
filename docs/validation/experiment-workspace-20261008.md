# 实验工作台验收记录（2026-10-08）

依据 [已确认规格](../intersection-evaluation-spec.md) 实施，术语遵循 [项目术语与写作规范](../terminology.md) 。

## 交付范围

三个 gpt-6.1-sol / medium 子代理分别实施 Pipeline、评价存档/API、Frontend；主代理审查、退回修改并复验。
实现单 TOML 命名案例和多方案、严格时空读取、批次共享 Refine、独立 roadnet_eval 存档、历史重新评价、手动清理与双地图对比。
Makefile 的 pipeline-full-test 已增加北京时间 2023 年 3 月 1 日全天筛选，仍使用 pipeline.toml；未运行全量数据任务。

## 验收中发现并修复

- 日期参数须以 epoch 秒绑定源表 DOUBLE PRECISION 时间字段。
- 新工作区须创建运行目录；归档连接必须把 libpq 布尔参数转换为整数。
- 容器中缺少 Git 仓库，统一 Make 入口传入宿主机 commit。
- 参数恢复需深合并并保存区域、真值和字段映射，不能依赖已被编辑的草稿文件。
- 重新评价使用历史实验区域；运行列表等历史操作不要求当前案例完整有效。
- 存档重试不得把可变错误字段混入不可变快照；评价失败须使 CLI 返回失败并展示原因。
- 评价记录时间采用实际插入时刻，避免同一事务内随机 UUID 决定“最新”版本。
- 新增 Shapely 依赖同时纳入 pyproject 与带哈希 requirements.lock。
- 前端选中历史评价时只显示对应指标；补充要素详情、截断提示和候选计数。
- 进程内存高水位明确为整个进程生命周期，不冒充单次方案峰值。
- 运行中 Detect 即使没有发布产物也保护共享 Refine；存在下游 Fitting 时拒绝清理对应 Detect。

## 自动化与集成证据

| 检查 | 结果 |
| --- | --- |
| make test，Compose | 22 项通过 |
| make test，Backend | 147 passed，6 skipped |
| make test，Pipeline | 1346 passed，89 skipped；12 个既有 rasterio 弃用警告 |
| Frontend npm test | 36 项通过 |
| Frontend npm run build | 通过 |
| 主代理真实 PG 存档与严格时空读取 | 6 项通过；唯一临时 schema，结束后清理 |
| 主代理真实 PG 清理保护 | 2 项通过；运行中 Detect 与下游 Fitting |
| 主代理真实 Backend API | 1 项通过；真实 SQL、只读事务、历史地图、筛选与 404 |
| 小型真实双方案端到端 | 通过，详见下文 |
| git diff --check | 通过 |

Pipeline pytest 使用 services/pipeline/config/pipeline.toml ，是测试套件，不是全量数据作业；未执行冒烟或全量数据检测。
真实 PG 测试显式配置 ROADNET_TEST_DSN，仅在独立临时 schema 中写入，不修改业务表。

端到端测试创建并删除两座唯一临时数据库，加入 72 个范围内点及时间/空间范围外的点。
实际运行 turn 与 joint 两方案，验证恰有一个 Refine、两个 Detect、零 Fit，保留点数为 72。
随后补充真值，保存两次评价；先清理一个方案时共享 Refine 保留，重复清理可执行；
再清理第二个方案后，独立归档仍返回相同地图及历史指标。测试数据库和测试工作区均已清理。
该小样本主要验证编排和归属，不声称验证真实城市交叉口召回质量；匹配正确性由独立非空几何测试覆盖。

主代理日志在仓库外： /tmp/roadnet-experiment-make-test-final.log 、 /tmp/roadnet-experiment-e2e-final.log 、
 /tmp/roadnet-experiment-api-acceptance.log 、 /tmp/roadnet-experiment-pg-acceptance.log 、 /tmp/roadnet-experiment-cleanup-acceptance.log 、
 /tmp/roadnet-experiment-frontend-test.log 和 /tmp/roadnet-experiment-frontend-build.log 。

## 当前初始化状态与限制

已创建独立 roadnet_eval 库及表，初始化单份本地模板于宿主机
 /data/tmp/road-network/experiments/experiment.toml （容器 /data/experiments/experiment.toml ）。
默认模板案例待填写；list 返回空历史，run 在未填时间时提前报错，未启动算法。
使用方式见 [工作台说明](../experiment-workspace.md) 。

浏览器工具返回无可用浏览器，因此未验收实际地图拖动、点击和视觉布局；前端构建、逻辑测试与真实 API 已通过。
既有无关配置、README/AGENTS 变更、QGIS zip 删除、监控/导入脚本等均未回退或提交。
所有改动尚未提交 Git。
