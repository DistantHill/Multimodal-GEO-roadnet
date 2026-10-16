# UTM 扩窗栅格、YOLOv8 分割训练与庄里验证

## 实现与数据

采用用户确认的标签政策：TP 为交叉口前景，FP 本身不作为前景；庄里文件中的
5 个 polygon 均为真实交叉口，只用于最终独立验证。

Refine：`6c9acb0c-f356-49ce-81d0-2dd7dad51e68`。
实际栅格配置为 `services/pipeline/config/pipeline.toml` ，10 m/px。
时间为北京时间 2023-03-01 00:00（包含）至 2023-03-07 00:00（不包含）。

执行顺序为：原始 polygon 投影到 UTM 32649 → 向外 buffer 300 m → 取外接矩形 →
筛选矩形内 kept 与 injected 点 → 无排序 `UNION ALL` → 栅格计数 → 非零像元 Q95 + sqrt。
矩形角落位于圆角 buffer 外的点也保留。图像对齐原有全局像元网格，最多有一像元的
边缘对齐空间。原始 polygon 不扩大，映射到该网格的像素坐标，并按像元中心生成掩膜。

两个 SQL 分支均使用 UTM 表达式 GiST 索引：

- `idx_trajectory_points_geom_utm32649`：原有索引。
- `idx_injected_points_geom_utm32649`：本次并发创建；数据库初始化 DDL 同步补齐。

真实计划： `docs/validation/sql-plans/polygon-raster-utm-buffer.json` 。
两分支的 `Index Cond` 均匹配 `ST_Transform(geom, 32649)`，不存在排序节点。
SQL 内容哈希检查记录保存在 `docs/sql-index-reviews.json` 。

实际栅格运行目录（主机路径）：

- 219 张训练来源图： `/data/tmp/road-network/raster/polygon-samples/cf837494-6c0c-4eb6-936c-9402db692fd0/` 。
- 5 张庄里图： `/data/tmp/road-network/raster/polygon-samples/d131804f-12f4-4511-b4d6-1e145f52656f/` 。

全部 224 张图非空。标签转换保留全部图像；204 条 TP 来源要素中有一对完全相同的
几何，前景目录去重后为 203 个 TP。窗口中的已知邻近 TP 同样标注并裁切至窗口，
FP 自身始终不标为前景。15 个 FP 窗口中，10 个包含邻近 TP，5 个为纯背景。
GeoPackage 原属性、feature ID、像素和地理坐标标注均保留。

按相互重叠窗口的连通分量划分，得到 168 张训练、51 张内部验证。两部分之间没有
重叠窗口；庄里 5 张窗口与训练来源窗口也不重叠。庄里不参与训练、提前停止或模型选择。
数据集清单记录来源、分组、每张图的实例数和文件校验值。

## 训练环境与过程

使用官方 Ultralytics [YOLOv8n-seg 权重](https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n-seg.pt)，
SHA-256 为 `a7cd8f929e1903d78a12a48efecab430209f18dc46cb96c3599a5980c63c423c`。

实际环境：GTX 1650（CUDA 报告总显存 3717 MB）、PyTorch 2.0.1、CUDA 11.7、
Ultralytics 8.3.0。主机加载的内核模块是 595.91.07，而用户态库已更新为 595.99.02，
普通 NVIDIA Container Toolkit 启动失败。最终从 Ubuntu 官方快照取得 595.91.07
计算库，只解包、只读挂载到 runc 容器，显式传入 GPU 设备；未安装或更改主机驱动。
CUDA 矩阵运算和模型训练均实际成功。

首轮初始化绘图期间发生主机全局 OOM，训练进程退出 137，没有完成训练轮次。
第二轮关闭训练绘图、使用 0 个数据加载子进程，并限制容器内存为 3 GiB 后完成。

最终训练参数：单类别 `intersection`，输入 256，batch 4，FP32（amp=false），
mask_ratio=1，seed=42，最多 100 epochs，patience=25。第 70 轮提前停止，按内部
验证综合 fitness 选择第 45 轮最佳权重。实际运行约 273.5 秒（包括首轮独立验证），
峰值 GPU 分配为 315,985,920 字节，约 301.35 MiB。

训练目录： `/data/tmp/road-network/vision/intersection-seg-20261011/run2/` 。
最佳权重 SHA-256：`5da8a5c9d523c5d589f17a5f0f0fae5d886a9aab77f97f221f4edff06c526f53`。

## 庄里结果

最终验证显式使用 workers=0、mask_ratio=1，保持与训练掩膜分辨率一致；未重新训练
或根据庄里结果选择权重。最终结果目录为
 `/data/tmp/road-network/vision/intersection-seg-20261011/final_validation/` 。

标准分割指标：精确率 0.66745，召回率 0.45455，mAP50 0.41355，mAP50–95 0.11714。
这是相对提供标注的 5 张窗口、11 次标注实例计算的结果。邻近 polygon 会在多个
窗口出现，11 次实例不是 11 个不同的原始交叉口。

另以原始 polygon 的像元中心掩膜为依据，每个原始目标只在自己的窗口评估一次。
固定置信度下限 0.25，最佳预测掩膜 IoU ≥ 0.5 视为命中：

| 原始 FID | 最佳掩膜 IoU | 命中 |
| --- | ---: | --- |
| 461 | 0.31224 | 否 |
| 462 | 0.53691 | 是 |
| 675 | 0.61957 | 是 |
| 702 | 0.67692 | 是 |
| 1249 | 0.09489 | 否 |

因此原始目标命中 3/5。模型仍有明显漏检和边界偏差，不将这次小规模验证描述为高精度模型。
保留完整预测掩膜 NPZ、逐图叠加、像素轮廓、置信度和 EPSG:4326 预测 GeoJSON。
GeoJSON 保留重叠窗口中的预测，没有跨窗口合并，来源窗口由 `window_fid` 标识。

## 检查与交付

- 栅格及真实 PostGIS 针对性测试：10 passed，包括矩形角落点保留、原始掩膜不外扩、
  带洞几何、时间区间、Refine 隔离和无排序累计。
- Vision 测试：数据转换与已有追踪测试 11 passed；空预测评估测试 1 passed，
  验证空输出仍记录漏检并生成预览。
- `make test` 使用已有 `roadnet-hpo-validation:latest` 测试依赖镜像覆盖 Pipeline 镜像，
  Backend 147 passed / 6 skipped；Pipeline 1604 passed / 100 skipped，75.58 秒。
- 224 张 YOLO 标签全部通过 class ID、坐标范围和多边形点数检查。
- 11 条相关 SQL 当前哈希记录及 UTM 计划证据通过；`git diff --check` 通过。

交付目录： `/data/tmp/road-network/vision/intersection-seg-20261011/deliverable/` 。
其中包括最佳模型、HTML 报告、完整指标、全部训练/内部验证/测试图像及 YOLO 标签、
5 个输入 GPKG、庄里原始栅格、预测掩膜和 GeoJSON、实际训练配置及 CSV、复现脚本。

压缩包： `/data/tmp/road-network/vision/intersection-seg-20261011/intersection-yolov8n-seg-20261011.zip` 。
共 513 个文件，7,633,143 字节；SHA-256：
`567aeafbac2b0282c1b9da0f757daf1f20c721368beeea954c96ea6f5ed0bb15`。
ZIP CRC 检查通过；包内 `SHA256SUMS` 覆盖除其自身之外的全部文件。
