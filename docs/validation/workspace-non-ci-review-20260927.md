# 非 CI/CD 工作区改动审查与验证

日期：2026-09-27。基线：`6e5b2cd`。验证对象是工作区，其中包括未提交的网格预算 WIP，
不能将结果描述为仅验证基线提交或仅验证下表可采用文件。

## 采用结论

| 改动 | 结论与边界 |
| --- | --- |
| 中心线 Replay，Backend 与前端联动 | 可采用。按持久化来源片段清单读取精确成员，校验运行关联、方向与资源上限。属性使用当前源值，未校验历史来源指纹。 |
| 开发/调试镜像及性能分析目录挂载 | 可采用。两个服务均成功构建 development/debug 目标；生产默认目标沿用 runtime。 |
| 栅格显式边界对齐与有效边界记录 | 可采用。边界按全局像素网格扩展；浮点对齐误差使用已有测试中的容差规则。请求与实际边界分别记录。 |
| 全量配置的栅格分辨率 1m→10m | 可采用为当前配置选择。每个像素覆盖面积扩大到原来的 100 倍，栅格细节粒度随之变化。 |
| 网格预算回退的 5 个既有 WIP 文件 | 暂不作为定稿采用。当前仍是 Python overflow 配对枚举，与最新 Spec 选择的 DuckDB 原生外存方向不一致。整 grid 输入及全 Run 候选/去重预算处理尚未完成。 |
| 列式架构 Spec 中的诊断及 QA 记录 | 保留记录。Q104–Q106 尚未回答，测试通过不能替代设计确认。 |

## 本次修复及补充

- 同步中心线地图要素测试的 `centerline_id` 与 `fit_id` 字段。
- 增加 Backend 成员选择、方向反转、共享点去重、运行归属、缺失成员/清单、车辆身份及预算拒绝覆盖。
- 增加真实 PostgreSQL 私有临时表测试，执行实际回放 SQL，验证区间外点不返回、反向片段方向正确、当前源行缺失使整个请求失败。测试事务回滚。
- 增加前端中心线请求上下文、选择互斥、ID 与选择数量校验覆盖。
- 增加 Compose 报告目录及 development 构建目标覆盖；清理配置测试继承的路径环境变量，避免测试结果受宿主机设置影响。
- 修复预算 WIP 的 membership 回退分支漏写 `overflow_pair_budget_exceeded` 监测字段；此修复不表示 WIP 已定稿。
- 更正 Pipeline Dockerfile 中 development/debug/默认目标的注释。

## 验证结果

- `make frontend-test`：前端测试与静态构建通过。
- Compose 配置/Make 命令测试：16 项通过。
- Backend 完整测试，容器内从配置生成 `ROADNET_TEST_DSN`：109 项通过，零跳过。
- Pipeline/CV 完整测试，同样启用真实 PostgreSQL：1261 项通过，零跳过，67.51 秒；12 项 Rasterio 弃用提醒。
- `make test`：通过。默认命令没有设置 `ROADNET_TEST_DSN`，Backend 107 passed/2 skipped，Pipeline/CV 1189 passed/72 skipped；真实 PG 覆盖另由上述完整命令验证。
- Backend/Pipeline 的 development 与 debug 镜像构建通过。
- Pipeline debug 镜像实际 UID 为 1000；短时 CPU 工作负载的 py-spy 火焰图采集 199 个样本、零错误，Scalene 模块运行生成 JSON 报告；`pip check` 通过。
- `git diff --check`：通过。

本次没有执行 Pipeline 五阶段冒烟或全量性能验收，也没有修改既有 Run、源数据或重启运行中的应用。
栅格 CLI 回归使用配置 `services/pipeline/config/pipeline-roadnet-test.toml` ，其数据库及输入由测试替身提供，
不等同完整 Pipeline 冒烟。原 26M 的性能与内存结论仍依活跃 Spec 的实际计量记录。
