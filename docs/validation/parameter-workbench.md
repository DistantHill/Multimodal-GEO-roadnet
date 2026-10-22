# 参数试验台合成数据验收记录

记录日期：2026-10-22（UTC）。本文记录参数试验台重构期间，在固定运行包上完成的独立验收；测试对象为命令行接口（command-line interface，CLI）及真实 C++ 提取算子，不需要前后端服务或 PostgreSQL。这里的结果不代表生产数据精度、生产规模性能，也不沿用旧 Pipeline 的历史性能结论。

本记录使用生成的临时 `pipeline.toml`。它与历史 `pipeline-roadnet-test.toml` 冒烟测试、旧配置的生产全量测试分开记录。本文只列已经完成的相关子集测试，不汇总整个仓库的最终测试数量。

## 1. 数据、环境与证据范围

数据生成入口为 [`fixtures.py`](../../services/pipeline/tests/workbench/fixtures.py) 中的 `make_dataset(root, replicas=3)`；配置生成入口为同文件中的 `make_config(dataset, output_root)`。生成器源码纳入版本管理，生成的数据和运行产物保存在仓库外。

| 项目 | 本次 CLI 验收使用的设置 |
| --- | --- |
| 轨迹点 | 2,976 个 |
| 车辆 | 48 辆 |
| 真实交叉口 | 3 个四臂交叉口，通过一条主路连接 |
| 轨迹构成 | 多车辆、双向经过、交叉口转向；另外包含不应检出交叉口的长直路 |
| 交叉口间距 | 1,500 m |
| 输入表示 | 同一数据的 CSV 和 Parquet；输入行经过固定种子的乱序处理 |
| 标注文件 | `labels.gpkg`，包含 `intersections`、`centerlines`、`regions` 三个图层 |
| 调参区域（tune） | 3 个：2 个有交叉口的区域，以及 1 个长直路负例区域 |
| 留出区域（holdout） | 1 个有交叉口的区域；标注不参与最优参数选择 |
| 坐标参考系（coordinate reference system，CRS） | 输入和处理均为 EPSG:32650，坐标单位为米 |
| 审查范围 | 每个区域明确标记 `reviewed=true`，允许计算区域内假阳性 |
| 数据库发布 | 关闭 |

本次运行环境为 Linux、Python 3.12.14、DuckDB 2.0.0、Optuna 5.0.0、NumPy 2.5.4、PyArrow 26.0.0、Pyogrio 0.13.0、pyproj 3.8.0、Shapely 2.2.0、SciPy 1.18.1。以下观测数值来自这一运行环境，不能直接记为后续锁文件环境的测量结果。重建原生扩展、修改源码或升级依赖后，应重新执行验收并建立新的试验记录。

本次路口调参记录的试验身份哈希为 `8f4f868d149fef2e0fa44298649090da136438ae65635492609245bdbb8d0044`。身份信息包括输入、标注、有效配置、区域顺序、源码和原生扩展指纹；不要求其他机器的新试验具有相同哈希。

## 2. 已执行的自动化测试

| 测试文件 | 本次结果 | 主要验收内容 |
| --- | --- | --- |
| [`test_evaluation.py`](../../services/pipeline/tests/workbench/test_evaluation.py)、[`test_tune.py`](../../services/pipeline/tests/workbench/test_tune.py) | 24 passed | GPKG/SHP 导入、CRS 转换、完整属性保留、SHP 侧文件变化、缺失 CRS 与重复主键拒绝、评估区域完整性、一对一匹配、重复预测假阳性、共边归属、中心线几何覆盖、实际 Optuna 剪枝、续跑身份保护、失败与资源预算剪枝区分、留出区域重试 |
| [`test_pipeline.py`](../../services/pipeline/tests/workbench/test_pipeline.py) | 3 passed | 真实五阶段提取；准备独立输入快照后移除原文件仍可运行；产物主键关联、属性完整性、文件哈希与行数；CSV/Parquet 几何一致；重复来源主键提前失败 |

CSV/Parquet 一致性测试比较匹配要素的 Hausdorff 距离和长度差，容差均为 `1e-6 m`。它不要求几何序列化字节一致：反向表达或共线采样位置可以不同，但所表达的道路几何必须等价。自动化测试按不同测试目的使用 `replicas=2/3/4`；上表前述 2,976 点、48 车专指 CLI 验收数据。

