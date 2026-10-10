# 转角阈值分层调参

术语遵循 [项目术语与写作规范](terminology.md) 。

本功能是既有实验工作台的扩展。候选算法固定为 `turn_threshold`，只搜索
`min_turn_deg`：既有提取条件成立且 `abs(turn_deg) > min_turn_deg` 的候选进入聚类。
其他提取参数、聚类参数和评价阈值保持固定，不启用 G*，不改变普通 Pipeline 默认配置。

## 已确认边界

- 使用人工划定的案例多边形；输入与评价边界相同，不外扩、不自动生成 200 m 圆。
  临近交叉口应在标注时合并到同一区域；不同区域不重叠。保留既有离开和重入断点处理。
- 不要求局部运行与全域 DBSCAN 输出等价。各保真度是固定区域集合，不是截断同一簇的计算近似。
- 沿用 Detect 最终交叉口面，支持 `center_distance`、`iou`、`both`，区域归属按中心点判断。
  原始转向候选数量是诊断指标，不将每个转向事件当作预测交叉口。
- L1 只报告召回率，FP、Precision、F1 为 null；L2/L3 和独立测试区域要求完整人工复核。
- 实验仍存入 `roadnet_eval`，Optuna 也使用 PostgreSQL；不创建 SQLite。
- 人工标注、区域、缓存和试验产物位于仓库外 DATA_ROOT。没有标注时不能运行真实 Pilot 或报告算法提升。

## 数据准备

继续维护既有实验 TOML 中的命名案例和一个 `turn_threshold` 单实例方案。
每个案例引用区域和真值面文件，并填写轨迹来源表和固定时间范围。
正样本真值可直接读取 GeoPackage，通过 `[hpo].labels_file`、`labels_layer`、`labels_id_field` 指定文件、图层和稳定 ID 字段。负样本是另一份区域 GeoPackage，不把其面当作正样本真值；负样本内存在正样本中心会阻止评价。

类型字段使用 `type`，预留十字路口、T字路口、Y字路口、环岛、匝道汇入/分流、复杂交叉口、其他；空值记为“未分类”，不阻止整体评价，但不会声称完成了类型分层分析。原始文件不会被改写。

GeoPackage 通过 GDAL 读取并转换为 WGS84 线性面。曲线采用明确的 1 度弧线步长，转换版本和源文件 SHA-256 随清单记录。2026-10-10 提供的标注文件已实际读取：1716 个要素，其中源 WKB 有 4 个 MultiSurface；转换后全数保留。原文件 SHA-256 为 `2eda35081410c025487031cd6b489ed47a9f1c42faa5173672c05ed21dc3c052`，当前 1716 个要素均未填写类型。

额外区域清单支持 GeoJSON FeatureCollection 或 GeoPackage，每个区域 Feature 包含稳定 `id`、与案例相同的几何，
以及以下属性：

```json
{"case":"district_a","spatial_group":"group_a","split":"tune","kind":"positive","reviewed_complete":true}
```

`split` 为 `tune` 或 `holdout`，`kind` 为 `positive` 或 `negative`。
如果 GPKG 尚无这些字段，可在 HPO TOML 的 `[region_metadata."要素ID"]` 中填写，
无需改写原文件。独立负样本文件的元数据键使用 `negative:<fid>`。
案例通过 `region_file`、`region_layer`、`region_id` 选择 GPKG 内的单个区域。
人工空间分组用于隔离调参区与独立测试区；代码同时检查几何和真值 ID 泄漏。
`reviewed_complete` 是人工确认声明，程序无法代替人工证明区域没有漏标。
轨迹快照标识必须对应固定来源数据；不得把会继续变化的表视为不可变快照。

L1、L2 按路口类型组合分层抽取完整区域，保持嵌套；L3 使用全部调参区域。
配置数量指区域数，一个区域可以包含多个交叉口；不强制附件示例中的 30/100/500 个中心。
独立测试区不参加调参和晋级。清单保存实际区域、真值、文件哈希及版本；改变输入须另开实验目录。

## 执行入口

在仓库根目录使用 `make pipeline-hpo ARGS='--help'`。
通过 `HPO_CONFIG_PATH` 指定容器内的仓库外 HPO TOML；默认位置为
 /data/experiments/intersection-hpo.toml 。该文件引用已有实验 TOML，不复制生产算法配置。配置模板见
 [配置示例](examples/intersection-hpo.example.toml) 。
模板阈值和校准策略仅作格式示例，需按实际区域设置。

命令顺序为 `audit`、`build-manifests`、`pilot`、`tune`、`promote`、`report`。
`audit` 检查输入，不启动 Detect；实际运行前须先通过审计。
`pilot` 对同一批固定阈值运行 L1/L2/L3，输出排序相关性、重采样区间和晋级误杀诊断。
`tune` 要求 Pilot 校准通过，以 L2 F1 为 Optuna 目标，L1 只作显式门槛。
`promote` 按 L2 选择候选，在 L3 选出最终阈值之后再评价独立测试区。

新增依赖包括 GDAL 命令行工具、Optuna 和 SQLAlchemy，运行容器需使用更新的 Pipeline 镜像。
新命令不自动运行全量数据测试，也不把“框架测试通过”视为“算法效果已验证”。

## 当前复用边界

适配层复用现有 Refine、Detect、评价器和存档接口；固定阈值与数据版本的已完成阶段可以复用。
跨阈值复用 Refine 时校验案例与配置归属。当前不承诺跨试验缓存所有候选特征。
评价结果的实验 UUID 可在既有实验工作台查询；L1 不存入错误的普通 F1 评价。

## 与原交接文档的差异

已按用户后续决定移除 Halo、全域等价、G* 搜索、SQLite 迁移和新建点匹配器。
87.3% 不是本仓库已核验基线；任何效果、成本和加速结论以真实标注实验为准。

## 验证记录

本次框架回归、PostgreSQL 持久化测试和用户 GPKG 实测见
 [2026-10-10 验证记录](validation/intersection-hpo-20261010.md) 。
运行环境需重新构建 Pipeline 镜像，以安装锁定的 Optuna/SQLAlchemy 与 GDAL。
