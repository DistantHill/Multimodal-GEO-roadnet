# CV 模块编排与外部权重管理规格说明（specification，Spec）

术语遵循[项目术语与写作规范](terminology.md)。

状态：草稿
创建日期：2026-09-13
更新日期：2026-09-13
访谈进度：Q1–Q4 已回答；Q5 的目录方案已被后续更正取代；用户明确要求迁移人工检查脚本，见文末。

## 1. 背景与问题

用户讨论整理 `src`、把计算机视觉（computer vision，CV）代码提取为 `cv/`，
并希望使用外部路径管理模型权重。本轮调用访谈技能（grilling），先确认设计，
再按授权实施；此前的目录建议不自动视为用户决定。

已检索 `docs/` 内文档正文（排除模板作为候选），阅读
`cv-normalization-preview.md`、`cv-normalization-tuning.md`，并核对已有前端、投影修复
Spec 的范围。两个 CV 文档分别覆盖归一化预览与参数搜索，没有覆盖模块重构和权重
生命周期；其他 Spec 属于不同功能。因此依 `docs/templates/grilling-spec.md`
创建本独立主题，保留原文档，不重复定义归一化算法选型。

迁移前代码与环境事实（只读核查，2026-09-13；以下旧路径作为历史证据保留）：

- `services/pipeline/src/track/v2/cv_preview.py` 同时包含统计、归一化、栅格、选窗和页面导出；
  `cv_tuning.py` 导入前者并从同目录读取 `image_compare.html`。
- `services/pipeline/pyproject.toml` 当前 package-data 为 `track.v2/image_compare.html`。
- `services/pipeline/src/track/v2/cli/cv_preview.py`、`cv_tune.py` 调用上述业务模块；
  `services/pipeline/src/pipeline/__main__.py` 提供现有 CLI 分发。
- 仅读取 `.env` 中非敏感路径变量：DATA_ROOT 已为
  `/home/reed/workspace/data/road-network`，MODEL_ROOT 已为该目录下的 `models`。
  DATA_ROOT 存在，外部 `models` 子目录不存在。
- 仓库 `models/yolo/yolov8n.pt` 存在，大小 6,549,796 字节；外部 DATA_ROOT
  当前未找到 `.pt/.pth/.onnx` 权重。文件存在性与大小来自只读文件检查。
- `infra/docker/compose.dev.yml` 将 MODEL_ROOT 挂为 `/models:ro`，显式 `.env`
  覆盖仓库目录 fallback；当前默认外部权重位置缺失。
- `Makefile` 中 `vision-check` 仍绑定仓库 `models/yolo`；权重迁移需要同步此入口。
- Pipeline 要求 Python >=3.12、NumPy >=2；Vision Dockerfile 使用 NumPy <2、
  Ultralytics 8.3.0。Vision 当前仅有 `track_export.py` 与 trackers，没有训练包。
- `.gitignore` 忽略 `models/` 与权重扩展名；网络结构源码宜使用 `networks/` 命名。

工具职责补充核查（2026-09-13，依据上述 CLI 与业务模块源码）：

- `cv-preview` 从原始轨迹点聚合像素计数，统计归一化标尺、选择样例窗口，
  输出五组归一化对比 HTML、PNG、原始计数与参数/来源文件。
- `cv-tune` 读取已有预览保存的计数与标尺，枚举 sqrt、log1p、asinh 的候选参数，
  输出交互 HTML、PNG、候选参数及统计 CSV；不重新查询数据库，不自动选出最佳参数。
- 两条命令是灰度预处理与可视化比较工具，均不训练模型或执行交叉口模型推理。
  此处的“调参”指灰度归一化参数；它们没有调整 YOLO、CenterNet 或 U-Net 的模型参数。

## 2. 目标与非目标

用户在 Q5 后明确更正：预览、调参与 `image_compare.html` 属于人工检查脚本，
全部迁往 `scripts/cv_human_check/`，不放在核心 `track.v2` 中。此具体迁移指令
已授权本次实施，取代此前建立 `track/v2/cv/` 的目录建议。

本次整理保留既有归一化算法、候选参数、HTML 交互及输出文件格式（Q1）。
同步迁移入口、测试和模板，更新必要的开发环境挂载与说明；不创建空的模型训练包。
其他 Pipeline 阶段、数据库卷、已有预览产物及个人 Obsidian 模板不属于本次修改范围。

权重外置仍是 Q2、Q4 已采纳的后续事项，与本次脚本迁移分别记录。
本次用户没有确认原 Q5 的完整方案，不借本次脚本迁移执行权重搬运。