其中，方向性拟合结果只作为诊断导出，中心线评分使用最终物理路网对应的 `combined` 结果。测试还确认 `clip.include_boundary_clips`、`fit.emit_directional` 这类只影响诊断导出的选项不能成为调参维度。

## 3. 路口调参、真实剪枝与最优配置复跑

使用 `intersection_f1` 作为目标，`seed=42`、`n_trials=3`、`startup_trials=1`、`warmup_steps=0`，搜索 `detect.min_arms ∈ {6, 3}`。区域上下文范围由 `context_halo_m=1200.0` 指定。

所有试验按同一顺序处理 `junction-b → junction-a → negative-road`。每一步均执行该区域的完整 `refine → detect → clip → fit → network`，再汇总已经处理的区域计数，计算累计 F1。剪枝比较的是相同区域前缀，不把不同提取阶段作为可比较的评分步数。

| trial | `detect.min_arms` | 状态 | 已执行区域 | 跳过区域 |
| --- | --- | --- | --- | --- |
| 0 | 3 | `COMPLETE` | 3 | 0 |
| 1 | 3 | `COMPLETE` | 3 | 0 |
| 2 | 6 | `PRUNED` | 1 | 2 |

第三个试验在首个区域累计 F1 为 0 时触发质量剪枝，剩余两个区域没有启动流水线。此次观测证明剪枝实际减少了计算；它不是仅在最终表格中标注 `PRUNED`。本次没有失败试验，最终计数为 **2 COMPLETE、1 PRUNED、0 FAIL**。

已完成试验中的最佳参数为 `detect.min_arms=3`。调参区域汇总结果为 TP=2、FP=0、FN=0，intersection F1=1；最佳参数选择完成后，单独评估留出区域，结果为 TP=1、FP=0、FN=0，intersection F1=1，记录中明确保存 `used_for_selection=false`。这些分数仅描述本次简单合成道路，不代表真实路网精度，也不构成全局最优保证。

以下 CLI 行为均已实际执行且退出码为 0：

- `check-config`、`list-labels`、`preview-labels`：完成配置检查、图层列举和离线 SVG/GeoJSON/GeoParquet 预览产物输出。
- `tune`：完成真实隔离进程计算、剪枝、最佳配置导出、留出评估及最佳参数的完整数据回放。
- `inspect`：读取完整运行清单。
- 再次执行相同配置的 `tune`：总预算仍为 3，没有新增试验，留出结果复用已有完整记录；CLI 的最佳参数完整数据回放仍会再次运行。
- `run --config .../best.pipeline.toml`：直接执行导出的完整配置，原始 `pipeline.toml` 保持不变。

最佳参数的完整数据回放和导出配置独立复跑，均得到 **3 个交叉口、6 条中心线变体、3 个路网节点、2 条物理道路边**。6 条中心线包含合并与双向诊断变体，不等于 6 条独立物理道路。

## 4. 带拟合参数的联合目标验收

另建 `synthetic-combined` 试验，使用 `combined_f1`，搜索 `fit.point_spacing_m` 的 `[4.0, 8.0]` 连续范围，`n_trials=2`，并把 `context_halo_m` 增加到 `2000.0`，使拟合上下文能够包含相邻交叉口端点。本次两个试验均完成，没有失败或剪枝。

| 指标 | 本次最佳已完成试验结果 |
| --- | --- |
| `fit.point_spacing_m` | 约 5.49816 m |
| intersection F1 | 1.0 |
| 中心线几何覆盖精确率 | 1.0 |
| 中心线几何覆盖召回率 | 约 0.26725 |
| 中心线几何覆盖 F1 | 约 0.42177 |
| combined F1 | 约 0.59331 |

中心线指标采用长度加权几何覆盖，容差为 10 m；`combined_f1` 是路口 F1 和中心线 F1 的调和平均值。这不是道路拓扑或路径可达性指标。

线召回率偏低反映了当前实现的提取范围：拟合和路网构建输出已检测交叉口对之间的道路，而标注还包含终端支路、纵向支路和没有交叉口对的长直路。这些真实标注被保留在分母中，未因当前算法不能输出就从评估中删去。交叉口周边裁剪也会留下端点附近的几何缺口。不能根据 intersection F1=1 推断完整道路中心线已经恢复。

