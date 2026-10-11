---
id: DOC-2026-1011-intersection-spatial-trace
type: project-document
projects: [Multimodal-GEO-roadnet]
status: active
---

# 交叉口漏检的空间溯源：排查过程与中心更新修复

本文按用户提供的项目过程文档结构，记录 2026-10-10 的交叉口漏检排查及两项已提交修复；2026-10-11 补充合并关联对话中的候选圆几何修改。重点是如何定位一个空间位置在哪一步失去检测结果，而非仅回放最终交叉口的构成轨迹。

## 排查过程

### 1. 先校准实验事实

| 实验 | 算法 | Run ID | 实际时间范围 | 原始最终交叉口数 |
| --- | --- | --- | --- | --- |
| 1 | joint | 40755816-f1b3-4c57-9e46-dcb309fc9a0d | 5 天 | 55 |
| 2 | joint | 9071362d-83f4-4b0f-a3f1-d455fe396110 | 1 天 | 32 |
| 3 | turn | 27bc81cb-d899-43dd-a7e3-aaef63325d2b | 5 天 | 0 |
| 4 | turn | 3dda3786-3858-4d88-a6d5-36dbe3c3e356 | 1 天 | 2 |

用户最初称长时间实验为“6 天”。运行清单中的实际范围为北京时间 2023-03-01 00:00 至 2023-03-06 00:00，即 120 小时；短时间实验至 2023-03-02 00:00。同一时间范围的 joint 与 turn 使用相同 Refine 输入。先核对输入、时间、参数、代码版本，才能把结果差异归到算法步骤。

### 2. 把地图上的位置作为查询入口

跨运行的交叉口或候选 ID 不能直接对齐。以用户指定区域和短时间实验检出位置为锚点，在每个运行中分别查看：

1. Refine 轨迹是否覆盖该位置。
2. 转向事件是否存在。
3. 转向事件是否通过热点筛选；joint 还要独立检查跨轨迹候选来源。
4. 是否生成候选簇；候选是否合并、拆分或中心移动。
5. 候选圆是否产生完整进出观测。
6. 分支数、观测数和有向转移种类数是否达到拓扑检验阈值。
7. 已通过的候选是否出现在最终持久化结果中。

这些步骤形成逐位置的证据链。发现某一步没有输出时，还要区分“前一步没有输入”“输入被规则剔除”和“证据未保存、无法判断”。不能把地图上没有结果一律解释为淘汰。

区域 1.1 的北界超过区域 1 的北界，需独立统计；投影后的经纬度矩形应按真实多边形判断，包围盒只用于缩小候选范围。距离和半径在米制坐标 EPSG:32649 下计算。

### 3. 找到首个发生差异的步骤

对短时间实验成功位置，逐步比较长时间实验的局部证据，而非只比较全局总数。此次得到两种不同机制：

| 空间锚点 | 短时间实验 | 长时间实验 | 定位结论 |
| --- | --- | --- | --- |
| turn 候选 55 所在位置 | 检出 | 对应候选 107 已生成，有 7,856 条完整进出观测，但原分支数为 1 | 失败发生在拓扑分支聚类及验收环节 |
| turn 候选 56 所在位置 | 局部转向事件 898，热点事件 820 | 同一个短时间候选圆内，转向事件 2,963，热点事件 0 | 失败发生在热点筛选，尚未进入候选聚类 |
| joint 候选 135 | 区域 1.1 的关注位置 | 有 2,127 条完整进出观测，但原分支数为 1 | 需要检查分支角聚类，而非继续怀疑轨迹缺失 |

候选 56 的局部事件增加，但 G* 使用全体事件邻居数的均值和标准差做相对评分。长时间实验的局部最高分约 0.295，低于 0.5 阈值；短时间实验约 0.928。固定 180 米邻域下的等效邻居数门槛由短时间的 850 升至长时间的 4,519。因此，多加时间和事件并不保证保留原热点。

这不是中心更新修复能解决的问题，需要另行讨论热点筛选语义。

### 4. 回放原计算，再只改变一个因素

对于已有候选、但拓扑不通过的位置，从不可变 Refine 数据读取完整子轨迹，使用原候选中心、拓扑半径和参数重建线段求交与进出配对，保留完整 entry/exit 分支角。先确认原算法能重现原拒绝结果，再替换中心更新公式。

这里的“局部”指只选择关注的候选，不是裁掉完整子轨迹上下文。仅按视窗截取轨迹点可能破坏穿越和配对语义。

反事实重算固定观测、阈值和验收规则，只把“旧中心与新角度的圆均值”替换为“所有成员方向向量的累计圆均值”。因此可以判断这一处实现对结果的影响。

| 候选 | 原分支数 | 累计成员圆均值后的分支数 | 局部重算结果 |
| --- | --- | --- | --- |
| turn 107 | 1 | 4 | 拓扑通过 |
| turn 108 | 1 | 5 | 拓扑通过 |
| turn 109 | 2 | 5 | 拓扑通过 |
| joint 135 | 1 | 5 | 拓扑通过 |