## 3. 需求与使用场景

- 人工检查工具从仓库检出目录运行；数据库预览复用 Pipeline 的数据库读取接口和投影代码。
- `cv_preview` 生成原始计数与五组预览；`cv_tune` 读取保存的数据，枚举三组归一化参数。
- 生成的报告和数据写到仓库外；命令仍拒绝覆盖已有输出目录。
- 图像比较模板与脚本放在一起，Pipeline 安装包不再携带此模板。
- 两条工具通过 `python -m scripts.cv_human_check.cv_preview`、
  `python -m scripts.cv_human_check.cv_tune` 运行。
  原先保留 `python -m pipeline` 两条 CV 子命令的方案（Q1、Q3）被本次整体迁出要求取代。

## 4. 方案与关键决策

当前目录方案（Q5 后用户更正）：

```text
scripts/cv_human_check/
├── __init__.py
├── cv_preview.py             # 数据库预览命令
├── preview.py                # 既有栅格、统计、归一化和报告函数
├── cv_tune.py                # 参数比较命令
├── tuning.py                 # 既有参数枚举和报告函数
├── image_compare.html
├── README.md
└── tests/
```

以迁移既有职责为本次范围，不额外拆出 `raster.py`、`normalization.py`。
未来生产模型训练或推理需要哪些可复用能力，届时依据实际依赖确定。
核心包不反向导入这些人工检查脚本；原 CV 业务模块、CLI 文件及 Pipeline 分发入口移除。

权重管理后续方案沿用 Q2、Q4：Git 保存最小模型清单，权重文件外置；标注下载的
预训练权重、本项目未训练；不做权重哈希、来源真实性或模型加载检验，不自动下载。
本次不实现该部分，不引入 Git LFS、DVC 或模型注册服务。

## 5. 接口、数据与架构影响

本次变更：更新脚本导入、输出目录保护的仓库根路径计算；移除 Pipeline 的模板包资源
配置和两个 CV 子命令；相关测试迁至脚本目录。开发 Compose 只读挂载本工具目录到
`/app/scripts/cv_human_check`，`make test` 将其测试纳入执行。生产 Compose 不变。

后端接口、数据库结构和数据库卷不变；运行数据继续通过 DATA_ROOT 使用。
用户的外部路径约定如下，属于已有上下文而非本次新增迁移：

| 用途 | 路径 |
| --- | --- |
| Windows 访问 | `\\wsl.localhost\Ubuntu-22.04\home\reed\workspace\data\road-network` |
| WSL DATA_ROOT | `/home/reed/workspace/data/road-network` |
| WSL MODEL_ROOT | `/home/reed/workspace/data/road-network/models` |
| 后续权重目标（Q2） | `${MODEL_ROOT}/yolo/yolov8n.pt` |

具体机器路径留在忽略的 `.env`；Windows UNC 路径不作为 Linux 挂载源。

## 6. 约束与非功能要求

- 源码与 HTML 模板进 Git；运行产物、权重和凭证不进 Git。
- 保留工作区中无关修改，包括既有 `visualizable.py` 删除。
- 不改变算法、候选参数或原始点计数语义。
- 本次不读取真实权重，不运行 `vision-check`，不启动模型训练。

## 7. 验收标准

1. 两条新脚本入口可运行帮助和参数检查；拒绝向仓库内输出运行数据。
2. 既有计数、归一化和报告测试通过，候选数量及冻结标尺语义保持一致。
3. 模板在新目录可读取，渲染后的 HTML 正确嵌入比较数据。
4. 核心源码不再导入这些工具，Pipeline 包资源配置不再包含 `image_compare.html`。
5. `make test` 包含迁移后的脚本测试；开发 Compose 配置可解析。
6. 数据库卷、现有权重和已生成报告没有被本次脚本迁移修改。

## 8. 实施与验证记录

已迁移脚本、模板及测试，更新调用路径、开发挂载、测试命令与使用说明。
验证结果（2026-09-13）：

- `make test`：Backend 66 项通过；Pipeline 与人工检查脚本合计 354 项通过、2 项跳过。
  脚本测试覆盖新模块命令、仓库内输出拒绝、归一化、冻结标尺、候选图片与模板渲染。
- `docker compose --env-file .env -f infra/docker/compose.dev.yml config --quiet` 通过。
- 与迁移前 Git 内容逐字节比较：`preview.py` 和 `image_compare.html` 完全一致；
  `tuning.py` 仅修改导入路径。因此本次未重复执行浏览器交互测试。
