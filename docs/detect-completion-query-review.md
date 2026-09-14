# Detect 提交校验查询审查（2026-09-14）

本次审查由 `test/public.day01` 百万行冒烟测试触发：Detect 已完成计算，但提交事务时的交叉点完整性校验超过 4 分钟。新运行尚无有效统计信息，多表连接的估算行数为 1，实际包含几十万条记录。

| 审查位置 | 风险与处理 |
| --- | --- |
| Detect 交叉点集合校验 | 将物化交叉点结果再次连接到预期集合，可能重复扫描无索引的中间结果。缺失检查改为对 `resolved_subtrack_points` 执行 `NOT EXISTS`，保留运行范围条件。 |
| Detect 交叉点位置、线段端点校验 | `refined_sequence_in_subtrack + 1` 自连接还可能按子轨迹编号进行合并连接，再逐对过滤序号；长子轨迹退化成大量成对比较。两处都改用 `LEAD` 窗口函数顺序推导后继点。前置校验已经保证精炼序列连续，因此语义等价。 |
| Detect 点集合、跨轨迹车辆、转向事件、进出观测校验 | 都处于新运行统计信息不足的事务内。完成校验函数使用局部 `enable_nestloop=off`，保留可用的哈希连接、合并连接，并为源点补充父 Refine 范围条件。进出观测显式拒绝缺失的解析端点。 |
| Fitting 来源片段计数与端点校验 | 存在相同的新运行多表连接风险。使用相同的函数局部设置，补充 Detect 范围条件；显式拒绝两个解析端点都缺失的情况，防止 NULL 比较漏报。 |
| Pipeline 空间范围、窗口点查询、Clip 点序列读取、Refine 子轨迹读取 | 检查了连接键、原始主键类型转换及运行范围条件。未发现上述后继点自连接或重复连接物化交叉点的结构；添加查询及结果遍历心跳。 |
| 源数据指纹 | 保留一次有序扫描、命名游标和按原始主键类型连接的实现；添加扫描心跳和已处理行数。 |
| Detect、Fitting 批量写入 | 保留 COPY/分批写入；添加写入表名、已发送行数、总行数和等待数据库响应期间的心跳。已发送行数不代表事务已经提交。 |
| Pipeline 来源追溯 | 按实体关系读取，存在逐实体往返；`visited` 避免重复遍历，源数据指纹已有运行级缓存。未发现本次的成对扫描结构；添加遍历心跳和节点数量，未改为批量图查询。 |
| Backend 交叉口回放、解析点读取 | 后继点读取已有 `LATERAL ... LIMIT 1`，点读取保留明确的运行、实体或子轨迹范围条件；本次未修改其查询及接口。 |