这些是选定候选的局部重算结果，不是重新运行四个实验后的最终数量，也不是道路真实交叉口的人工验收结果。此次完整角度重算覆盖 20 个关注候选，不能外推为全部候选均已核验。

## 实施和踩坑

### 现有 replay 为什么难以回答“在哪一步消失”

交叉口 replay 入口先按 intersection_id 查询最终 intersections，再取得 candidate_intersection_id，回看所属转向事件、跨轨迹事件及轨迹点。这条路径适合解释“这个成功结果由哪些输入构成”。

代码入口： [/data/workspace/Multimodal-GEO-roadnet/apps/backend/src/backend/replay/select_intersection_members.py:14](/data/workspace/Multimodal-GEO-roadnet/apps/backend/src/backend/replay/select_intersection_members.py:14) 。

失败位置没有最终 intersection_id，就无法从这条入口继续回放。即便存在候选，展示构成点也没有自动给出每个检验条件的实际值、阈值与判定。当前拓扑模型有 reason，但是否覆盖所有失败阶段、是否持久化并能在界面查询，仍需分别检查，不能仅因内存模型存在该字段就认为溯源完整。

此次没有完全依赖 replay 界面，而是组合了运行清单、候选记录、事件分布、不可变 Refine 数据、局部原算法重算和单因素反事实重算。部分失败计算细节需要重新计算才能补齐。

### 空间溯源中的具体陷阱

- **总数相近不代表检出了同一批位置。** 需要逐空间锚点匹配，保留候选合并、拆分和位移信息；最近中心只能作为匹配线索，不能直接证明同一对象。
- **没有候选不等于没有轨迹。** 先检查事件和热点筛选，避免在拓扑阶段寻找不存在的候选。
- **观测很多不代表分支足够。** 候选 107 有 7,856 条观测，但旧实现把方向聚成一条分支。
- **三个半径不能混用。** 本次 G* 邻域为 180 米；原实验候选半径来自候选成员范围；拓扑求交半径为 max(90 米, 候选半径 + 70 米)。复现需使用原实验实际中心和半径，不能随当前代码变化重建一个不同的圆。
- **当前代码不能直接代表历史算法。** 历史复现应固定代码版本、输入和参数；修复后再运行属于新结果。
- **反事实通过不等于完整实验验收。** 局部重算用于验证原因，完整实验和人工空间核验用于确认实际效果。

### 已实施修复一：分支方向中心更新

旧实现每次用旧中心和新角度各占一半计算圆均值，丢失历史成员权重。例如 0°、20°、40° 被更新为 25°，而全体成员的圆均值为 20°。在连续角度输入下，中心漂移可能吸收另一条道路分支。

修复为每个分支累计所有成员的 sin/cos，中心由累计向量的 atan2 得到。仍使用排序输入、原贪心指派规则和原阈值。本次没有向量化匹配，也没有替换聚类方法。

修复入口： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/extract_topology.py:42](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/extract_topology.py:42) 。

提交：`8bdeeba`，`fix: retain all member weights in branch centers`。新增三个回归测试，覆盖成员等权、中心漂移误合并和跨 0° 圆均值；`make test` 验证 Backend 147 项通过、6 项跳过，Pipeline 1,482 项通过、107 项跳过。未重新运行完整实验流水线。

### 已实施修复二：候选圆采用几何中位数与累计 95% 半径

