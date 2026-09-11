# sqrt、log1p、asinh 调参方案

2026-09-11 用户选择训练比较保留 sqrt、log1p、asinh。旧五组页面保留作历史预览，
linear 与 CDF 不进入本轮训练候选。当前没有训练模型，也没有通过验证选出最优参数。

## 调研结论

令 `c` 为原始点数、`Q` 为训练非零像素的固定参考上限、`u=min(c,Q)`：

- `sqrt(u/Q)`：固定 Gamma=0.5，本轮不把它扩展成 Gamma 参数搜索。
- `log1p(u/s)/log1p(Q/s)`：调的是计数尺度 `s`，无需搜索对数底数。
- `asinh(u/s)/asinh(Q/s)`：`s` 控制近似线性到对数的转折位置。

同样 Q 下，较小 s 提亮低计数；较大 s 接近线性。Astropy 的 LogStretch 用
`log(1+a*x)/log(1+a)`，令 `x=u/Q` 可得本项目参数对应 **a=Q/s**；
AsinhStretch 用 `asinh(x/a)/asinh(1/a)`，对应 **a=s/Q**。
两种 API 中名为 a 的参数含义相反，不能直接复制相同数值。
[LogStretch](https://docs.astropy.org/en/stable/api/astropy.visualization.LogStretch.html)、
[AsinhStretch](https://docs.astropy.org/en/stable/api/astropy.visualization.AsinhStretch.html)。

可行的调参方法比较：

| 方法 | 本项目判断 |
| --- | --- |
| 人工曲线/灰度预览 | 廉价地发现过暗、过饱和和噪点提亮；不能证明检测收益 |
| 对数间隔粗网格，再局部细化 | 本轮优先；每组仅一个连续尺度参数，加三档 Q，搜索空间很小 |
| 随机搜索，s 按 log-uniform 采样 | 后续联合搜索更多训练参数时可用；避免把大部分预算集中在大 s 的近线性区 |
| 贝叶斯/TPE 搜索 | 模型训练昂贵、有可靠验证目标后再用，目前仅图片评估不值得引入 |
| Successive halving/训练剪枝 | 可以减少差配置训练成本，但稀疏目标的早期表现可能不稳定，不能过早淘汰 |
| 最大化图像熵/对比度 | 不作为最优目标；提亮噪点、均衡化分布也能提高这些指标 |

网格、随机搜索和 successive halving 参考
[scikit-learn 调参文档](https://scikit-learn.org/stable/modules/grid_search.html)；
随机搜索的高维实验依据见
[Bergstra 与 Bengio, 2012](https://www.jmlr.org/papers/v13/bergstra12a.html)，
不能把高维搜索优势直接当作本项目一维搜索的已验证结果。
TPE 和剪枝机制见
[Optuna 官方教程](https://optuna.readthedocs.io/en/stable/tutorial/10_key_features/003_efficient_optimization_algorithms.html)。

## 当前数据意味着什么

2 m 栅格、原预览训练区域：非零计数 P50=1、P90=2、P95=3，P99.5=25。
86.34% 非零像素只有一个点，主要可见结构处在 1–3 点这一段。
**同计数的真实道路点与噪点不可能仅靠全局单调映射分开。** 必须靠邻域结构、
模型或后处理处理；不能把亮度增加视为信噪比已提高。

Q=25 时的公式计算结果：

| 映射 | 1 点 | 3 点 | 10 点 |
| --- | ---: | ---: | ---: |
| sqrt | 0.200 | 0.346 | 0.632 |
| log1p, s=0.5 | 0.279 | 0.495 | 0.774 |
| log1p, s=1 | 0.213 | 0.426 | 0.736 |
| log1p, s=2 | 0.156 | 0.352 | 0.688 |
| asinh, s=0.5 | 0.313 | 0.541 | 0.801 |
| asinh, s=1 | 0.225 | 0.465 | 0.766 |
| asinh, s=2 | 0.149 | 0.371 | 0.718 |
| asinh, s=10（旧预览值） | 0.061 | 0.180 | 0.535 |

判断：log1p 的旧值 s=1 可以继续作为中间基线；asinh 的旧值 s=10 对低计数
偏暗，优先向 0.5、1、2 探索。新页面默认显示 log1p=1、asinh=1，仅用于比较，
**没有覆盖原冻结 profile，也不是最终训练默认参数**。

## 具体搜索顺序

1. 固定原始点、2 m 栅格、空间划分、标签、Q=P99.5。
2. 各组预览 `s ∈ {0.1, 0.25, 0.5, 1, 2, 5, 10}`，重点比较 0.5、1、2。
   共 1 个 sqrt 基线、7 个 log1p、7 个 asinh，而不是把两套 s 做 7×7 笛卡尔积。
3. 每组留下两三个可接受值；在候选附近按几何间隔细化，例如 0.5–2 可增加
   0.707、1.414。图像预览只用于缩小范围。
4. 固定候选尺度再检查 Q=P99/P99.5/P99.9，标尺始终从训练区域估计。
   当前三档 Q 分别为 14、25、94；只改 Q 已会显著影响亮度，因此不同时盲调。
5. 人工复核并固定空间分组训练/调参验证/最终测试区域。原 preview 区域看过后
   只能作开发验证，不能再算最终未见测试。统计量只拟合训练区域，按空间组
   分割避免相邻窗口交叉污染。[分组交叉验证](https://scikit-learn.org/stable/modules/cross_validation.html)、
   [预处理泄漏](https://scikit-learn.org/stable/common_pitfalls.html)。
6. 首轮三模型比较使用相同配置、数据、预算和对应随机种子，再分别优化。
   检测优先比较相同每平方公里 FP 负担下的目标召回；圆额外报告圆心/裁剪半径误差；
   U-Net 同时报告像素 Dice/IoU 与经固定后处理得到的目标召回，不能用大量背景
   像素导致的高 accuracy 代替交叉口效果。最终候选重复多个种子，报告波动。
7. 若继续扩大搜索，建议 s 使用 log-uniform 分布，Q 作为离散选项，使用随机搜索
   或 TPE。最终按独立测试集验收，不反复查看测试集挑参数。

## 新预览工具

```sh
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  python -m pipeline cv-tune \
  --preview /data/vision/normalization/preview-20260911-v2 \
  --output /data/vision/normalization/tuning-20260911
```

只读已有 `profile.json`、`manifest.json`、`tile-*-counts.npz`，不访问数据库，
也不改动旧预览。8 个窗口、每窗 45 张参数图片，共 360 张。
`index.html` 内嵌图片，可离线即时切换；`comparison.json` 是可替换的数据配置；
`training-candidates.json` 记录三组候选、搜索范围与尚未选定的最终参数；
`sweep-metrics.csv` 保存低计数亮度、截断与量化诊断。NPZ 继续保存原始计数。
这些是选窗预览产物，不是已准备完成的全量训练集。

网页支持独立调整两种 s、切换 Q、放大、规则圆叠加、导出当前参数及保存 HTML。
原有五组历史预览保留。泛用个人模板位于用户指定 Obsidian `00_inbox`，使用
合成示例而非项目数据；页面渲染模板源码为 Pipeline 的 `image_compare.html`。
