# CVAT Community 导入与往返实测（2026-09-30）

本记录验证 `scripts/cv_human_check/export_coco.py` 生成的检测预标注包能否导入 CVAT Community
并完成复核往返。术语遵循[项目术语与写作规范](../terminology.md)。

## 环境与对象

CVAT 版本 2.76.1，`cvat_server` 与 `cvat_ui` 已连续运行 5 天，UI 监听 `http://localhost:8080`。
导入前 `engine_task`、`engine_job`、`engine_image` 均为 0 行，`engine_project` 只有 1 个
无标签的 `roadnet` 项目，因此这是该部署的首次实际导入。

被验证的包由固定提交 `63ba627` 的 `export_coco.py` 在 2026-09-16 生成，包含 26 张 `sqrt`
灰度图与 28 个检测框。**本次验证没有重新运行 `export_coco`，而是直接复用了该已生成包**，
因此 `export_coco` 的写出行为、`DATA_ROOT` 归属和 `--method` 语义都未在本轮覆盖。

验证时源包位于 `/home/reid/workspace/data/road-network/vision/datasets/detect-173ade7d-f03d-4c9d-8e46-906279085c9b-bbox-20260916/coco-review`，
其后复制到 `/data/datasets/roadnet/vision/...`，见下文「数据集归位」。
注意该路径不在 live `.env` 的 `DATA_ROOT=/data/tmp/road-network` 之下，也不在 personal
checkout 的 `DATA_ROOT=/home/reed/workspace/data/road-network` 之下（用户 `reed` 在本机不存在）。
`vision/` 目录建于 2026-09-16 12:05，先于包生成时间 9 小时。

为不改动用户密码，访问通过 Django `Token` 完成，未创建或修改任何账号。`cvat_server` 容器
只挂载自身数据卷，未挂载数据集目录，因此驱动脚本在宿主机运行并访问 `http://localhost:8080`。

## 与文档不一致的接口事实

以下结论为代码事实，核查路径为容器内 `/opt/cvat/cvat/apps/engine`。

- CVAT 2.76.1 只广告 `COCO 1.0` 与 `COCO Keypoints 1.0` 两个 COCO 格式；不存在名为
  “COCO 1.1” 的格式。导入与导出均使用 `COCO 1.0`。
- 标注上传接口 `POST /api/tasks/{id}/annotations` 读取的查询参数名是 `format`，不是
  `format_name`；传入后者返回 400 `Unknown input format ''`。`format_name` 只用于 TUS 数据上传路径。
- 旧导出接口 `GET /api/tasks/{id}/annotations?format=...` 已返回 410。数据集导出改为
  `POST /api/tasks/{id}/dataset/export`，携带 `format`、`save_images`、`filename`，返回 `rq_id`，
  再轮询 `GET /api/requests/{rq_id}`，完成后从 `result_url` 下载。
- 标签属性名必须与标签上已定义的属性完全一致，否则在导入时被**静默丢弃**：
  `import_dm_annotations` 按 `_get_attribute_id` 过滤，而 `_soft_attribute_import` 默认为 False。
  预标注包依赖这条规则，因此必须先按包内 README 建好两个属性再上传标注。
- 在 `project_id` 下创建任务时不能同时提交 `labels`；`roadnet` 项目的 `label_set` 为空，
  因此本次在任务上直接定义 `intersection` 标签及 `source_intersection_id`、`review_status` 两个属性。
- 编辑已有框必须用 `PATCH /api/tasks/{id}/annotations?action=update`。`PUT` 是整体替换语义，
  携带既有 `id` 会返回 400 `must be absent`。

## 导入结果

镜像上传后任务规模为 26，状态进入 `annotation`。预标注导入后读回 28 个 `shapes`、0 个 `tracks`、
0 个 `tags`，与源包 28 个标注一致。属性落库为 `source_intersection_id` 文本（默认空）与
`review_status` 选择（默认 `unreviewed`）。

逐图核对：26 张图全部找到对应帧；每张图的框数与源包数量差为 0；跨图的文件名与框对应关系无误。
`metadata/circles.json` 中的 28 个圆全部转化为检测框，**没有任何圆因完全落在画布外而被丢弃**。
同时复核了包内的几何自洽性：28 个框的 `x`、`y`、`width`、`height` 均非负，`x + width` 与
`y + height` 均未超出图像边界，`area` 全部等于 `width * height`。

## 导出往返

以 `COCO 1.0` 导出并下载，得到的标注文件中图像 26 个、标注 28 个、类别为 `intersection`，
与源包完全一致。框坐标差异全部在 0.01 像素以内，来自 CVAT 内部的浮点精度，不构成语义偏移。
最大偏差出现在 `intersection-3543.png`，源 `(0.815, 0.475, 126.551, 126.551)` 导出为
`(0.81, 0.47, 126.56, 126.56)`。`source_intersection_id` 与 `review_status` 均按原值保留；
导出额外增加 `occluded: false` 与 `rotation: 0.0` 两个固定值字段。