补充来源为用户引用的 [关联对话](codex://threads/01a12979-ab8b-7aa2-b17f-90f2b8cd87e4?hostId=remote-ssh-discovered%3Aubuntu-home) 。该线程为继承会话，具体讨论和提交记录来自其父会话 `01a12425-a25d-7953-a8ec-d9b9aea0c530`。已核对继承边界内的用户要求、实施说明、测试输出以及 Git 提交，未将其他对话或当前未提交改动纳入本节。

用户在确认原候选圆计算后，要求改用几何中位数（Geometric Median）作为候选圆心，成员到该中心的累计 95% 距离作为候选半径。这是候选几何定义的改变，与前一项分支方向圆均值缺陷修复不同。

| 对象 | 原四次实验使用的定义 | 新运行使用的定义 |
| --- | --- | --- |
| 候选圆心 | 全部候选成员 XY 的算术平均值 | 最小化所有成员到中心欧氏距离之和的几何中位数 |
| 候选半径 | 到原圆心的最大成员距离 | 到新圆心距离排序后的第 `ceil(0.95 × n)` 个距离 |
| 成员与来源统计 | 所有聚类成员 | 仍保留所有成员，包括新候选圆外的成员 |
| 拓扑圆 | 原中心，`max(最小半径, 候选半径 + padding)` | 新中心，沿用同一个半径扩展公式 |
| 分支方向中心 | 另由进出角聚类计算 | 仍由进出角聚类计算，不等同于候选圆心 |

实现使用可处理迭代位置与成员点重合的修正 Weiszfeld 方法；重合点保留各自事件权重，joint 不额外按来源加权。先减去局部原点再迭代，避免直接累加大的投影坐标偏移。位移不超过 0.000001 米时停止，最多 10,000 次；迭代耗尽时明确报错，不保存未收敛结果。几何中位数在退化数据上可能不唯一，测试允许实现返回符合目标的解。

95% 半径采用 `np.quantile(..., method="inverted_cdf")`，不是默认线性插值分位数。实际 API 需要支持该参数的 NumPy 版本，项目依赖为 NumPy >= 2.0；不能直接用系统旧 NumPy 环境复现。距离排序第 k 个的含义是覆盖至少 k 名成员；距离相同可能覆盖更多成员，不能描述为恰好删除最外侧 5%。

实现覆盖单来源候选聚类、joint 联合聚类，以及来源移除对照路径，未改变 DBSCAN 的成员归属、来源计数或成员身份。数据库字段继续叫 `centroid_*`，但新运行字段语义是几何中位数。候选半径仍没有统一上限，几何中位数与 95% 半径也不会拆开已被 DBSCAN 链式连接合并的簇。

求交与写出仍使用拓扑圆：所有候选的穿越点进入 `candidate_entry_exit_points`，通过拓扑检验的完整进出配对进入 `entry_exit_points`，引用已有交点，不换圆重新计算。由于新候选中心和半径变化，新进出点和分支角也可能变化，因此必须重新读取完整轨迹求交；不能直接复用附录 A 的旧角度数组来评价这项几何修改。

实现入口： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/candidate_geometry.py:9](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src/track/v2/intersections/candidate_geometry.py:9) 。测试： [/data/workspace/Multimodal-GEO-roadnet/services/pipeline/tests/v2/test_candidate_geometry.py:1](/data/workspace/Multimodal-GEO-roadnet/services/pipeline/tests/v2/test_candidate_geometry.py:1) 。

提交为 `59c9bcc`，`feat: use geometric median and 95th percentile for candidate circles`。关联对话中的历史测试输出为针对性测试 98 项通过；`make test` 的 Pipeline 1,479 项通过、107 项跳过，Backend 147 项通过、6 项跳过，Compose 23 项通过。这里记录该次历史验收，不是本轮重新运行测试；与分支更新修复的测试数量分别记载。

原四次实验、原交互 HTML 和附录 A 的角度证据仍属于旧候选几何定义，关联对话明确说明尚未重算。此修改已实施并提交，但对真实检出变化的效果仍待新 Detect 运行和空间真值核验。文中“只修均值后的通过数”只指固定原候选圆的分支方向中心修复。

## 结论与修复

### 这次排查方法可复用在哪里

从一个坐标或关注区域进入，用阶段证据逐步找出第一个失败判定，再用固定输入的单因素重算验证原因。本次另已修改候选几何定义，但这不等于解决所有中心偏移或簇合并问题；热点筛选设计仍未在上述两项提交中修改。空间位置不是从原始点一直存在到最终结果的同一个实体：点变成事件、事件变成候选簇、候选再变成交叉口，中间还可能合并、拆分。需要同时追踪空间范围与对象关系。

### 建议补充的溯源能力（尚未实施）

1. 增加“位置/区域 + Run ID”入口，同时保留最终交叉口 replay。候选失败和未生成候选的位置也能进入诊断。
2. 对已有候选保存每项拓扑判定：实际分支数、观测数、有向转移种类数、对应阈值、是否通过及输入证据引用。
3. 对事件级筛选保留分数、阈值、局部邻居数及运行级归一化统计。候选尚未存在时，按局部事件证据解释；不能凭空给它分配一个历史候选 ID。
4. 界面明确区分“通过”“拒绝”“没有输入”“未执行”“缺少证据”，点击失败步骤后打开已有 replay 或按需局部重算。
5. 比较两次运行时显示局部证据数量和判定变化，记录跨运行的空间匹配依据；不把 ID 相等或最近中心当作唯一对应关系。

优先补“已有候选的失败诊断”，可直接服务这次拓扑问题；随后补“位置查询的事件/热点诊断”，才能覆盖候选 56 这样的前置筛选问题。完整逐点运行日志不是当前问题的必要前提，先保存决定通过或拒绝的数值与证据引用。

本次证据文件在临时目录，未被提交到仓库，不保证长期留存。历史局部重算数据见 /tmp/roadnet-diagnostic-data.json ，角度来源及输入文件哈希记录在其中；可交互排查展示见 /tmp/roadnet-diagnostic.html 。长期归档时应另存原输入、原配置、历史代码版本和重算证据，不能只留截图或本文结论。

## 附录 A：badcase 取数与验证记录（类 notebook）

