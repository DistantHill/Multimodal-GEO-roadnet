# 跨轨迹网格查询卡顿：原因、修复与验收

后续实现（2026-09-29）：正式 Detect 网格读取已改为单条命名游标直出查询，不再创建临时 hits 表。下文保留 2026-09-25 的事故过程和 Q89 基准记录；其中“流水线仍使用临时 hits 表”描述的是当时实现。本次切换未重新执行索引、结果哈希或耗时复核。

记录日期：2026-09-25。

## 文档摘要

命名游标对网格命中查询选出了错误的嵌套循环计划，造成对 Refine kept 点的重复扫描。实现先将命中点写入事务临时表，再用命名游标读取命中点和邻点。Q89 随后比较了直出 UTM GiST 查询与两段式方案：八个目标的完整结果相同，单轮平均完整读取时间分别为 1.492 秒和 2.557 秒。对比后清理两库派生数据，并将 schema 从 v7 迁移到 v8，将两个来源行键列改为 BIGINT。

下文先记录事故诊断和游标修复，再记录 Q89 对比、清理和迁移。Q89 的直出 UTM 查询是基准对比路径；流水线仍使用临时 hits 表加游标实现。

## 事故诊断与游标修复

记录日期：2026-09-25。诊断环境为 `roadnet_test`，Refine Run `6c14e329-04c7-45d1-87fe-8ab4ff7b767e`，其 `source_table` 为 `public.day01`，处理坐标系为 EPSG:32649。热点列表来自 `scripts/explain_grid_queries.py` 的 `TARGETS`，包含重复执行的 `(3,6)` 和 `(4,7)`。

### 1. 事故结论与根因

卡住的主要原因是 **同一条 SQL 用普通语句和 PostgreSQL 命名游标执行时，基础命中点部分采用了不同的连接计划**。普通语句为 `day01` 和 `kept_trajectory_points` 选择散列连接；命名游标的计划却以空间候选点为外侧，对该 Refine 的大量 kept 点执行内侧扫描，再用 `s.id = k.source_row_key::bigint` 过滤。规划器估计精确坐标过滤后只有 1 个 `day01` 点，因此低估了这种重复扫描的代价。实际空间候选点有数万，重复扫描被大幅放大。流水线停在 `FETCH FORWARD 4096`，因为首次取数还必须完成最终排序所依赖的查询工作。

查询中的前后邻 `LEFT JOIN LATERAL`、相关 `COUNT(*)` 和排序确实增加工作量，但分段实测没有支持“它们单独导致十分钟卡顿”的早期推断。`public.day01` 的识别也是正确的。

### 2. 规划估算偏差与慢查询成因

原实现把空间筛选、与 Refine kept 点连接、前后邻查找和最终排序放进一条交给命名游标的 SQL。`day01.geom` 的 GiST 索引可筛出与反投影包络相交的候选点；随后还要用 `ST_X(ST_Transform(s.geom, 32649))`、`ST_Y(...)` 判断处理坐标系下的精确矩形。现有索引没有为这两个表达式提供选择性统计。以 `(3,6)` 的计划为例，包络索引扫描估算约 23,585 行，精确过滤后的 `Bitmap Heap Scan day01` 却被估算为 **1 行**。

#### 默认范围选择率的实测核对

在 PostgreSQL 16.4 的 `roadnet_test` 上，对同一个 `(3,6)` AABB 逐步增加谓词，只执行只读 `EXPLAIN` 和 `COUNT(*)`，得到以下结果。X/Y 范围都是对同一个 `ST_X(ST_Transform(...))` 或 `ST_Y(ST_Transform(...))` 表达式同时施加上下界。

| `day01` 谓词 | 计划估算行数 | 实际行数 |
| --- | ---: | ---: |
| `geom &&` 反投影包络 | 23,585 | 25,762 |
| 包络 + `X >= xmin` | 7,862 | 未单独计数 |
| 包络 + `xmin <= X <= xmax` | 118 | 25,700 |
| 包络 + `ymin <= Y <= ymax` | 118 | 25,434 |
| 包络 + X 双边范围 + Y 双边范围 | 1 | 25,372 |