## 复核往返

按人工复核的真实动作验证一次完整闭环：把 `intersection-3543.png` 上的一个框平移 `(+3, -2)`
并把 `review_status` 改为 `accepted`；在 `intersection-3580.png` 上新增一个框，赋予
`source_intersection_id=999999` 与 `review_status=uncertain`；随后导出。

结果：任务从 28 个框变为 29 个框；平移后的框位置与两个属性都正确回传；新增框及其属性正确回传；
29 个标注全部带 `source_intersection_id`，没有出现无法关联回源包的图。

26 张图**每张都至少有一个框**，即当前包不含负样本。`intersection-3543.png`、`intersection-3580.png`
与 `intersection-3741.png` 各带两个框，这类图上单凭文件名无法区分框属于哪个圆，
`source_intersection_id` 是唯一区分依据；清空该属性会使回传结果无法映射回源圆。这是设计约束，不是缺陷。

## 验证产物与归属

两个导出包归入数据根 `/data/service-data/vision/datasets/`，与模型目录 `models/` 同级，
不再写回数据集目录。移动前后 sha256 一致：

| 文件 | 字节 | sha256 |
| --- | --- | --- |
| `cvat-roundtrip-export.zip` | 10,089 | `232886f4c50862d2c0530fa819d5f8c4173b0fc9a2ba35357e8f2f5785d1cff9` |
| `cvat-reviewed-export.zip` | 10,306 | `b52adb94e3dd3ce2ae0a585a1f55686ffb246b0fc9749d971e32a3404afe54af` |

`cvat-roundtrip-export.zip` 是导入后原样导出的无损往返凭据；`cvat-reviewed-export.zip` 含上节
的演示性编辑（多一个 `source_intersection_id=999999` 的框、一个框为 `accepted`），不是干净数据。
两个包均以 `save_images=False` 导出，内部只有 `annotations/instances_default.json`。

CVAT 中保留验证任务 `roadnet-coco-roundtrip-20260930`，其已恢复为导入后的干净状态：
28 个框、几何与源包逐框一致、无演示框、`review_status` 全为 `unreviewed`。CVAT 服务端
`cache/export/` 下另有 3 个导出缓存文件，可随任务删除。

## 数据集归位

2026-09-30 将宿主机数据集复制到 `/data/datasets/roadnet/vision`，并在 CVAT 侧保持可见。
该位置位于 CVAT 的 share 挂载根 `/data/datasets` 之下，因此容器内路径为
`/home/django/share/datasets/roadnet/vision`，Server files 填
`datasets/roadnet/vision/...`。复制范围与校验：

| 内容 | 来源 | 文件数 | 校验 |
| --- | --- | --- | --- |
| `datasets/` | `/home/reid/workspace/data/road-network/vision` | 169 | 逐文件 sha256 一致，无缺失 |
| `normalization/` | 同上 | 638 | 逐文件 sha256 一致 |
| 两个往返导出包 | `/data/service-data/vision/datasets` | 2 | sha256 与移动后一致 |

源目录保留未删。数据集目录现在同时存在于三个位置，来源与用途各不相同：

- `/home/reid/workspace/data/road-network/vision`：生成 `coco-review` 时的原始位置，保留。
- `/data/service-data/vision`：`MODEL_ROOT` 所在的模型目录，另含上述两个导出包。
- `/data/datasets/roadnet/vision`：本次归位后的位置，也是 CVAT 直接可读的位置。

注意这三处都不是通过 `DATA_ROOT` 派生的。仓库 README 将 `DATA_ROOT`
（`/data/tmp/road-network`）定义为「保存可丢弃的 Pipeline 工作区、中间文件和运行产物」的
挂载根，而本数据集不在其下；`export_coco` 的 README 示例使用的容器内路径
`/data/vision/datasets/...`（即宿主机 `/data/tmp/road-network/vision/datasets/...`）
在当前部署中并不存在。因此重新生成或重建 CVAT 任务时，需按实际位置指定路径，
不能直接照搬该示例。

## 复现备忘

包内 README 的 UI 操作步骤（建任务 → 建标签 → 上传 `images.zip` → 上传
`annotations/instances_default.json`）与本次通过 API 执行的步骤等价，因此未在图形界面逐一点击。

重新执行 `export_coco` 时，输出目录已存在会直接抛出 `FileExistsError`，命令行入口也在
`output already exists` 处拒绝；其 `--output` 还要求位于 `DATA_ROOT` 之下。要重新生成
`coco-review` 必须先删除该目录或换新输出路径。本轮未覆盖这些行为。

`scripts/cv_human_check/tests` 在本轮前后均通过 32 项；本轮未修改该目录的源码。

驱动脚本留存于 `/tmp/cvat_import.py`、`/tmp/cvat_review_roundtrip.py`、`/tmp/cvat_restore.py`。
