# Multimodal GEO Roadnet · 参数试验台

从轨迹数据执行 **refine → detect → clip → fit → network**，用人工标注评估结果，
通过 Optuna 搜索参数并提前停止表现较差的试验。当前默认入口只运行核心流水线，
无需启动前端、后端、PostgreSQL 或 Qdrant。

所有输入、算法、评估、搜索范围和资源预算统一放在根目录
[`pipeline.toml`](pipeline.toml)。Python 负责文件读写与过程编排，
Refine、Detect、Clip、Fit 的几何核心通过 pybind11 调用 C++17 原生扩展。
没有原生扩展时直接报错，不切换到另一套 Python 算法。

## 快速开始

推荐 Linux、Python 3.12 和支持 C++17 的编译器。Linux 路径已实测；macOS 的资源采样
分支尚未验证，当前不支持 Windows 原生运行，可使用 Linux 环境。

```sh
python3.12 -m venv .venv
. .venv/bin/activate
CC=gcc CXX=g++ make install
```

`make install` 使用带哈希的依赖锁文件，在仓库外构建并安装原生 wheel。
默认构建目录为 `/tmp/roadnet-workbench-build`，可用 `BUILD_ROOT` 改变；这个变量只影响
构建产物位置，不覆盖算法参数。单独构建可运行 `make build`。

修改 `pipeline.toml` 的 `input.path`、字段映射、源坐标系、计算坐标系及 `output.root`。
`processing_crs` 必须为适合数据区域的米制投影，示例 `EPSG:32650` 不适用于所有地区。
然后运行：

```sh
python -m pipeline check-config --config pipeline.toml
python -m pipeline run --config pipeline.toml
python -m pipeline inspect --config pipeline.toml
```

`check-config` 检查配置、原生扩展、搜索空间及已配置的标注；`run` 执行源数据校验和五个
阶段，返回运行目录、要素数量与资源报告。所有 CLI 命令只接受一个配置文件，不接受
逐项算法参数覆盖。也可使用 `make pipeline-check`、`make pipeline-run` 等同名入口。

### 输入数据

支持 Parquet、CSV，以及可选的 PostgreSQL 只读输入。文件支持单文件、目录或 glob。

| 配置映射 | 必需 | 含义 |
| --- | --- | --- |
| `input.columns.id` | 是 | 所有输入文件中全局唯一的源数据行键 |
| `vehicle_id` | 是 | 车辆标识；用于排序与分组 |
| `timestamp` | 是 | ISO 时间或按 `timestamp_unit` 解释的数值时间 |
| `x`、`y` | 是 | `source_crs` 下的坐标，不能把经纬度直接当米使用 |
| `heading`、`speed` | 否 | 缺少字段时设为空字符串；航向和速度可由相邻点计算 |

无效坐标、时间等被记录到 `rejected.parquet`；重复源主键直接失败。
源数据规范化后按车辆、时间和源键排序，保存独立快照与原始属性 JSON。
文件内容及输入配置参与指纹；并发导入同一快照由进程锁保护。

## 标注、预览和调参

在同一 `pipeline.toml` 中指定已标注的 GeoPackage（GPKG）或 Shapefile（SHP）。
GPKG 明确指定图层，SHP 保留 `.shp`、`.shx`、`.dbf`、`.prj` 等配套文件。
标注必须自带有效坐标系。交叉口支持点或面，中心线支持线，评估区域使用面。

```toml
[labels.intersections]
path = "/data/labels/reviewed.gpkg"
layer = "intersections"
id_field = "id"

[labels.centerlines]
path = "/data/labels/reviewed.gpkg"
layer = "centerlines"
id_field = "id"

[labels.regions]
path = "/data/labels/reviewed.gpkg"
layer = "regions"
id_field = "id"
split_field = "split"
reviewed_field = "reviewed"
```

`regions` 中每个区域的 `split` 为 `tune` 或 `holdout`，`reviewed` 声明该区域已经完整
审查。完整审查区域才能判断漏检和误报；只有零散正例的标注不能直接作为 F1 的真值。
区域内部不得重叠，共边要素会唯一归属。简单用法也支持 TOML 中的
`labels.reviewed_bbox` 与 `reviewed_complete=true`，但只有一个区域时没有中途剪枝机会。

```sh
python -m pipeline list-labels --config pipeline.toml
python -m pipeline preview-labels --config pipeline.toml
python -m pipeline tune --config pipeline.toml
```

预览输出 `<output.root>/labels/labels-preview.svg`、GeoJSON 和 GeoParquet，直接用浏览器
或 GIS 工具打开，不需要 Web 服务。`tune` 会读取同一配置中的搜索空间，例如：

```toml
[tuning.search_space."detect.cluster_eps_m"]
type = "float"
low = 15.0
high = 40.0
step = 5.0
```

每次试验按相同顺序运行多个评估区域，以当前区域前缀的累计指标调用
Optuna `report()` / `should_prune()`。剪枝会跳过后续实际计算。
留出区域（holdout）只在选定参数后评估。支持目标：

| `tuning.objective` | 指标与适用参数 |
| --- | --- |
| `intersection_f1` | 路口一对一匹配；适合 Refine / Detect 参数 |
| `centerline_f1` | 按长度计算的中心线几何覆盖 F1；需要中心线标注 |
| `combined_f1` | 路口 F1 与中心线几何 F1 的调和平均；可评估 Fit 参数 |