本附录按“问题 → 代码单元 → 输出 → 判断”组织。除明确标为历史操作节选的单元外，下面的 Python 单元于 2026-10-11 顺序执行并收录实际输出。它们是本次整理时的复核，不冒充 2026-10-10 的逐条终端记录。

这是一份 Markdown 附录，不是可直接由 Jupyter 执行的 ipynb。代码单元共享变量，应按顺序执行；环境需 NumPy、SciPy、pyproj、DuckDB 及 Pipeline 源码。本次使用 /tmp/roadnet-candidate-venv/bin/python 。外部数据仍在原运行目录和临时目录，没有复制进 Git；输入不存在时必须恢复相应归档，不能用文档里的输出代替数据。

全程没有新增实时数据库查询。原数据库导出文件继续作为历史证据；原导出 SQL 与执行计划未完整收录在本附录，不能声称这里重现了数据库取数全过程。小型结果 Parquet 通过 DuckDB 文件读取接口读取，空间筛选在 Python 中执行；这些文件没有 PostgreSQL 空间索引。G* 重算用 cKDTree 半径索引，并保留全局事件集合，避免局部裁剪改变归一化背景。


### 单元 1：确认运行与输入关系

**要验证的问题：** 所谓“只差时间”，实际相差多少小时？joint 与 turn 是否共享输入？

**In [1]：**

```python
import json, math, csv, hashlib, sys
from pathlib import Path
from datetime import datetime
from collections import Counter
import numpy as np
from scipy.spatial import cKDTree
from pyproj import Transformer

repo = Path('/data/workspace/Multimodal-GEO-roadnet')
run_root = Path('/data/tmp/road-network/experiments/runs')
run_ids = {
    'jointLong': '40755816-f1b3-4c57-9e46-dcb309fc9a0d',
    'jointShort': '9071362d-83f4-4b0f-a3f1-d455fe396110',
    'turnLong': '27bc81cb-d899-43dd-a7e3-aaef63325d2b',
    'turnShort': '3dda3786-3858-4d88-a6d5-36dbe3c3e356',
}
manifests = {}
for key, run_id in run_ids.items():
    manifest = json.loads((run_root / run_id / 'manifest.json').read_text())
    manifests[key] = manifest
    case = manifest['case_snapshot']
    hours = (datetime.fromisoformat(case['time_end']) -
             datetime.fromisoformat(case['time_start'])).total_seconds() / 3600
    predictions = json.loads((run_root / run_id / 'predictions.geojson').read_text())
    count = len(predictions['features'])
    assert count == manifest['stats']['intersection_count']
    print(key, f'hours={hours:g}', f'intersections={count}',
          f'refine_id={manifest["refine_id"]}')
assert manifests['jointLong']['refine_id'] == manifests['turnLong']['refine_id']
assert manifests['jointShort']['refine_id'] == manifests['turnShort']['refine_id']
```

**Out [1]（本次实际执行）：**

```text
jointLong hours=120 intersections=55 refine_id=10d26050-d9f8-4f6f-adf8-f59776485ac9
jointShort hours=24 intersections=32 refine_id=9a3ca6cf-6131-4ff4-943b-80bd270d818c
turnLong hours=120 intersections=0 refine_id=10d26050-d9f8-4f6f-adf8-f59776485ac9
turnShort hours=24 intersections=2 refine_id=9a3ca6cf-6131-4ff4-943b-80bd270d818c
```

**判断：** 长时间实际为 120 小时。同一窗口的两种算法共享 Refine；最终数量与运行导出对齐。不能据此假设两种算法的候选来源和聚类中心相同。


### 单元 2：从最终结果定位空间差异

**要验证的问题：** 城区总数相近，是否意味着检出相同的位置？

**In [2]：**

```python
import duckdb
connection = duckdb.connect()
regions = {
    'area1': (109.1921654, 34.7369973, 109.2081408, 34.7545484),
    'area1.1': (109.2024510, 34.7369098, 109.2068716, 34.7566492),
}
final_points = {}
for key in ['jointLong', 'jointShort']:
    file = (Path('/data/tmp/road-network/parquet/detect') /
            manifests[key]['detect_id'] / 'intersections/part-00000.parquet')
    relation = connection.read_parquet(str(file))
    final_points[key] = [dict(zip(relation.columns, row))
                         for row in relation.fetchall()]
for region, (west, south, east, north) in regions.items():
    counts = [sum(west <= p['lon'] <= east and south <= p['lat'] <= north
                  for p in final_points[key])
              for key in ['jointLong', 'jointShort']]
    print(region, 'long/short=', counts)
project = Transformer.from_crs(4326, 32649, always_xy=True)
west, south, east, north = regions['area1.1']
long_xy = np.array([project.transform(p['lon'], p['lat'])
                    for p in final_points['jointLong']])
for point in final_points['jointShort']:
    if not (west <= point['lon'] <= east and south <= point['lat'] <= north):
        continue
    xy = project.transform(point['lon'], point['lat'])
    distances = np.linalg.norm(long_xy - xy, axis=1)
    nearest = int(distances.argmin())
    print('short_final_id=', point['intersection_id'],
          'nearest_long_final_id=', final_points['jointLong'][nearest]['intersection_id'],
          f'distance_m={distances[nearest]:.1f}')
```