- `git diff --check` 用于检查变更格式；本次未运行真实数据库预览或模型加载。

## 9. 风险与待决事项

- 用户明确要求工具迁出核心，旧 Python 导入和旧 Pipeline CV 子命令不再可用，
  使用说明已指向新入口。
- 脚本依赖仓库检出目录及 Pipeline Python 环境，不随独立 Pipeline 安装包分发。
- 外部权重迁移、最小清单及缺失权重处理仍待后续实施，不能报告已完成。
- 原 Q5 汇总含已被更正的目录和入口安排，不再用其作为当前全文确认问题。

## 10. 确认记录

设计确认：Q1–Q4 保留原文；目录归属与入口方案以 Q5 后“这些都放在…scripts/cv_human_check”
的更正为准。该更正不代表原全文确认，Spec 保留草稿状态以区分剩余权重事项。
实施授权：本次用户明确指令授权脚本、模板及必要配套迁移，正文第 2–8 节记录其范围。
验收确认：上述自动化验证已通过，跳过项如实保留；尚无用户验收确认。

## 附录：完整 QA 记录

### 初始上下文（非本轮设计问答）

用户上轮问题原文：

> 整src文件夹，内部应该怎么编排？把cv模块提取到cv文件夹里，模型权重放在git里管理合适吗？单纯提问探讨

用户本轮调用原文：

> $grill-me 外部path \\wsl.localhost\Ubuntu-22.04\home\reed\workspace\data\road-network ，这样实现上面的结论，ok吗

用户技能转发原文：

```text
<skill>
<name>grill-me</name>
<path>/home/reed/.codex/skills/grill-me/SKILL.md</path>
---
name: grill-me
description: A relentless interview to sharpen a plan or design.
disable-model-invocation: true
---

Call the Skill tool with "grilling".

</skill>
```

本轮开始前助手建议的完整内容见会话历史；不把这些建议当成用户已确认的决定。
本 Spec 的设计问答从 Q1 开始，旧主题的历史问答仍属于原会话，不在此重编号。

### 第 1 轮

#### Q1 — 这次重构到什么范围？

> 这次是只整理已有 CV 代码及对应测试、导入和模板资源，还是连整个 Pipeline src 和 Vision 训练目录一起重编排？

> 推荐本轮只整理已有 CV：建立 `track/v2/cv/`，保留 `cli/` 和现有两条命令；Cleaner、Detect、Clip、Fitting、Network 维持现状。Vision 本轮只调整权重相关入口，三个模型的训练代码另行实现。

> 用户回答：Q1 只整理cv模块

正文关联：第 2、3、4、5、7、8 节。
回答来源：第 1 轮合并回答，对应 Q1 的原文摘录。
当前解释：本轮只整理现有 CV 模块及其必要调用、测试与模板资源，不扩大为全部 src 重构。

#### Q2 — 是否这次把现有权重真正迁入外部目录，并建立最小模型清单？

> 目前 `.env` 已指向外部 `models`，但该目录不存在；实际 `yolov8n.pt` 仍在仓库的忽略目录里。这次完成迁移与清单，还是只调整代码目录、暂缓权重迁移？

> 推荐本轮完成：把现有权重迁到 `${MODEL_ROOT}/yolo/yolov8n.pt`，校验 SHA-256 和加载成功后移除原文件；同步 `vision-check` 等入口，在 Git 中记录相对路径、哈希、来源与用途。权重二进制不进 Git，本轮不引入 Git LFS、DVC 或模型注册服务。

> 用户回答：Q2确定，不用检验了，现在的模型都是直接下载的，还没训练

正文关联：第 2、3、4、5、6、7、8 节。
回答来源：第 1 轮合并回答，对应 Q2 的原文摘录。
当前解释：接受迁移及最小清单，明确取消原推荐的权重哈希和加载检验；
清单记录“下载的预训练权重，本项目未训练”，不填写未经检验的哈希与训练结果。

### 第 1 轮合并回答 — 对应 Q1、Q2

接收日期：2026-09-13。
用户回答原文：

> Q1 只整理cv模块 Q2确定，不用检验了，现在的模型都是直接下载的，还没训练

解释范围：Q1 限定代码重构范围；Q2 接受权重迁移并取消权重检验，补充权重来源与训练状态。
正文同步：第 2–10 节已按限定更新，原问题与发问时推荐保留不改。

### 第 2 轮 — 兼容与缺失处理（2026-09-13）

发问时前提：只整理 CV；迁移现有下载权重；不执行权重哈希或模型加载检验。

