# 灰度图人工检查工具

本目录保存原始轨迹点的灰度归一化预览、候选参数比较和 HTML 模板。
术语遵循[项目术语与写作规范](../../docs/terminology.md)。

- `cv_preview.py`：命令行入口，从数据库聚合原始点，生成五组算法预览。
- `preview.py`：像素计数栅格、归一化统计、映射与报告生成。
- `cv_tune.py`：命令行入口，从保存的预览生成三组归一化参数对比。
- `tuning.py`：参数枚举、图片与统计文件生成。
- `image_compare.html`：交互页面模板，和脚本一起维护。
- `export_intersections.py`：按 Detect 和经纬度范围导出全部交叉口的原始点裁剪，
  保存计数、三组灰度图、规则圆元数据和离线对比页。
- `export_coco.py`：将已有裁剪导出为 CVAT Community 可导入的 COCO 检测预标注包。
- `tests/`：归一化、报告、命令与输出路径的回归测试。

这些工具供人工比较输入灰度图，不训练模型，也不自动选择最佳参数。
生成的 HTML、PNG、NPZ 和统计文件写到仓库外的 `DATA_ROOT`，本目录只保存源码。

在仓库根目录运行；本地 Python 环境需要安装 Pipeline 及其依赖：

```sh
python -m scripts.cv_human_check.cv_preview --help
python -m scripts.cv_human_check.cv_tune --help
```

开发 Compose 已将本目录只读挂载到 Pipeline 容器的 `/app/scripts/cv_human_check`，
可复用其 Python 环境。完整参数示例见[五组预览](../../docs/cv-normalization-preview.md)
和[三组调参](../../docs/cv-normalization-tuning.md)。原来的 `python -m pipeline cv-preview`
与 `cv-tune` 入口已移除；从上述脚本入口运行。

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  python -m scripts.cv_human_check.cv_tune \
  --preview /data/vision/normalization/preview-20260911-v2 \
  --output /data/vision/normalization/tuning-new
```

`make test` 包含本目录测试。单独运行：

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  python -m pytest scripts/cv_human_check/tests
```

按圆心筛选范围内全部交叉口（参数顺序：西、南、东、北）：

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  python -m scripts.cv_human_check.export_intersections \
  -d test --detect-id 173ade7d-f03d-4c9d-8e46-906279085c9b \
  --bbox 109.15371 34.60404 109.26205 34.67948 --pixel-size 2 \
  --output /data/vision/datasets/detect-173ade7d-f03d-4c9d-8e46-906279085c9b-bbox-20260916
```

输出必须是 `DATA_ROOT` 下的新目录。按规则裁剪半径的外接矩形向外对齐像素网格，
保留矩形内全部原始点（包括 Refine 排除的点），不缩放、不绘制规则圆、不生成分割掩膜。
筛选范围只限制圆心，完整裁剪可以包含范围外的点。共享 P99.5 标尺从本批非零像素估计，
重叠像素只计一次；log1p、asinh 均先取尺度 1，明确标为未调优的初始值。
本批是待复核的训练正样本候选，尚未补充负样本或划分验证/测试区。

## 标注工具与数据集格式

人工标注采用 [CVAT Community](https://github.com/cvat-ai/cvat)（核心 MIT 许可），
数据交换采用 [COCO](https://docs.cvat.ai/docs/dataset_management/formats/format-coco/)。
本目录的 HTML 仅保留历史灰度预览用途，不扩展为自制标注工具。

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  python -m scripts.cv_human_check.export_coco \
  --source /data/vision/datasets/detect-173ade7d-f03d-4c9d-8e46-906279085c9b-bbox-20260916 \
  --output /data/vision/datasets/detect-173ade7d-f03d-4c9d-8e46-906279085c9b-bbox-20260916/coco-review \
  --method sqrt
```

包内提供 `images.zip`、`annotations/instances_default.json`、完整 `coco.zip` 和导入说明。
只标注一组灰度图片，其余两组按图片名复用标签，不扩增独立样本数量。
检测标签包含可见相邻规则圆的外接框；尚未生成的掩膜以空 `segmentation` 表示。
COCO 无原生圆字段，原始圆及地理变换保存在 `metadata/circles.json`；编辑矩形不会
同步修改原始圆。当前包用于复核已有裁剪，区域窗口和背景样本仍待补充。