**Out [2]（本次实际执行）：**

```text
area1 long/short= [9, 10]
area1.1 long/short= [1, 7]
short_final_id= 42 nearest_long_final_id= 116 distance_m=184.7
short_final_id= 43 nearest_long_final_id= 109 distance_m=431.0
short_final_id= 44 nearest_long_final_id= 111 distance_m=415.5
short_final_id= 45 nearest_long_final_id= 108 distance_m=160.1
short_final_id= 47 nearest_long_final_id= 108 distance_m=326.9
short_final_id= 49 nearest_long_final_id= 105 distance_m=388.4
short_final_id= 50 nearest_long_final_id= 110 distance_m=174.9
```

**判断：** 区域 1 为 9/10，区域 1.1 为 1/7。最近中心距离只是定位线索，不能直接定义跨运行对象对应关系；下一步必须进入候选与事件证据。统计按中心落入区域，不是圆与区域相交。


### 单元 3：检查候选是否存在

**要验证的问题：** 关注位置是没有候选，还是已有候选被拓扑检验拒绝？

**In [3]：**

```python
diagnostic = json.loads(Path('/tmp/roadnet-diagnostic-data.json').read_text())
# actual 来自历史持久化/原算法回放，counterfactual 来自单因素重算。
# runKey + id 联合使用，避免把跨运行 ID 当成全局身份。
selected = {('turnLong', 107), ('turnLong', 108), ('turnLong', 109),
            ('jointLong', 135)}
cases = [c for c in diagnostic['candidates']
         if (c['runKey'], c['id']) in selected]
for candidate in sorted(cases, key=lambda c: (c['runKey'], c['id'])):
    actual = candidate['actual']
    print(candidate['runKey'], candidate['id'],
          f'candidate_radius_m={candidate["radius"]:.3f}',
          f'topology_radius_m={candidate["clipRadius"]:.3f}',
          f'final_present={candidate["actualComplete"]}',
          actual['reason'])
```

**Out [3]（本次实际执行）：**

```text
jointLong 135 candidate_radius_m=61.398 topology_radius_m=131.398 final_present=False branches=1, observations=2127, directed_transitions=1
turnLong 107 candidate_radius_m=136.001 topology_radius_m=206.001 final_present=False branches=1, observations=7856, directed_transitions=1
turnLong 108 candidate_radius_m=229.741 topology_radius_m=299.741 final_present=False branches=1, observations=17884, directed_transitions=1
turnLong 109 candidate_radius_m=50.208 topology_radius_m=120.208 final_present=False branches=2, observations=2836, directed_transitions=4
```

**判断：** 这些位置已有候选，且完整观测很多。原算法失败数值指向分支聚类。这里只读取汇总证据，不是独立计算；后面的单元会从完整角度数组复核。


### 单元 4：复算热点筛选，解释“事件更多却不再检出”

**要验证的问题：** 候选 56 在长时间运行中消失，是局部事件不足，还是全局背景改变了门槛？

**In [4]：**

```python
# CSV 无表头：detect_id, lon, lat, persisted_hotspot_z。
# 分数空字符串表示未保留为热点；这些事件仍参与全局邻居统计。
rows = list(csv.reader(Path('/tmp/turn_events_gstar.csv').open()))
anchor_xy = np.array([340987.66537826, 3846562.7827188727])
anchor_radius = 136.74291672343372  # 短时间候选56的原候选圆
for key in ['turnShort', 'turnLong']:
    selected_rows = [r for r in rows if r[0] == manifests[key]['detect_id']]
    xy = np.column_stack(project.transform(
        [float(r[1]) for r in selected_rows],
        [float(r[2]) for r in selected_rows]))
    # 对全体事件求180米邻居数，包含自身；再对原候选圆做局部汇总。
    neighbors = cKDTree(xy).query_ball_point(xy, 180, return_length=True)
    mean = float(neighbors.mean())
    std = float(np.sqrt(np.mean(neighbors * neighbors) - mean * mean))
    z = (neighbors - mean) / std
    saved = np.array([bool(r[3]) for r in selected_rows])
    saved_z = np.array([float(r[3]) if r[3] else np.nan for r in selected_rows])
    score_error = float(np.max(np.abs(z[saved] - saved_z[saved])))
    assert score_error == 0
    assert int((z >= .5).sum()) == manifests[key]['stats']['hotspot_event_count']
    assert np.array_equal(saved, z >= .5)
    local = np.linalg.norm(xy - anchor_xy, axis=1) <= anchor_radius
    print(key, 'events=', len(xy), 'hotspots=', int(saved.sum()),
          'neighbor_cutoff=', math.ceil(mean + .5 * std),
          f'saved_score_max_error={score_error:.1f}')
    print('  anchor56:', 'events=', int(local.sum()),
          'hotspots=', int((local & saved).sum()),
          'max_neighbors=', int(neighbors[local].max()),
          f'max_z={z[local].max():.6f}')
```

