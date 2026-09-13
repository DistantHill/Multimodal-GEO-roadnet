# main 投影与查询修复验收记录

日期：2026-09-12。实施基线：`main` / `b8dceeec2b72f53fab5dbb8cf29992ac2fbe69c3`。

结论：本次规格范围内的实现与功能验收通过；大数据性能门禁和真实投影变化基线尚未验收。
改动保留在工作区，未提交、推送或切分支。

## 规格与分工

- 完整规格：[main-projection-query-repair-spec.md](main-projection-query-repair-spec.md)。先落盘，再交给实现 agent。
- 实现：用户指定的 `gpt-5.6-sol`、`medium` 子 agent。
- 验收：主 agent 独立检查完整改动、调用链、数值反例、真实 SQL 和测试；发现问题后交回同一实现 agent 修复，再复验。
- 使用说明：[run-projection-context.md](run-projection-context.md)。

## 已验收的实现范围

1. 正式 DB Detect 在读点前验证父 Clean 状态、来源、fingerprint 和投影上下文；空数据不绕过，外部 context 仅作一致性断言。
2. 复用 DB 读取过程验证实际点的有限性、范围及 Run 尺度规则，包括 injected；不增加范围验证的全表扫描、DDL 或投影 Geometry。
3. Run CRS 与 query UTM 分离；按最终 footprint 选择最东标准 UTM 带，最多进入两个相邻三度网格；query 1.5% 与 Run 默认 1% 分离。
4. 支持域内使用边界极值构造保守 4326 粗筛；SQL 使用原始窗口数值闭区间精筛，并返回 Run x/y。
5. 同 CRS reference/optimized 基准与 legacy→fixed 投影差分报告分开；输入可追溯，审批绑定哈希，缺失阶段明确为 `not_evaluated`。

验收期间发现并修复了：精确触带的浮点取整、跨赤道/反经线/异常 TM 投影范围漏包、参考实现车辆与子轨迹身份混淆、报告篡改校验遗漏、半成品阶段被误报为零差异，以及精筛 64 ULP 外扩偏离原数值窗口的问题。最终精筛不做 ULP 或 epsilon 外扩。

多子轨迹排序保留原四元组前缀，仅为原先冲突的键追加确定性 track-rank tie-break；旧歧义项顺序可能明确化。未修改历史结果。

## 最终测试结果

| 验证 | 结果 |
| --- | --- |
| 修改前本地 Pipeline 基线 | 324 passed，1 skipped |
| 隔离容器 `make test`：Backend | 66 passed |
| 隔离容器 `make test`：Pipeline | 351 passed，2 skipped |
| 本地 Pipeline 全套，启用真实 PostGIS | 352 passed，1 skipped |
| `git diff --check` | 通过 |

容器测试的两个 skip 分别为未设置 `ROADNET_TEST_DSN` 的集成测试，以及缺少既有 `Hfm_130pts.csv` fixture。启用测试 DSN 后仅后者跳过。

容器测试执行仓库原有 `make test` recipe，仅通过 `COMPOSE_DEV` 替换为隔离配置：使用已有镜像、只读代码挂载、不启动开发数据库或使用业务卷。

可重复执行的 Pipeline 命令（工作目录 `services/pipeline`）：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m pytest -q -p no:cacheprovider
```

真实 PostGIS 测试需另外设置指向专用测试库的 `ROADNET_TEST_DSN`；提交的集成测试只创建临时表。

## 主 agent 独立验证

- 真实 Clean 元数据 SQL：无 clean_id、未知 Clean、未完成 Clean、缺失 CRS、非法 CRS 均在 extent 查询前失败；有效空 Clean 正确继承上下文与来源。该定向检查的有效空分支隔离了 fingerprint 计算，fingerprint 既有保护另由代码审查与回归套件覆盖。
- 两个真实 PostGIS 窗口均使用 query EPSG:32650、Run EPSG:32649；测试 Run 显式允许跨经度 area，不改变默认 Run 策略。

| 地理中心与米制尺寸 | 源点数 | 无粗筛 oracle / raw 返回数 | kept + injected 返回数 |
| --- | --- | --- | --- |
| 113.99E、39N；5 km × 3 km | 4,212 | 2,180 / 2,180 | 4,360 |
| 117.6E、39N；340 km × 100 km | 4,217 | 2,161 / 2,161 | 4,322 |

样本含内部、外部、四边附近和中央经线极值点。oracle 不使用生产粗筛或边界 helper，直接比较 `ST_Transform` 与原始窗口界限。raw、kept、injected 的身份集合完全一致，其他 Clean 的 injected 不混入，返回 Run x/y 误差小于 `1e-6 m`。

- 新增 SQL 边界测试还经过反向检验：只在测试进程内模拟恢复 64 ULP 外扩，测试能够失败，确认它确实约束精筛而非仅验证粗筛。
- 北、南半球随机局部 UTM 矩形扫描：2,275 个受支持矩形、每个 804 个边界采样，未发现超过 `1e-9°` 的漏包；另有 386 个已接受 query 请求的高密度边界检查。随机检查仅补充支持域及极值方法审查，不作为数学外包依据。
- 真实 SQL 同 CRS 基准及完整 CLI：126 个合成源点，53 个候选、1 个聚类；候选数值最大差为 0，来源与聚类 payload 一致。仅运行功能 smoke，没有开启性能门禁或写基线。
- 真实 legacy/fixed 两线段反例：WGS84 椭球位移约 `0.0004760104933 m`，报告保持 `pending_review`；未提供的 Clip/Fitting 标记为 `not_evaluated`。

## 未验证项与安全边界

- 未运行 500k/1m 数据量、三次重复、5x 加速的性能门禁；小样本耗时不代表性能达标。
- 未生成或接受真实业务投影差分基线；Clip/Fitting 报告行为有确定性 fixture 覆盖，但不等于真实历史全链路稳定性验证。
- 未恢复 v1/v2 数据，未修改历史 Run、业务数据、数据库 schema 或持久化投影列。
- PostGIS 验收复用本机已有 `postgis/postgis:16-3.4` 镜像，以 `--pull=never` 启动独立实例；数据目录为 tmpfs，端口仅绑定 localhost，没有业务挂载或镜像下载。
- 验收完成后移除本次临时 PostGIS 实例；其合成测试数据可由 fixture 重建。其他容器和业务卷保持不变。