`23,585 × 0.005 = 117.925`，与单轴双边范围估算 118 行吻合；再乘一次 `0.005` 得 `0.589625`，计划显示至少 1 行。单个 `X >= xmin` 的 7,862 行约为 23,585 的三分之一。因此 **0.005 是一组双边范围的默认选择率，不是每一条 `>=`、`<=` 谓词各乘一次 0.005**。实际 X 范围保留了包络内约 99.8% 的点，Y 范围约 98.7%，可见默认值在这个网格上严重偏低。[PostgreSQL 16 的规划器默认值源码](https://github.com/postgres/postgres/blob/REL_16_STABLE/src/include/utils/selfuncs.h)定义了 `DEFAULT_RANGE_INEQ_SEL = 0.005`。

统计对象检查也支持这一解释：`pg_stats` 中有 `public.day01.geom` 的列统计，但 `pg_stats_ext_exprs` 对 `public.day01` 返回 0 行，`pg_statistic_ext` 无相关对象，`pg_indexes` 无 `ST_Transform` 表达式索引。仅查询 `pg_stats WHERE tablename='day01' AND attname LIKE '%st_transform%'` 不够全面：`pg_stats` 的 `attname` 是列名；扩展表达式统计应查 `pg_stats_ext_exprs`，并同时核对表达式索引。[PostgreSQL 的 `pg_stats_ext_exprs` 文档](https://www.postgresql.org/docs/16/view-pg-stats-ext-exprs.html)说明了该视图的用途。

#### UTM 表达式空间索引方案的可行性

对当前来源表 `geometry(Point,4326)`，可以为固定的 `(source_table, processing_srid)` 建 `GiST (ST_Transform(geom, 32649))`，然后用投影后的点与 EPSG:32649 的网格矩形做空间相交。此查询直接使用已有 UTM 网格边界，不需要所谓的 `forward_bounds()`；仓库目前也没有这个函数。必须有对应来源表和目标 SRID 的表达式索引，否则单独的 UTM 谓词在当前 `day01` 上被计划为全表顺序扫描。现有修复仍保留 WGS84 GiST 预筛，所以不能在缺少 UTM 索引时直接删掉该条件。

`ST_Transform(geom, 32649) && ST_MakeEnvelope(..., 32649)` **不能单独替代精确 X/Y 判断**。`&&` 只比较几何包围盒；在 PostGIS 3.4 的实测中，人工构造的点即使在网格左边界外 1–30 毫米，仍可能满足 `&&`。六个独立热点网格里，`(7,1)`、`(2,0)`、`(3,1)` 分别出现 6、11、7 个 `&&` 的实际误收点。使用 `ST_Intersects(ST_Transform(s.geom,32649), ST_MakeEnvelope(...,32649))` 才能让表达式 GiST 承担索引预筛，并进行精确相交复核；六个热点网格中，该谓词与原闭区间 X/Y 判断的源点集合差异均为 0。[PostGIS 的 `&&` 文档](https://postgis.net/docs/geometry_overlaps.html)和[`ST_Intersects` 文档](https://postgis.net/docs/ST_Intersects.html)分别说明了包围盒与精确相交的区别。

这个方案值得在固定来源表与 SRID 上做索引构建和实测，当时尚未在 roadnet_test 创建持久 UTM 索引，也未切换生产查询；Q89 后续建立索引并进行了专门对比。项目允许不同来源表和处理 SRID；单独给 `day01` 建一个 32649 索引不能覆盖整个通用查询路径。即使 UTM 索引改善空间命中估算，`source_row_key::bigint` 对 kept 键索引的影响和游标连接顺序仍需在新计划中复查。

同一查询的计划差异如下。这里的数字是 **规划器估算**，不是旧游标完整执行后的实际循环次数：

| 执行方式 | 基础连接 | 关键计划形状 |
| --- | --- | --- |
| 普通 SQL | `Hash Join` | 扫描 Refine kept 点，构建/使用源点散列表，以 `source_row_key::bigint = s.id` 连接 |
| `DECLARE CURSOR FOR ...` | `Nested Loop` | 外侧 `Bitmap Heap Scan day01` 估算 1 行；内侧 `Bitmap Heap Scan kept_trajectory_points` 估算 631,861 行；`s.id = k.source_row_key::bigint` 出现在 `Join Filter` |

由于连接键只是循环后的过滤条件，游标计划没有对每个源点直接用 `day01` 主键或 kept 的 source key 做一次精确索引查找。外侧若有数万候选点，内侧大范围扫描和连接比较会被反复触发。这解释了为何单独数源点很快，完整的命名游标却停在 `FETCH`。数万点本身不是不可处理的规模；**错误连接顺序与重复工作**才是关键。

#### Q89 迁移前：来源键 TEXT 与源表 id BIGINT 的影响

当前 `roadnet_test` 中 `day01.id`、`trajectory_points.id` 均为 `BIGINT`，而 `kept_trajectory_points.source_row_key` 是 `TEXT`。连接使用 `s.id = k.source_row_key::bigint`，每次比较需要对 kept 键做类型转换。更重要的是，现有唯一索引 `(refine_id, source_row_key)` 索引的是原始文本值，不能直接充当 `source_row_key::bigint` 的数值索引。旧游标计划在 kept 点上使用的索引条件只有 `refine_id = ...`，连接键留在 `Join Filter`，这是嵌套循环缺少精确键查找的一个助因。类型不一致不是唯一原因；精确空间过滤的严重低估和游标选出的连接顺序同样关键。

不能仅凭当前两个来源表是整数主键，就把通用列直接改为 `BIGINT`。`validate_source_table()` 允许整数、UUID、`TEXT` 和 `VARCHAR` 主键；`excluded_points.source_row_key`、Python `PointRef.source_row_key` 及 Refine Parquet 的来源键仍按文本处理。若只改 kept 表，来源指纹查询中 kept 与 excluded 键的 `UNION`、读取后的 Python 类型和其他来源表都需要逐一调整。`CREATE TABLE IF NOT EXISTS` 中改声明也不会变更已存在的数据库表；真正改型需要数据校验、迁移 SQL、索引重建和相关代码/测试同步。

还要区分两种行数。之前的 `points_count=25,372`（`(3,6)`）统计的是 `day01` 中通过网格空间过滤的源点；修复后实际返回 **16,202 个 Refine 命中点**，因为结果还要求存在对应的 kept 点，并包含少量 injected 点。两者不能直接等同。普通 SQL 的完整 `EXPLAIN ANALYZE` 对 `(3,6)` 实测约 **4.31 秒、16,202 行**；去掉最终排序的版本约 **4.64 秒**，也说明排序不是这次十分钟卡顿的主因。

### 3. 排查过程

1. 最初在 Debug Console 中试图拦截 `fetch_points_in_grid()` 构造的 SQL。脚本设置 `autocommit=True` 后，命名游标因缺少事务报 `NoActiveSqlTransaction`。后续 trace 拦截又没有可靠地停在 SQL 执行前，数据库端出现 `FETCH FORWARD`，实际启动了慢查询。pydevd 的评估超时警告只是调试表达式运行过久的表现，不能单独判定死锁。
2. 改用独立诊断脚本时，曾使用错误的 Refine ID 或不完整的投影配置，分别遇到 `unknown Refine Run` 和 `coordinate_semantics_version=2` 校验错误。正确环境是 `roadnet_test`，Refine 记录明确指向 `public.day01`；查询没有误读 `trajectory_points`。
3. 结果目录也曾被误判为 root 权限问题。容器里的 `/tmp` 与宿主机工作区并不是同一目录；还需区分“尚未创建结果文件”和“文件位于容器未挂载的路径”。诊断产物随后改放工作区 `generated/`。这属于产物可见性问题，不是 SQL 卡顿成因。
4. 辅助计数脚本早期硬编码 `day01`，并混淆了源几何的 EPSG:4326 与网格的 EPSG:32649。修正后从 `refine_runs.source_table` 读取表名，先反投影网格包络，再按正式查询的处理坐标过滤。所得原始源点计数依次为 25,372、29,321、32,562、41,407、15,250、16,833，重复网格计数相同。
5. 初步 `EXPLAIN` 发现精确坐标过滤被估成 1 行，于是曾把卡顿主要归因于大量 `LATERAL` 邻点查找和相关 `COUNT(*)`。分段实测 `(3,6)`：基础命中计数约 1.16 秒、含 refined 序号约 1.32 秒；对整格命中点做前邻约 2.92 秒、后邻约 2.62 秒、两者一起约 4.57 秒。这个结果推翻了“邻点部分单独运行十几分钟”的判断。
6. 真正关键的对比是 `EXPLAIN ... DECLARE ... CURSOR FOR` 与普通 `EXPLAIN`。后者展示的散列连接计划约 4 秒可完成；前者展示的是会反复扫描 kept 点的嵌套循环。`EXPLAIN ANALYZE` 会实际执行查询，而普通 `EXPLAIN` 只规划；两者都不能自动代表流水线中命名游标的计划。旧命名游标在单次 12 秒 `statement_timeout` 下仍未完成，此前多个残留诊断会话也可能加剧争用，但没有得到旧游标的完整基准耗时。
7. 尝试仅调整 `cursor_tuple_fraction`，游标计划未改变；尝试禁用 nested loop 虽改变了计划，却仍未在 12 秒内完成。因此没有把规划参数开关作为修复。

### 4. 游标修复

`src/track/v2/db/fetch_refine_points_in_grid.py`（原 `db/grid_points.py`）现在把原查询拆成同一事务中的两步：

```text
普通 SQL：CREATE TEMP TABLE cross_track_hits_<uuid> ON COMMIT DROP AS
          WITH hits AS (...) SELECT * FROM hits

命名游标：SELECT h.*, prev.*, nxt.*
          FROM cross_track_hits_<uuid> h
          LEFT JOIN LATERAL (...) prev ON TRUE
          LEFT JOIN LATERAL (...) nxt ON TRUE
          ORDER BY ...
```

普通语句先完成空间过滤和 Refine kept 连接，避免该部分受游标的错误连接计划影响。命名游标只负责按批读取命中点及其前后邻；`batch_size=4096` 的接口、输出列和邻点语义保持不变。临时表只存在于当前连接和事务，`ON COMMIT DROP` 在事务结束时删除。`scripts/explain_grid_queries.py` 也改为在临时表生成后，使用 `EXPLAIN ... DECLARE CURSOR FOR` 查看实际游标阶段计划；输出到 `generated/cross-track-explain/cursor_explain_*.json`。这些 JSON **不含执行时间**，因为该脚本运行的是非 `ANALYZE` 的计划检查。

### 5. 修复后基线验证（Q89 对比前）

在 `roadnet_test` 上通过真实的 `fetch_points_in_grid()` 命名游标路径读取 `TARGETS` 中全部八次网格，每条 SQL 设 15 秒超时，结果如下：

| 次序 | 网格 | Refine 命中行 | 总耗时 |
| ---: | --- | ---: | ---: |
| 1 | `(3,6)` | 16,202 | 2.332 秒 |
| 2 | `(4,7)` | 13,065 | 2.406 秒 |
| 3 | `(7,0)` | 30,138 | 3.237 秒 |
| 4 | `(7,1)` | 28,237 | 3.244 秒 |
| 5 | `(2,0)` | 6,166 | 1.668 秒 |
| 6 | `(3,1)` | 10,503 | 1.830 秒 |
| 7 | `(3,6)` | 16,202 | 2.273 秒 |
| 8 | `(4,7)` | 13,065 | 2.226 秒 |

单格拆分实验中，`(3,6)` 的命中点临时表生成约 0.76 秒，随后游标读取全部 16,202 行约 1.51 秒。`tests/v2/test_grid_points.py` 和 `tests/v2/test_grid_scheduler.py` 的单元与 PostGIS 集成测试共 **6 项通过**。本次没有重跑完整 pipeline，因此验证结论覆盖已定位的网格数据库读取阶段；若流水线后续仍慢，需要单独计时线段生成、相交检测和候选合并。临时表将消耗数据库临时存储，容量随单格命中点数增长。

当前 `/app` 是宿主机工作区挂载，不是只能从镜像读取的代码目录。因此单独修改 PostgreSQL 表结构不需要重建容器；修改挂载的 Python 源码后，运行中的 Python worker 仍需重启才能重新导入代码。是否重建镜像只取决于部署时是否要把代码打包进新镜像。数据库类型迁移本身不能靠 Docker rebuild 完成。

### 6. 事故相关文件

- 实现：`src/track/v2/db/fetch_refine_points_in_grid.py`（由 `db/grid_points.py` 迁移）
- 热点列表和游标计划脚本：`scripts/explain_grid_queries.py`
- 原始源点计数：`generated/cross-track-point-counts/points_counts.json`
- 原始普通 SQL 计划：`generated/cross-track-explain/plain_plan_3_6.json`
- 原始游标计划：`generated/cross-track-explain/cursor_plan_3_6.json`
- 修复后八次游标计划：`generated/cross-track-explain/cursor_explain_*.json`

## Q89 实施、性能复核与数据维护

日期：2026-09-25。

### 网格查询对比

目标 Refine Run：6c14e329-04c7-45d1-87fe-8ab4ff7b767e。索引为
roadnet_test.public.idx_day01_geom_utm32649，定义是对
ST_Transform(geom, 32649) 的 GiST 表达式索引。八个直出查询的
EXPLAIN ANALYZE 计划都实际使用了该索引。

计时使用服务端命名游标完整读取每个结果集，包含执行和全部 fetch。两段式方案
分别计时临时 hits 表创建与后续邻点游标，并报告总和。另行运行的
EXPLAIN ANALYZE 使用普通游标；下表的 Actual Rows × Actual Loops 是计划节点值，
不是命名游标耗时。扫描循环总和统计所有 Node Type 名称含 Scan 的节点；两段式
总和相加 hits 查询和邻点查询的两个完整 EXPLAIN 计划。基线 day01 几何扫描列
分别列出 geom Bitmap Index Scan 与其 Bitmap Heap Scan。完整计划与 buffers
保存在 services/pipeline/generated/cross-track-explain/grid_compare_*.json 和
services/pipeline/generated/cross-track-explain/grid_compare_summary.json 中。

| 目标 | 网格 | 完整结果行 | day01 几何扫描 Actual Rows × Loops（直出 idx_day01_geom_utm32649；基线 idx_day01_geom Bitmap Index / day01 Bitmap Heap） | 扫描节点 Loops 总和（直出 / 两段式） | 直出秒 | hits 创建秒 | 邻点游标秒 | 两段式总秒 |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | (3, 6) | 16,202 | 25,372 × 3；25,762 × 1 / 8,457 × 3 | 174,231 / 174,233 | 1.863 | 1.167 | 1.995 | 3.162 |
| 2 | (4, 7) | 13,065 | 29,321 × 3；29,357 × 1 / 9,774 × 3 | 145,720 / 145,722 | 1.074 | 0.545 | 1.679 | 2.225 |
| 3 | (7, 0) | 30,138 | 32,562 × 3；33,264 × 1 / 10,854 × 3 | 366,910 / 366,912 | 2.724 | 0.745 | 2.823 | 3.569 |
| 4 | (7, 1) | 28,237 | 41,407 × 3；42,026 × 1 / 13,802 × 3 | 344,900 / 344,902 | 2.193 | 0.753 | 2.703 | 3.456 |
| 5 | (2, 0) | 6,166 | 15,250 × 3；17,196 × 1 / 5,083 × 3 | 66,158 / 66,160 | 0.883 | 0.242 | 1.261 | 1.503 |
| 6 | (3, 1) | 10,503 | 16,833 × 3；17,662 × 1 / 5,611 × 3 | 117,000 / 117,002 | 0.772 | 0.260 | 1.608 | 1.867 |
| 7 | (3, 6) | 16,202 | 25,372 × 3；25,762 × 1 / 8,457 × 3 | 174,231 / 174,233 | 1.385 | 0.552 | 1.880 | 2.432 |
| 8 | (4, 7) | 13,065 | 29,321 × 3；29,357 × 1 / 9,774 × 3 | 145,720 / 145,722 | 1.042 | 0.539 | 1.703 | 2.242 |

八组结果的完整有序行哈希均相同，包含命中点、前后邻点、注入点及排序。八组直出查询都使用了目标 GiST 索引且无超时。直出完整读取耗时均低于对应两段式总耗时；本轮测量均值分别为 1.492 秒和 2.557 秒，直出低 41.6%。这是本次单轮测量结果。

### 派生数据与文件清理

性能对比完成并保存计划后，在 roadnet_test 和 roadnet 中清空了相同的
17 张 Refine/Detect/下游派生表。清理前确认外部外键边为零；没有使用
CASCADE。九个已登记的 Refine/Detect Parquet 目录已移除，日志和未登记文件
保持原样。逐表计数、保护表计数和九个目录路径记录在
 services/pipeline/generated/cross-track-explain/cleanup_manifest.json 中。

清理后再次核对：roadnet_test.day01 为 1,000,000 行，
roadnet_test.trajectory_points 为 8 行，roadnet.trajectory_points 为
26,000,000 行；两库的 17 张派生表均为空。

### Schema v8 与验证

新增 v7→v8 事务迁移：先锁定版本表、保留的 Refine Runs 及两个目标表，再检查
旧 TEXT 行键必须是规范十进制形式 0 或无前导零的有符号整数，且处于有符号
64 位范围。迁移也会检查每个保留 Refine Run 的来源表仍存在、具有 id 列且其
类型属于 smallint、integer、bigint。检查通过后将 excluded_points.source_row_key
与 kept_trajectory_points.source_row_key 改为 BIGINT，最后发布版本 8。锁等待
上限为 5 秒；不兼容数据会中止事务并保持 v7。新建 schema 也使用 BIGINT。

在迁移前确认两个数据库均为 v7、目标列均为 TEXT、17 张派生表为空、
无活动写语句，且保护表行数正确。迁移后两库均为 v8，两个目标列均为
BIGINT，派生表仍为空，保护表行数不变。

验证结果：迁移集成用例连接 roadnet_test，8 passed，覆盖 BIGINT 正负边界转换、
正溢出和负溢出、非数字及非规范拼写拒绝、UUID 来源主键拒绝和失败回滚。完整
Pipeline smoke 测试为 1,141 passed、1 skipped。AGENTS 要求的 make test 通过：
Compose 路径测试 5 passed，Backend 83 passed，Pipeline 1,075 passed、67 skipped。
完整计划与结果位于 services/pipeline/generated/cross-track-explain/ 。
