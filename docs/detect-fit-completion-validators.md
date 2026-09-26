# Detect / Fitting 完成校验函数审查与停用测试（2026-09-14）

> 当前状态（2026-09-15）：用户已要求停止 2600 万行全量测试，本次重启已正常
> 中断并标记 failed，十张结果表均为空，不再自动续跑。百万行性能、代码修改及
> 现存临时产物见[本次任务汇总](pipeline-validation-summary-20260915.md)。
> 下文“仍在运行”“下一步”等表述为各次记录当时的历史状态。

本文记录两个数据库完成校验函数的来源、实际职责、新旧差异、性能证据以及停用后的测试。
当前执行授权：在 `test`、`roadnet` 停用两个完成校验触发器；先跑
`test/public.day01` 百万行测试，通过后跑 `roadnet/public.trajectory_points`
2600 万行全量测试；问题由本次工作定位、修正并复测。执行结果在文末记录。

## 来源与必要性

函数定义保存在 `services/pipeline/src/track/v2/cli/init_db.py`，由 `init-db`
安装到每个目标数据库。它们不是数据库迁移的一次性检查，新库同样安装，之后在
每次对应 Run 标记完成并提交时自动执行。运行入口只检查 schema 版本号 4，
没有核对函数体版本，因而同为 v4 的数据库可以执行不同版本的校验。

当前这组完成校验在提交 `261a479`（2026-09-11，schema v3）中出现，同处代码
清理六种旧的逐行语义校验触发器；函数注释说明改为聚合扫描。这是可追溯的实现
沿革，不构成明确的用户需求。检索当前设计文档、提交说明和测试，未找到要求
每次提交必须通过数据库常驻函数整批复查的明确需求。代码里的 A80 标记未找到
对应原始需求文本，不能据此证明该实现必需。

提交 `da639ba`（2026-09-14）执行 Clean → Refine 改名；迁移重建已有函数。
提交 `05a1f1a`（2026-09-14）优化两个校验函数并添加分项通知。
这些是版本记录时间，不代表准确编写时间，也不能据此确定各库安装时刻。

当前有 9 个项目自定义触发器函数，`test` 中共挂接 47 个触发器。
本次只停用其中 2 个完成校验触发器；状态、修改和删除保护函数以及主键、
外键、唯一约束等继续保留。函数定义保留用于追溯，触发器停用后不再自动调用。

数据关系应该由业务构造与写入逻辑保证。是否仍需独立审计，应逐项说明能发现
什么实际错误及业务层是否已经保证，不能由“已有函数及对应测试”推导为生产
必需。本次停用实验不等于已经完成全部业务约束的替代实现或必要性审查。

## 两函数的逐项执行逻辑

每段 `SELECT ... INTO violation` 寻找一条错误；找不到则变量为 NULL，有错误
就 `RAISE EXCEPTION`。`LIMIT 1` 限制返回的错误数，不能保证只检查一行；合法
数据通常需要完成扫描、排序或聚合才能确认不存在错误。`RETURN NEW` 返回触发
函数的 Run 行，不是再次写入结果。

| Detect 顺序 | 数据与检查 | 新旧差异及成本 |
| --- | --- | --- |
| 父 Refine | 按 ID 查父 Run 是否 completed | 相同，单行查询 |
| 实际 p/i 集合 | 解析点关联 kept/injected，物化来源、车辆、子轨迹和精炼序号 | 新增父 Refine 范围；物化仍保留 |
| 预期 p/i 集合 | 从父 Refine 组合 kept/injected，按锚点、类型、ID 用 ROW_NUMBER 重建顺序 | 相同，仍需有序扫描或排序 |
| 集合和归属比较 | FULL JOIN 实际与预期集合；BOOL_OR 检查父 Run/子轨迹归属；MIN/MAX 车辆检查混车 | 比较逻辑基本相同，仍为整批检查 |
| 序列连续性 | 依赖唯一约束，用 min/max/count 检查最终和精炼序号连续且从零开始 | 相同，整批分组 |
| 精炼点最终顺序 | LAG 检查精炼顺序中的最终序号严格递增 | 相同；LAG 并非新增优化 |
| 交叉点集合及位置 | 实际交叉点关联线段与完整交叉口；检查预期交叉点是否全部出现在解析序列 | 新增 LEAD 后继表、基表 NOT EXISTS，去除 actual_crossings 强制物化 |
| 被引用的线段起点 | 汇总候选交叉点和跨轨迹两侧的线段起点，检查均有后继 p/i 点 | 第二处 LEAD 替代后继自连接 |
| 跨轨迹车辆 | 两侧线段起点关联来源点，拒绝同子轨迹或同车 | 新增四个父 Refine 范围条件；多表关联仍保留 |
| 转向事件 | 检查事件引用的是 p/i 解析点 | 相同；不重算转向角 |
| 进出观测 | 检查进/出类型 1/0、同候选、同子轨迹、进点先于出点 | 显式拒绝缺失解析端点；关联结构基本相同 |

“交叉点在线段之间”检查的是最终序号区间，不是几何坐标是否在线段上。
两处 `refined_segments` 属于独立 SQL，各自计算，不跨 SQL 共享结果。

| Fitting 顺序 | 数据与检查 | 新旧差异及成本 |
| --- | --- | --- |
| 父 Detect | 按 ID 查父 Detect 是否 completed | 相同 |
| 来源数量 | 中心线 LEFT JOIN 来源清单，分别 COUNT FILTER 正反向记录，与声明数量比较 | 新增父 Detect 范围，聚合结构相同 |
| 来源端点 | 清单关联中心线、两端交叉口、交叉点及解析序列；检查 exit→entry、同子轨迹、顺序、正反方向端点 | 新增父 Detect 范围及解析端点 NULL 检查，多表关联结构相同 |

正向来源应为中心线 A 的出点 → B 的进点；反向来源应为 B 的出点 → A 的进点。
该校验不检查拟合曲线的几何质量。

## 性能从何而来

1. Detect 两处 `refined_sequence + 1` 自连接改为 `LEAD`。旧计划若只按子轨迹
   配对，再逐对过滤序号，长子轨迹可能产生接近平方数量的比较；完整索引查找
   的旧计划并不必然慢。新实现取得顺序后线性推导后继，仍可能支付排序成本。
   等价性依赖前置精炼序号连续性检查，并且先排除交叉点再计算后继。
2. 缺失交叉点检查从关联 `actual_crossings AS MATERIALIZED` 改为对
   `resolved_subtrack_points` 的 `NOT EXISTS`。实际数据库有唯一索引
   `resolved_points_crossing_key(detect_id, crossing_point_id)`。物化一次不代表
   只读取一次；改写提供基表索引或批量反连接的选择，避免重复扫描无索引中间
   结果。不是 NOT EXISTS 关键词本身保证更快。
3. 两函数增加函数局部 `enable_nestloop=off`，抑制新 Run 行数严重误估时的
   退化连接计划。这不绝对禁止嵌套循环，也可能损失本来适合索引查找的计划；
   它是规划偏好，不是复杂度保证。
4. 显式补充父 Refine / Detect 范围，可能帮助缩小批量关联集合；点 ID 已唯一，
   因而不能宣称增加条件一定显著提速。
5. `NOT (...)` 改为 `(...) IS NOT TRUE`、显式检查缺失解析端点属于正确性修正。
   前者处理 SQL NULL 的三值逻辑；后者防止两个缺失端点都为 NULL 而漏检。
   这些改动没有提速证据。
6. RAISE NOTICE 只把当前检查项传给客户端心跳，不减少计算量。