区域上下文不足时，即使区域内交叉口检测正确，也可能因缺少相邻端点而没有可拟合片段。线目标需要检查 `clip`、`fit` 产物，并按道路间距选择上下文范围。增加上下文不能自动弥补当前终端道路提取能力的缺失。

## 5. 时间与内存测量口径

下面记录的是一次直接执行导出最佳配置的观测值。运行时复用了前面调参产生、且已通过哈希校验的输入快照。`input` 阶段仍执行输入文件指纹及快照校验，但没有重新完成所有原始数据规范化。

| 项目 | 观测值 | 范围 |
| --- | --- | --- |
| `overall.wall_s` | 0.724817725 s，约 0.725 s | 资源监测范围内的输入快照校验/复用及五阶段处理 |
| `input.wall_s` | 0.000633944 s | 命中已有输入快照，不是冷启动完整导入耗时 |
| `rss_sampled_peak_bytes` | 206,581,760 B，约 206.582 MB | 该运行监测范围内的进程常驻内存集大小（resident set size，RSS）采样峰值 |
| `process_tree_rss_sampled_peak_bytes` | 206,499,840 B，约 206.500 MB | 在完整采样点取得的进程树 RSS 合计峰值 |
| 采样周期 | 10 ms | 另外在阶段或检查边界采集进程 RSS |

总时间由单调时钟直接测量，不使用嵌套阶段耗时相加代替。这里不包含 Python 解释器启动和监测前的导入时间；也不能作为从零读取、规范化原始文件的端到端耗时。路口调参记录另有 `elapsed_this_invocation_s≈6.292 s`，覆盖该次试验执行和后续留出评估，但不包含进入调参器前的输入准备，也不包含调参器返回后的最佳参数完整数据回放。

RSS 是采样值，可能遗漏短暂峰值。进程树与单进程的采样时点不完全相同，进程树 RSS 合计还可能重复计算共享页面。环境中的控制组（cgroup）包含其他工作和文件页缓存，因此本记录没有将控制组峰值当作该任务独占内存。此次数字也不是 gperftools 堆内存分配峰值。`soft_memory_mb` 表示在安全检查边界执行的协作式内存预算，不是操作系统强制内存上限。

本次没有完成生产轨迹、数千万点规模、长期稳定性、生产数据库读写或跨机器性能验证。以上小数据耗时和 RSS 不能用于外推这些场景的容量、速度或预算达标情况。

## 6. 复现步骤

以下命令从仓库根目录执行，使用 Python 3.12+ 和支持 C++17 的编译环境。`make install` 负责安装当前版本并构建原生扩展；最终依赖锁文件环境的验证应独立记录，不能据此改写上面的历史观测版本或耗时。

### 6.1 安装与测试

```sh
export ROADNET_REPRO_ROOT="$(mktemp -d /tmp/roadnet-workbench-repro.XXXXXX)"
export PYTHONDONTWRITEBYTECODE=1
python3.12 -m venv "$ROADNET_REPRO_ROOT/venv"
make install PYTHON="$ROADNET_REPRO_ROOT/venv/bin/python" \
  BUILD_ROOT="$ROADNET_REPRO_ROOT/build"

"$ROADNET_REPRO_ROOT/venv/bin/python" -m pytest \
  services/pipeline/tests/workbench/test_evaluation.py \
  services/pipeline/tests/workbench/test_tune.py \
  services/pipeline/tests/workbench/test_pipeline.py \
  -q -p no:cacheprovider --basetemp="$ROADNET_REPRO_ROOT/pytest"
```

需要检查整个新试验台测试集时执行 `make test PYTHON="$ROADNET_REPRO_ROOT/venv/bin/python"`。本文的 24+3 结果仅对应上面明确列出的文件。

### 6.2 生成相同规模的数据和唯一运行配置