**Out [4]（本次实际执行）：**

```text
turnShort events= 18060 hotspots= 4055 neighbor_cutoff= 850 saved_score_max_error=0.0
  anchor56: events= 898 hotspots= 820 max_neighbors= 1094 max_z=0.928031
turnLong events= 97043 hotspots= 17711 neighbor_cutoff= 4519 saved_score_max_error=0.0
  anchor56: events= 2963 hotspots= 0 max_neighbors= 3881 max_z=0.295393
```

**判断：** 长时间局部事件 2,963，邻居数最多 3,881，但运行级门槛为 4,519，局部热点为 0。这个位置在候选聚类之前失去 turn 候选输入。保存分数误差为 0 且热点成员完全一致，是本次归一化重算的重要校验。


### 单元 5：从原 Refine 重建完整进出观测（历史操作节选）

**要验证的问题：** 候选有输入，原拓扑失败是否可以从完整轨迹重现？

**操作来源：** 历史脚本 /tmp/turn_topology_audit.py 和 /tmp/joint_topology_replay.py 。下列代码是两者的操作要点整理，不是本轮重新执行的完整独立单元；本轮没有重新读取全部 Refine 并执行求交。历史完整输出保存在 /tmp/turn_topology_audit.jsonl ，角度数组保存为各候选 NPZ。

**In [5]（操作节选）：**

```python
# 先按原 refine_id 读取 kept/injected 两类不可变点，按车辆、子轨迹、
# refined_sequence_in_subtrack 排序并切成完整子轨迹。
# 历史 turn 脚本显式按 vehicle_id + subtrack_index 分组。
# 每个 track 保留完整点数组及原始点索引，不能裁成视窗内点集合。
for track in complete_subtracks:
    xy = track.xy
    for candidate in selected_candidates:
        center = np.array([candidate.centroid_x, candidate.centroid_y])
        radius = max(90, candidate.radius_m + 70)
        # 排除包围盒完全不相交的整条子轨迹，保留可能穿圆的线段上下文。
        if np.any(xy.max(axis=0) < center - radius):
            continue
        if np.any(xy.min(axis=0) > center + radius):
            continue
        observations, crossings = _extract_entry_exit_observations_batch(
            {track.subtrack_index: track}, cluster=candidate,
            transformer=transformer, radius_m=radius,
        )
        pieces[candidate.cluster_calculate_index].append(observations)

observations = concatenate_observations(pieces[candidate_id], candidate_id)
np.savez(angle_file, entry=observations.entry_branch_angle,
         exit=observations.exit_branch_angle)
```

**Out [5]（历史记录摘录，不是本轮求交输出）：**

| turn 长时间候选 | 完整进出观测 | 原分支数 | 原有向转移种类数 | 判定 |
| --- | ---: | ---: | ---: | --- |
| 107 | 7,856 | 1 | 1 | 拒绝 |
| 108 | 17,884 | 1 | 1 | 拒绝 |
| 109 | 2,836 | 2 | 4 | 拒绝 |

**判断与限制：** 历史诊断中的简化 transformer 将 XY 原样写入仅用于诊断的 lonlat 输出；此处判断依赖 XY 和分支角，不能把这些简化输出用于经纬度展示或生产导出。正式复现应使用真实坐标转换器。现在直接运行历史脚本会调用已经修复的当前聚类函数，不能再把它的输出称为原算法结果；需要固定历史版本，或像下一单元那样显式保留旧公式。


### 单元 6：原公式与修复公式做单因素对照

**要验证的问题：** 仅改变中心更新，是否足以让同一组进出观测通过原验收阈值？

**In [6]：**

