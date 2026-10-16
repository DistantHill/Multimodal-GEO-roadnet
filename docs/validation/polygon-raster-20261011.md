# Polygon 栅格样本验证

新增 `python -m pipeline rasterize --polygons` 批量入口。按文件名前缀标注 TP / FP，
每个 polygon 保留独立样本，kept 与 injected 通过无排序 `UNION ALL` 合并。

## 实际输入与时间

- Refine：`6c9acb0c-f356-49ce-81d0-2dd7dad51e68`，状态 completed。
- 来源点表：`public.trajectory_points`；输出坐标参考系：EPSG:32649。
- 实际配置： `services/pipeline/config/pipeline.toml` ，分辨率 10 m/px。
- 时间：北京时间 2023-03-01 00:00:00（包含）至 2023-03-07 00:00:00（不包含），覆盖 3 月 6 日全天。
- 附件根目录： `/home/reid/.codex/attachments/4c1023d5-3bff-470e-9fa3-de6d14727765/` 。

| 文件 | 标签 | 样本数 | kept 点数 | injected 点数 | 空样本 |
| --- | --- | ---: | ---: | ---: | ---: |
| FP_intersection_fuping.gpkg | FP | 15 | 22,991 | 84 | 0 |
| TP_intersection_fuping.gpkg | TP | 111 | 482,225 | 1,183 | 0 |
| TP_intersection_fupingjiaoqu.gpkg | TP | 54 | 22,675 | 98 | 0 |
| TP_intersection_yanliang.gpkg | TP | 39 | 609,996 | 1,624 | 0 |
| 合计 | TP 204 / FP 15 | 219 | 1,137,887 | 2,989 | 0 |

点数按样本累计，重叠 polygon 中的相同点分别计入对应样本，不代表全区域去重点数。
样本宽 5–20 px，高 6–21 px。未扩大地理范围或重采样原始灰度图；预览使用最近邻放大。

## 空间查询验证

kept 结果表没有 `geom` 空间索引，因此先筛选来源表的 EPSG:4326 几何，再通过
`source_row_key` 与 `refine_id` 查找本次 Refine 成员。injected 分支直接筛选其 EPSG:4326
几何。两分支均执行包围盒候选筛选和 `ST_Covers` 精确过滤，保留 polygon 边界点，
排除孔洞内部点。仅在筛选后投影 kept 坐标，injected 使用持久化 XY。

原生 `EXPLAIN (FORMAT JSON)` 计划保存在
 `docs/validation/sql-plans/polygon-raster.json` 。参数为上述 Refine 和时间窗口，
polygon 来自 FP 文件首个要素（FID 32），处理 SRID 为 32649。
确认命中 `idx_trajectory_points_geom`、`idx_injected_points_geom`，空间条件位于
`Index Cond`，kept 成员查找使用 `kept_points_source_member_key`。查询没有排序节点。
SQL 内容哈希及检查结果已加入 `docs/sql-index-reviews.json` 。

## 产物验证

运行 ID：`7bc3e866-8f56-409e-9f6d-ec16feb21b64`。

产物目录： `/data/tmp/road-network/raster/polygon-samples/7bc3e866-8f56-409e-9f6d-ec16feb21b64/` 。
同级 ZIP： `/data/tmp/road-network/raster/polygon-samples/7bc3e866-8f56-409e-9f6d-ec16feb21b64.zip` 。

共 1,107 个文件，ZIP 为 4,519,859 字节，SHA-256：
`428cd9ab42547a1b5cb70895965e4787f6accf3eaf0c86d28c21931d956ee3a2`。
包含全部灰度 PNG、计数和灰度 GeoTIFF、像素及地理坐标标注、CSV、GeoJSON、
本地筛选预览页、六张总览和四个来源各前三个样本的预览图。 `SHA256SUMS` 覆盖
除其自身以外的全部产物；ZIP CRC 检查通过。

逐一验证全部 219 个样本：

- GeoTIFF 坐标系为 EPSG:32649，横纵分辨率均为 10 m。
- 原始像元计数之和等于该样本 kept + injected 点数。
- PNG 与灰度 GeoTIFF 像元完全相同。
- 灰度与原始计数重新计算的非零 Q95 + sqrt 结果完全一致。
- 标注中的标签、feature ID、点数与 CSV 一致。
- 人工查看总览和四个来源的预览，确认标签、polygon 边界和放大像元可见。

## 回归测试

- 首轮 raster 及新增单元测试：33 passed。
- 最终新增测试（含真实 PostGIS 临时表测试）：9 passed；覆盖带洞多面、重复要素、
  无效输入、边界像元、空样本、无序累计、CLI 约束、失败清理与可读发布目录。
- PostGIS 测试验证两分支的时间左闭右开、精确 polygon 筛选、边界包含、孔洞排除、
  Refine 隔离，以及同位置 kept / injected 均计数。仅创建连接级临时表并回滚。
- `make test` 首次执行：Backend 147 passed、6 skipped；Pipeline 旧镜像缺少仓库已声明
  的 optuna，收集测试时中断。
- 临时 Pipeline 容器执行 `pip install --require-hashes -r requirements.lock` 补齐锁定依赖，
  随后执行 `python -m pytest tests scripts/cv_human_check/tests`：1597 passed、106 skipped，
  耗时 72.56 秒。全部依赖变更仅位于随后删除的临时容器，旧开发镜像未覆盖。
- 曾尝试完整重建 Pipeline 镜像，后改用上述临时容器测试并取消本次构建；没有遗留构建任务。
- SQL hook 的 10 条新增 SQL 检查记录与当前源码哈希、计划证据均通过验证；
  `git diff --check` 通过。

本次没有启动 Pipeline 全流程冒烟或全量运行；仅复用已完成 Refine 生成栅格样本。
