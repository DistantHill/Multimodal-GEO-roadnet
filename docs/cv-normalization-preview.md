# 交叉口 CV 灰度归一化预览

本阶段为已确认的 CenterNet 圆回归、YOLOv8 检测、U-Net 概率图三条独立路线
准备输入比较。只生成统计与预览，不训练模型，不生成尚未定义清楚的“前 85%”掩膜。

后续用户已选择 **sqrt、log1p、asinh** 三组进入训练候选；本页五组保留为历史
调研预览。新一轮尺度参数搜索与交互页面见 [三组调参方案](cv-normalization-tuning.md)。

## 已确认的实验约定

- 只聚合来源表原始落点，包括被 Clean 排除的原始点；不连线、不注入插值、不平滑。
- 使用 Detect → Clean 继承的米制投影。统计与预览共享像素大小和数据时间范围。
- 仅从训练区域的非零像素拟合标尺，验证/推理冻结；空像素始终输出 0。
- 比较 linear、sqrt、log1p、asinh、CDF；前四者比较 P99/P99.5/P99.9 截断。
- 先人工筛选灰度图，再训练。首轮三模型共用归一化；后续允许分别优化。
- 归一化实验固定标签，不能随输入变换重新生成标签。

## 运行

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  python -m pipeline cv-preview \
  --database test \
  --detect-id 91257149-a6e7-41cc-8142-c6ca83977605 \
  --output /data/vision/normalization/preview-20260911-v2 \
  --pixel-size 2 --tile-size 512 \
  --log-scale 1 --asinh-scale 10
```

替换为实际已完成的 Detect Run。输出目录必须是仓库外的新目录，已有目录拒绝覆盖。
Compose 的 `/data` 对应宿主机 `DATA_ROOT`；例如本次为
`/home/reed/workspace/data/road-network/vision/normalization/preview-20260911-v2`。
旧容器可能保留旧环境变量，推荐使用当前 Compose 配置启动一次性任务。

命令使用只读、可重复读事务，验证 Clean 来源指纹并核对来源行数，再按来源表
`geom` 计数。所有原始点都必须被计入，空 Geometry 报错。数据库无写入。
SQL 聚合为稀疏的全局像素计数，只为选中的窗口建立稠密图；默认限制 200 万个
非零像素，超限应选更小来源或显式调整 `--max-pixels`。这一工具用于预览，
并非已完成大规模训练数据导出或跨机器流式处理优化。

## 栅格与预览划分

默认 2 m/像素、512×512 像素，即每窗 1024×1024 m。这是预览起点，尚未确定
最终训练分辨率。原点为投影坐标 `(0, 0)`；像素索引为
`floor(x/res), floor(y/res)`，边界半开；PNG 北向朝上。

默认按已占用瓦片东西范围的中点划分；也可用 `--split-x` 指定米制东坐标，
程序将其向下对齐到瓦片边界。设分割列为 `s`：

- `tile_x < s - 1`：预览用训练区域。
- `tile_x >= s + 1`：未参与拟合的预览区域。
- 中间两列：隔离带，不参与标尺拟合或窗口展示。

**该空间划分是可重现的预览设置，不是已人工复核的同城训练/验证集。** 当前来源
地理范围较广，正式训练前必须确认同城范围、缓冲距离、完整标签与可用时间窗口。
不得把看过的预览区域当成未经选择的最终测试集。

每侧最多选择密集、高峰值、规则交叉口、稀疏四种窗口。后三种排除本侧已选窗口，
规则窗口仅按已有伪标签定位，稀疏例从至少 20 个非零像素的窗口中选占用量四分位附近。
这些类别是自动选图条件，并不是人工确认的场景语义。
规则圆作为独立 SVG 叠加，默认隐藏，不烘焙进任何灰度 PNG。

## 映射定义

记 `c` 为原始计数，`Q` 为训练非零像素计数的指定分位数，`u=min(c,Q)`：

| 方法 | 输出 [0,1] |
| --- | --- |
| linear | `u / Q` |
| sqrt | `sqrt(u / Q)` |
| log1p | `log1p(u/s) / log1p(Q/s)`，预览默认 `s=1` |
| asinh | `asinh(u/s) / asinh(Q/s)`，预览默认 `s=10` |
| CDF | `c=0 → 0`；正计数映射至训练非零像素的经验 `P(C <= c)` |

分位数采用与展开全部非零像素后 NumPy 线性 percentile 等价的加权计算。
CDF 使用右连续经验分布：并列计数同亮度，未见中间值使用前一个累计概率，
高于训练最大值映射为 1。它不再使用 Q 截断，也不按每张图重算。
这是本次明确选择的 CDF 实现，不声称完全复现 RING-Net 的零值与并列处理。

归一化函数输出 float32；预览 PNG 才执行 `round(255*g)` 转 uint8。
页面单独报告非零像素被量化为 0 的数量、超过 Q 的数量和白色像素数。
CDF 提亮单点并不代表单点更可靠；saturation 也不是判定交叉口的置信度。

## 输出

- `index.html`：可独立打开的页面，图片内嵌，无网络依赖；可切换分位数、规则圆。
- `profile.json`、`training-histogram.csv`：冻结映射及非零训练计数频数。
- `manifest.json`：来源、指纹、投影、时间范围、空间划分、选窗、映射统计。
- `tile-*-counts.npz`：原始整型计数，可重建浮点映射。
- `tile-*-*-p*.png`：五组灰度图与截断敏感性预览。

所有产物在 DATA_ROOT，均不进入 Git。源码在 Pipeline 的独立批处理入口，Backend
请求处理与 Vision GPU 运行时不承担数据扫描。

## 本次数据发现（2026-09-11）

`roadnet` 默认库为空；`test` 库的 `public.day01` 有 1,000,000 个原始点。
选择的 Detect Run 为上述命令中的 ID，继承 EPSG:32649，有 292 个规则交叉口。
时间戳范围为 `[1677600000, 1677686398]`，预览使用这一整段观测数据。

在默认空间划分与 2 m 栅格下，训练区域有 618,283 点、332,879 个非零像素；
预览区域有 381,717 点、165,752 个非零像素；隔离带没有点。
训练非零计数 P50=1、P90=2、P95=3、P99=14、P99.5=25、P99.9=94、max=2852。
其中约 86.34% 非零像素只有一个点，因此当前 CDF 会将单点映射到约 0.863，
需要重点人工比较低密度道路与噪点同时被显著提亮的效果。

## 调研依据

- [NumPy log1p](https://numpy.org/doc/stable/reference/generated/numpy.log1p.html)：稳定计算 `ln(1+x)`；归一化比值的底数相消是换底公式的结果。
- [Astropy AsinhStretch](https://docs.astropy.org/en/stable/api/astropy.visualization.AsinhStretch.html)：低值线性、高值对数的可调转折。
- [scikit-image exposure](https://scikit-image.org/docs/stable/api/skimage.exposure.html)：Gamma、log、CDF 与 CLAHE 等方法定义。
- [RING-Net 原论文](https://cdn.amazon.science/c2/c8/2b9e65ea4c28a5ef484194e41754/ring-net-road-inference-from-gps-trajectories-using-a-deep-segmentation-network.pdf)：有计数 CDF 归一化先例；其连线栅格与多通道输入不同于本项目的原始落点，不能据此推断模型效果。