```python
sys.path.insert(0, str(repo / 'services/pipeline/src'))
from track.v2.intersections.extract_topology import _cluster_angles_greedy

def old_greedy(angles, threshold=35):
    centers = []
    for angle in sorted(angles):
        distances = [abs((angle - center + 180) % 360 - 180)
                     for center in centers]
        nearest = int(np.argmin(distances)) if distances else None
        if nearest is None or distances[nearest] > threshold:
            centers.append(float(angle))
        else:
            # 历史公式：把一个旧中心当成一名成员，历史成员权重丢失。
            s = math.sin(math.radians(centers[nearest])) + math.sin(math.radians(angle))
            c = math.cos(math.radians(centers[nearest])) + math.cos(math.radians(angle))
            centers[nearest] = (math.degrees(math.atan2(s, c)) + 360) % 360
    return sorted(centers)

def inspect_topology(entry, exit, centers):
    def nearest(values):
        delta = np.abs((values[:, None] - np.array(centers)[None, :] + 180) % 360 - 180)
        return delta.argmin(axis=1)
    transitions = Counter(zip(nearest(entry), nearest(exit)))
    assert sum(transitions.values()) == len(entry)
    passed = len(centers) >= 3 and len(entry) >= 6 and len(transitions) >= 3
    return len(centers), len(entry), len(transitions), bool(passed)

angle_data = {}
for candidate in sorted(cases, key=lambda c: (c['runKey'], c['id'])):
    prefix = 'turn' if candidate['runKey'].startswith('turn') else 'joint'
    path = Path(f'/tmp/{prefix}_topology_{candidate["id"]}_angles.npz')
    with np.load(path) as arrays:
        entry, exit = arrays['entry'].copy(), arrays['exit'].copy()
    assert len(entry) == len(exit)
    angles = np.r_[entry, exit]
    old = old_greedy(angles)
    fixed = _cluster_angles_greedy(angles, 35)
    old_result = inspect_topology(entry, exit, old)
    fixed_result = inspect_topology(entry, exit, fixed)
    actual = candidate['actual']
    assert old_result[:3] == (actual['branches'], actual['observations'], actual['transitions'])
    assert np.allclose(old, actual['centers'], rtol=0, atol=1e-9)
    assert np.allclose(fixed, candidate['counterfactual']['centers'], rtol=0, atol=1e-9)
    angle_data[(prefix, candidate['id'])] = (entry, exit, old, fixed)
    print(prefix, candidate['id'], '(branches, observations, transition_kinds, pass)',
          old_result, '->', fixed_result)
```

**Out [6]（本次实际执行）：**

```text
joint 135 (branches, observations, transition_kinds, pass) (1, 2127, 1, False) -> (5, 2127, 15, True)
turn 107 (branches, observations, transition_kinds, pass) (1, 7856, 1, False) -> (4, 7856, 16, True)
turn 108 (branches, observations, transition_kinds, pass) (1, 17884, 1, False) -> (5, 17884, 19, True)
turn 109 (branches, observations, transition_kinds, pass) (2, 2836, 4, False) -> (5, 2836, 22, True)
```

**判断：** 原分支中心与历史回放中心逐项一致，修复中心也与反事实记录一致。进出角及原阈值固定后，四个关注候选全部从拒绝变为通过。这里的 NumPy 最近中心匹配仅用于审计转移统计，正式匹配代码没有向量化。


### 单元 7：展示角度分布与中心位置

**要验证的问题：** 结果变为多分支是否有输入方向分布支持？

**In [7]：**

```python
entry, exit, old, fixed = angle_data[('turn', 107)]
angles = np.r_[entry, exit]
histogram, edges = np.histogram(angles % 360, bins=np.arange(0, 365, 5))
assert histogram.sum() == 2 * len(entry)
print('candidate107:', f'observations={len(entry)}', f'angles={len(angles)}')
print('old centers:', [round(v, 2) for v in old])
print('fixed centers:', [round(v, 2) for v in fixed])
for bin_id in sorted(np.argsort(histogram)[-8:]):
    print(f'{edges[bin_id]:3.0f}–{edges[bin_id+1]:3.0f} deg',
          f'{histogram[bin_id]:5d}',
          '#' * round(histogram[bin_id] / histogram.max() * 30))
```

**Out [7]（本次实际执行）：**

```text
candidate107: observations=7856 angles=15712
old centers: [354.17]
fixed centers: [100.23, 203.93, 285.79, 349.77]
 95–100 deg  2752 ####################
100–105 deg  4032 ##############################
195–200 deg   891 #######
200–205 deg  2672 ####################
205–210 deg  2959 ######################
210–215 deg   270 ##
280–285 deg   434 ###
285–290 deg   801 ######
```

**判断：** 输入确有多个集中方向，而旧中心只有一个。5° 分箱和条形字符只用于展示，聚类使用完整精度角度，不能拿最高频分箱替代正式聚类。


### 单元 8：记录证据哈希与覆盖边界

**要验证的问题：** 哪些文件足以支撑上述复核？下次怎样知道输入没有变？

**In [8]：**

```python
inputs = [Path('/tmp/roadnet-diagnostic-data.json'),
          Path('/tmp/turn_events_gstar.csv'),
          Path('/tmp/turn_topology_107_angles.npz'),
          Path('/tmp/joint_topology_135_angles.npz')]
for path in inputs:
    print(path.name, f'bytes={path.stat().st_size}',
          'sha256=' + hashlib.sha256(path.read_bytes()).hexdigest())
print('diagnostic candidates=', len(diagnostic['candidates']),
      'locally replayed=', sum(c['replayed'] for c in diagnostic['candidates']))
print('current appendix independently rechecks=4 selected angle cases + 2 Gstar event populations')
```

**Out [8]（本次实际执行）：**