参考 PostgreSQL 16 的[连接规划参数](https://www.postgresql.org/docs/16/runtime-config-query.html#GUC-ENABLE-NESTLOOP)、
[CTE 物化](https://www.postgresql.org/docs/16/queries-with.html#QUERIES-WITH-CTE-MATERIALIZATION)、
[NULL 比较](https://www.postgresql.org/docs/16/functions-comparison.html)。

## 版本与实测证据

只读核查确认：`test` 的两个旧函数体与 `05a1f1a^` 一致，没有 LEAD、分项
NOTICE 或函数局部规划设置；`roadnet` 已安装优化版。源代码更新不会自动覆盖
已有数据库函数，这导致 test 继续执行旧 SQL。

- 旧版：日志 `20260914_052905_153099_pid-1_log.txt` 在 6557.4 秒仍记录提交
  心跳，无完成记录；后来只读采样已无活跃提交会话，不能据此认定成功。
- 优化版：百万行源数据、679649 解析点、393 候选/292 完整交叉口的回滚测试，
  强制执行 Detect 全部延迟约束耗时 15.8378 秒。记录位于
  `/tmp/roadnet_detect_validation_smoke_v2.log`，脚本为
  `/tmp/roadnet_detect_validation_smoke.py`。
- 15.8378 秒不包含最终 COMMIT，也不是 Fitting 实测。没有逐项开关实验，不能
  把改善分摊给单项改动，更不能据此断言 2600 万行下可线性扩展。

## 精确新旧差异

以下为数据库旧函数对应提交与优化版的差异；未列出的语句保持原样。

### validate_detect_completion_function

```diff
--- old:05a1f1a^
+++ new:05a1f1a
@@ -1,6 +1,11 @@
 CREATE OR REPLACE FUNCTION validate_detect_run_completion()
 RETURNS trigger
 LANGUAGE plpgsql
+-- Completion inspects an entire newly written run. Its statistics
+-- may still estimate one row: nested joins can then repeatedly scan
+-- that run even when unique indexes exist. Prefer set-based joins
+-- only inside this function; PostgreSQL restores the caller setting.
+SET enable_nestloop = off
 AS $function$
 DECLARE
     violation TEXT;
@@ -14,6 +19,8 @@
         RAISE EXCEPTION
             'completed Detect Run requires a completed parent Refine Run';
     END IF;
+
+    RAISE NOTICE 'track_v2 validation: Detect：校验 Refine 点集合与车辆归属';

     -- This is intentionally one aggregate scan, not one query per
     -- resolved row.  It validates parent-Refine membership, subtrack
@@ -39,8 +46,10 @@
           FROM resolved_subtrack_points AS resolved
           LEFT JOIN kept_trajectory_points AS kept
             ON kept.id = resolved.kept_point_id
+           AND kept.refine_id = NEW.refine_id
           LEFT JOIN injected_points AS injected
             ON injected.id = resolved.injected_point_id
+           AND injected.refine_id = NEW.refine_id
          WHERE resolved.detect_id = NEW.id
            AND resolved.crossing_point_id IS NULL
     ), expected_sources AS (
@@ -114,6 +123,8 @@
         RAISE EXCEPTION '%', violation;
     END IF;

+    RAISE NOTICE 'track_v2 validation: Detect：校验连续点序列';
+
     -- UNIQUE constraints prevent duplicates; min/max/count therefore
     -- prove that both snapshots are dense zero-based sequences.
     SELECT 'resolved subtrack refined/final sequence must be contiguous from zero'
@@ -131,6 +142,8 @@
     IF violation IS NOT NULL THEN
         RAISE EXCEPTION '%', violation;
     END IF;
+
+    RAISE NOTICE 'track_v2 validation: Detect：校验精炼点顺序';

     SELECT
         'resolved p/i final sequence must strictly increase with refined sequence'
@@ -151,10 +164,27 @@
         RAISE EXCEPTION '%', violation;
     END IF;

+    RAISE NOTICE 'track_v2 validation: Detect：校验交叉点集合与线段位置';
+
     -- The resolved crossing set must exactly equal the crossing set
     -- of complete intersections.  The same join also validates that
     -- every crossing inherits its refined segment's subtrack.
-    WITH actual_crossings AS MATERIALIZED (
+    -- Use the indexed base table for missing crossings. Rejoining a materialized CTE here
+    -- can repeatedly scan the entire unindexed crossing set when a
+    -- freshly inserted run is underestimated by the planner.
+    -- Derive successors in one ordered pass. A self join on
+    -- (subtrack, refined_sequence + 1) can become a merge join on
+    -- subtrack alone, comparing every pair in long subtracks.
+    WITH refined_segments AS (
+        SELECT id, subtrack_index, sequence_in_subtrack,
+               LEAD(sequence_in_subtrack) OVER (
+                   PARTITION BY subtrack_index
+                   ORDER BY refined_sequence_in_subtrack
+               ) AS successor_sequence
+          FROM resolved_subtrack_points
+         WHERE detect_id = NEW.id
+           AND refined_sequence_in_subtrack IS NOT NULL
+    ), actual_crossings AS (
         SELECT resolved.id AS resolved_id,
                resolved.crossing_point_id,
                resolved.subtrack_index,
@@ -162,20 +192,14 @@
                crossing.id AS crossing_id,
                segment_start.subtrack_index AS source_subtrack,
                segment_start.sequence_in_subtrack AS segment_start_sequence,
-               successor.sequence_in_subtrack AS successor_sequence,
+               segment_start.successor_sequence,
                complete_intersection.id AS complete_intersection_id
           FROM resolved_subtrack_points AS resolved
           LEFT JOIN candidate_entry_exit_points AS crossing
             ON crossing.id = resolved.crossing_point_id
            AND crossing.detect_id = resolved.detect_id
-          LEFT JOIN resolved_subtrack_points AS segment_start
+          LEFT JOIN refined_segments AS segment_start
             ON segment_start.id = crossing.segment_start_point_id
-           AND segment_start.detect_id = crossing.detect_id
-          LEFT JOIN resolved_subtrack_points AS successor
-            ON successor.detect_id = segment_start.detect_id
-           AND successor.subtrack_index = segment_start.subtrack_index
-           AND successor.refined_sequence_in_subtrack =
-               segment_start.refined_sequence_in_subtrack + 1
           LEFT JOIN intersections AS complete_intersection
             ON complete_intersection.detect_id = crossing.detect_id
            AND complete_intersection.candidate_intersection_id =
@@ -199,10 +223,10 @@
                            THEN 'resolved crossing does not exist'
                        WHEN complete_intersection_id IS NULL
                            THEN 'resolved crossing is not attached to a complete intersection'
-                       WHEN NOT (
+                       WHEN (
                            segment_start_sequence < sequence_in_subtrack
                            AND sequence_in_subtrack < successor_sequence
-                       ) THEN
+                       ) IS NOT TRUE THEN
                            'resolved crossing must lie between its refined segment endpoints'
                        ELSE 'resolved crossing belongs to the wrong subtrack'
                    END AS issue
@@ -210,23 +234,37 @@
              WHERE crossing_id IS NULL
                 OR complete_intersection_id IS NULL
                 OR source_subtrack IS DISTINCT FROM subtrack_index
-                OR NOT (
+                OR (
                     segment_start_sequence < sequence_in_subtrack
                     AND sequence_in_subtrack < successor_sequence
-                )
+                ) IS NOT TRUE
             UNION ALL
             SELECT 'complete-intersection crossing is absent from resolved sequence'
               FROM expected_crossings AS expected
-              LEFT JOIN actual_crossings AS actual
-                ON actual.crossing_point_id = expected.id
-             WHERE actual.resolved_id IS NULL
+             WHERE NOT EXISTS (
+                 SELECT 1
+                   FROM resolved_subtrack_points AS actual
+                  WHERE actual.detect_id = NEW.id
+                    AND actual.crossing_point_id = expected.id
+             )
            ) AS invalid
      LIMIT 1;
     IF violation IS NOT NULL THEN
         RAISE EXCEPTION '%', violation;
     END IF;

-    WITH segment_uses AS (
+    RAISE NOTICE 'track_v2 validation: Detect：校验线段端点';
+
+    WITH refined_segments AS (
+        SELECT id,
+               LEAD(id) OVER (
+                   PARTITION BY subtrack_index
+                   ORDER BY refined_sequence_in_subtrack
+               ) AS successor_id
+          FROM resolved_subtrack_points
+         WHERE detect_id = NEW.id
+           AND refined_sequence_in_subtrack IS NOT NULL
+    ), segment_uses AS (
         SELECT 'candidate crossing'::TEXT AS kind,
                crossing.segment_start_point_id AS point_id
           FROM candidate_entry_exit_points AS crossing
@@ -247,20 +285,15 @@
            )
       INTO violation
       FROM segment_uses AS segment_use
-      LEFT JOIN resolved_subtrack_points AS segment_start
+      LEFT JOIN refined_segments AS segment_start
         ON segment_start.id = segment_use.point_id
-       AND segment_start.detect_id = NEW.id
-      LEFT JOIN resolved_subtrack_points AS successor
-        ON successor.detect_id = segment_start.detect_id
-       AND successor.subtrack_index = segment_start.subtrack_index
-       AND successor.refined_sequence_in_subtrack =
-           segment_start.refined_sequence_in_subtrack + 1
-     WHERE segment_start.refined_sequence_in_subtrack IS NULL
-        OR successor.id IS NULL
-     LIMIT 1;
-    IF violation IS NOT NULL THEN
-        RAISE EXCEPTION '%', violation;
-    END IF;
+     WHERE segment_start.successor_id IS NULL
+     LIMIT 1;
+    IF violation IS NOT NULL THEN
+        RAISE EXCEPTION '%', violation;
+    END IF;
+
+    RAISE NOTICE 'track_v2 validation: Detect：校验跨轨迹车辆归属';

     SELECT 'track cross segments must belong to different vehicles/subtracks'
       INTO violation
@@ -273,12 +306,16 @@
        AND to_point.detect_id = track_cross.detect_id
       LEFT JOIN kept_trajectory_points AS from_kept
         ON from_kept.id = from_point.kept_point_id
+       AND from_kept.refine_id = NEW.refine_id
       LEFT JOIN injected_points AS from_injected
         ON from_injected.id = from_point.injected_point_id
+       AND from_injected.refine_id = NEW.refine_id
       LEFT JOIN kept_trajectory_points AS to_kept
         ON to_kept.id = to_point.kept_point_id
+       AND to_kept.refine_id = NEW.refine_id
       LEFT JOIN injected_points AS to_injected
         ON to_injected.id = to_point.injected_point_id
+       AND to_injected.refine_id = NEW.refine_id
      WHERE track_cross.detect_id = NEW.id
        AND (
            from_point.subtrack_index IS NOT DISTINCT FROM
@@ -292,6 +329,8 @@
         RAISE EXCEPTION '%', violation;
     END IF;

+    RAISE NOTICE 'track_v2 validation: Detect：校验转向事件';
+
     SELECT 'turn event point must be a refined p/i resolved point'
       INTO violation
       FROM turn_events AS turn_event
@@ -305,7 +344,11 @@
         RAISE EXCEPTION '%', violation;
     END IF;

+    RAISE NOTICE 'track_v2 validation: Detect：校验进出观测';
+
     SELECT CASE
+               WHEN entry_resolved.id IS NULL OR exit_resolved.id IS NULL
+                   THEN 'entry/exit endpoints must exist in resolved sequence'
                WHEN entry_crossing.crossing_type IS DISTINCT FROM 1
                     OR exit_crossing.crossing_type IS DISTINCT FROM 0
                    THEN 'entry/exit crossing types must be 1/0'
@@ -344,6 +387,8 @@
                 IS DISTINCT FROM intersection.candidate_intersection_id
            OR exit_crossing.candidate_intersection_id
                 IS DISTINCT FROM intersection.candidate_intersection_id
+           OR entry_resolved.id IS NULL
+           OR exit_resolved.id IS NULL
            OR entry_resolved.subtrack_index
                 IS DISTINCT FROM exit_resolved.subtrack_index
            OR entry_resolved.sequence_in_subtrack >=
```

### validate_fit_completion_function

```diff
--- old:05a1f1a^
+++ new:05a1f1a
@@ -1,6 +1,11 @@
 CREATE OR REPLACE FUNCTION validate_fit_run_completion()
 RETURNS trigger
 LANGUAGE plpgsql
+-- Completion inspects an entire newly written run. Its statistics
+-- may still estimate one row: nested joins can then repeatedly scan
+-- that run even when unique indexes exist. Prefer set-based joins
+-- only inside this function; PostgreSQL restores the caller setting.
+SET enable_nestloop = off
 AS $function$
 DECLARE
     violation TEXT;
@@ -15,6 +20,8 @@
             'completed Fit Run requires a completed Detect Run';
     END IF;

+    RAISE NOTICE 'track_v2 validation: Fitting：校验来源片段数量';
+
     -- Counts are relative to the persisted centerline direction:
     -- false manifests are forward evidence and true manifests are
     -- reverse evidence.  LEFT JOIN also exposes a missing manifest.
@@ -26,6 +33,7 @@
         ON manifest.centerline_id = centerline.id
        AND manifest.detect_id = centerline.detect_id
      WHERE centerline.fit_id = NEW.id
+       AND centerline.detect_id = NEW.detect_id
      GROUP BY centerline.id,
               centerline.forward_clip_count,
               centerline.reverse_clip_count
@@ -40,7 +48,11 @@
         RAISE EXCEPTION '%', violation;
     END IF;

+    RAISE NOTICE 'track_v2 validation: Fitting：校验来源片段端点';
+
     SELECT CASE
+               WHEN start_resolved.id IS NULL OR end_resolved.id IS NULL
+                   THEN 'centerline clip endpoints must exist in resolved sequence'
                WHEN start_crossing.crossing_type IS DISTINCT FROM 0
                     OR end_crossing.crossing_type IS DISTINCT FROM 1
                    THEN 'centerline clip must start at exit and end at entry'
@@ -78,9 +90,12 @@
         ON end_resolved.crossing_point_id = end_crossing.id
        AND end_resolved.detect_id = manifest.detect_id
      WHERE centerline.fit_id = NEW.id
+       AND centerline.detect_id = NEW.detect_id
        AND (
            start_crossing.crossing_type IS DISTINCT FROM 0
            OR end_crossing.crossing_type IS DISTINCT FROM 1
+           OR start_resolved.id IS NULL
+           OR end_resolved.id IS NULL
            OR start_resolved.subtrack_index
                 IS DISTINCT FROM end_resolved.subtrack_index
            OR start_resolved.sequence_in_subtrack >=
```

## 停用方式及维护边界

在 test 和 roadnet 分别执行以下两个精确的触发器停用操作，不使用 DISABLE
TRIGGER ALL，也不修改 session_replication_role：

```sql
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE public.detect_runs DISABLE TRIGGER detect_completion_check;
ALTER TABLE public.fit_runs DISABLE TRIGGER fit_completion_check;
COMMIT;
```

验证 `pg_trigger.tgenabled = 'D'`；其余项目触发器状态应与操作前一致。
函数体保留，完成校验不再在提交时自动执行。源数据不做修改。
如将来重新运行现有 init-db，代码会重建触发器并恢复启用；本次运行不得调用
init-db。这里记录的是已安装数据库状态，尚未改变初始化代码的默认行为。
恢复方式是对上述两个触发器分别执行 ENABLE TRIGGER，且应在另行授权后进行。

## 本次测试执行记录

已在 `test` 和 `roadnet` 提交停用操作，并逐项核对操作前后全部项目触发器：
每库两个完成校验触发器的 `tgenabled` 均由 O 变为 D，其余 45 个项目触发器
状态完全一致。审计快照：`/tmp/roadnet-validate-disable-audit-20260914.json`。

百万行源表实际 `COUNT(*) = 1000000`。复用最近已完成 Refine
`bcac9210-3dd4-497a-ac64-8b872deafa1c`，继承 EPSG:32649；其来源指纹 SHA256
为 `6f49bc6263660f344dd135977e0cc45013a1d971b340c22dd32cdad8a55d40df`。
保留用户当前 `stream_batch_size = 1000` 配置。本次从 Detect 执行到 Network，
不会重做 Refine。命令：

```sh
python -u -m pipeline run --database test \
  --refine-id bcac9210-3dd4-497a-ac64-8b872deafa1c \
  --stats-json /audit/test-1m-stats.json
```

首次停用后的百万行运行日志：`/tmp/roadnet-smoke-artifacts-20260914/test-1m.log`，
Pipeline 日志 `20260914_084041_002702_pid-1_log.txt`。新 Detect
`ba3de7d2-98e3-451b-afa5-b74162ac6ee4` 已实际提交，数据库状态核验为 completed：

| 指标 | 首次运行 |
| --- | ---: |
| Detect 计算（含来源指纹检查） | 109.279 秒 |
| Detect 持久化（含实际提交） | 39.074 秒 |
| Detect 阶段总时长 | 148.400 秒 |
| 候选 / 完整交叉口 | 393 / 292 |
| 解析点 | 679649 |
| 候选进出点 / 进出观测 | 83947 / 31207 |

随后 Clip 解析点读取持续数分钟。只读 EXPLAIN 保存于
`/tmp/clip_baseline_explain.out`：按车辆数过滤的子轨迹聚合结果估计只有 36 条，
最终结果估计 3383 行，实际本次解析点 679649 行；多层嵌套循环以及先聚合
再读解析点的结构得到确认。没有执行 EXPLAIN ANALYZE，不能把耗时精确归因
到单个节点。主 agent 对自己启动的测试发送 SIGINT，结束旧 Clip 查询，保留
已提交 Detect 作对照；该首次运行没有完成百万行全流程。

代码修正拆分给三个 subagent：Detect 磁盘工作区和空间查询；按子轨迹流式
持久化；Clip/Fitting 流式读取与磁盘暂存。此前26M已有全量点工作区内存失败
证据，修正必须先通过小型语义回归和百万行实测，再用于26M验收。
通过百万行测试后再执行 roadnet 2600 万行测试。代码修正由 subagent 实施，
主 agent 负责规划、运行测试和结果验收。

### 百万行修正后全流程

复用同一 Refine，Detect `43a58c97-1f20-459e-a499-135cea67e450`、Fitting
`3cb7db23-af38-46a0-a9e9-93ea47b0f7c8` 均已实际提交为 completed。
命令退出码 0，完整执行到 Network，总计 **1052.818 秒**。

| 指标 | 实测 |
| --- | ---: |
| Detect 计算 | 924.152 秒 |
| 其中跨轨迹计算 | 700.4 秒；27,800,109 个包围盒线段对 |
| 其中拓扑计算 | 147.503 秒 |
| Detect 持久化，含实际提交 | 47.165 秒 |
| 其中解析序列写入 | 36.408 秒 |
| Clip，含来源指纹检查 | 37.036 秒 |
| Fitting 计算 | 39.864 秒 |
| 候选 / 完整交叉口 | 393 / 292 |
| 转向 / 跨轨迹事件 | 56,178 / 58,660 |
| 解析点 / 候选进出点 / 进出观测 | 679,649 / 83,947 / 31,207 |
| 全部片段 / 交叉口间片段 | 35,005 / 23,797 |
| 中心线，含三个方向变体 | 1,506 |
| Network 节点 / 边 | 292 / 556 |

日志为 `20260914_085532_553162_pid-1_log.txt`，控制台日志和统计位于
`/tmp/roadnet-smoke-artifacts-20260914/test-1m-fixed.log`、
`test-1m-fixed-stats.json`。运行中采到 Pipeline 内存 591.1 MiB；最后一次
控制组峰值读取为 670,216,192 B，OOM 事件为零。这些是运行中的采样，容器
结束后已自动删除，不能把该数值宣称为整个流程最终峰值。

主 agent 另执行只读语义对照，50.11 秒完成：逐表读取、在本地临时 SQLite
归一化运行生成的 ID，八类排序 SHA-256 与基线均一致。覆盖候选、完整交叉口、
交叉点来源与精确坐标、解析点最终序列、归一化 ID 分配顺序、转向与跨轨迹引用、
进出观测端点。未调用数据库完成校验函数。数据库不保存 segment_fraction，
因此只能比较稳定线段来源、坐标与最终插入位置，不能声称直接比较了该字段。
脚本和结果为同一目录的 `compare_detect_semantics.py`、
`detect-semantic-compare.log`。

### 修正范围与性能边界

- Detect 的完整轨迹和线段暂存到运行私有的磁盘工作区，按空间包围盒调度。
  精确求交、聚类、拓扑业务规则不变；完整子轨迹保持方向上下文。
- 持久化改为命名游标读取、分批分配 ID 与 COPY，只保存结果实际引用的点映射。
  p/i 先分配 ID、c 后分配 ID，最终序列语义经上述对照验证。
- Clip 删除车辆整批聚合，逐完整子轨迹读取；混车明确报错。片段可重复遍历、
  按 ID 读取，暂存在磁盘，供全部拟合方向和来源清单使用。
- Fitting 按交叉口对暂存，逐片段投影，采样矩阵使用磁盘映射，分块计算精确
  中位数。正常退出和异常均清理临时文件。
- 实测暴露的短作用域心跳改为长期读取、拓扑候选、解析序列写入和拟合循环的
  累计状态；读取行、片段、交叉口对分别使用明确单位。

百万行磁盘实现存在明显跨轨迹性能退化，不把降低内存等同于性能全面达标。
该运行启动后另修正了命中前反复解码点对象的开销：先运行同一数值求交内核，
仅命中后加载来源。在同批合成数据中，4.218 → 2.238 秒、来源点解码
64,945 → 4,483 次，5,650 个候选完整摘要相同；这不是百万行新版本耗时。
运行中的真实工作区因写锁不能安全进行只读抽样，未采用不一致文件副本的结果。
磁盘调度与剩余全局候选内存边界见 [候选调度说明](detect-candidate-patches.md)。

所有上述修正及心跳回归后，`make test`：Backend 74 项通过，Pipeline
1025 项通过、51 项跳过。真实 PostgreSQL 的游标/COPY/回滚集成用例另已通过。

### 2600 万行全量运行

百万行全流程和语义对照通过后，已启动 roadnet 全量测试，复用最近完成的
Refine `f1986249-8382-4d00-972f-900b6e81ea43`。来源为
`public.trajectory_points`，指纹 row_count 为 26,000,000，EPSG:32649，SHA-256
为 `a7e8ee0619042614d8a6ebb4f2feea7a964b1dd1a9e88f7a4250c05b34274bb7`。
开始前再次确认 roadnet 两个完成校验触发器为 D。

主机约 6.7 GiB 内存且 swap 已满，本次测试容器通过临时 Compose 配置设置
2500 MiB 内存上限、同值 memswap_limit（禁用该容器 swap）。此为实测运行
配置，不修改项目默认配置，也不代表已经验证了全量内存上限。
容器 `roadnet-smoke-no-validate-26m-20260914` 保留退出状态以供验收。
本次 Detect ID 为 `1fa24637-0711-4a67-a81a-19a51f5e422d`，Pipeline 日志
`20260914_091620_730996_pid-1_log.txt`。
日志为 `/tmp/roadnet-smoke-artifacts-20260914/roadnet-26m.log`；成功统计应输出
到同目录 `roadnet-26m-stats.json`。

该次全量测试已由主 agent 中断，退出码 130，未完成 Detect。完整读取了
874,088 条子轨迹、产生 1,428,473 个转向事件；跨轨迹阶段处理到
17,678,336 个包围盒线段对、32,936 个候选，耗时 937 秒，吞吐仍然偏低。
停止前控制组峰值为 2,515,591,168 B，OOM 事件为零；这是性能原因中断，
不能描述为内存失败或全量通过。停止后数据库无残留连接，十张结果表均为
零行，Detect 已标记 failed，复用的 Refine 和源数据未修改。

subagent 随后在空间索引中加入保守方向粗桶，精确角度及求交仍由原内核
判断；异常方向采用全桶兜底，并释放跨批次及跨阶段不再需要的轨迹元数据。
完整回归为 Backend 74 项通过、Pipeline 1050 项通过、51 项跳过。
新的百万行实测日志为 `test-1m-heading.log`，使用同一 Refine 和上述
2500 MiB 测试容器上限；必须先核验全流程及对照结果，再重跑全量。

### 方向粗筛百万行复测与 Clip 计划问题

新 Detect `2786875d-9a19-4aa4-9a00-ebdce36774c4` 已完成提交。
跨轨迹阶段 30.3 秒，粗筛后 521,764 个线段对、58,660 个候选；Detect
计算合计 253.538 秒，拓扑 148.640 秒，仍为 393 个候选、292 个完整交叉口。
此轮同时包含命中后来源解码和方向粗筛，不能把全部改善单独归因于方向索引。
只读八类结果对照 49.905 秒完成，完整排序摘要与原基线一致，日志为
`detect-heading-semantic-compare.log`。

后续 Clip 首次 FETCH 持续超过 251 秒，数据库持续使用 CPU、没有锁等待。
主 agent 中断该次测试，退出码 130；已提交 Detect 保留，尚无 Fitting Run，
数据库无残留连接。控制组峰值为 716,107,776 B，OOM 事件为零。
这轮只完成 Detect 验收，不能写作百万行全流程通过。

subagent 比较了普通 SELECT 和实际命名游标形式的 EXPLAIN：前者为哈希连接，
后者为多层嵌套循环。PostgreSQL 默认按预计消费 10% 的结果规划游标；
`cursor_tuple_fraction=1` 才按完整消费考虑总成本，见
[PostgreSQL 16 查询规划文档](https://www.postgresql.org/docs/16/runtime-config-query.html#GUC-CURSOR-TUPLE-FRACTION)。
此前普通 SELECT 的 EXPLAIN 不能证明实际游标采用同一计划。

同时，原 FETCH 始于 10:21:28 UTC，而解析点和交叉点表自动统计分别发生于
10:22:03、10:22:05，因此当前 EXPLAIN 不能还原原声明时的计划。
未知 Run ID 的低估模拟暴露另一风险：交叉口连接可能只按 detect_id 查询，
再逐候选过滤，形成重复整批扫描。修正由 subagent 实施，主 agent 待真实
游标消费及新运行测试后补充验收结果。

Clip 修正后在上述两个已提交 Run 上完整消费游标，分别为 12.102 秒和
10.060 秒；首条完整子轨迹分别在 5.991 秒和 4.256 秒返回。两次均为
34,281 条子轨迹、679,649 个点、63,771 个序列内交叉点，车辆、子轨迹顺序、
坐标、穿越类型和关联完整交叉口几何的排序摘要一致。此处序列内交叉点数量
不同于所有候选进出点数量。两次外部 cursor_tuple_fraction 均恢复为 0.1。
读取映射的内存为 O(完整交叉口数量)，本次 292 项，不保存全部解析点。
证据归档于实测目录的 `clip_fixed_real_read.log` 和 `check_clip_cursor_read.py`。

同类审查覆盖两库五条业务查询的普通 SELECT、默认 DECLARE、完整消费 DECLARE，
共三十份计划，保存于 `/tmp/cursor-plan-review/`。来源指纹读取在两库、原始
来源读取在 roadnet 存在同类规划差异，统一接入 `db/plan_full_scan_cursor.py` 的声明助手。
助手保存原设置，在保存点内临时按完整消费规划，声明后恢复；不改变超时。
精炼成员及 Detect 精炼轨迹查询未发现该设置导致的计划差异，未扩大修改。
计划成本不等于实际耗时，不把当前统计计划当作原卡住查询的执行记录。

真实 PostgreSQL 定向测试 12 项通过，覆盖游标完整消费、全新非自动提交连接、
声明失败时已有事务写入和外部配置保留。完整回归 Backend 74 项通过，Pipeline
1054 项通过、54 项跳过。新百万行全流程日志为 `test-1m-cursor.log`，仍复用
原 Refine；必须完成此轮验收后才再次启动 2600 万行。

### 游标修正后的百万行全流程验收

此轮全流程已通过，退出码 0。Detect `43aa8a80-86c5-4fb6-9c0f-ebe805a85c76`、
Fitting `5aec5fa1-f147-45d8-b523-1ba4cf371439` 均经数据库确认 completed。
总耗时 **377.738 秒**：Detect 计算 253.752 秒、持久化及提交 51.116 秒、
Clip（含来源指纹）35.375 秒。刚提交的新 Detect 成功进入并完成 Clip。
仍为 393 个候选、292 个完整交叉口，35,005 个片段、1,506 条中心线（含
方向变体），Network 292 个节点、556 条边、12 个孤立节点。
统计为 `test-1m-cursor-stats.json`；全部整数统计与此前百万行成功结果一致。
容器退出状态 OOMKilled=false；未获取该轮完整控制组峰值，不以采样替代峰值。

通过后已再次启动全量，容器 `roadnet-smoke-no-validate-26m-heading-20260914`，
仍复用 `f1986249-8382-4d00-972f-900b6e81ea43`，使用相同测试容器上限。
开始前确认 roadnet 两个触发器仍为 D。日志为 `roadnet-26m-heading.log`，
成功统计输出到 `roadnet-26m-heading-stats.json`。容器内包装脚本在 Pipeline
退出后记录控制组峰值和 OOM 事件，再以 Pipeline 原退出码结束。

### 全量实测后收敛游标规划修正

上述全量进程启动时，来源指纹仍使用完整消费规划。随后发现真实回归：
旧默认游标在 30 秒心跳已有 13 万行，本次直到 240.3 秒才有 15 万行。
两轮可见消费期吞吐约 5.95 万和 5.93 万行/秒，额外首批等待没有被后续
读取收益抵消。旧完整指纹约 464.6 秒（由下一阶段日志反推，非精确函数计时）。
该轮指纹已完成，阶段转换反推约 674.0 秒，增加约 209.5 秒（45.1%）。
当前计划显示新设置增加宽来源结果的哈希和排序；这与等待增量相符，但没有
EXPLAIN ANALYZE，不能将各节点实际耗时精确归因。

因此撤回来源指纹的声明助手接入：其默认嵌套循环按来源主键唯一查找，并非
Clip 重复扫描整批交叉口的风险。Parser 本轮因复用 Refine 未执行，其仅依据
成本估算添加的规划改动也撤回。最终声明助手只用于已通过真实消费验收的 Clip；
来源指纹和 Parser 恢复原声明方式并尊重外部设置。公共助手异常与事务测试保留。
当前全量进程不热加载改动，仍包含启动时指纹规划设置，不能将其耗时标作
撤回后的性能结果。语义及来源指纹计算规则均未改变。
收敛后的完整回归仍为 Backend 74 项通过、Pipeline 1054 项通过、54 项跳过；
真实 PostgreSQL 定向测试 12 项再次通过。全量来源指纹检查通过，已进入
轨迹读取阶段，后续结果继续记录。

全量完整读取及工作区构建为 2145.4 秒，仍为 874,088 条轨迹、1,428,473 个
转向事件，读取阶段未发生 OOM。随后跨轨迹查询 CPU 利用偏低，磁盘读取
持续增长。源码及 SQLite 计划确认，轨迹和线段按 ID 查找仍访问包含载荷的
宽表，命中前不解码对象并不代表无需读取载荷所在页。

第一轮独立布局基准与全量并发，未设置额外容器资源上限。期间主机内存和 I/O
压力上升，但未获取该基准峰值，不能把全部压力归因于它；该基准最终正常
退出，退出码 0，没有已确认的 OOM。其并发耗时不用于性能结论。

主 agent 随后暂停全量并保留进程及工作区，隔离运行有硬限制的对照：256 MiB
内存、同值 memswap、1 核、无网络、180 秒以内、最多十万个候选。
两种布局均生成 35,126 个宽筛线段对、18,190 个候选，完整摘要一致。
原版提取 7.825 秒、读盘 845.59 MB；覆盖索引版本 2.973 秒、109.23 MB，
工作区大小 66.60 MB → 67.58 MB。该基准退出码 0，控制组峰值
247,877,632 B，OOM 及上限事件均为零，日志为 `roadnet_io_layout_bench_capped.log`。
基准完成后全量进程已恢复。暂停及主机资源竞争期间计入的墙钟时间必须与
正常运行耗时区分，不能直接用于计算全量提速倍数。

覆盖索引修正由 subagent 实施，尚待真实重跑；当前全量进程仍使用启动版本。

该次全量进程随后由主 agent 正常中断，退出码 130，临时工作区清理完毕。
停止前累计 884,736 个宽筛线段对、114,935 个跨轨迹候选，心跳墙钟
1692 秒包含暂停及资源竞争，不能作为隔离吞吐结果。包装脚本记录控制组
峰值 2,172,612,608 B，上限及 OOM 事件均为零。十张结果表为空，Detect
`0e691c5d-dcd9-4f4e-8865-e9c62c728690` 已标记 failed；Refine 和来源保持不变。
这次仍未完成 Detect，不作全量通过结论。

### 覆盖索引版本百万行验收

`test-1m-cover.log` 全流程退出码 0，总耗时 371.676 秒。Detect
`7e6654eb-e1c8-4674-9915-f6510257c846` 和 Fitting
`60dba9b7-738c-4c21-b252-c85f42722011` 均确认 completed，30 项整数统计
与最初百万行成功结果相同。跨轨迹 29.2 秒，Detect 计算 255.952 秒、
持久化及提交 45.203 秒、Clip 34.128 秒。百万行跨轨迹耗时与覆盖索引前接近，
不能借此宣称全量已提速。

包装脚本记录完整控制组峰值 774,799,360 B，触及上限及 OOM 事件均为零，
统计为 `test-1m-cover-stats.json`。覆盖索引全仓回归 Backend 74 项通过，
Pipeline 1056 项通过、54 项跳过。只读完整结果摘要对照和扫描进度回归
完成后，再进行全量重跑。

只读八类排序摘要对照 46.784 秒完成，全部一致，日志为
`detect-cover-semantic-compare.log`。随后仅补充源线段有序扫描与保守进度：
实际计划仍由覆盖索引提供顺序，无临时排序；当前源段未完全耗尽前不冒报完成，
仅正常结束记满。最终完整回归 Backend 74 项通过、Pipeline 1064 项通过、
54 项跳过。覆盖索引和保守进度版本全量已启动，容器
`roadnet-smoke-no-validate-26m-cover-20260914`，日志 `roadnet-26m-cover.log`，
复用原 roadnet Refine，仍使用 2500 MiB 测试上限并在退出时记录控制组峰值。
这次来源指纹已恢复原声明方式；成功统计为 `roadnet-26m-cover-stats.json`，
全量验收结果待运行完成后记录。

### 覆盖索引全量运行中的进度采样（尚未通过）

当前 Detect 为 `d73df6df-a011-429f-bfcd-60a404039512`，继续复用 Refine
`f1986249-8382-4d00-972f-900b6e81ea43`。来源指纹恢复原游标规划后，约
481 秒完成 26,000,000 行检查并进入下一阶段。完整轨迹读取及工作区构建
耗时 2141.5 秒，得到 874,088 条轨迹、1,428,473 个转向事件；跨轨迹阶段
需扫描的源线段总数为 14,985,948。

跨轨迹阶段的两次日志采样如下，均为运行中累计量：

| 阶段已用时间 | 源线段已扫描至少 | 已处理宽筛线段对 | 候选命中 |
| --- | --- | --- | --- |
| 568.9 秒 | 30,148 / 14,985,948 | 1,441,792 | 153,011 |
| 649.1 秒 | 32,453 / 14,985,948 | 1,605,632 | 163,106 |

同阶段较早容器资源采样为 1.923 GiB、CPU 16.18%；本次读取到的后续资源
采样为 1.925 GiB、CPU 20.60%。这些是瞬时采样，未与每条进度心跳严格
时间对齐，不能替代完整控制组峰值。记录时尚未观察到 OOM，但 Detect
仍未完成，不作全量验收通过结论。不同源线段的配对密度及磁盘访问代价不同，
不能依据当前扫描前缀线性推算整个阶段耗时。

主 agent 已安排利用空间顺序改善磁盘访问局部性的小规模原型验证；该原型
尚未修改生产路径，也未证明当前全量性能问题已解决。进度证据为
`roadnet-26m-cover.log`，资源采样为 `roadnet-26m-cover-memory.jsonl`，原始
详细日志为 `/data/logs/20260914_120928_930912_pid-7_log.txt`。

### 覆盖索引全量停止与后续修正范围

主 agent 在跨轨迹 674.1 秒采样后暂停容器，以隔离原型基准；当时源线段
扫描前缀至少 32,650，累计 1,622,016 个宽筛线段对、171,140 个候选命中，
进程和工作区暂时保留。随后心跳墙钟包含暂停，不能用总 elapsed 计算吞吐。
同期只读控制组采样为 `anon=2031968256 B`、`file=12562432 B`、
`workingset_refault_file=5258883`，`max/oom/oom_kill` 均为 0；这支持文件页
缓存承压的判断，但不能断言压力全部来自转向事件驻留。

该容器之后于 `2026-09-14T13:10:41Z` 退出，退出码 130、OOMKilled=false。
包装脚本记录完整控制组峰值 2,224,668,672 B，`max/oom/oom_kill` 均为 0。
`stop-full-cover.sql` 与 `stop-full-cover.log` 确认十张结果表均为 0，并以
`UPDATE 1` 将本轮 Detect 标记 failed；来源数据和复用 Refine 保持不变。
本轮没有完成 Detect，全量验收仍未通过。

空间顺序原型通过 15 项测试。隔离基准两种顺序均为 35,126 个线段对、
18,190 个命中，摘要均为
`9085b7c203d992598f7af0e62774f37f687d40d27f3ab4cad9e2b1485fa596db`。
包含构建、排序及提取的总耗时为 6.997 秒与 6.700 秒，改善约 4.2%；
控制组峰值 195,731,456 B，OOM 事件为 0。该原型未合入生产，不能据小规模
基准宣称解决全量瓶颈；日志已归档为 `roadnet_source_order_capped.log`。

下一项获准修正是读取时将转向事件写入磁盘序列，并将转向事件持久化改为
有界批量 COPY。writer 定向测试已确认单次迭代、满批与尾批顺序、稀疏来源
映射、累计心跳以及失败回滚；完整工作流和全量性能仍须重新验收。

### 转向事件磁盘暂存版本的百万行验收

本轮数据库路径在读取轨迹、提取转向事件时直接写入 `DiskSequence`，临时目录
优先使用 `DATA_ROOT`，不再先构建全部转向事件列表。因此跨轨迹阶段不需要
驻留全部完整转向事件对象。G* 从磁盘顺序读取事件编号及 XY 紧凑坐标进行
计算，再顺序读取并还原命中的热点事件；该调整仅适配存储方式，保留既有
邻域判定、标准分数、事件编号、筛选规则及输出顺序。

Detect 成功返回后，由 `IntersectResult` 持有转向事件暂存；内部失败或中断
会关闭暂存。Pipeline 在结果持久化和事务提交成功后立即关闭结果，异常路径
也通过 `finally` 关闭。写入器先顺序收集需要的来源引用，再顺序批量 COPY
转向事件，保留尾批、编号顺序、来源映射和事务原子性；外层心跳累计行数与
时间，批次不会重置进度。另补充 Detect 的 Ctrl-C 处理：捕获
`KeyboardInterrupt` 后回滚未提交结果，将尚未完成的 Run 标记 failed，再
传播中断；已成功提交的 completed Run 仍受保护，不被后续异常改写。

`test / public.day01` 复用 Refine `bcac9210-3dd4-497a-ac64-8b872deafa1c`
的百万行测试已退出，退出码 0。Detect 为
`e3dcdcae-1bad-4cef-81df-aaca8321b941`，Fitting 为
`6b05c29d-47c5-412d-b876-ef60361b9c1b`，全流程耗时 357.134408 秒。

| 计时范围 | 耗时（秒） |
| --- | --- |
| Detect 计算 | 248.800812 |
| Detect 持久化及提交 | 39.263509 |
| 其中已解析序列写入 | 28.647113 |
| Clip 组装 | 32.024646 |
| 轨迹读取及转向候选提取 | 51.5 |
| 跨轨迹候选提取 | 28.1 |
| 拓扑构建 | 141.668 |

跨轨迹累计 521,764 个宽筛线段对、58,660 个候选命中；30 项整数统计与
此前成功基线全部一致，最终为 292 个路网节点、556 条边。完整控制组峰值为
762,621,952 B，`memory.events` 的 `max`、`oom`、`oom_kill` 均为 0。
证据归档于 `/tmp/roadnet-smoke-artifacts-20260914/`，包括
`test-1m-turn-spool.log`、`test-1m-turn-spool-stats.json`、
`test-1m-turn-spool-memory-peak.txt` 和 `test-1m-turn-spool-memory-events.txt`。

此前完整回归为 Backend 74 项通过、Pipeline 1091 项通过、55 项跳过；
随后补充取消边界的定向回归为 33 项通过。最终完整回归退出码 0，Backend
74 项通过、Pipeline 1096 项通过、55 项跳过，日志为
`make-test-turn-spool-final.log`。八类结果语义摘要对照在 50.466 秒完成，
退出码 0，结果 `identical`，日志为 `detect-turn-spool-semantic-compare.log`；
对照消除新生成数据库 ID 的差异，并比较稳定来源、几何及最终序列。
数据库查询也确认上述 Detect 和 Fitting 均为 completed，百万行验收通过。

本轮减少跨轨迹期间完整转向事件的驻留以及转向事件写入的行元组峰值；
跨轨迹最终候选列表、成员归属和数值索引仍会随对应对象数量按 O(N) 增长。
因此这次修改与百万行测量不构成全流程固定内存的承诺，也不能据此推算
2600 万行运行的峰值或耗时。

通过百万行验收后，主 agent 已启动新的 2600 万行全量测试，容器为
`roadnet-smoke-no-validate-26m-turn-spool-20260914`，Detect 为
`57dfe902-8e5d-473c-92ef-d30a435b09a8`，仍复用 Refine
`f1986249-8382-4d00-972f-900b6e81ea43`，使用 2500 MiB 容器内存上限。
日志及资源证据文件前缀为 `roadnet-26m-turn-spool`，包装脚本在退出时记录
完整控制组峰值和事件计数。该轮当前仍在运行，尚未通过全量验收。

### 数值宽筛隔离实验及全量监测

全量运行的轨迹读取及转向候选提取耗时 1830.9 秒，得到 874,088 条轨迹、
1,428,473 个转向事件和 15,860,036 个有效点。随后进入跨轨迹候选提取。
监测仅读取日志计数和进程资源指标，不将原始轨迹行传入模型上下文。

另在暂停全量进程期间完成一次受限的数值宽筛对照实验：使用相同生成规则的
328,000 条两点轨迹，覆盖索引为 34,856,960 B，超过 32 MiB SQLite 缓存。
实验容器限制为 640 MiB、1 核、600 秒，候选保护阈值为 100 万。
参考版产生 1,456,808 个线段对和 758,090 个候选，包含构建、准备、提取及
排序摘要的总耗时为 428.927 秒。内存映射版尚未完成时，整体实验达到
600 秒上限并以 124 退出，因此没有完整候选摘要对照或可用的总体提速结论。
容器内存峰值为 671,088,640 B，`max` 为 60,028，`oom` 和 `oom_kill`
均为 0；这表示触及内存上限并发生回收，不能表述为没有内存压力。
实验记录为 `/tmp/roadnet-numeric-broadphase-large/result.log`。

该原型未合入生产。实验结束后已恢复原全量进程，继续复用其当前工作区；
跨轨迹阶段日志的已用时间包含上述暂停时间，不能直接作为净计算耗时。
在线监测记录为
`/tmp/roadnet-smoke-artifacts-20260914/roadnet-26m-turn-spool-watch.jsonl`。

2026-09-14 23:23（UTC+8）再次暂停该全量进程。此时扫描下界为
1,119,372 / 14,985,948 条源线段，累计 46,047,232 个宽筛线段对及
5,621,363 个候选；此前最新运行样本的 `max`、`oom`、`oom_kill` 均为 0。
暂停原因是已确认的后续候选列表内存风险，而非已发生的 OOM 或数据库提交。
当前 `extract_disk_cross_track_candidates` 在排序后对每个候选执行
反序列化与 `replace`，并将全部结果收集为列表。

使用同一 Python 镜像和候选类型，对 100 个合成候选执行相同构造后，按
`sys.getsizeof` 统计实例、实例字典、直接浮点字段、两个来源引用实例及列表
引用槽，最小为每候选 616 B；不计引用字典、已清洗来源引用、字符串、整数
编号、临时对象及 Pipeline 的其他内存。以此前已实际生成的 5,300,869 个
候选计算，该部分为 3,265,335,304 B（约 3.04 GiB），超过本次容器的
2,621,440,000 B 上限。这是依据相同对象布局的保守测算，不是全量进程
已观测到的 RSS 或 OOM。记录为 `candidate-list-memory-lower-bound.json`。
因此需先验收候选及成员磁盘暂存链路，再重新运行全量；隔离原型尚未合入。

### 候选、成员及写入器磁盘暂存链路合入

上一轮全量随后收到 SIGINT 并正常退出，退出码 130。Detect
`57dfe902-8e5d-473c-92ef-d30a435b09a8` 自动标记 failed；十张结果表只读
计数均为 0，证据为 `verify-full-turn-spool-stop.log`。完整控制组内存峰值为
2,037,018,624 B，`max`、`oom`、`oom_kill` 均为 0。停止依据是上述后续
列表内存风险测算，不能描述为已发生 OOM；本轮仍未完成 Detect，全量验收
未通过。

候选及成员暂存链路先在 `/tmp/roadnet-member-chain-prototype` 隔离副本中
实施。数据库路径将全局排序、编号后的跨轨迹候选顺序写入 `CandidateStore`，
噪声候选保留；簇成员、合并后的成员和来源引用使用可重复读取的只读视图。
每轮成员关系独立管理，消融结束关闭对应关系，最终结果关闭候选存储。
来源摘要复用顺序读取时形成的摘要；归属写入只读窄成员身份，不因逐簇关联
再次解码完整候选。数值聚类输入改用紧凑数组，保持原聚类算法、候选顺序及
编号规则。排序及编号阶段单独提供以候选数为单位的心跳，不与提取阶段的
线段对计数和保守源线段扫描进度混用。

写入器增加运行隔离的临时工作区，优先使用 `DATA_ROOT`。消费请求与 p/i
编号映射保存为窄记录，按精确的 `(point_kind, point_id, subtrack_index)`
来源键外部排序、顺序归并，再按消费顺序恢复行流；不会为每个端点执行随机
SQL 查找。成员归属同样顺序关联，相同事件属于同一候选的重复允许，属于
不同候选则报错。跨轨迹规范线段对通过外部排序检查重复与退化，只检查报错，
不去重。cross、turn 和候选穿越点均分批 COPY，使用外层累计心跳。

外部排序按固定记录块生成排序文件，并限制每轮归并路数。p/i 仍按原 Refine
读取顺序预留 ID，全部 p/i 后才预留 c；同位置穿越点的排序键、端点 epsilon、
进出顺序、来源引用及 COPY 行顺序均保留。错误继续交给调用者回滚事务，
工作区在正常返回、异常及中断时关闭。临时磁盘量随输入增长，排序块按记录数
限制而非精确字节预算；大于缓存时的磁盘成本尚未通过全量测量。

隔离副本定向回归为 71 项通过、2 项跳过；真实 PostgreSQL 写入器定向
回归 22 项全部通过，包含此前跳过的用例。随后完整 Pipeline 回归为
1123 项通过、55 项跳过，退出码 0。记录分别为
`member-chain-isolated-tests.log`、`member-chain-postgres-tests.log` 和
`member-chain-full-isolated-tests.log`。测试覆盖完整 eager/lazy 结果、独立
归属与 COPY 行参考结果、来源键、p/i/c 序列、跨批重复检查、真实事务回滚
及存储关闭；修正了存储先关闭后迭代器关闭的游标清理问题。

依据上述隔离验收，本轮仅将六个运行模块和七个测试及辅助文件逐一合入，
保留其他生产改动与 `stream_batch_size=1000`。合入后主 agent 仍需执行
完整仓库回归及百万行测试，再验收全量，不能把隔离单测通过等同于全量通过。

此方案没有消除所有随规模增长的内存：DBSCAN 全局数值数组、网格、标签及
可用点集合仍然增长；拓扑候选穿越点、观测、部分 ID 映射和最终输出行列表
也仍有规模相关开销。全 p/i 窄映射、消费请求和排序中间文件增加临时磁盘
空间。当前结论仅为已合入候选、成员及指定写入关联路径的内存修正，不构成
全流程固定内存或 2600 万行必然通过的承诺。

### 候选与成员磁盘暂存版本的百万行验收

合入后的 `make test` 退出码为 0：Backend 74 项通过，Pipeline 1147 项
通过、55 项跳过，日志为 `make-test-member-spool-final.log`。
`test / public.day01` 继续复用 Refine
`bcac9210-3dd4-497a-ac64-8b872deafa1c`，百万行测试退出码为 0；
Detect `173ade7d-f03d-4c9d-8e46-906279085c9b` 和 Fitting
`4137e353-8c2c-45b1-a072-9586f38fb4f8` 的数据库状态均为 completed。

全流程耗时 387.917273 秒，其中 Detect 计算 261.424 秒，持久化及提交
58.713973 秒，已解析序列写入 40.980 秒，Clip 组装 30.703 秒。
相比上一版 357.134408 秒增加约 30.8 秒；这是存储调整后的实测结果，
不能宣称总体提速。容器完整内存峰值为 715,898,880 B，`max`、`oom`、
`oom_kill` 均为 0；该峰值不包含独立 PostgreSQL 容器的内存。

30 项整数统计与上一版全部相同，最终为 292 个节点、556 条边。
八类持久化结果的规范化语义摘要在 45.445 秒完成，结果为 `identical`；
比较稳定来源、坐标、归属及最终序列，不比较新分配数据库 ID 的字面值。
证据为 `test-1m-member-spool.log`、`test-1m-member-spool-stats.json`、
`test-1m-member-spool-memory-peak.txt`、`test-1m-member-spool-memory-events.txt`
和 `detect-member-spool-semantic-compare.log`。

通过上述验收后，于 2026-09-14 23:40（UTC+8）启动下一轮全量，容器为
`roadnet-smoke-no-validate-26m-member-spool-20260914`，Detect 为
`2dd63416-2d9c-40ca-8eec-4296a7f5c223`，仍复用 roadnet 的 Refine
`f1986249-8382-4d00-972f-900b6e81ea43`，保持 2500 MiB 容器上限。
日志、统计和资源证据前缀为 `roadnet-26m-member-spool`。
该全量尚未完成，主 agent 继续在线监测。两个数据库的
`detect_completion_check`、`fit_completion_check` 在本轮百万行启动后
再次核对均为 D；未执行 `init-db` 或重新启用这两个整批校验。


### 当前全量的 DBSCAN 内存风险与紧凑存储修正

当前 `roadnet-smoke-no-validate-26m-member-spool-20260914` 仍持续提取跨轨迹候选，
尚未完成该阶段及全流程。观察到 28,628,879 个候选时，旧 DBSCAN 全局结构的
保守内存下界 `92N − 7196` 已达到 **2,633,849,672 B**，超过本次主 agent
自行设定的 **2,621,440,000 B** 容器上限。该估算包含 XY 数组、标签/核心标记/
点所在网格引用、网格索引与 Python 整数、活动集合槽位；未计字典、网格对象头和
容量余量。证据为 `dbscan-global-memory-lower-bound.json`。这是进入聚类后的
确定结构风险，当前尚未观察到 OOM，不能写成已发生内存溢出。

已合入紧凑存储修正：网格索引和待处理集合使用有符号整数数组，核心三态使用
`bytearray`，活动集合改为每格头节点与全局前后链数组，并取消逐点网格引用。
各格原始索引顺序通过有序合并保留，仍按原升序入栈、后进先出扩展；距离计算、
核心规则、簇编号、边界归属、非有限输入的扫描回退保持不变。公共标签仍为
`list[int]`，每簇共享标签整数，没有在末尾重新创建每点 Python 整数。

隔离的小型精确标签、活动链和临时恢复工具回归共 **109 项通过（4.54 秒）**。
此前一版隔离完整 Pipeline 测试为 **1158 项通过、55 项跳过**；它不代表本次
合入后的真实数据库验收。合入的回归只使用固定期望和现有扫描实现对照，未加入
整个旧 Detect 文件或临时恢复工具。当前运行进程已加载旧版代码，不会因文件合入
自动使用紧凑 DBSCAN。

该修正仍保留随候选数及占用网格数增长的全局结构，不构成任意规模的固定内存
保证。尚未完成紧凑版本的真实百万行或全量验收；下一步由主 agent 运行百万行，
并验证完成候选快照在新 Pipeline/Detect Run 中恢复的数据库链路。不得将上述
单测或此前百万行结果表述为本版 2600 万行全量通过。


### 紧凑 DBSCAN 的百万行捕获与恢复验收

随后完成更新版本验收：隔离回归 **113 项通过（4.46 秒）**；`make test`
退出码为 0，Backend **74 项通过（1.23 秒）**，Pipeline **1182 项通过、
55 项跳过（17.63 秒）**。完整测试日志为
`/tmp/roadnet-smoke-artifacts-20260914/make-test-dbscan-compact.log`。

`test / public.day01` 继续复用 Refine
`bcac9210-3dd4-497a-ac64-8b872deafa1c`，两次真实数据库运行均退出码 0、
`OOMKilled=false`：

| 运行 | Detect ID | Fitting ID | 全流程耗时（秒） |
| --- | --- | --- | ---: |
| 普通计算并保存候选快照 | `a3470d77-9f7b-47d3-8ec0-e6162def200f` | `a6de3925-12cf-4fb0-a0ad-79ff8aff1028` | 428.811188 |
| 新运行恢复候选快照 | `deb731ae-86b9-450f-a109-38b69a9b811c` | `9338132b-0d96-4968-8f06-e8c680241709` | 405.533579 |

普通计算运行的耗时包含保存快照，并且与尚在执行的全量运行并行，因此不能直接
据此判断纯 Pipeline 性能变化。恢复使用新的 Detect ID，重新读取轨迹和转向
事件，复用已经完成的跨轨迹候选。临时恢复工具仍仅用于此次离线验收，不合入
产品接口。

两次运行的 **30 项整数统计**均与候选和成员磁盘暂存版本的基线一致。恢复运行
与基线 Detect `173ade7d-f03d-4c9d-8e46-906279085c9b` 的 **八类持久化结果
语义摘要全部为 `identical`**，对照耗时 50.611 秒。证据日志为
`dbscan-recovery-semantic-compare.log`；两次统计、审计记录及日志的文件名前缀
分别为 `test-1m-dbscan-capture` 和 `test-1m-dbscan-recover`，均归档于
`/tmp/roadnet-smoke-artifacts-20260914/`。

快照 `/data/test-1m-dbscan-candidate-snapshot-20260915` 已保存 **58,660 个
候选**。工具通过恢复到独立私有副本、只清理私有副本来保留原快照；恢复后的
原件文件状态仍待主 agent 独立核对，此处不将工具的生命周期保障写成已完成
文件复核。

以上结果通过了紧凑 DBSCAN 的百万行普通计算与新运行恢复链路验收。
全量仍由原进程持续提取：最近约扫描 **878 万 / 1499 万条源线段**，累计约
**3144 万个候选**，尚无 OOM。它尚未切换到紧凑版本的新进程，也尚未完成
Detect 或全流程，**2600 万行全量验收仍未通过**。

### 关机中断后的任务状态（2026-09-15）

本次任务目标仍为：记录并停用 `test`、`roadnet` 的两个完成校验触发器，完成
百万行验收后，复用最新已完成 Refine 执行 `roadnet / public.trajectory_points`
2600 万行全量测试；修正阻止测试完成的查询、内存与进度问题。此前的紧凑存储
修改服务于这一目标，当前没有另行开展全面性能调优或架构重构。

用户确认电脑中途关机。重新连接后，原全量容器
`roadnet-smoke-no-validate-26m-member-spool-20260914` 已退出，退出码 **255**、
`OOMKilled=false`。持久日志的最后进度仍为跨轨迹候选提取：已扫描至少
**12,677,315 / 14,985,948 条源线段**、处理 **301,203,456 对线段**、产生
**36,829,662 个候选**，该阶段累计 **28,670.8 秒**；没有候选提取完成记录。
持久日志路径为
`/home/reed/workspace/data/road-network/logs/20260914_154050_672038_pid-7_log.txt`。

关机前的 `/tmp/roadnet-smoke-artifacts-20260914/`、临时恢复工具和监测会话在
重连后不可用，因此前文引用的这些 `/tmp` 路径仅为历史证据位置，不能再视作
当前可访问的归档。仓库代码与本文中的百万行验收记录仍在；两个百万行验收
容器保留退出码 0。`DATA_ROOT` 下的百万行候选快照原件仍在，清单记载
58,660 个候选，数据库文件为 41,861,120 B。

全量遗留文件包括约 35.0 GB 的 `detect-workspace-trc7537p/workspace.sqlite`、
16,384 B 的 `roadnet-cross-members-u899uk89/members.sqlite`，以及转向事件
暂存文件。文件存在不代表其内容已完成提交或可用于恢复；这些文件尚不构成
已经验收的全量候选快照，不能直接跳过剩余提取或宣称全量完成。

主 agent 已启动原 PostgreSQL 并只读核对：Detect
`2dd63416-2d9c-40ca-8eec-4296a7f5c223` 仍遗留 `running`，`roadnet` 的两个
完成校验触发器仍为 `D`。截至本条记录，尚未修改该 Run 状态，尚未启动新全量
运行。下一步核对数据库结果与恢复条件，保留中断证据，再继续全量验收。

恢复检查随后确认：旧 Detect 的十张结果表均为零，父 Refine 仍为 `completed`。
暂存工作区明确使用 `journal_mode=OFF`、`synchronous=OFF`，不承担持久恢复职责；
全量候选尚未排序编号并封存，不能依据暂存文件存在而跳过未完成的提取。
主 agent 在再次以十张表均为空作为更新条件后，将旧 Detect 标记为 `failed`。

2026-09-15 10:37（UTC+8）已启动新容器
`roadnet-full-dbscan-compact-restart-20260915`，复用原 Refine
`f1986249-8382-4d00-972f-900b6e81ea43`，新 Detect 为
`b0bfe8e2-1d8d-476d-a8bf-a392a7b0c218`。本次使用已通过百万行验收的紧凑
DBSCAN，重新执行 Detect；Refine 不重跑。容器内存上限仍为 2500 MiB。
运行日志、统计、退出码和控制组内存结果改存持久目录
`/data/run-audits/full-dbscan-compact-restart-20260915/`，对应宿主机
`/home/reed/workspace/data/road-network/run-audits/full-dbscan-compact-restart-20260915/`。
统计及结束文件仅在相应步骤实际完成后生成，不能预先视为全量通过。

本次数据库路径的 Detect 仍为单 Python 计算进程。转向事件按轨迹分批，
跨轨迹候选由磁盘 R-tree 筛选线段对后逐批计算，各批次串行；聚类与拓扑验证
也由同一进程执行。分批及空间索引加速不等于多进程并行。本次没有启动额外的
并行改造或性能实验。
