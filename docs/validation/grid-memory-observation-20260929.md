# Grid 内存配置与无硬上限观测验证（2026-09-29）

本轮代码提交 `a2e6a74`：Detect grid 的独立 DuckDB 实例使用 4GB，Turn/Cross
各自保持 4GB，其他实例继续使用公共 1GB，工作线程保持 4。测量容器不设硬内存
上限；Pipeline 的 `memory.current` **超过** 6,000,000,000 B 时，在当次日志写
`ERROR` 并继续运行。30 秒读取一次，持续超阈值每 10 分钟最多再记一次。
这一阈值是观察值，不是新的失败条件。

正式数据库 Detect 路径由容量 2 的队列将候选批送入 DuckDB staged 关系，
没有发现跨 grid 的 Python ndarray 列表持续累计。替代的非 staged 调用路径
仍有 `legacy_batches` 累积，不参与此前 26M 正式运行。该审计不能推定历史
OOM 的唯一原因。此前 `046b087` 的 4GB 控制组 OOM 指标和日志仍按主 Spec
第 7.2 节保存；本轮配置尚未运行新的 26M 全量测试。

验证结果：

- 生产 Pipeline 镜像针对性测试 148 passed，覆盖 grid 专用 DuckDB 实际
  `memory_limit`、full/smoke 配置及旧 TOML 回退、严格大于 6GB 边界。
- 低阈值子进程测试实际将 `ERROR` 写入 Pipeline 日志，随后正常退出 0；
  未分配 6GB 内存。
- `make test` 中基础 Compose 测试 16 passed；Backend 106 passed、2 skipped、
  1 failed。失败为既有 `test_map_store.py::test_store_clip_reads_resolved_crossings_and_returns_between_clip`
  fixture 与来源表结构查询不匹配，本轮未改 Backend。
- 因 `make test` 在 Backend 停止，另执行同等 Pipeline/CV 命令，结果
  1211 passed、72 skipped；未将整体 `make test` 写为通过。
- 版本化 Compose 测量覆盖文件在短容器中实读到 `memory.max=max` 和
  `memory.swap.max=max`。Docker 29.1.3 的本地实测说明，取消容器硬内存上限后，
  `memswap_limit: 0` 不再使 swap 上限为 0；测量脚本记录实际 swap 值，
  不声称延续此前 swap 0 的条件。

从固定代码快照或对应隔离检出运行，命令示例位于
`services/pipeline/scripts/measure_pipeline_run.py` 的模块说明。使用
`services/pipeline/config/pipeline.toml` 测全量，使用
`services/pipeline/config/pipeline-roadnet-test.toml` 测冒烟；本轮没有启动这两种
大规模作业。测量脚本要求 `memory.max=max`，记录运行返回码、实际控制组
内存及 swap 设置、峰值、OOM 事件和受保护源表运行前后只读行数。
