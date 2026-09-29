# Detect 候选提取与任务块调度

Detect 先提取转向事件（turn events）和跨轨迹候选（cross-track candidates），
再将汇总后的候选送入全局聚类。任务块（patch）的划分独立于检测内核。

| 位置 | 职责 |
| --- | --- |
| `services/pipeline/src/track/v2/candidates/build_patches.py` | 完整轨迹分块；空间网格构建、容量检查、线段对分块、规范归属去重及长线段回退调度 |
| `services/pipeline/src/track/v2/candidates/detect_turn_events.py` | 构造和拼接转向事件批次 |
| `services/pipeline/src/track/v2/trajectories/results/read_parquet.py` | 使用 DuckDB 窗口和 SQL 阈值计算转向事件 |
| `services/pipeline/src/track/v2/candidates/detect_cross_track_pairs.py` | 准备有效线段；`detect_cross_track_patch` 对指定线段对执行精确求交并保留来源追溯信息 |
| `services/pipeline/src/track/v2/candidates/dbscan_index.py` | 紧凑存储 DBSCAN 候选索引及活动网格链接 |
| `services/pipeline/src/track/v2/candidates/cross_track_relation.py` | 保存并流式读取跨轨迹候选及其聚类成员关系 |
| `services/pipeline/src/track/v2/candidates/turn_event_relation.py` | 保存并流式读取转向事件候选 |
| `services/pipeline/src/track/v2/candidates/collect_candidates.py` | 调用检测内核、汇总候选、统一编号、报告进度和心跳 |
| `services/pipeline/src/track/v2/candidates/grid.py` | 根据 Refine 范围划分闭合 XY 网格 |
| `services/pipeline/src/track/v2/candidates/grid_segments.py` | 从网格读取批次构建有界线段列 |
| `services/pipeline/src/track/v2/candidates/detect_grid_crossings.py` | 检测网格内跨轨迹交点并调度候选结果 |
| `services/pipeline/src/track/v2/intersections/detect_intersections.py` | 连接读取、候选提取与全局聚类阶段 |
| `services/pipeline/src/track/v2/intersections/extract_topology.py` | 按检测簇提取轨迹进出观察并构建交叉口拓扑 |

正式 Detect 从 Refine Parquet 读取点，由 DuckDB 窗口计算转向角并直接应用
SQL 阈值，输出的事件再组成有界批次。内存轨迹入口将轨迹任务块写入临时
DuckDB 表，调用同一 SQL 计算路径。关闭转向检测时仍消费输入任务块。

内存轨迹入口先准备跨轨迹线段，再由 `build_cross_track_index` 构建空间网格，
`iter_cross_track_patches` 输出有界线段对数组，交给检测内核计算。
网格跨度保存在调度模块中，检测线段不携带网格属性。
同一子轨迹的线段不配对；同车不同子轨迹允许配对。覆盖多个网格的配对只归属一个网格，长线段走有界回退配对。
所有命中按原有顺序排序后赋予全局编号，聚类在汇总后执行。

INFO 开始与结束摘要、定时心跳和进度更新由外层候选提取负责，检测内核不打印
逐任务块日志。心跳覆盖读取、空间网格构建和计算期间，计数在任务块完成后更新。

跨轨迹候选仍使用 NumPy 批量计算；转向事件的窗口和阈值计算由 DuckDB 完成。
内存轨迹入口仍由调用方持有轨迹工作区，也未引入进程池。
配置字段及持久化结构保持原有格式；内存入口分别记录 `turn_candidates`
与 `cross_track_candidates`。

回归测试见 `services/pipeline/tests/v2/test_candidate_patches.py`，覆盖任务块惰性读取、
尾批次、候选编号与来源、同车子轨迹、读取器异常关闭，以及不同网格大小和
线段对批量下与历史参考实现的候选一致性。完整验证命令为 `make test`。

## 正式 DB Detect 入口（2026-09-29）

正式入口 `_detect_intersections_db` 要求 Refine 列式读取器，使用独立的
`TurnEventRelation` 与 `CrossCandidateRelation` 承接候选；入口和资源释放见
[/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:433](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:433) 。

- 转向事件由 Refine Parquet 读取器执行 DuckDB SQL 窗口和配置阈值计算，按批写入
  `TurnEventRelation`，并更新累计事件数与心跳。调用见
  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:450](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:450) ，
  SQL 入口见
  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/results/read_parquet.py:281](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/trajectories/results/read_parquet.py:281) 。
- 跨轨迹候选由 `run_grid_cross_track_task` 按 Refine 范围和网格任务调度，候选写入
  `CrossCandidateRelation`。网格内点及邻点使用 PostgreSQL 临时命中表和服务端游标
  分批读取；调用见
  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:476](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:476) ，
  读取边界见
  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/fetch_refine_points_in_grid.py:155](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/db/fetch_refine_points_in_grid.py:155) 。
- 聚类后由 Refine 读取器提供拓扑所需子轨迹；正式路径调用见
  [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:480](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/detect_intersections.py:480) 。

对应回归位于 `test_refine_parquet_reader.py`、`test_turn_event_wiring.py` 和
`test_grid_detect_wiring.py`。
