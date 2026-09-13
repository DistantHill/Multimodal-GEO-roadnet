# 灰度图人工检查工具

本目录保存原始轨迹点的灰度归一化预览、候选参数比较和 HTML 模板。
术语遵循[项目术语与写作规范](../../docs/terminology.md)。

- `cv_preview.py`：命令行入口，从数据库聚合原始点，生成五组算法预览。
- `preview.py`：像素计数栅格、归一化统计、映射与报告生成。
- `cv_tune.py`：命令行入口，从保存的预览生成三组归一化参数对比。
- `tuning.py`：参数枚举、图片与统计文件生成。
- `image_compare.html`：交互页面模板，和脚本一起维护。
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
