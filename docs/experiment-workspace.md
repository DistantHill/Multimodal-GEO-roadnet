# 交叉口实验工作台

术语遵循 [项目术语与写作规范](terminology.md) 。

工作台使用一份仓库外 TOML。基础配置、命名案例、命名参数方案和评价规则集中编辑；运行目录由系统生成。默认容器配置文件为 /data/experiments/experiment.toml ，对应宿主机 DATA_ROOT 下的 experiments 目录。

## 候选交叉口的圆心与半径

候选聚类完成后，在处理坐标参考系的米制坐标中，以全部成员点的
几何中位数（Geometric Median）为圆心：最小化所有成员到圆心的欧氏距离之和。
每个事件权重相同，重合事件保留各自的计数；joint 不对不同候选来源额外加权。

候选半径取这些距离的累计95%分位数：从小到大排序后，选择第
`ceil(0.95 × n)` 个距离，即覆盖至少95%成员的最小观测距离，不作线性插值。
圆外成员仍保留在簇内，来源计数和成员身份不变。所有启用的聚类插件及
来源移除对照共用此几何计算。数据库中的 `centroid_*` 字段继续使用原名，
新运行存储的是几何中位数，而非算术平均圆心。

拓扑验证半径仍为 `max(min_intersection_radius_m, 候选半径 + topology_radius_padding_m)`，
进出点按该拓扑圆的边界计算。几何中位数使用可处理重合点的修正 Weiszfeld
迭代，位移收敛阈值为0.000001米；达到10000次仍未收敛时明确报错。
既有实验结果和导出不自动重算；新旧圆心及半径的对比需要重新运行 Detect。

## 命令说明与当前机器的用法

`pipeline-experiment` 是本项目 Makefile 中的目标。在主目录执行 `make pipeline-experiment` 找不到该目标。下面统一用 `make -C` 指定仓库，可在任意目录执行；仓库搬迁后替换目录即可。

`ARGS` 是传给 Make 的变量，其内容会转交给实验命令：`init` 初始化、`run` 运行、`list` 查看历史。算法参数仍在本地 TOML 中编辑。

2026-10-08 当前机器已初始化 `roadnet_eval` 和 /data/tmp/road-network/experiments/experiment.toml ，无需重复初始化。模板的案例时间和区域尚未填写。先编辑这份文件，填写 `time_start`、`time_end`，以及 `region_file` 或 `bbox`，然后运行：

```sh
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='run'
```

也可以先查看历史，不要求填写案例，不会启动算法：

```sh
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='list'
```

## 新环境：首次初始化

```sh
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='init --base-config /app/config/pipeline.toml'
```

基础模板 /app/config/pipeline.toml 是容器内路径，对应仓库中的 services/pipeline/config/pipeline.toml 。生成的本地配置位于 DATA_ROOT 下的 experiments/experiment.toml ，当前机器对应 /data/tmp/road-network/experiments/experiment.toml 。后续编辑生成的本地配置。

初始化模板中的案例故意不提供时间和区域：填好后才允许运行。原始密码仍由环境提供，不写入 TOML 或历史参数。

```toml
[experiment]
case = "hancheng_mar01"
schemes = ["turn", "joint"]

[cases.hancheng_mar01]
source_table = "trajectory_points"
time_start = "2023-03-01T00:00:00+08:00"
time_end = "2023-03-02T00:00:00+08:00"
region_file = "regions/hancheng.geojson"
bbox = []
# 或设置 region_file = ""，并填写 bbox = [[110.3, 35.4], [110.5, 35.6]]
# truth_file = "truth/hancheng.geojson"

[schemes.turn.detect]
use_turn_events = true
use_cross_track_candidates = false

[schemes.joint.detect]
use_turn_events = true
use_cross_track_candidates = true

[evaluation]
mode = "center_distance"
distance_m = 30.0

[archive]
database = "roadnet_eval"
```

上面的片段放在初始化生成的完整配置中，不替代 PostgreSQL、投影和运行资源配置。示例范围和阈值仅展示写法，不代表已标注真值或推荐评价阈值。
区域文件支持 WGS84 Polygon/MultiPolygon，多个面合并。路径相对于本地 TOML，region_file 和 bbox 可以同时保留，但必须恰好一个非空；缺省、空字符串（含纯空白）和空数组视为空值并忽略。两个都空或都非空时，报错 `case <案例名>: provide exactly one region_file or bbox`。
时间带时区、左闭右开；只读取区域内及边界上的点，不外扩。轨迹离开再进入区域时保留断点。

```sh
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='run'
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='run --case hancheng_mar01 --scheme turn'
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='list'
```

多个方案顺序执行；同批次相同输入和 Refine 参数共享 Refine。不同批次需要显式传入 --refine-id，且输入和参数必须一致。
Make 入口把宿主机当前 commit 传给容器，不保存工作树改动。直接调用 Python CLI 时，可通过 PIPELINE_GIT_COMMIT 提供 commit。

## 历史、评价与清理

每个实验有独立 UUID。开发库保存算法中间产物，roadnet_eval 保存参数、案例快照、交叉口几何和评价历史。开发库产物被清理后仍可查看历史交叉口和指标。
网页进入“实验评价”，可以按案例、方案、commit、状态、运行时间筛选，查看详情和不同评价版本，并选择两次实验进行地图联动与参数差异对比。

```sh
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='evaluate EXPERIMENT_UUID --truth-file /data/experiments/truth/hancheng.geojson'
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='retry-archive EXPERIMENT_UUID'
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='restore EXPERIMENT_UUID --output /data/experiments/experiment.toml'
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='cleanup EXPERIMENT_UUID'
```

恢复参数会更新指定的草稿；运行历史不改变。清理是显式操作，仅清理已完整存档实验的中间产物，并保护其他实验仍使用的 Refine。日志及评价存档保留。
无真值时显示“未评价”。真值是含唯一 ID 的交叉口面 GeoJSON；修改真值或规则后新增评价记录，无需重跑 Detect。
匹配模式为 center_distance、iou、both；分别要求 distance_m、iou_threshold 或两者。匹配采用最大数量的一对一分配，再优化距离或 IoU。零分母显示“不适用”，不计算未定义的 TN。

## 全量测试日期

```sh
make pipeline-full-test SOURCE_TABLE=trajectory_points
```

此目标继续使用 services/pipeline/config/pipeline.toml ，默认读取北京时间 2023 年 3 月 1 日全天：[2023-03-01T00:00:00+08:00, 2023-03-02T00:00:00+08:00)。可用 FULL_TEST_TIME_START 和 FULL_TEST_TIME_END 显式覆盖。
冒烟测试仍使用 services/pipeline/config/pipeline-roadnet-test.toml ，不自动附加该日期限制。运行已经筛选日期的 Refine 时，范围必须一致。
全量运行的监控按 README 的“全量测试日志监控”执行，不在会话中轮询。