不会影响目标的参数会被拒绝，例如用路口 F1 调 `fit.point_spacing_m`。
几何覆盖指标不代表拓扑正确性，目前不支持用它优化 Network 参数。

结果保存在 `<output.root>/studies/<study_name>/`：`best.pipeline.toml` 是完整可执行的
最佳配置，`best.json` 包含调参与留出结果，`trials.json` 保存所有状态，
`optuna.journal` 支持恢复。`tune` 最后还会用最佳配置完整复跑源数据。
“最佳”仅指已完成试验中的最佳观察值。

```sh
python -m pipeline run --config /data/roadnet-workbench/studies/roadnet/best.pipeline.toml
```

增加 `tuning.n_trials` 后重跑可追加试验；它是总预算。数据、标注、原生二进制、代码或
语义配置变化时需要新 `study_name`，避免把不可比较的结果混进同一研究。
PostgreSQL 输入每次调用都会生成新快照身份；需要跨调用续跑时，先固定为 Parquet/CSV
文件来源，再建立研究。

## 产物与数据主键

每次运行独占 `<output.root>/runs/<run_id>/`。各阶段有 `manifest.json`，记录父阶段、
配置摘要、行数、字节数、主键和文件 SHA-256；空输出仍有可读取的类型定义。

| 阶段 | 主要文件 | 主键与关联 |
| --- | --- | --- |
| Refine | `points.parquet`、`excluded.parquet`、`segments.parquet` | `point_id`、`segment_id`；源键、子轨迹键、插入点父键 |
| Detect | `candidates.parquet`、`intersections.parquet` | `event_id`、交叉口 `id`；源点/线段键和 `refine_id` |
| Clip | `clips.parquet` | `clip_id`；两端交叉口、点 ID、点属性和自身完整几何 |
| Fit | `centerlines.parquet`、`memberships.parquet` | 中心线 `id`；交叉口对、成员片段及其几何 |
| Network | `nodes.parquet`、`edges.parquet` | 节点/边 `id`；交叉口、中心线和阶段键 |

Refine 保留点自身携带坐标、几何、时间、速度、航向和原始属性，读取几何不需要回查
`trajectory_points`。其他阶段同样保存自身计算或展示所需的属性；外键用于追溯来源。
关键要素另导出 EPSG:4326 GeoJSON，计算用 GeoParquet 保留米制坐标系。

数据库默认关闭。开启 `[database] enabled=true` 时，仅发布 `intersections` 和
`centerlines` 两张自包含成果表，保留主键、血缘、完整属性和 GiST 索引。
连接字符串通过 TOML 指定名称的环境变量读取。试验与中间产物不入库；历史业务表不会
被自动删除，迁移边界见[详细设计](docs/parameter-workbench.md#数据库与迁移)。

## 运行时间与内存

每次运行生成 `resources.json`，记录整体及各阶段的真实墙钟时间、CPU 时间、
`read / compute / write` 分项时间、输入输出数量，以及 C++ 线程采样的 RSS 峰值。
进程生命周期高水位、进程树 RSS 和 cgroup 指标分别命名，不作为同一种“峰值内存”。

需要分析原生堆分配时使用开源 **gperftools/libtcmalloc**。先安装完整库，例如 Debian /
Ubuntu 的 `libgoogle-perftools-dev`，在 TOML 中设 `resources.heap_profile=true`，
再在启动 Python 前预加载：

```sh
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc.so.4 \
  python -m pipeline run --config pipeline.toml
```

库路径以实际安装位置为准；不能用 `libtcmalloc_minimal` 替代。工作台控制 profiler
启停并按阶段输出 `.heap`，无需设置 `HEAPPROFILE`。未正确预加载会明确报错。
堆分析有额外开销，应与常规性能试验分开比较。

`soft_memory_mb` 是采样后的协作式内存预算；算子内部另有候选、线段对、分组和拟合点数
保护阈值。它们不提供操作系统硬上限。时间、内存口径及规模限制详见
[资源测量说明](docs/parameter-workbench.md#运行时间与内存测量)。

## 代码与验收

活动实现集中在 `services/pipeline/src/pipeline/` 的单层模块，C++ 位于
`services/pipeline/native/`；没有新增服务框架、数据库元数据服务或后台 API。

```sh
make test
```

测试会生成自己的合成轨迹和 GPKG/SHP，覆盖原生算子、五阶段血缘、标注、评分、实际
剪枝、恢复、输入缓存并发和资源采样。实际 CLI 验收及尚未验证的事项见
[`docs/validation/parameter-workbench.md`](docs/validation/parameter-workbench.md)。

当前实现的主要边界：

- 原生 Detect 使用转向/穿越候选、DBSCAN 与臂数验证，没有逐项移植历史 G*、HDBSCAN、
  视觉判别插件，不能声称与旧流水线输出完全等价。
- Fit / Network 当前生成两个已检测路口之间的道路；终端支路、缺少端点路口的直路不会
  自动变成边。区域调参需给足 `context_halo_m`，覆盖相邻端点。
- 路网以交叉口主键表达连接；中心线几何止于路口圆域边界，不强制吸附到路口中心。
  目前是二维几何重建，没有可靠的立交高程判别。
- 大规模真实数据精度、26M / 2 GB / 一小时等历史目标、真实 PostGIS 发布和 Docker
  镜像构建尚未在本次环境验收。

历史前后端、`track.v2` 和旧配置资料保留作迁移参考；当前默认入口不加载它们。
旧部署的复现入口见[历史运行边界](docs/legacy-runtime.md)。
