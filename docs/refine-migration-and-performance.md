# Refine 迁移与加速说明

当前精炼阶段（Refine）原名为 Clean / Cleaner。历史设计说明与问答保留原名，
实际接口以本文和当前代码为准。

## 命名与部署

| 原标识 | 当前标识 |
| --- | --- |
| `track.v2.cleaner.clean` / `clean_vehicle` | `track.v2.trajectories.refine_trajectories.refine` / `refine_vehicle` |
| `CleanerConfig` / `CleanerResult` | `RefineConfig` / `RefineResult` |
| `[cleaner]` | `[refine]` |
| `runtime.cleaner_flush_point_threshold` | `runtime.refine_flush_point_threshold` |
| `clean_runs` / `clean_id` | `refine_runs` / `refine_id` |
| `cleaned_sequence_in_subtrack` | `refined_sequence_in_subtrack` |
| `--clean-id` | `--refine-id` |
| 前端 `cleanId` / API `clean_id` | `refineId` / `refine_id` |
| `track.v2.intersect2` | `track.v2.intersections.detect_intersections` |
| `track.v2.parser0` / `track.v2.sources` | `track.v2.trajectories.read_source` / `parse_rows` |

正式 Python 入口使用 `from track.v2.trajectories.refine_trajectories import refine`。
包级增量入口仍为 `from track.v2 import refine_vehicle`，由 `track.v2` 延迟导出。
`cleaner`、`cleaner1`、`clean1`、`refine1`、`parser0`、`intersect2`、`detect2`
薄壳已删除，不再提供旧导入路径或包级 `clean` 别名。Parser 来源读取与行解析
分别位于 `trajectories.read_source` 和 `trajectories.parse_rows`；正式 Pipeline 和测试
直接调用这些模块及 `trajectories.refine_trajectories`。
旧基准命令薄壳和 `manual_*` 临时调试脚本已于 2026-09-25 从仓库移除；下方计时数字是历史记录，当前 checkout 不再提供对应的可执行基准入口。
旧配置节、旧 CLI 参数与旧 API 字段需要调用方同步修改。

数据库结构（schema）版本升至 v4。先停止使用目标库的 Pipeline 和 Backend，
核对连接目标，使用新代码执行：

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline \
  python -m pipeline init-db --migrate-refine
```

迁移在一个事务内改名表、列、索引、约束、触发器及关联函数，并更新版本登记。
PostgreSQL 不会自动改写 PL/pgSQL 函数正文，因此迁移重新安装权威关系保护函数。
失败时整个事务回滚；已为 v4 时重复执行无须再次改名。
运行 UUID、点 ID、结果数据及既有 `parameters` 历史记录保留；新运行参数使用新名称。
之后一起启动新版本服务与客户端。`--reset` 会删除业务结果，不用于此次保留数据升级。
代码交付不代表已对现有业务库执行迁移。

## 对象构建与加速

现有历史记录证明 `PointRef` 是主要内存组成之一，未找到它占据最多运行时间的
计时证据。当前 Parser 每个正常来源行创建一个 `PointRef` 和一个 `TrajectoryPoint`；
Refine 保留点复用原有 `PointRef`，投影坐标和子轨迹编号更新会复制点对象。

本轮移除了普通 `TrajectoryPoint` 坐标更新中的通用 `dataclasses.replace()`，
直接构造一次结果对象，并合并子轨迹改名与坐标更新。子类继续走 `replace()`，
保留扩展字段。未发生静止点剔除时直接返回，省去逐点 `PointRef` 集合哈希。
点身份、附加字段、子轨迹编号和坐标语义保持一致。

本机 Python 3.12 微基准中，构造 `PointRef` 约 0.72 μs，构造 `TrajectoryPoint`
约 1.18 μs，通用坐标复制约 3.08 μs，直接坐标复制约 1.20 μs。
10 万行合成 Parser 性能剖析中，两个构造器自身时间分别约占 11.4% 和 18.7%。
该占比包含剖析器开销，且不包含数据库读取；不能据此断言生产瓶颈。
生成的数据类构造器在 `pstats` 中可能共享同一源码位置，脚本使用不同位置标签
避免将两个构造器的时间错误合并。

2 万个连续行驶点的完整 Refine 合成测试中，仅切换坐标复制方式即从约
76.5 ms 降到 47.7 ms，约 1.61 倍；结果逐项一致。它不包含数据库读写、
来源指纹和真实数据中的转向分布，不代表生产全流程加速倍数。
最终脚本复测为 105.6 ms → 55.9 ms（1.89 倍），Parser 构造器占比分别为
12.1% 和 19.6%；记录这些波动，避免将单次计时作为固定性能承诺。

上述对象构建计时与数据库读取/精炼基准的原入口已移除；这些结果保留为历史记录，
不构成当前可复现命令或当前版本性能保证。

后续依据真实样本计时再选择优化：分别测量读取与解析、Refine 计算、批量写入、
来源指纹；若计算占主导，再评估按完整车辆分组的有界进程池及序列化成本；
若写入占主导，在内存预算内调整批量阈值。不要仅因对象数多就增加并发进程。

## 验证

删除兼容薄壳后再次运行 `make test`：Backend 74 项通过，Pipeline 与 CV 968 项通过、
37 项可选测试跳过；旧兼容入口的测试已移除，其余测试直接验证正式接口。
新增数据库迁移测试单独连接 PostgreSQL 执行，2 项通过，验证数据保留、结果
不可修改、重复执行及事务回滚。测试仅使用事务内新建的独立数据库结构，
搜索路径排除 `public` 回退。前端测试与生产构建通过；`git diff --check` 通过。