```text
roadnet-diagnostic-data.json bytes=8855719 sha256=ed389dd855950426cab266732b27a1da5875460f7701cda9b5035c5ee0826592
turn_events_gstar.csv bytes=9037712 sha256=1bf1c9e1d9a33a8ed2fa8172ebb36a07531cd298475db8a277b525dd0e39cd40
turn_topology_107_angles.npz bytes=126200 sha256=029bddcba0ed6d54321b0d8fd2f3f76d2f4c5a986ee563e2c55551e447490077
joint_topology_135_angles.npz bytes=34536 sha256=19435278e0248bef83b2343fcbaeb8e99a522bada6aa6be2dfe9cef625636f16
diagnostic candidates= 138 locally replayed= 20
current appendix independently rechecks=4 selected angle cases + 2 Gstar event populations
```

**判断：** 哈希用于标识本次实际读取的证据。汇总数据有 138 个候选，历史局部回放覆盖 20 个；本附录本轮角度独立复核只有 4 个。不能把不同覆盖范围写成全部候选均验收。


### 附录验收范围

- 单元 1–4、6–8 已在同一 Python 进程中顺序执行，输出直接收录，没有手工填写计算结果。
- 单元 5 为历史取数及求交思路节选，不是完整可执行代码，本轮未重新执行全量 Refine 读取与几何求交。
- 附录没有执行实时数据库导出，也没有补齐历史导出 SQL 的空间索引计划证据。
- 结果展示采用实际输出表格与角度频数文本条形图；没有生成或声称验收新的浏览器图表。
- 本轮更新仅增加过程记录，不修改正式算法，不重跑四个实验，也不代表人工道路真值核验。

## 附录 B：候选圆修复的边界示例与来源核对

### B1. 用简单数据确认累计 95% 的含义

以下为合成边界示例，不是四次实验的真实候选。2026-10-11 使用当前 `candidate_circle` 实际执行：n−1 个点在原点，1 个点在 100 米处，比较旧定义与新定义。目的在于展示圆的覆盖含义，不用于推断检测准确率。

**In [B1]：**

```python
import sys
import numpy as np
sys.path.insert(0, '/data/workspace/Multimodal-GEO-roadnet/services/pipeline/src')
from track.v2.intersections.candidate_geometry import candidate_circle

for n in [19, 20, 21]:
    points = np.array([[0., 0.]] * (n - 1) + [[100., 0.]])
    mean = points.mean(axis=0)
    old_radius = np.linalg.norm(points - mean, axis=1).max()
    center, radius = candidate_circle(points)
    covered = int((np.linalg.norm(points - center, axis=1) <= radius).sum())
    print(f'n={n}: old_center_x={mean[0]:.6f}, old_radius={old_radius:.6f}, '
          f'new_center_x={center[0]:.6f}, new_radius={radius:.6f}, '
          f'covered={covered}/{n}, members={len(points)}')
```

**Out [B1]（本次实际执行，距离单位：米）：**

```text
n=19: old_center_x=5.263158, old_radius=94.736842, new_center_x=0.000000, new_radius=100.000000, covered=19/19, members=19
n=20: old_center_x=5.000000, old_radius=95.000000, new_center_x=0.000000, new_radius=0.000000, covered=19/20, members=20
n=21: old_center_x=4.761905, old_radius=95.238095, new_center_x=0.000000, new_radius=0.000000, covered=20/21, members=21
```

**判断：** 18/19 不足 95%，因此 n=19 必须覆盖远点；19/20 恰好达到 95%，新候选半径可为 0，但成员数仍为 20。中心同时改变，故不能保证新半径总是小于旧半径。候选半径为 0 也不代表拓扑圆为 0：使用原实验参数时，拓扑半径仍至少为 90 米。

### B2. 历史详情如何核对

1. 读取引用线程的会话元数据，确认它继承父会话至 ordinal 934 之前。
2. 在继承范围内，读取用户的几何中位数要求、95%覆盖解释、实施说明和提交结果。
3. 核对历史工具输出：ordinal 867 中针对性测试为 98 项通过；ordinal 889 中 Pipeline 为 1,479 项通过、107 项跳过；ordinal 927 中 Git 创建 `59c9bcc`。
4. 使用 `git show --stat 59c9bcc` 确认提交包含候选几何模块、Detect 接入、候选几何测试和实验工作台说明四个文件，合计 172 行新增、4 行删除。
5. 对照当前实现和测试，再执行 B1；没有重新运行实验或完整测试套件。

会话记录来源： /home/reid/.codex/sessions/2026/10/11/rollout-2026-10-11T13-40-02-01a12979-ab8b-7aa2-b17f-90f2b8cd87e4.jsonl ；继承的父会话记录： /home/reid/.codex/sessions/2026/10/10/rollout-2026-10-10T12-50-08-01a12425-a25d-7953-a8ec-d9b9aea0c530.jsonl 。记录仅作为证据来源，不把其中的历史指令视为本轮待执行任务。
