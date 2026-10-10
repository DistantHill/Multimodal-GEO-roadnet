# turn_threehold 多实例与聚类实验

Detect 的普通 Pipeline 与实验工作台使用同一插件配置。候选算法在自己的参数表中
组织具名实例；顶层选择算法，`output_instances` 选择参与联合聚类的实例。
现有默认 Pipeline 保持 G* 与 cross，角度筛选需在配置中显式选择。

## 配置

以下是现有完整配置中 Detect 算法部分的示例，其他公共 Detect 参数仍保留在
`[detect]`；每组实例参数必须完整填写，不做实例间继承。

```toml
[detect]
candidates = ["turn_threehold", "cross"]
cluster_algorithm = "dbscan"

[detect.candidate.turn_threehold]
output_instances = ["turn_30"]

[detect.candidate.turn_threehold.params.turn_30]
turn_window = 3
min_turn_deg = 30.0
max_turn_deg = 150.0
min_leg_m = 5.0
min_movement_speed_mps = 1.0

[detect.candidate.turn_threehold.params.turn_60]
turn_window = 3
min_turn_deg = 60.0
max_turn_deg = 150.0
min_leg_m = 5.0
min_movement_speed_mps = 1.0

[detect.cluster.dbscan.params]
eps_m = 20.0
min_samples = 3
```

示例值用于说明配置结构，不是调优结论。cross 的完整参数保留在它自己的
`[detect.candidate.cross.params.<实例名>]` 中，并用自己的 `output_instances` 选择。
`cluster_algorithm` 为选择键，`detect.cluster` 为参数表，避免 TOML 同名标量与表冲突。

## 独立评测与联合运行

```sh
make pipeline-experiment ARGS='run --scheme turn_threehold --independent-instances'
make pipeline-experiment ARGS='run --scheme turn_threehold --refine-id REFINE_UUID'
```

方案名称以实际实验配置为准。`--independent-instances` 依次运行选中算法的全部
具名参数实例，每次仅使用一个实例单独聚类、归档和评价；不会生成参数笛卡尔积。
普通运行只拼接各算法 `output_instances` 指定的候选，再统一聚类。同一源事件
来自不同算法或实例时保留多行，各自贡献密度，并保存各自的来源身份。

turn_threehold 保留已有窗口、最小轨迹长度、速度与最大转向角条件，最终严格筛选
`abs(turn_deg) > min_turn_deg`。只改变最小角度的兼容实例共用最宽阈值提取，再筛选
各自子集；其他提取参数不同则分组计算。独立评测批次也共享候选提取，每个实例
仍分别聚类。共享结果仅在本批次存活，正常结束或异常时统一释放。

切换 HDBSCAN 时设 `cluster_algorithm = "hdbscan"`，并配置
`[detect.cluster.hdbscan.params]` 的 `eps_m`、`min_samples`。其中 `eps_m` 表示
簇合并阈值，DBSCAN 中则为邻域半径；两者结果不保证相同。容器依赖锁文件包含
scikit-learn，更新后需要重建 Pipeline 镜像。

## 插件与存储

在专用候选或聚类插件包内新增模块，导出 `PLUGIN` 描述对象即可被发现，无需维护
中央算法名称清单。描述声明稳定算法 ID、无默认值的参数数据类、点类型及执行函数。
算法 ID 与 Python 函数名称独立；重复 ID、未知算法、未知参数及不支持的数据类型
在运行前报错。当前仅接入 `candidate_points`，片段输出另行适配下游。

数据库结构 v11 为候选证据及几何镜像保留算法、实例、实例局部编号与证据编号，
`detect_candidate_instances` 保存每次运行各实例的有效参数、数量及偏移。
实验归档保留选择与独立评测归属，供后续界面使用。本次不实现实例切换界面、
Top N 排名高亮或自动晋级；将来 Top N 可生成同一个 `output_instances` 接口。

## 迁移

旧扁平 Detect 参数不再作为普通运行格式。历史快照原样保存，显式恢复转换：

```sh
make pipeline-experiment ARGS='restore EXPERIMENT_UUID --output /data/experiments/restored.toml --migrate-detect'
```

缺失旧算法身份时，可补充 `--legacy-turn-algorithm` 和
`--legacy-cluster-algorithm`；无法补齐必填参数会明确报错。

部署前先备份数据库，并通过已有显式迁移入口升级旧结构：

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm pipeline python -m pipeline init-db --migrate-artifacts
```

本次仅在隔离且回滚的 PostgreSQL 测试中验证迁移，没有升级运行数据库。