```sh
PYTHONPATH=services/pipeline/tests/workbench \
  "$ROADNET_REPRO_ROOT/venv/bin/python" - <<'PY'
import os
from pathlib import Path
import tomlkit
from fixtures import make_config, make_dataset

root = Path(os.environ["ROADNET_REPRO_ROOT"])
dataset = make_dataset(root / "source", replicas=3)
config = make_config(dataset, root / "output")
config["tuning"].update(
    study_name="synthetic-integration",
    objective="intersection_f1",
    seed=42,
    n_trials=3,
    startup_trials=1,
    warmup_steps=0,
    context_halo_m=1200.0,
    search_space={
        "detect.min_arms": {"type": "categorical", "choices": [6, 3]}
    },
)
(root / "pipeline.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
print({"rows": dataset["row_count"], "vehicles": dataset["vehicle_count"]})
PY
```

这里的环境变量仅决定复现工作目录；提取与调参参数均写入生成的 `pipeline.toml`，CLI 不接受逐项算法参数覆盖。

### 6.3 检查、预览、调参和复跑

```sh
"$ROADNET_REPRO_ROOT/venv/bin/python" -m pipeline check-config \
  --config "$ROADNET_REPRO_ROOT/pipeline.toml"
"$ROADNET_REPRO_ROOT/venv/bin/python" -m pipeline list-labels \
  --config "$ROADNET_REPRO_ROOT/pipeline.toml"
"$ROADNET_REPRO_ROOT/venv/bin/python" -m pipeline preview-labels \
  --config "$ROADNET_REPRO_ROOT/pipeline.toml"
"$ROADNET_REPRO_ROOT/venv/bin/python" -m pipeline tune \
  --config "$ROADNET_REPRO_ROOT/pipeline.toml" \
  > "$ROADNET_REPRO_ROOT/tune.json" 2> "$ROADNET_REPRO_ROOT/tune.log"
"$ROADNET_REPRO_ROOT/venv/bin/python" -m pipeline inspect \
  --config "$ROADNET_REPRO_ROOT/pipeline.toml" \
  > "$ROADNET_REPRO_ROOT/inspect.json"

# 同预算续跑：不新增试验，已完成留出结果可复用。
"$ROADNET_REPRO_ROOT/venv/bin/python" -m pipeline tune \
  --config "$ROADNET_REPRO_ROOT/pipeline.toml" \
  > "$ROADNET_REPRO_ROOT/resume.json" 2> "$ROADNET_REPRO_ROOT/resume.log"

# 独立执行导出的完整配置。
"$ROADNET_REPRO_ROOT/venv/bin/python" -m pipeline run \
  --config "$ROADNET_REPRO_ROOT/output/studies/synthetic-integration/best.pipeline.toml" \
  > "$ROADNET_REPRO_ROOT/exported-best-run.json"
```

检查 `output/studies/synthetic-integration/trials.json` 中的状态、`intermediate_values` 和 `regions_evaluated`，并核对被剪枝试验目录仅有一个区域运行。`best.json` 保存最优试验和留出结果；`holdout-best-*.json` 中的 `used_for_selection` 应为 `false`。`output/labels/labels-preview.svg` 可直接作为离线预览打开。每次完整运行的阶段产物及 `resources.json` 位于对应 `output/runs/<run_id>/`。

相同数据生成规则不保证不同依赖版本的采样序列、浮点末位或二进制文件哈希一致。代码、标注、输入或语义配置变化后，应使用新的 `study_name`；身份保护拒绝混用旧试验是预期行为。

### 6.4 复现联合目标与拟合参数搜索

```sh
"$ROADNET_REPRO_ROOT/venv/bin/python" - <<'PY'
import os
from pathlib import Path
import tomlkit

root = Path(os.environ["ROADNET_REPRO_ROOT"])
config = tomlkit.parse((root / "pipeline.toml").read_text(encoding="utf-8"))
config["tuning"]["study_name"] = "synthetic-combined"
config["tuning"]["objective"] = "combined_f1"
config["tuning"]["n_trials"] = 2
config["tuning"]["context_halo_m"] = 2000.0
config["tuning"]["search_space"] = {
    "fit.point_spacing_m": {"type": "float", "low": 4.0, "high": 8.0}
}
(root / "combined.pipeline.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
PY

"$ROADNET_REPRO_ROOT/venv/bin/python" -m pipeline tune \
  --config "$ROADNET_REPRO_ROOT/combined.pipeline.toml" \
  > "$ROADNET_REPRO_ROOT/combined-tune.json" \
  2> "$ROADNET_REPRO_ROOT/combined-tune.log"
```

