# turn_threehold 与 HDBSCAN 实验

这两个算法仅由实验工作台配置加载，不注册到常规 Pipeline TOML。
常规 Detect 继续使用局部热点统计量（G*）筛选和 DBSCAN 聚类。

## 配置与运行

在现有实验 TOML 中新增命名方案，选中所需方案后运行原有实验命令：

```toml
[schemes.turn_threehold.detect]
turn_candidate_algorithm = "turn_threehold"
clustering_algorithm = "dbscan"
min_turn_deg = 45.0

[schemes.turn_threehold_hdbscan.detect]
turn_candidate_algorithm = "turn_threehold"
clustering_algorithm = "hdbscan"
min_turn_deg = 45.0
```

```sh
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='run --scheme turn_threehold'
make -C /data/workspace/Multimodal-GEO-roadnet pipeline-experiment ARGS='run --scheme turn_threehold_hdbscan --refine-id REFINE_UUID'
```

默认使用现有案例和基础参数，包括跨轨迹开关。需要仅测试转向候选时，在方案中添加
`use_cross_track_candidates = false`。也可以在实验 TOML 的 `[detect]` 设置算法作为
各方案的共同默认值；普通 Pipeline 配置会拒绝这两个实验参数。

省略实验算法参数时，默认 `turn_candidate_algorithm="g_star"`、
`clustering_algorithm="dbscan"`。HDBSCAN 也可与原 G* 筛选组合对比。

## 输入与筛选

实验继续从已完成的 Refine Run 读取列式轨迹数据，复用其坐标参考系、保留点与注入点。
显式指定 `--refine-id` 时仍检查输入案例、源数据和 Refine 参数是否一致。
相同输入与 Refine 参数的多个实验方案在同批次共享 Refine。

turn_threehold 保留已有转向事件提取的窗口、最小轨迹长度、速度和最大转向角条件，
随后直接筛选 `abs(turn_deg) > min_turn_deg`，不计算 G*。`min_turn_deg` 即 n，
45° 为示例；左右转都保留，等于阈值的点被剔除。筛选保持列式字段、事件索引、
来源引用与顺序，`g_star_z` 及其有效性标记不会被改写。

## 聚类与输出

转向候选和跨轨迹候选继续合并后联合聚类。HDBSCAN 与 DBSCAN 使用相同坐标输入、
标签格式和簇组装逻辑，返回相同的成员引用、来源计数、来源比例、中心和半径结构。
来源消融分析的重新聚类也使用所选算法，后续拓扑验证、持久化、Parquet 导出和评价
复用实验工作台流程。参数快照与配置摘要包含算法选择，历史恢复保留这些选择。

HDBSCAN 使用 scikit-learn 实现，依据
[官方接口](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.HDBSCAN.html)
映射现有参数：

| 参数 | HDBSCAN 含义 |
| --- | --- |
| `candidate_dbscan_eps_m` | `cluster_selection_epsilon`，单位米的簇合并阈值 |
| `candidate_dbscan_min_samples` | `min_samples`，包含点本身；同时设置 `min_cluster_size=max(2, min_samples)` |

允许单簇结果，采用欧氏距离和单任务计算。非有限坐标及不足最小簇大小的输入标为
噪声 `-1`；有效簇按首个成员出现顺序编号为 `0, 1, …`。同一输入顺序和参数下重复
运行结果稳定。输入输出结构兼容不意味着两种算法产生相同的簇；HDBSCAN 的
`eps_m` 是簇合并阈值，而 DBSCAN 使用它作为邻域半径。

安装更新后的依赖或重建 Pipeline 镜像后运行实验，新增依赖为 scikit-learn。