函数局部设置会在退出函数时恢复调用者配置，成功与异常路径均有数据库测试。`enable_nestloop=off` 是规划偏好，不保证所有情况下完全消除嵌套循环；它仅用于必须校验整个新运行的两个函数，不更改数据库全局规划参数。批量连接仍消耗排序或哈希工作区，本次没有提高 `work_mem`。参见 PostgreSQL 的[连接规划参数](https://www.postgresql.org/docs/16/runtime-config-query.html#GUC-ENABLE-NESTLOOP)和[函数局部设置](https://www.postgresql.org/docs/16/sql-createfunction.html)。

读取器保留调用者的 `statement_timeout`。仅对当前读取事务设置 `SET LOCAL idle_in_transaction_session_timeout=0`，允许 FETCH 之间的计算暂停，事务结束后恢复。

心跳由独立观察线程发出，不使用正在执行 SQL 的连接；嵌套操作共享线程，退出时停止并回收。数据库校验通过带固定前缀的 NOTICE 告知当前检查项，提交期间由临时监听器更新心跳标签。没有可计算的剩余工作量时隐藏百分比；Detect 计算期间最高显示 99%，成功提交后才显示 100%。Detect 与 Fitting 的持久化计时均包括提交校验。

验证使用以下方式：

- `make test`：Backend、Pipeline、Compose 及已有辅助脚本测试。
- 设置 `ROADNET_TEST_DSN` 后执行 `tests/v2/test_completion_postgres.py`：在唯一命名的数据库结构中造数并整体回滚，覆盖旧统计信息、多条短子轨迹、单条长子轨迹、缺失交叉点、错误线段端点、进出类型、Fitting 计数/方向/缺失端点、超时和规划设置恢复、阻塞 SQL 期间的心跳。
- `tests/v2/test_refine_migration_postgres.py`：验证现有迁移与新版校验定义兼容。
- 复用已完成的 Refine `e63ebb1a-c347-4c48-bb0a-4bc5ee58354e`，在 `test/public.day01` 上重跑 Detect。在测试事务中临时替换校验函数、写入结果，执行 `SET CONSTRAINTS ALL IMMEDIATE` 后整体回滚；运行记录和函数替换均未保留，源数据未修改。

百万行复测结果：

| 项目 | 实测 |
| --- | ---: |
| Detect 计算 | 138.6 秒 |
| 结果写入 | 52.6 秒 |
| 全部延迟约束校验 | 15.8 秒 |
| 解析点 | 679,649 |
| 候选交叉口 / 完整交叉口 | 393 / 292 |
| 候选进出点 / 完整进出观测 | 83,947 / 31,207 |

计算、写入时间受同机负载影响；本次验证针对数据库校验卡点。15.8 秒是强制执行延迟约束的时间，不包含最终 COMMIT；本次也没有重跑 Refine、Clip、Fitting 全流程。复测日志位于 `/tmp/roadnet_detect_validation_smoke_v2.log`，测试驱动位于 `/tmp/roadnet_detect_validation_smoke.py`。

现有数据库需要重新执行 `init-db` 更新函数定义。对本次目标可执行：

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline init-db --database test
```

这是现有 v4 数据库的幂等初始化，不使用 `--reset`。本次复测只在回滚事务中安装了新定义，未持久更新现有数据库函数。

2026-09-14 随后按用户要求在 `roadnet` 复用最近已完成的 Refine，执行了全流程冒烟：

```sh
python -m pipeline run --database roadnet \
  --refine-id f1986249-8382-4d00-972f-900b6e81ea43
```

该 Refine 对应 `public.trajectory_points` 的 26,000,000 行源数据，保留 15,834,624 点、注入 25,412 点，继承 EPSG:32649。启动前已将两个完成校验函数持久更新到 `roadnet`；`test` 数据库仍保持前述回滚测试的状态。

本次 Detect 为 `55c0de2f-d3f0-4f04-bb7b-5151ae200bc7`。源数据指纹校验通过；轨迹读取首批结果等待约 160 秒，数据库采样未发现锁阻塞。后续读取和转向事件检测出现持续内存压力，心跳曾间隔数分钟，最后记录到 2,740,000 点。中止前容器内存采样为 3.555 GiB，机器总内存约 6.7 GiB，2 GiB 交换空间接近用满。`_stream_extract_method_1` 将处理过的轨迹保存在全局 `tracks` 字典中供后续阶段使用，尚未处理完全部保留点就已出现明显资源压力。

因此主动发送 SIGINT 中止测试，退出码 130；这不是已经发生的容器 OOM，也不是提交校验失败。确认进程退出和事务回滚后，将本次 Detect 从 `running` 标记为 `failed`。十张关联结果表的本次运行记录数均为 0，无活动数据库会话，原 Refine 仍为 `completed`，源数据未修改。Clip、Fitting、Network 以及 Detect 提交均未执行，本次冒烟未通过。

日志：`/tmp/roadnet-smoke-reuse-f198-20260914.log`；Pipeline 日志名为 `20260914_034520_974051_pid-1_log.txt`，保存在配置的日志目录。