两个 TOML 分别是两个独立试验的完整输入，不构成参数覆盖层。联合目标的验收重点是拟合参数真正进入五阶段计算、线标注确实参与评分，以及缺失道路继续影响召回率。

## 7. 原始证据位置

本次机器可读摘要位于仓库外 `/tmp/roadnet-workbench/evidence/workbench-evaluation.json`；CLI 日志、配置和运行产物位于 `/tmp/roadnet-workbench/evaluation-cli/`。这些是本次会话的临时证据路径，不是克隆仓库后即可取得的数据集。仓库仅保留生成器、测试代码和本记录，未复制原始日志、二进制数据或运行结果。后续环境通过上面的命令重新生成独立证据。

## 8. 锁文件安装验收

在独立的干净环境 `/tmp/roadnet-locked` 中，通过 `make install` 从最新源码构建并安装 `roadnet-pipeline 0.2.0` 原生 wheel，安装包源码哈希与本次源码一致。该环境采用依赖锁文件中的 DuckDB 1.5.5、Optuna 4.9.0、NumPy 2.5.3。`pipeline` 与 `_native` 均从该环境的 `site-packages` 加载，根目录 `pipeline.toml` 的 `check-config` 返回 `VALID`。这组安装验收独立于前文 DuckDB 2.0.0 / Optuna 5.0.0 环境，不替换其历史耗时和 RSS 数值。

在全新目录 `/tmp/roadnet-workbench/locked-cli/` 使用同一 fixture 生成 2,976 点、48 车数据，保留 3 个调参区域和 1 个留出区域。配置与第 3 节一致，只执行一次真实 `tune`，包含通过 `spawn` 启动的隔离计算进程。生成 fixture 时仅将测试目录加入 `PYTHONPATH`；执行 CLI 时明确取消 `PYTHONPATH`，直接使用安装包：

```sh
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  /tmp/roadnet-locked/bin/python -m pipeline tune \
  --config /tmp/roadnet-workbench/locked-cli/pipeline.toml \
  > /tmp/roadnet-workbench/locked-cli/tune.json \
  2> /tmp/roadnet-workbench/locked-cli/tune.log
```

| 验收项 | 锁文件环境结果 |
| --- | --- |
| CLI 退出状态 | 0 |
| 试验状态 | 2 COMPLETE、1 PRUNED、0 FAIL |
| 真实剪枝 | trial 2 只有 1 个区域运行清单，剩余 2 个区域未执行 |
| 最佳已完成试验 | `detect.min_arms=3`，调参 intersection F1=1 |
| 留出评估 | COMPLETE，intersection F1=1，`used_for_selection=false` |
| 自动最佳参数完整回放 | 3 个交叉口、6 条中心线变体、3 个节点、2 条物理道路边 |
| 新试验台最终完整测试集 | 95 passed，7.76 s |
| SQL 审查 hook 独立测试 | 19 项 unittest 通过 |
| gperftools 补充检查 | 真实预加载环境下另 1 项测试通过 |

CLI 验收后，对两个 JSON 发布函数补充了同目录唯一临时文件与原子替换，修复并发写入共享 `latest.json` 等文件时的竞争；新增 6 项并发/失败清理回归。随后从最终源码离线重建并安装 wheel，重新执行完整测试集。

最终命令为 `PYTHONDONTWRITEBYTECODE=1 make test PYTHON=/tmp/roadnet-locked/bin/python`，没有设置 `PYTHONPATH`，结果为 **95 passed in 7.76s**。95 项结果与前文 24+3 子集存在覆盖关系，不能相加作为测试总数；SQL hook 和 gperftools 检查另行记录。最终 wheel 的 SHA-256 为 `0eb6c5bf6d048f22f16c51765739b2feba9ede7bb0c21fcc0141aa7a153319f4`，构建和测试日志位于仓库外 `/tmp/roadnet-workbench/final-build.log`、`final-test.log`。

此轮试验身份哈希为 `561118d073249a91850debab8580b46d3bba331ab51f20c7aeef15c413c6cf7a`，CLI 摘要位于仓库外 `/tmp/roadnet-workbench/locked-cli/summary.json`。本轮没有重复执行续跑、独立 `run` 或联合目标试验；这些行为的既有验收仍见前文。