#### Q3 — 旧 Python 导入路径是否保留？

> 迁到 `track.v2.cv` 后，是否还要保留 `track.v2.cv_preview`、`track.v2.cv_tuning` 的兼容转发？

> 推荐统一更新仓库内调用和测试，移除旧模块，不增加兼容转发；`cv-preview`、`cv-tune` 两条命令保持不变。

> 用户回答：都采用建议

正文关联：第 3、4、5、7、8 节。
回答来源：第 2 轮合并回答，同一原句共同适用于 Q3、Q4。
当前解释：采纳移除旧模块、统一新导入路径的建议，保留两条命令。

#### Q4 — 加载时缺少权重如何处理？

> 如果外部目录缺少所需权重，入口应报告缺失路径，还是自动从网络下载？

> 推荐报告缺失路径，由你放入下载的权重后再运行；本轮不增加自动下载功能。

> 用户回答：都采用建议

正文关联：第 3、5、6、7、8 节。
回答来源：第 2 轮合并回答，同一原句共同适用于 Q3、Q4。
当前解释：采纳缺失时报路径、由用户放置权重的建议，不自动下载。

### 第 2 轮合并回答 — 对应 Q3、Q4

接收日期：2026-09-13。
用户回答原文：

> 都采用建议

解释范围：共同采纳 Q3、Q4 的推荐；不推定为全文最终确认或新增实施授权。
正文同步：第 2–10 节同步已采纳决定，状态改为待确认。

### 第 3 轮 — 最终设计确认（2026-09-13）

发问时前提：Q1–Q4 已回答，当前没有其他待决设计分支。

#### Q5 — 是否确认完整方案并结束本轮访谈？

> 最终方案：
> 1. 只整理现有 CV 代码到 `track/v2/cv/`，拆分栅格、归一化、预览、调参与模板；算法和页面交互保持现状。
> 2. 统一更新导入，移除旧模块；保留 `cv-preview`、`cv-tune` 命令。
> 3. 权重迁至 `/home/reed/workspace/data/road-network/models/yolo/yolov8n.pt`，同步入口；Git 只记录最小模型清单，标注下载的预训练权重、本项目未训练；按你的要求不做权重检验。
> 4. 权重缺失时报告路径，不自动下载；代码、路径和模板按影响范围验证，数据库卷保持原状。
>
> 以上是否就是最终方案，可以结束本轮 grilling？

> 推荐确认此方案并结束访谈；本题确认设计，实施授权另行记录。

> 用户回答：待回答。

正文关联：第 2–10 节。

本轮检查：Q1–Q4 问答及两条合并回答完整保存，历史推荐保持原文；
正文与最新回答一致；Q5 为唯一待回答项，全文处于待确认状态；QA 附录位于文末。

### Q5 后续补充 — 现有工具职责澄清（2026-09-13）

关联原记录：第 3 轮 Q5，完整方案最终确认仍待回答。
用户补充原文：

> 等下，这个cv_preview和cv_tune是啥,我看了下是调参的html文件的生产工具?

答复依据：核查 `cli/cv_preview.py`、`cli/cv_tune.py`、`cv_preview.py`、
`cv_tuning.py`。前者命令从数据库聚合原始点并生成预览及可复用数据；后者命令
从已保存数据枚举归一化参数并生成交互对比报告。两者均不训练或运行交叉口识别模型，
也不自动选出最佳参数。
当前解释：这是职责澄清问题，不是对 Q5 的确认，也不推定用户要求改变目录方案。
正文同步：第 1 节补充源码事实、第 2 节明确本轮整理的实际范围；访谈进度注明澄清。

### Q5 后续更正 — 人工检查脚本迁出核心（2026-09-13）

关联原记录：Q1、Q3 的目录与命令约定，以及 Q5 的最终方案提问。
用户更正原文：

> 这种算是scripts把，不应该在核心track.v2模块里，还有这个image_compare，这些都放在/home/reed/workspace/personal/Multimodal-GEO-roadnet/scripts/cv_human_check

当前解释：明确指定工具与模板归属，并直接要求迁移。将业务、入口、模板和相关测试
一起迁到指定目录，核心不保留工具分发入口；以独立模块命令运行。
该具体指令授权此范围的实施，不推定为权重迁移或原 Q5 全文确认。
正文同步：第 2–10 节改为当前目录、入口、实施范围和验收；原 QA 与发问时推荐保留。
本轮检查：更正原文完整保存；Q5 旧方案已标明被取代；未新增虚假的确认回答。
