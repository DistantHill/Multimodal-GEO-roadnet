# Detect 网格查询条件复核（2026-09-29）

本记录只针对当前源码与有限数据库计划检查；没有运行 2600 万点全量 Pipeline。正式 Detect 的调用路径是 `run_detect_stage` → `detect_intersections` → `run_grid_cross_track_task` → `fetch_points_in_grid`。只在启用跨轨迹候选时读取这些网格。

| 查询路径 | `WHERE` / 筛选目的 | 复核结论 |
| --- | --- | --- |
| `db/fetch_refine_points_in_grid.py` 源点命中 | `ST_Intersects(ST_Transform(s.geom, processing_srid), ST_MakeEnvelope(xmin, ymin, xmax, ymax, processing_srid))`；同一个 `refine_id` 连接 kept 点 | 用运行的投影坐标系和闭合网格边界；`processing_srid` 先与 Refine Run 核对，再作为整数写入表达式，边界值保持参数化。可匹配对应 SRID 的表达式 GiST。 |
| 同文件注入点命中 | `i.refine_id` 加权威 `i.x/i.y` 的四个闭区间比较 | 保留 XY 边界点；不再用从经纬度逆算的几何包围盒预先排除注入点。 |
| 同文件前后邻点 | `refine_id`、`subtrack_index` 和 `(anchor, kind, point_id)` 的方向比较 | 邻点允许位于网格外；kept 与 injected 两支按 Refine 顺序各取最近一行，再跨两支选最近一行。没有增加网格边界过滤。 |
| `candidates/grid_segments.py` DuckDB 溢出暂存 | `rn = 1` 按五字段线段身份去重；`segment_order >= ? AND segment_order < ?` 分块读取 | 前者保留首次输入，后者为半开页范围；最终排序和去重需覆盖本格线段，不应当作漏加空间条件。 |
| `candidates/cross_track_relation.py` 候选暂存 | `grid_order = ?` 丢弃失败格；`candidate_rank = 1` 按四字段配对身份保留首个代表；`FILTER (WHERE ...)` 检查空值、有限坐标、有效标记和身份 | 各条件作用于已经生成的候选，不是数据库源点的空间命中。无需要随 UTM 改动的条件。 |
| `candidates/query_source_windows.py` 来源窗口 | 全量 extent，以及经纬度包围盒加查询坐标系 XY 条件 | 这是独立的来源窗口读取路径，不是本次 Detect 跨轨迹网格任务。全量 extent 为该路径所需；旧空间条件不作为当前网格查询的证据。 |
| `scripts/only_fetch_count_ingrid.py` 计数辅助脚本 | 同投影 SRID 的 `ST_Intersects` | 已同步索引可用的谓词；只计原始源表点，不含 Refine kept 筛选或 injected，不能作为正式网格命中量。 |
| `scripts/explain_grid_queries.py` 诊断对照 | `utm_direct` 是 UTM 几何相交；`source_bbox` 保留旧经纬度包围盒加投影 XY 条件 | 脚本自行构造 SQL，不调用正式读取函数；旧条件仅用于历史方案对照，不能作为当前生产查询的计划证据。 |

细查确认：kept 与 injected 的序号计数都限定同一 `refine_id`、同一子轨迹，分别对注入锚点使用 `<` 与 `<=`；注入锚点有唯一约束。前后邻点两支均限定同一 Run 和子轨迹，用 `(anchor, kind, point_id)` 比较并各取最近行，再二选一。候选暂存的有效性检查将各个 `OR` 分支单独括号包围，同时单独检查空值；没有发现因括号或有效标记导致的漏筛。线段和候选的最终归并必须处理已收集的整格数据，因此没有额外源点空间条件。

正式库 `roadnet.public.trajectory_points` 和测试库 `roadnet_test.public.trajectory_points` 的 UTM 32649 表达式 GiST 索引在本轮检查之前均已存在并有效；本轮只对两表运行 `ANALYZE` 更新统计，没有新建或重建索引。独立复核从正式函数生成的 SQL，在正式库已有 completed Refine Run 对应的 5 km 代表格使用非执行 `EXPLAIN`：命中点阶段选中 `idx_trajectory_points_geom_utm32649`，邻点阶段使用 kept/injected 子轨迹索引和源点主键。正式库计划的源扫描估计为 77,889 行，Append 估计为 47,470 行，均为规划估计值。测试库实际 Refine Run 的 source 是 `trajectory_points`，同样可选中对应 UTM 索引；测试库没有 `day01` Refine Run，对 `day01` 的代用 SQL 仅能检查索引可用性。空 hits 表的游标计划只检查结构和索引，不代表真实命中行数下的执行时间，也不能据此声称整格性能已验证。真实 PostgreSQL 小夹具验证闭合边界、跨界邻点、注入点、顺序和批量读取：3 项通过。

`make test` 在 Backend 来源指纹夹具失败后停止（106 通过、2 跳过、1 失败）；另行运行 Pipeline 测试为 1210 通过、72 跳过、1 失败。Pipeline 失败项 `test_unversioned_legacy_schema_is_refused_without_reset` 期待错误文本包含 `no legacy in-place migration`，实际为 `no unversioned in-place migration`。已把改动前提交 `b17a0ced` 的 Pipeline 源码与该单项测试解包到临时目录，在同一容器中独立重跑，得到相同失败；该项不是本轮网格修改引入。Backend 失败发生在本轮未修改的来源指纹夹具和验证路径，本轮未更改它。
