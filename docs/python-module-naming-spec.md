# Python 包层级与源码命名规格说明（specification，Spec）

术语遵循[项目术语与写作规范](terminology.md)，不在本文件复制通用术语表。

状态：待确认
创建日期：2026-09-24
更新日期：2026-09-26
访谈进度：已回答 Q1–Q12；待完整设计确认
代码事实核对版本：提交 `f3e214f406a0ef6e04b41d86305689d22a367e0d`（2026-09-24 核对）；源码事实按该提交核验，路径是历史快照标识，不能据此判断文件仍在当前工作区。文中的相对链接只用于导航当前仍存在的文件；历史路径以行内代码形式保留。

## 1. 背景与问题

已有的[Python 源码模块命名审查与建议](python-module-naming-recommendations.md)覆盖核对版本下 `apps/backend/src` 与 `services/pipeline/src` 中的 129 个 Python 源码文件，但其规则把仓库目录、Python 包、模块文件和函数的命名责任混在一起；逐文件目标表也可能把“目标导入路径”误解为“每个文件名都必须完整描述输入、动作与输出”。用户调用 grilling 审查该方案，并询问各层实际负责什么。本 Spec 记录本次设计访谈，不覆盖已有审查文档。

核对版本下的代码事实：发行包名与 `src` 源码根由 [services/pipeline/pyproject.toml](../services/pipeline/pyproject.toml) 和 [apps/backend/pyproject.toml](../apps/backend/pyproject.toml) 分别配置；仓库根目录不在 `track.v2...` 或 `backend...` 这类导入名中。`track`、`track.v2`、`track.v2.db` 和 `backend` 有包初始化文件；普通源码文件如当前 [Detect Parquet 导出模块](../services/pipeline/src/track/v2/intersections/results/export_to_parquet.py) 是模块，文件内有函数、类和常量。提交 `f3e214f` 的历史快照中，`services/pipeline/src/track/v2/manual_thin_benchmark/` 有 `_init_.py` 但没有 `__init__.py`；该目录在该快照下属于隐式命名空间包。该目录已按 2026-09-25 的新决策删除，以上路径只说明固定基线事实。Python 官方[导入系统文档](https://docs.python.org/3.12/reference/import.html)说明包是一类特殊模块，常规包和命名空间包均可包含子包与模块。

## 2. 目标与非目标

目标：确定包路径、模块文件和模块成员各自承载的语义；确定动作型模块与类型/契约型模块的命名方式；确定既有逐文件建议在后续实施中的适用范围。

设计目标覆盖包、模块和成员的命名责任及存量文件候选评估。后续用户另作决定：主工作树内 basename 为 `manual_*` 的临时文件与目录不纳入长期 Git 跟踪；已删除主工作树中受 Git 跟踪的 `manual_thin_benchmark/` 目录，并增加递归忽略规则；忽略的 `.worktrees/run-level-projection-crs` 独立旧副本按 Q11 保留。清理实施时，其余源码候选仍在分批迁移；截至本次复核，基线中的非手工候选均已实施或按职责证据保留。该状态与完整 Spec 的用户确认分别记录，后者仍未完成。

## 3. 需求与使用场景

已确认（Q1）：开发者应能从完整导入路径判断领域与主要职责；包路径、模块名和成员名共同表达语义，文件基名不必重复父包已经明确的词。已确认（Q2）：执行单一操作或转换的模块采用动作型名称；类型、接口契约、配置或紧密相关策略模块可用名词。已确认（Q3）：规则先用于新文件和正在修改的模块；存量文件按依赖与职责分批评估迁移，已有清单是候选目标。

已确认（Q4）：阶段专属代码先按阶段或数据域归包，确实被多个阶段共同使用的设施放入公共包。已确认（Q5）：一个模块围绕内聚职责组织，可以包含相关函数、类型、常量和辅助函数；独立变化、调用或复用的职责再拆分。已确认（Q6）：完整导入路径应能辨认处理对象和主要动作，来源存储或目标格式在区分相邻模块时显式写出；没有独立结果文件的校验、编排等操作不强行造结果词。已确认（Q7）：子包按实际职责边界建立，不采用固定目录深度。

已确认（Q8）：供业务代码直接导入调用的公共函数名应说明动作和对象，避免只用 `run`、`export` 等短名；本次审查进一步明确，命令行入口可保留 `main`。已确认（Q9）：带目标格式的转换按“动词＋来源对象＋`to`＋目标”排列；父包已有来源对象时，模块基名可省略重复部分。

用户明确说明本项目只有其本人使用（Q10 后续补充）。存量模块改名时同步更新现行代码中的导入、命令入口、脚本、测试及现行使用文档中的引用，并移除旧模块；历史问答和基准证据保留原文，必要时另加迁移说明。不为假设的外部调用方保留旧名字。2026-09-25 新决定进一步规定仓库中的 `manual_*` 文件/目录只作临时调试内容，不纳入 Git；本 Spec 的本轮 QA 记录了原文及其实施边界。

## 4. 方案与关键决策

设计树：

1. 完整导入路径承载完整语义（已确认，Q1）。目录/包表示领域与边界，文件/模块表示职责，函数/类/常量表示成员；文件基名可省略父包已表达的信息。类名说明其表示的对象、结果、配置或职责角色，不套用动作型模块的命名规则。常量名说明固定值的业务含义，必要时体现单位或适用范围。
2. 动作型规则按模块职责适用（已确认，Q2）。操作/转换模块用动词；类型、契约、配置或策略模块可用名词。尚需按此原则重审具体文件映射。
3. 现有清单是分批迁移的候选目标（已确认，Q3），不是一次性强制改名清单。
4. 阶段专属代码优先按阶段/数据域归包，共用设施放公共包（已确认，Q4）。原建议文档中统一按技术职责归入 `artifacts`、`relations`、`algorithms` 的目标路径须重审，不能直接作为最终映射。
5. 模块围绕一个内聚职责，可包含相关成员；独立变化、调用或复用的职责再拆分（已确认，Q5）。不采用一函数一文件。
6. 完整导入路径必须辨认处理对象与主要动作；来源存储或目标格式仅在有区分作用时显式写出；不强造结果词（已确认，Q6）。
7. 子包在多个模块共享稳定领域边界，或独立领域需要清楚导入入口时建立；无固定深度（已确认，Q7）。
8. 供业务代码直接导入调用的公共函数，名称应说明动作和对象，不使用无上下文的短名（已确认，Q8）。命令行入口可保留 `main`，其具体用途由命令名和入口模块说明；不要求仅为满足本条而改名（本次审查的范围澄清）。
9. 以单一操作为主要职责的模块采用动作型名称。对于需要在名称中表达目标格式的转换，按“动词＋处理对象＋`to`＋目标”排列；父包已明确处理对象时，模块基名可省略重复部分（已确认，Q9）。校验、编排等操作不要求具有 `to_目标` 部分（已确认，Q6）。
10. 只有用户本人使用本项目；改名时同步更新仓库内引用并移除旧模块，不保留面向假设外部使用者的转发文件（已确认，Q10 后续补充）。
11. 命名决策已收敛；逐文件候选按 Q4 阶段/数据域优先和其余规则重审。审查映射仍限定于提交 `f3e214f` 的 129 个源码文件；后来新增的 `source_row_key_migration.py` 不在该基线审查内，已单独评估并按 Q2 改为动作型名称 `migrate_source_row_key.py`；Q9 仅适用于需要表达目标格式的转换，对数据库 schema 迁移不适用，故不添加 `to_bigint`。2026-09-25 已从主工作树删除该基线中的 31 个受 Git 跟踪的临时手工工具，并从命名迁移候选中排除；忽略的独立旧工作树副本按后续 Q11 决定保留。其余 98 个非手工基线项现已全部核对，其中 67 项已迁移、拆分或核实移除，31 项按职责证据保留，没有待处理的基线候选。候选映射不代表所有项目都必须一次性迁移。各批实施分别记录，不能据此推定完整 Spec 确认。

规则应用示例（目标命名示意，尚非逐文件迁移决定）：`track.v2.intersections.results.export_to_parquet` 中，`intersections.results` 表示 Detect 产生的交叉口结果，`export_to_parquet` 表示转换与目标格式；若函数被直接导入，其公开名称仍应说明动作和对象，例如 `export_detect_results_to_parquet()`。跨阶段共享的 Parquet 清单可以用 `track.v2.artifacts.parquet_manifest` 这样的名词型模块名。`f3e214f` 基线根层曾存在 `detect.py` 等同名模块，故当时不能直接建立 `track.v2.detect.*`；这些根模块现已迁入数据域包，示例记录历史冲突和正确的迁移顺序。只有实际职责边界支持这些子包时才建立对应目录。

## 5. 接口、数据与架构影响

命名设计影响 Python 导入路径、`pyproject.toml` 中的命令入口、Docker 启动目标、测试导入及文档引用。核对版本下，项目命令入口使用 `track.v2.cli.init_db:main` 等形式，见 [services/pipeline/pyproject.toml](../services/pipeline/pyproject.toml) 。后端启动目标使用 `backend.main:app`，见 [apps/backend/Dockerfile](../apps/backend/Dockerfile) 。命令入口的 `main` 可保留，具体用途由命令名与入口模块说明。命名方案本身不改变数据结构或持久化格式。

`f3e214f` 基线下的直接导入事实：基线路径 `services/pipeline/src/track/v2/detect_stage.py` 当时直接导入并调用 `export_detect_parquet`；基线 `pipeline.py` 直接导入 `run_detect_stage`，现行编排模块为 [orchestration/run_pipeline.py](../services/pipeline/src/track/v2/orchestration/run_pipeline.py)。这些基线事实说明业务函数名会脱离模块名直接出现在调用点，故 Q8 要求其独立说明动作和对象。当前导出函数已依此改为 `export_detect_results_to_parquet`。

## 6. 约束与非功能要求

按项目现有术语保持阶段名一致。完整导入名应可读，不机械重复上级包已表达的词。`f3e214f` 历史快照中的基准脚本曾使用固定相对路径和源码哈希校验；示例历史路径为 `services/pipeline/src/track/v2/manual_thin_benchmark/manual_bench_windows.py`，其源码现已随该临时目录删除。哈希清单和基准证据继续留在历史记录中，不为适配当前目录直接改写。

## 7. 验收标准

设计方向（Q1–Q10）的验收检查：规则能对包、模块、函数、类和常量分别给出命名责任；动作模块与名词模块区分；阶段专属代码与共享设施分开；模块按内聚职责组织；按实际边界设包而无固定深度；完整路径可辨认对象和动作，只有具区分作用的来源或结果才显式命名；需要表达目标格式的转换使用“动词＋处理对象＋`to`＋目标”词序且不重复父包信息，校验、编排等操作不要求 `to_目标`；供业务代码直接导入调用的公共函数说明动作和对象，命令入口可保留 `main`；逐文件映射作为固定基线下的职责审查与实施记录。基线 98 个非手工项已全部迁移/拆分或按职责证据保留；基线后新增模块仍需单独评估。

将来每批迁移更新现行代码、命令入口、脚本、测试及现行使用文档中的引用，并删除旧模块。历史问答和基准证据保留原文，必要时另加迁移说明。每批按以下结果验收：

1. 该批新模块路径可导入；现行代码不再依赖已删除的旧模块。
2. 该批涉及的命令入口、启动目标及相关测试通过。
3. 现行使用文档与代码一致；历史记录保留，不以“全仓库旧字符串零命中”作为验收标准。

本轮新增的临时工具清理标准：主工作树 `services/pipeline/src/track/v2/manual_thin_benchmark/` 不存在；测试与运行入口不导入该目录；`.gitignore` 对仓库任意层级下 basename 以 `manual_` 开头的文件和目录生效；现行操作文档不再给出已移除工具的可执行命令，历史 Spec、QA 与基准记录保留旧路径和原始证据。

## 8. 实施与验证计划

已确认（Q3）：新文件和正在修改的模块先采用规则，存量文件按依赖与职责分批评估。命名建议按 Q1–Q10 及后续审查更正维护。129 个基线项中，31 个 `manual_*` 文件已从主工作树删除并退出候选；余下 98 项已完成核对，67 项已迁移/拆分/核实移除，31 项按职责证据保留，没有待处理基线候选。已实施批次更新现行代码引用并删除旧模块，不新增兼容转发，逐项验证详见 QA 附录。递归忽略规则生效，依赖手工工具的测试已移除，现行操作文档已更新；忽略的独立旧副本依 Q11 保留。Q4/Q5/Q7 按实际消费者修订了建议边界，包括 `member_sequences`、`clip_relation`、`db.queries` 四域拆分和共享 `SpatialWindow` 位置。基线后新增的数据库迁移模块已按 Q2 改为动作型名称；Q9 对 schema 迁移不适用。2026-09-26 当前源码根合计 139 个 `.py` 文件（Backend 17、Pipeline 122）。`make test` 已通过：Compose 配置路径测试 5 passed，Backend 83 passed，Pipeline 使用全量配置 `pipeline.toml` 为 1,047 passed、67 skipped、8 warnings。实施与全量验证完成；Spec 状态仍为“待确认”，因为尚未收到对完整规则与方案的明确全文确认。历史 QA 与基准证据保留；基线后新增模块另行评估。

提交顺序（Q12）：先单独提交 Q89，再提交命名迁移。此顺序适用于用户回答所指的整个 Q89 工作，不将 Q89 限缩为 CV 配置修复；具体隔离仍按各提交的实际差异核对。

## 9. 风险与待决事项

- Q1–Q12 的局部回答、基线映射实施和 `manual_*` 清理已完成验证；Q12 已确定先单独提交 Q89，再提交命名迁移。状态“待确认”表示用户尚未明确确认完整 Spec。当前没有待处理的基线命名候选。
- 建议清单以 `f3e214f` 为基线；之后增加的源码需要另行审查，不能计入原 129 个文件的结论。
- 文件名若重复整个包路径，可能增长而不增加信息；文件名过短则可能脱离上下文后难以辨认。Q1 已确定以完整导入路径评估语义，具体边界仍需结合实际调用方式决定。
- 拆分与改名是不同操作；不能把职责混合的大文件仅靠更长的名字解释清楚。

## 10. 确认记录

待用户对上述完整命名规则与分批迁移原则作最终确认。Q1–Q12 的局部回答不替代完整 Spec 确认。Q12 仅确定先单独提交 Q89、随后提交命名迁移的顺序。实施授权、具体批次验收与全文设计确认分别记录；当前授权和实施结果见 QA 附录末尾。

## 附录：完整 QA 记录

### 第 1 轮 — 命名层级与规则边界（2026-09-24）

发问时前提：仅确认了代码目录与 Python 导入层级的事实；既有建议文档尚未被确认为最终规范。

#### Q1 — 完整语义单位

> 命名规范应以什么作为能表达完整语义的单位？
> A. 完整可导入路径：包名说明领域/阶段，文件名说明模块独有职责，函数、类和常量说明成员；父包已有的词不必在每个文件中重复。
> B. 每个文件名必须独立写全领域、来源、动作和结果，即使父包已表达这些信息。

> 推荐 A。Python 的目录包与文件模块共同组成导入名；在文件名里机械重复父包会掩盖真正区分相邻模块的词。

> 用户回答：Q1 A，完整路径承担完整语义

正文关联：第 3、4、7 节。回答来源：第 1 轮合并回答中的逐字摘录。当前解释：已选择 A；具体词语如何分配到每一层仍待 Q4 及后续问题。

#### Q2 — 动词规则的适用对象

> 所有 `.py` 文件都必须使用“动词＋宾语”吗？
> A. 只有以单一操作/转换为主的模块采用动作型文件名；定义类型、接口契约、配置或紧密相关策略的模块可用名词。
> B. 所有文件都统一使用动词型文件名，包括模型、配置和异常模块。

> 推荐 A。文件是可包含多个相关成员的模块；强制把 `models.py` 一类文件改成 `define_models.py` 不会更准确。

> 用户回答：Q2 A

正文关联：第 3、4、7 节。回答来源：第 1 轮合并回答中的逐字摘录。当前解释：已选择 A；具体文件映射在规则收敛后重审。

#### Q3 — 存量文件的适用范围

> 现有 129 个文件的逐项建议应如何使用？
> A. 先把确认的规则用于新文件和正在修改的模块；存量文件按依赖与职责分批迁移，清单是候选目标。
> B. 把清单视为所有现存文件必须立即改名的目标，不分批次。

> 推荐 A。现有 CLI 入口和历史基准脚本引用具体模块路径；分批迁移便于逐批验证。

> 用户回答：Q3 A。

正文关联：第 3、4、5、8、9 节。回答来源：第 1 轮合并回答中的逐字摘录。当前解释：已选择 A；清单作为分批迁移的候选目标。

### 第 1 轮合并回答 — 对应 Q1–Q3

接收日期：2026-09-24。
用户回答原文：

> Q1 A，完整路径承担完整语义 ；Q2 A；Q3 A。

解释范围：Q1、Q2、Q3 均选择 A；Q1 另明确完整路径承担完整语义。没有确认具体包层级或逐文件目标名称。
正文同步：第 3、4、7、8、9 节及访谈进度。

### 第 2 轮 — 包边界与模块职责（2026-09-24）

发问时前提：Q1 已确定完整导入路径承担语义；Q2 已确定动作模块与名词模块可分别命名；Q3 已确定存量清单是分批迁移候选。

#### Q4 — 包的主分层轴

> 阶段专属的代码与跨阶段的公共设施应怎样分包？
> A. 阶段专属代码先按阶段/数据域归属，例如 `track.v2.detect_results.export_to_parquet`；确实被多个阶段共同使用的设施再放到 `track.v2.artifacts` 等公共包。
> B. 所有代码先按技术职责归入 `artifacts`、`relations`、`algorithms` 等公共包，阶段名主要留在文件名里，例如 `track.v2.artifacts.export_detect_results_to_parquet`。

> 推荐 A。Detect、Refine 的结果有各自的业务语义；共享清单、发布机制等放公共包即可。实际新增子包仍需有清楚的职责边界，不能为单个短文件机械增加层级。

> 用户回答：全部采取推荐

正文关联：第 3、4、7、9 节。回答来源：第 2 轮合并回答，Q4–Q5 共同适用的逐字原句。当前解释：已选择 A；阶段/数据域优先，公共设施单列。

#### Q5 — 模块的职责边界

> 一个 `.py` 模块应容纳多少内容？
> A. 围绕一个内聚职责，允许相关的公开函数、类型、常量和私有辅助函数共存；当两个职责可独立变化、调用或复用时再拆分。
> B. 严格一个函数对应一个文件，文件名直接等于函数名。

> 推荐 A。Python 模块本身就是组织相关定义的单位；用“一个函数一文件”处理当前大文件会产生大量碎片，也无法解释共享类型与辅助函数。

> 用户回答：全部采取推荐

正文关联：第 3、4、7、9 节。回答来源：第 2 轮合并回答，Q4–Q5 共同适用的逐字原句。当前解释：已选择 A；模块按内聚职责组织。

### 第 2 轮合并回答 — 对应 Q4–Q5

接收日期：2026-09-24。
用户回答原文：

> 全部采取推荐

解释范围：此句回复第 2 轮同时展示的 Q4、Q5，选择两题的推荐 A；不扩展到尚未提出的问题，也不构成完整 Spec 确认。
正文同步：第 3、4、7、9 节及访谈进度。

### 第 3 轮 — 信息显式程度与包创建条件（2026-09-24）

发问时前提：Q1–Q5 均已选择推荐。完整导入路径承担语义；阶段/数据域优先归包，公共设施单列；模块按内聚职责组织。

#### Q6 — 来源、动作、结果的显式程度

> 对执行单一操作的模块，“来源、动作、结果”应写到什么程度？
> A. 完整导入路径必须能辨认处理对象和主要动作；当来源存储或目标格式能区分相邻模块时，再显式写 `from_postgres`、`to_parquet` 等。对校验、编排等没有独立输出文件的动作，不强行编造“结果”词。
> B. 每个动作模块的完整导入路径一律同时写出来源、动作和结果；校验、编排等操作也必须指定一个结果名，即使没有独立的结果文件。

> 推荐 A。这样保留用户要求的语义，同时让目录承担已确认的领域信息；例如 `track.v2.detect_results.export_to_parquet` 已说明来源、动作和目标格式。

> 用户回答：Q6 采用推荐

正文关联：第 3、4、6、7 节。回答来源：第 3 轮合并回答中的逐字摘录。当前解释：已选择 A；具体词序仍待讨论。

#### Q7 — 新增子包的门槛

> 何时为一组源码新建子包？
> A. 多个模块共享稳定的领域边界，或某个独立领域确实需要清楚的导入入口时新建；其余文件保留在最近的合适包中，不规定固定目录深度。
> B. 无论内容多少，都机械建立“运行单元/阶段/数据域/技术职责/动作”固定层级。

> 推荐 A。包是导航与依赖边界；固定层级会给只有一个文件的职责增加空目录，也会拉长导入名。

> 用户回答：Q7 按实际职责建立边界

正文关联：第 3、4、6、7 节。回答来源：第 3 轮合并回答中的逐字摘录。当前解释：与推荐 A 一致；按职责边界设包，不使用固定深度。

### 第 3 轮合并回答 — 对应 Q6–Q7

接收日期：2026-09-24。
用户回答原文：

> Q6 采用推荐；Q7 按实际职责建立边界

解释范围：Q6 选择推荐 A；Q7 的表述与推荐 A 一致。没有确定函数名形式或逐文件目标路径。
正文同步：第 3、4、7、9 节及访谈进度。

本轮检查：Q1–Q7 的完整问题、推荐与原回答均保留；第 3 轮合并原文完整；正文与最新回答一致；后续问题尚未发出；QA 附录位于文末。

### 第 4 轮 — 成员命名、词序与迁移兼容（2026-09-24）

发问时前提：Q1–Q7 已回答。完整路径承担模块语义，阶段/数据域优先分包，动作与输出词只在有区分作用时写出；现存文件按职责分批迁移。

#### Q8 — 对外函数名的独立可读性

> 对外函数从模块直接导入后，名字应如何保持可读？
> A. 公共函数名自身说明动作与对象，必要时带目标格式；不重复整段包路径。私有辅助函数可依靠模块上下文使用较短名称。
> B. 公共函数统一用 `run()`、`export()`、`read()` 等短名，调用方必须以模块限定调用来补足语义。

> 推荐 A。现有生产代码直接导入 `run_detect_stage` 和 `export_detect_parquet` 后裸调用，公共函数名需要在调用点独立说明动作与对象。

> 用户回答：Q8 公共函数对外说明动作和对象，避免用短名

正文关联：第 3、4、5、7 节。回答来源：第 4 轮合并回答中的逐字摘录。当前解释：与推荐 A 一致；公共函数自身说明动作和对象。

#### Q9 — 动作模块的词序

> 对带目标格式的转换，采用哪种词序？
> A. 完整语义为“动词＋来源对象＋`to`＋目标”；若父包已说明来源对象，模块可缩为 `export_to_parquet`。例如 `track.v2.detect_results.export_to_parquet`。
> B. 文件基名使用“动词＋目标＋来源对象”，例如 `export_parquet_detectresult`。

> 推荐 A。`to_parquet` 明确方向，`detect_results` 明确被转换的对象；模块名可借助父包避免重复。

> 用户回答：Q9 按推荐

正文关联：第 3、4、6、7 节。回答来源：第 4 轮合并回答中的逐字摘录。当前解释：已选择 A；旧建议文档中的示例与逐文件目标待重审。

#### Q10 — 旧导入路径的处理

> 分批迁移现存模块时，旧导入路径如何处理？
> A. 每批同步更新仓库内部导入、命令入口和测试；只有确实作为外部接口使用的旧路径才设明确的临时兼容转发，并安排移除条件。
> B. 所有旧模块都永久保留为别名，以免任何调用方需要更新。

> 推荐 A。项目存在直接模块导入和命令入口；批次内同步更新可以检验迁移结果，兼容转发按实际对外依赖保留。

> 用户回答：Q10 你说的是人话吗？重新组织语言

正文关联：第 5、7、8、9 节。回答来源：第 4 轮合并回答中的逐字摘录。当前解释：用户要求重写问题，未选择 A 或 B；以下使用具体例子重新发问。

### 第 4 轮合并回答 — 对应 Q8–Q10

接收日期：2026-09-24。
用户回答原文：

> Q8 公共函数对外说明动作和对象，避免用短名 ；Q9 按推荐 ；Q10 你说的是人话吗？重新组织语言

解释范围：Q8 与推荐 A 一致；Q9 选择推荐 A；Q10 只要求把问题说清楚，尚未对旧模块去留作决定。
正文同步：第 3、4、5、7、9 节及访谈进度。

### Q10 发问措辞更正 — 改名后旧文件的处理（2026-09-24）

关联原记录：第 4 轮 Q10 及其用户反馈。决定编号仍为 Q10；保留原问法作为历史记录。

> 比如把 `track.v2.refine_parquet` 改名后，仍用旧名字导入它的代码会失败。旧名字还要继续能用吗？
> A. 把本项目中使用旧名字的地方一起改掉；如果确实有项目外的使用者，就暂时让旧名字也能用，并提前说明何时停用。
> B. 旧名字永远保留，让新旧名字一直都能用。
> C. 旧名字立即删除，不检查是否还有人在用。

> 推荐 A。本项目的引用一起更新和验证；确有外部使用者时，给他们迁移时间。

> 用户回答：外部使用者是谁？这是个人开发项目，只有我一个人用，不要徒增防御性假设

正文关联：第 3、4、5、7、8、9 节。回答来源：下方 Q10 后续补充原文。当前解释：用户否定“外部使用者”前提；改名时更新本项目内的引用并删除旧模块，无需兼容转发。

### Q10 后续补充或更正 — 个人项目的使用范围（2026-09-24）

关联原记录：第 4 轮 Q10、Q10 发问措辞更正及用户要求重新组织语言的回答。
用户补充或更正原文：

> 外部使用者是谁？这是个人开发项目，只有我一个人用，不要徒增防御性假设

当前解释：没有外部调用方这一事实前提，原推荐中的外部兼容分支不适用。有效决策为分批改名时一并更新仓库内引用并移除旧模块；不设置仅为假设外部调用方服务的兼容层。此回答不构成完整 Spec 确认或实施授权。
正文同步：第 3、4、7、8、9、10 节及访谈进度。

本轮检查：Q1–Q10 的完整问题、推荐与原回答均保留；Q10 原问法、重写问法和用户更正按收到顺序记录；正文与最新回答一致；完整设计确认待用户答复；QA 附录位于文末。

### Q9 等后续审查更正 — 规则范围与迁移验收（2026-09-24）

关联原记录：第 3 轮 Q6、第 4 轮 Q8–Q10 及 Q10 后续补充。以下是本次审查原文；它修正文档正文的规则范围和验收表述，不改写先前问答，也不构成完整 Spec 确认。

用户补充或更正原文：

```text
**这份规格说明的总体方向合理，但有一处明确的规则冲突，以及几处需要补清的地方。最重要的问题是：Q9 的适用范围在正文里被扩大了。**

## 1. 必须修改：把“转换模块的词序”误写成了“所有动作模块的词序”

Q9 原始问题明确限定为\*\*“带目标格式的转换”\*\*，例如 `export_detect_results_to_parquet`。但第 4 节第 9 条写成了“动作模块按‘动词＋来源对象＋`to`＋目标’排列”，第 7 节验收标准也沿用了这个扩大后的范围。 &#x20;

这与 Q6 已确认的规则发生冲突：\*\*校验、编排等操作不应强行制造结果词。\*\*例如校验操作不需要为了满足命名规则，变成 `validate_..._to_...`。

**建议统一改为：**

> 以单一操作为主要职责的模块采用动作型名称。对于需要在名称中表达目标格式的转换，按“动词＋处理对象＋`to`＋目标”排列；父包已明确处理对象时，模块基名可省略重复部分。校验、编排等操作不要求具有 `to_目标` 部分。

修改第 4 节第 9 条和第 7 节即可，**不要改写附录里的历史问答原文**。

## 2. 需要补清：Q8 对普通业务函数和命令入口是否一视同仁

第 4 节规定公共函数名要说明动作和对象，不使用无上下文短名；但第 5 节同时引用了实际命令入口 `track.v2.cli.init_db:main`。文档没有明确说明 `main` 这类入口是否属于该限制范围。&#x20;

**这不是说现有&#x20;****`main`****&#x20;有错，而是规范应避免让实施者据此机械地改掉所有&#x20;****`main`****。**

建议补充：

> 供业务代码直接导入调用的公共函数，名称应说明动作和对象。命令行入口可保留 `main`，其具体用途由命令名和入口模块说明；不要求仅为满足本条而改名。

这是本次审查提出的范围澄清，不应倒填为用户在 Q8 中已经说过的话。

## 3. 需要补清：类和常量的命名责任还没有真正说明

第 7 节要求规则能够分别说明“包、模块、函数／类／常量”的命名责任。但正文对函数规定得比较明确，对类和常量基本只写了“表示成员”，还不足以指导实际命名。&#x20;

不必扩展成完整的代码风格手册，补两句话就够：

> 类名说明其表示的对象、结果、配置或职责角色，不套用动作型模块的命名规则。常量名说明固定值的业务含义，必要时体现单位或适用范围。

这样才完整回答了这份文档最初提出的问题：**每一层到底负责表达什么。**

## 4. 需要修改：迁移要求没有区分“现行引用”和“历史记录”

正文要求同步更新导入、入口、脚本、测试和相关文档；但附录又明确要求保留历史问答、更正及其原文。文档还指出历史基准脚本存在固定相对路径和源码哈希校验。这里需要明确哪些引用应更新、哪些记录应保留。 &#x20;

**“删除旧模块”不等于“整个仓库不能再出现旧模块名”。**历史问答中出现旧路径，和运行代码仍依赖旧路径，是两件不同的事。

建议将迁移验收要求写得更具体：

> 每批迁移更新现行代码、命令入口、脚本、测试及现行使用文档中的引用，并删除旧模块。历史问答和基准证据保留原文，必要时另加迁移说明。涉及固定路径或源码哈希的检查，应区分现行验证与历史证据，不为适配新目录而直接覆盖历史记录。

然后把目前笼统的“验证新导入和入口可用”补成三项可检查结果：

- &#x20;该批新模块路径可导入，现行代码不存在仍依赖已删除模块的引用。&#x20;
- &#x20;该批涉及的命令入口、启动目标及相关测试通过。&#x20;
- &#x20;当前使用文档与代码一致；历史记录保留，不能用“全仓库旧字符串零命中”作为验收标准。&#x20;

这只是把已有迁移要求说清楚，**不需要增加兼容转发文件或假设外部调用方**。你在 Q10 中对此已经说得很明确。

## 5. 建议修改：代码事实的引用方式不便于后续核对

第 1、5、6 节多处使用 `/data/workspace/Multimodal-GEO-roadnet/...` 绝对路径并附行号。它们依赖当前工作区位置；而本次设计恰好涉及文件迁移，之后文件名和行号也可能变化。日期只能说明记录时间，不能唯一确定对应的代码版本。&#x20;

建议区分两种链接用途：

**指向当前文件的导航链接**使用仓库相对路径；**用于支撑历史代码事实的引用**记录对应提交版本。文档开头统一记录一次核对版本即可，不必每段重复。

另外，“覆盖现存 129 个源码文件”“某目录只有 `_init_.py`”等，应继续明确为特定代码版本下的事实，不要写成长期有效的项目属性。

---

**不建议推翻现有设计，也不需要重新做 Q1–Q10 访谈。**完整导入路径共同表达语义、按内聚职责组织模块、按实际边界设包、分批更新引用后删除旧模块，这些原则在文档中是连贯的。

优先修正 **Q9 范围扩大**，再补清**函数入口例外、类与常量责任、迁移验收范围**。附录虽然较长，但承担历史记录职责，不应因为精简正文而删除或改写。

本次结论针对这份文档的规则一致性与可执行性；没有独立复核其引用的全部 129 个源码文件，也没有运行导入或测试。
```

当前解释：Q9 的词序限定于需要表达目标格式的转换，与 Q6 对校验、编排等操作的要求一致；Q8 对直接导入的业务函数适用，`main` 是本次新增的命令入口范围澄清；类与常量各自承担成员层语义。迁移验收只检查现行依赖和使用文档，历史问答与基准证据保留原文。第 1、5、6 节的代码事实限定于文首核对版本，相对链接只作当前文件导航。

正文同步：第 1、3、4、5、6、7、8 节及文首代码事实核对版本。完整 Spec 仍待最终确认；未执行源码迁移。

### 首批实施授权与验收记录（2026-09-25）

关联内容：Q1–Q10 命名规则、Q9 后续审查更正，以及第 8 节分批实施计划。

用户实施授权原文：

> 本轮 Q89 工作已完成并验收。请按你在这个会话中已经确定的计划继续实施，先核对当前仓库状态与计划前提，再完成必要改动和验证。

解释范围：用户确认前一轮 Q89 工作已完成并验收，并授权按既定计划先核对仓库状态、实施必要改动和验证。该授权不是对本 Spec 全文的确认；也不构成对本条记录所述首批迁移的后续验收。

本轮实施记录：起始 HEAD 为 `f3e214f`。工作区已有 Q89 相关未提交修改及基线后新增的 `source_row_key_migration.py`；首批选择未与这些修改重叠的栅格计数职责，将 `track.v2.raster.aggregation` 移至 `track.v2.raster.aggregate_point_counts`，并更新 `runner.py` 与栅格测试导入，删除旧模块，没有保留转发模块。命名建议文档的规则、层级、候选映射和基线说明已据 Q1–Q10 及后续更正修订。历史问答与原审查事实保留；建议清单限定于 `f3e214f` 的 129 个源码文件，当前工作区 130 个文件中的新增模块标明未纳入原审查。

验证记录：Python 3.12 解释器运行 `-m py_compile` 检查新模块、调用方和测试文件通过；以系统 Python 3.10 和 `PYTHONPATH=src` 运行 `tests/v2/test_raster_mvp.py` 中 `grid_boundaries`、`tiling_mask`、`unsupported_configuration` 三项通过（3 passed）；新导入路径及点计数行为的直接检查通过，旧模块路径未解析到现行源码。整份测试文件在系统 Python 3.10 下另行尝试，8 项因环境依赖/版本不匹配失败（NumPy 不支持 `method=`、缺少 `rasterio`/`rich`，以及 `StrEnum` 需要 Python 3.11+）；Docker daemon 拒绝访问，因此无法用项目容器复测。离线准备 Python 3.12 环境时依赖缓存没有 `duckdb`，未完成环境创建。

当前状态：本批改动和可用验证已完成，尚未收到对本批的验收确认；Spec 完整设计仍为“待确认”。后续模块继续依第 7 节逐批迁移和验证。

本轮检查：授权原文完整保留；实施范围、当前工作区基线、未覆盖的新文件、验证结果和限制均已记录；没有将前轮 Q89 验收写成本批验收或完整 Spec 确认；QA 附录仍位于文末。

### 首批栅格模块容器复验补充（2026-09-25）

关联记录：上方“首批实施授权与验收记录”；此项补充发生在该批实施记录之后，不覆盖其系统环境下的验证历史。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_raster_mvp.py`。

结果：11 passed，8 warnings。按项目约定，测试配置为 `pipeline-roadnet-test.toml`。此前系统 Python 3.10 下的失败仅保留为环境说明，不作为该批最终结论。

### 第二批栅格模块实施与验证（2026-09-25）

实施范围：将 `normalization.py`、`point_source.py`、`runner.py`、`tiling.py` 分别迁移到 `render_counts_to_uint8.py`、`read_refine_points.py`、`run_rasterization.py`、`write_rgb_tiles.py`；更新栅格 runner 内部导入、`cli/rasterize.py` 和 `test_raster_mvp.py`，删除四个旧模块，不留兼容转发。模块职责检查确认它们分别承担计数归一化/渲染、Refine 点读取、栅格作业编排及切片写出。`manual_thin_benchmark` 中的路径与源码哈希引用未指向这些栅格文件；相关历史证据未修改。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_raster_mvp.py`。

结果：11 passed，8 warnings。按项目约定，测试配置为 `pipeline-roadnet-test.toml`。源码与测试中不再导入旧模块名，CLI 仍使用原命令入口 `track.v2.cli.rasterize:main`。

当前状态：第二批实现与相关测试验证已完成，尚未收到该批单独的用户验收；完整 Spec 仍待确认。

本轮检查：已更新四项建议映射和实施状态；现行导入、CLI、测试已同步；相关历史基准证据未改；容器测试命令明确记录配置文件；QA 附录仍位于文末。

### 第三批验证回放模块实施与验证（2026-09-25）

实施前核对：工作区包含 Q89 未提交内容；用户已说明 Q89 完成并验收，并授权继续执行既定计划。根据职责，`validation/network_replay.py` 从有边界的持久化 Fitting 输入重放 Network，属于只读验证职责，且位于无冲突的现有 `validation` 包。本模块计算并检查同目录 `network.py` 的固定源码摘要；迁移只改变验证模块文件名及引用，未改动摘要目标源码，路径和摘要仍有效。

实施范围：将 `track.v2.validation.network_replay` 改为 `track.v2.validation.replay_network_from_fitting`；同步更新 `validation/semantic_diff.py`、`test_network_replay.py` 和 `test_semantic_diff.py` 的现行导入与测试替身引用，删除旧模块，不增加兼容转发。两个源码/测试文件含有 Q89 的其他未提交修改，本批只改动必要的导入和对应的测试对象名称，保留其余差异。建议文档同步记录该模块已迁移及同名阶段根模块的路径冲突/依赖顺序。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_network_replay.py tests/v2/test_semantic_diff.py`。

结果：44 passed，3 skipped。按项目约定，测试配置为 `pipeline-roadnet-test.toml`。源码及测试的现行导入不再引用 `track.v2.validation.network_replay`；测试保留 `network_replay` 字符串作为运行期 schema/错误标签，不代表旧模块依赖。`network.py` 的哈希锚点原文保留。

当前状态：本批实现与相关测试验证已完成，尚未收到本批单独验收；完整 Spec 仍待确认。

本轮检查：已更新建议映射和实施状态，限定 Q89 文件改动为必要导入引用，测试通过；历史哈希证据与 QA 原文保留；QA 附录仍位于文末。

### 第三批之后的仓库全量验证补充（2026-09-25）

主对话使用 `make test` 完成仓库全量验证。Compose 中 Pipeline 实际设置 `PIPELINE_CONFIG_PATH=/app/config/pipeline.toml`，即项目约定的全量测试配置。结果：infra Docker 路径测试 5 passed；Backend 83 passed；Pipeline 1,075 passed、67 skipped、8 warnings。该全量验证发生在第三批定向测试（44 passed、3 skipped）之后，作为当前整仓验证记录；不改变第三批单独验收状态，也不代表完整 Spec 已确认。

### 第四批验证比较模块实施与验证（2026-09-25）

实施范围：将 `track.v2.validation.semantic_diff` 改为 `track.v2.validation.compare_run_lineages`。职责是对已完成 Pipeline 血缘做有界、只读的语义比较，归属现有 `validation` 包；更新 `track/v2/cli/semantic_diff.py` 和 `tests/v2/test_semantic_diff.py` 的现行导入，删除旧模块，不留兼容转发。测试文件保留 Q89 中其他既有差异，本批只改动相关导入。命令名称 `semantic-diff`、CLI wrapper 路径及对外参数不变。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_semantic_diff.py`。

结果：35 passed，2 skipped。另在同一项目容器中确认 `track.v2.validation.compare_run_lineages` 可导入、旧路径 `track.v2.validation.semantic_diff` 的 `find_spec` 返回 `None`；`python -m pipeline semantic-diff --help` 成功。测试配置为 `pipeline-roadnet-test.toml`。源码、CLI wrapper 与测试中不再依赖已删除的 validation 旧模块；CLI wrapper 文件 `track.v2.cli.semantic_diff` 是仍在使用的独立命令模块。

边界说明：`track.v2.field_config` 尚未迁移。它还被 `manual_bench_point_objects.py` 导入；该脚本由性能复现文档引用，本批不修改这个独立基准入口或历史计时依据，后续单独核对再决定是否迁移。

当前状态：本批实现、定向测试、导入和 CLI help 验证已完成，尚未收到本批单独验收；完整 Spec 仍待确认。后续全仓 `make test` 结果见下方补充记录。

### 手工调试文件移除与 Spec 状态核对（2026-09-25）

实施范围：删除 `services/pipeline/src/track/v2/manual_thin_benchmark/` 下 31 个受 Git 跟踪文件，整目录不再存在；在 `.gitignore` 添加递归 `**/manual_*` 规则。删除仅依赖这些调试工具的 4 个测试文件及 `test_detect.py` 中 2 个手工门禁测试；将 same-CRS 生产候选对比测试改为直接调用 Detect 生产函数，并移除静态配置边界测试对已删除目录的排除。更新现行投影、Refine 与 Schema v3 操作文档，不再提供相关命令；历史 Spec、QA、基准证据的引用和结果保留。源码和测试不再导入该目录。当前源码根共有 99 个 Python 文件：98 个基线非手工文件与 1 个基线后新增的 `source_row_key_migration.py`。

验证：定向容器命令 `docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_detect.py tests/v2/test_cross_track_same_crs_benchmark.py tests/test_config_boundaries.py`，结果 105 passed、1 skipped。`make test` 结果：infra Docker 路径 5 passed；Backend 83 passed；Pipeline 1,047 passed、67 skipped、8 warnings。Pipeline 使用项目全量默认配置 `pipeline.toml`。`.gitignore` 样例检查覆盖根目录和多级目录，`git diff --check` 通过。

Spec 完成度核对：命名设计方向 Q1–Q10 已收敛，但全文仍“待确认”。移除 31 个手工工具后，基线 129 项中剩余 98 项非手工映射，其中 7 项已迁移、28 项评估后保留；55 项迁移候选、6 项拆分候选、1 项待核实移除和 1 项 `detect.py` 职责拆分，共 63 项仍需实施或核实。逐文件映射是候选，不要求一次性全部迁移。新增 `source_row_key_migration.py` 未纳入原逐文件审查。因此本次清理不表示 Spec 中其余候选已全部完成。

第四批记录的 `field_config.py` 暂缓原因适用于当时的工作区：它被 `manual_bench_point_objects.py` 导入。该脚本现已按本次决定删除，所以这一引用不再是当前依赖；`field_config.py` 自身迁移仍未实施，留作后续独立批次核对。历史第四批文字保留，不将原状改写为当前事实。

用户原文按收到顺序追加：

#### 新决策 — 临时 `manual_*` 文件（2026-09-25）

> 核对任务/data/workspace/Multimodal-GEO-roadnet/docs/python-module-naming-spec.md是否全部完成
> manual\_\*全部删除，以后不再进行git跟踪，只作为临时调试文件

当前解释：basename 以 `manual_` 开头的文件和目录作为临时调试内容，不纳入 Git；本次从主工作树删除受 Git 跟踪的 `manual_thin_benchmark/` 目录并加入递归忽略规则；独立旧工作树副本是否清理由后续 Q11 单独决定。核对结果也表明其余命名候选尚未全部完成，完整 Spec 仍待确认。

#### 后续验收与提交安排（2026-09-25）

> 验收成功后，让subagent分块commit；如果验收发现还有未完成的，则继续让subagent按照spec完成工作，如果有和spec偏差的地方，让subagent返修直至验收通过

当前解释：主对话验收成功后再按逻辑批次分块提交；验收发现未完成项或偏差时继续实施或返修。当前工作尚在验收阶段，尚未提交。

#### 后续协作边界（2026-09-25）

> 如果有spec以外的内容，拿不准需要我介入的，直接提醒我@grill me

当前解释：该提醒适用于未来遇到 Spec 之外且确需用户判断的事项；它不是本轮启动 grilling 访谈的指令。

本轮检查：31 个基线手工文件已删除并有忽略规则；现行源码/测试和操作文档已清理相关依赖；历史 QA/Spec/基准引用未为消除旧字符串而改写；三条用户原文保留并按收到顺序记录；Spec 未误标为全部完成；QA 附录仍位于文末。

### 第五批字段映射模块实施与验证（2026-09-25）

实施前核对：`track.v2.config` 是已有包，含 `__init__.py`；`pipeline_config_loader.py` 当前从 `track.v2` 层的 `field_config.py` 加载字段规格。新位置 `track.v2.config.field_mapping` 与现有包结构无路径冲突。字段映射模块只依赖 `track.v2.errors` 与 `track.v2.models`；迁移后保留对父包错误类型和模型类型的相对引用。项目配置包初始化期间会导入配置 loader，loader 再导入字段映射模块；本批容器测试验证这条实际导入链可用。

实施范围：将 `track.v2.field_config` 移至 `track.v2.config.field_mapping`，更新 `parser.py`、`config/pipeline_config_loader.py`、`test_parser.py` 和 `test_public_api.py` 的现行导入，删除旧模块，不留兼容转发。字段配置接口和业务参数不变；现行使用文档没有旧模块路径引用，命名建议表同步标记为已迁移。此前第四批与手工清理记录中“尚未迁移”的说明反映其记录当时的状态；本批完成后不再是当前状态。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_parser.py tests/v2/test_public_api.py tests/v2/test_pipeline_config.py tests/v2/test_pipeline_config_wiring.py tests/test_config.py`。

结果：58 passed。另在同一项目容器确认 `track.v2.config.field_mapping` 可导入，旧路径 `track.v2.field_config` 的 `find_spec` 返回 `None`。首次定向运行发现文件搬入配置包后仍使用原层级的相对导入；已改为父包 `errors`/`models` 导入后复测通过。另运行 `docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/test_config_boundaries.py`，结果 6 passed。源码、测试和现行使用文档中不再引用旧模块路径；Spec 与基线表保留历史路径以记录审查来源。

基线后新增模块审查：`track.v2.db.source_row_key_migration` 在 `db` 领域内执行一个 schema v7 到 v8 的 source row key 迁移，包路径和对象可辨认；它是单一操作模块，按 Q2 应使用动作型名称。Q9 仅适用于需要表达目标格式的转换；数据库 schema 迁移不属于此类，故不添加 `to_bigint`。当时建议后续单独迁移为 `track.v2.db.migrate_source_row_key`；该文件及其 `cli/init_db.py`、迁移测试调用与 Q89 当前差异交叉，因此该批仅记录结论，未改动这些文件，留待独立批次处理。

当前状态：基线 98 个非手工文件中，8 项已迁移、28 项评估后保留；54 项迁移候选、6 项拆分候选、1 项待核实移除和 1 项 `detect.py` 职责拆分，共 62 项仍待实施或核实。基线后新增模块已经过命名评估但其独立改名尚未实施；候选仍按职责和依赖分批处理，完整 Spec 尚待整体验收。

### 第六批基线后新增模块改名与验证（2026-09-25）

实施范围：依据单一操作模块适用的 Q2，将 `track.v2.db.source_row_key_migration` 改名为 `track.v2.db.migrate_source_row_key`。Q9 只约束需要表达目标格式的转换；数据库 schema 迁移不属于这类转换，故命名中不加 `to_bigint`。更新 `cli/init_db.py` 两处延迟导入及 `test_source_row_key_migration.py` 的模块导入，删除旧模块路径，不保留兼容转发。函数名、schema 版本、迁移行为不变；CLI 源码与测试中已有 Q89 差异，本批只更改导入路径，保留其他内容。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_source_row_key_migration.py tests/v2/test_init_db_schema.py`。

结果：18 passed、8 skipped；8 项 PostgreSQL 迁移集成测试因未设置 `ROADNET_TEST_DSN` 跳过，`test_init_db_schema.py` 的 18 项测试通过。容器内新模块导入成功，旧路径 `track.v2.db.source_row_key_migration` 的 `find_spec` 为 `None`；`python -m pipeline init-db --help` 成功。`git diff --check` 通过。现行源码和测试中无旧模块导入；建议文档实施摘要与本 Spec 当前状态均记录改名。基线后新增模块的命名评估已按 Q2/Q9 正确归因；本模块改名独立完成，未与第五批字段映射迁移混批。

当前状态：该新增模块改名已完成，不计入基线逐文件候选数。基线 98 个非手工项中，8 项已迁移、28 项保留、62 项仍需实施或核实；完整 Spec 仍待整体验收。本轮未提交。

### 第七批 Backend API 与回放模块迁移（2026-09-25）

实施前核对：`map_schemas.py` 定义 bbox、Replay/Clip 请求和 GeoJSON 响应契约，归入新建的 `backend.api.map_contracts`；`intersection_replay.py` 和 `replay_segments.py` 都是回放特征选择/构造职责，归入新建的 `backend.replay.select_intersection_members` 与 `backend.replay.build_segment_features`。两个 Replay 模块从 `map_api` 导入错误类型和 GeoJSON helper；`map_api` 通过方法内局部导入调用它们，保留此方向可避免模块顶层反向导入环。`database_catalog.py` 同时定义目录 API router、数据库/表路由及仅由该路由使用的 `table_counts` helper，主体是只读目录 API。按 Q5 保持该内聚边界，本批把原建议 `backend.stores.database_catalog` 修订为 `backend.api.database_catalog`，不拆出单一调用的存储 helper，也不为未来 `map_api.py` 拆分提前创建 `stores` 包。

实施范围：将四个基线模块迁入 `api` 和 `replay` 包并删除旧文件；更新 `map_api.py` 的契约导入及两个 Replay 局部导入、`main.py` 的数据库目录 router 导入、相应测试导入/patch 路径，以及现行使用文档 `pipeline-streaming-implementation.md` 的契约路径。将回放线段构造函数改为动作型 `build_segment_features`。未实施 `map_api.py` 的后续拆分。`map_api.py` 与 Backend 测试含有 Q89 现存修改，本批只修改必要导入与线段构造函数引用，保留其余差异。

容器测试：仓库 Compose 原文件解析时因 `services.pipeline.environment` 中已有的 `${POSTGRES_PASSWORD:asdf}` 插值格式失败，本批未修改该文件。测试改用 `/tmp/roadnet-compose.dev.fixed.yml` 临时副本，仅将该默认值改为合法的 `${POSTGRES_PASSWORD:-asdf}`，并设置项目目录为 `infra/docker` 以保留原挂载路径。命令：`docker compose --project-directory /data/workspace/Multimodal-GEO-roadnet/infra/docker --env-file /data/workspace/Multimodal-GEO-roadnet/.env -f /tmp/roadnet-compose.dev.fixed.yml run --rm --no-deps backend pytest tests/test_database_catalog.py tests/test_intersection_replay.py tests/test_replay_segments.py tests/test_map_api.py tests/test_map_store.py tests/test_map_database_selection.py tests/test_health.py`。结果：64 passed。临时副本不在仓库内，仓库 Compose 文件保持未修改。

验证与引用检查：覆盖 `api.database_catalog`、契约、回放、地图接口及 `main.py` 路由注册的七份 Backend 测试通过。代码导入图及测试执行确认 Replay 模块与 `map_api` 的延迟调用可用。现行源码、测试和使用文档不再引用四个旧模块路径；命名建议表按 `f3e214f` 基线保留旧文件名并标记已迁移，历史文档中原文引用保留。`git diff --check` 通过。

当前状态：本批迁移四个基线项。98 个基线非手工文件中，12 项已迁移、28 项评估后保留；50 项迁移候选、6 项拆分候选、1 项待核实移除及 1 项 `detect.py` 职责拆分，共 58 项仍待实施或核实。`backend.map_api.py` 的职责拆分仍是后续候选，本批没有处理；完整 Spec 尚待整体验收。本轮未提交。


### 第 5 轮 — 临时手工文件范围澄清（2026-09-25）

发问时前提：主工作树中的受跟踪 `manual_*` 文件已删除，递归忽略规则已生效；另发现被忽略的 `.worktrees/run-level-projection-crs` 独立旧工作树副本仍有 `manual_*` 源码。

#### Q11 — 是否清理独立旧工作树副本

> @grill me：主工作树的 manual_* 已删除；忽略的 .worktrees/run-level-projection-crs 旧副本仍有 manual_* 源码。是否也清理这个独立副本？
>
> 选项 A：保留独立副本（推荐）
>
> 选项 B：连旧副本一起清理

> 发问时推荐：保留独立副本（推荐）。
>
> 用户回答：保留独立副本（推荐）

正文关联：第 2、6、7 节。
当前解释：本项目命名/手工清理范围是主工作树中被跟踪的内容；按用户回答保留被忽略的 `.worktrees/run-level-projection-crs` 旧副本，不修改该独立工作树。此选择不撤销主工作树的删除，也不改变递归忽略规则。

### 第八批共享 artifacts 模块迁移与验证（2026-09-25）

实施前核对：三个模块分别承担 Parquet 清单构建与校验、Detect/Refine 共用的安全暂存发布，以及对两阶段产物的核对计划与清理。源码显示清单和发布设施由 Detect 与 Refine 两条产物流共同使用，Reconcile CLI 同时扫描两阶段目录；符合 Q1/Q4 的跨阶段共享设施边界。Git 基线中三个旧模块均有已跟踪对象，但未发现依赖其固定文件路径或源码摘要的当前基准清单；历史基线表保留旧路径以标记审查来源。

实施范围：将模块迁入 `track.v2.artifacts.parquet_manifest`、`track.v2.artifacts.publish_dataset` 和 `track.v2.artifacts.reconcile_datasets`，新增 `artifacts/__init__.py`；更新 Detect/Refine 阶段、Parquet 读写器、CLI、相关测试的导入，删除三个旧模块，不留转发。测试中的本地别名同步采用新模块名。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_artifact_parquet.py tests/v2/test_artifact_publication.py tests/v2/test_artifact_reconcile.py tests/v2/test_artifact_reconcile_apply.py tests/v2/test_artifact_reconcile_postgres.py tests/v2/test_artifacts_cli.py tests/v2/test_detect_parquet_export.py tests/v2/test_detect_parquet_export_postgres.py tests/v2/test_detect_parquet_reader.py tests/v2/test_refine_parquet.py tests/v2/test_refine_parquet_reader.py tests/v2/test_refine_parquet_postgres.py`。

结果：134 passed、8 skipped。新模块路径在项目容器可导入；旧模块路径 `track.v2.artifact_parquet`、`track.v2.artifact_publication`、`track.v2.artifact_reconcile` 的 `find_spec` 均为 `None`。`python -m pipeline artifacts --help` 成功。8 项跳过来自 PostgreSQL 集成测试所需环境条件。现行源码、测试无旧模块导入；历史建议基线表和 Spec 的历史记录保留原路径。测试使用 `pipeline-roadnet-test.toml`。

当前状态：本批迁移 3 个基线项。98 个基线非手工项中，15 项已迁移、28 项评估后保留；47 项迁移候选、6 项拆分候选、1 项待核实移除及 1 项 `detect.py` 职责拆分，共 55 项仍待实施或核实。当前源码根有 102 个 `.py` 文件，包括 98 个基线非手工项、1 个基线后新增 DB 模块、以及 Backend `api`、Backend `replay`、Pipeline `artifacts` 三个包初始化文件。候选映射不要求一次性全部迁移，完整 Spec 尚待整体验收；本轮未提交。

本轮检查：已按 Q11 更新 manual 清理的适用范围；保留原始问答与既有 QA；三个旧导入路径在当前源码/测试中已消失；命名建议表标记本批已迁移；源码文件总数和剩余候选数与当前状态一致；QA 附录仍位于文末。

### 第 6 轮 — CV 配置修复与命名迁移提交边界（2026-09-26）

发问时前提：HEAD 中 `scripts/cv_human_check/export_intersections.py` 导入不存在的 `default_postgres_config`；工作树中的 Q89 修改已改用 `load_startup_config` 修复该配置入口。临时 migration-only 树的 Pipeline 测试结果为 1,012 passed、59 skipped；CV 测试收集失败。当前问题只待用户选择如何安排提交边界，不据此预先认定配置修复归属或提交顺序。

#### Q12 — CV 配置修复的提交安排

> @grill me：CV 脚本原有配置修复属于 Q89；若它继续保持未提交，Pipeline 迁移提交中的脚本会因旧配置导入而无法运行。你希望怎样安排提交？
>
> 选项 A：先提交 CV 的 Q89 配置修复，再提交 Pipeline 命名迁移（推荐）
>
> 选项 B：把 CV 配置修复并入 Pipeline 命名提交
>
> 选项 C：保持 Q89 未提交，接受迁移提交中的 CV 脚本暂时不可运行

> 发问时推荐：先提交 CV 的 Q89 配置修复，再提交 Pipeline 命名迁移（推荐）。
>
> 用户回答：单独提交Q89，再提交命名迁移。

正文关联：实施与验证计划、提交边界。
回答来源（独立单题回答原文）：“单独提交Q89，再提交命名迁移”。
当前解释：先单独提交 Q89，再提交命名迁移；该回答涵盖 Q89 整体，不限于 CV 配置修复。该提交顺序不改变现有源码、临时 index、HEAD 或 Spec 的全文确认状态。


### 第五批导入环返修与第九批 telemetry 迁移验证（2026-09-25）

实施后续检查发现：新增 `track.v2.config.field_mapping` 被 `parser.py` 顶层导入时，`test_detect_writer_streaming.py` 的导入链会经过 `db_writer`、`detect`、`parser`、`config.__init__` 和 `pipeline_config_loader`，在 `detect` 尚未完成初始化时再次读取 `IntersectConfig`，形成循环导入。返修将 `build_field_mapping` 导入延迟到 Parser 的 CSV/Postgres 构造路径；不回退第五批模块路径，也不改变字段映射逻辑。

返修验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_heartbeat.py tests/v2/test_logger.py tests/v2/test_resource_budget.py tests/v2/test_detect_writer_streaming.py tests/v2/test_completion_postgres.py tests/v2/test_clip_fitting_streaming.py tests/v2/test_lazy_imports.py tests/v2/test_parser.py tests/test_config.py tests/test_config_boundaries.py tests/v2/test_public_api.py`；结果 136 passed、12 skipped。另确认 telemetry 三个新路径可导入、旧路径不存在。该返修记录是第五批后的当前导入图验证，不改写第五批原测试结果。

第九批迁移基线 `heartbeat.py`、`logger.py`、`resource_budget.py` 到 `track.v2.telemetry.heartbeat`、`track.v2.telemetry.console_logging`、`track.v2.telemetry.snapshot_run_environment`，新建 `telemetry/__init__.py`，删除旧模块并同步现行导入。三个职责分别是进度心跳、日志输出和运行环境快照，形成 Pipeline telemetry 领域包；推荐表标记已迁移。主工作区源码根当前共 103 个 `.py` 文件；98 个基线非手工项中，18 项已迁移、28 项保留，52 项仍需实施或核实。

本轮检查：QA 记录第五批导入环返修及验证；推荐表包含三个 telemetry 迁移状态；当前源码数量与新包初始化文件一致；旧 QA 和历史基线事实未改写。

### 第十批 CLI 入口、阶段共享契约和支持模块迁移（2026-09-26）

实施范围：五个 CLI 模块分别迁移为 `track.v2.cli._load_config`、`track.v2.cli.report_pipeline_events`、`track.v2.cli.replay_run`、`track.v2.cli.run_pipeline` 和 `track.v2.cli.compare_run_lineages`；命令名和 `pyproject.toml` 中公开入口保持不变。同步更新 `pipeline/__main__.py`、CLI 消费者、测试及 `cli/artifacts.py` 中的配置加载器导入。后者因共享同一命令包，在本批同步改为 `_load_config`。

CLI 定向验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/test_config.py tests/test_config_boundaries.py tests/v2/test_run_cli.py tests/v2/test_clip_cli.py tests/v2/test_pipeline_api.py tests/v2/test_pipeline_events.py tests/v2/test_replay.py tests/v2/test_semantic_diff.py tests/v2/test_delete_run.py tests/v2/test_raster_mvp.py tests/v2/test_init_db_schema.py`；结果 111 passed、2 skipped、8 warnings。`pipeline run`、`replay`、`semantic-diff` 的 help 以及 `roadnet-pipeline-init-db --help` 通过，五个新模块可导入，旧五个路径不存在。随后按当前工作树复跑 `python -m pipeline artifacts --help` 和 `tests/v2/test_artifacts_cli.py`，分别成功和 9 passed。

阶段共享模块核对后，将 `stage_support.py`、`stage_types.py` 成组迁入 `track.v2.stages.support`、`track.v2.stages.types` 并创建 `stages/__init__.py`。前者提供多个阶段运行器共同调用的事件/计时函数，后者定义多个运行器交接的阶段引用与结果；不依赖某个单阶段。更新各阶段运行器、Pipeline 编排器、事件测试与生命周期测试导入。同期候选模块搬移的短暂中间状态导致第一次联合收集无法导入 `candidate_patches`；候选模块负责人修正其消费者后重跑，未改动候选模块作为此批修复。最终定向命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_pipeline_events.py tests/v2/test_run_pipeline_lifecycle.py tests/v2/test_pipeline_api.py tests/v2/test_clip_fitting_streaming.py tests/v2/test_detect_writer_streaming.py`；结果 55 passed、1 skipped。两个新模块均可导入，`track.v2.stage_support` 与 `track.v2.stage_types` 的 `find_spec` 为 `None`。`docs/pipeline-stage-runner-refactor-plan.md` 当前结构和引用已同步到包路径。

关系模块 Q4/Q7 复核：`member_sequences.py` 的现行消费者全部属于 Detect 侧，应归入 `intersections` 数据域并等待该数据域的成组迁移；`clip_relation.py` 虽由 Clip 与 Fitting 两阶段共用，但当前没有与其同批且职责相合的 Clip/Fitting 数据域包。本轮将前者目标修订为 `track.v2.intersections.member_sequences`，后者暂保留旧路径并留到 Clip/Fitting 数据域边界批次；两个模块本轮均未移动，也不提前创建单模块 `relations` 包。

本批五个 CLI 与两个 stages 基线模块已迁移，源码/测试的现行导入已更新且无兼容转发。全仓其他候选仍分批实施，完整 Spec 尚未完成或验收；不提交。


### 第十一批 Detect candidates 领域模块迁移（2026-09-26）

实施范围：将 `candidate_patches.py`、`cross_track.py`、`turn_events.py`、`dbscan_storage.py`、`cross_candidate_relation.py`、`turn_event_relation.py` 迁入 `track.v2.candidates` 数据域包，路径分别为 `build_patches`、`detect_cross_track_pairs`、`detect_turn_events`、`dbscan_index`、`cross_track_relation`、`turn_event_relation`。同步更新算法、关系、调度、测试和现行使用说明的导入，删除六个旧模块，不保留兼容转发。两个关系模块根据实际消费者按 Q4 放入 Detect 候选数据域；原建议 `intersections.relations.*` 目标已修订为 `candidates.*`。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_candidate_patches.py tests/v2/test_cross_candidate_relation.py tests/v2/test_cross_candidate_relation_integration.py tests/v2/test_turn_event_relation.py tests/v2/test_turn_event_wiring.py tests/v2/test_turn_writer_streaming.py tests/v2/test_dbscan_compact.py tests/v2/test_cross_track_same_crs_benchmark.py tests/v2/test_grid_candidates.py tests/v2/test_refine_parquet_reader.py tests/v2/test_detect.py`。结果：323 passed、2 skipped。新六路径可用，旧六路径不存在；现行源码/测试中无旧模块导入。依赖这些模块的历史源码与镜像 SHA-256 基准 Spec 保留原文。测试使用项目冒烟配置 `pipeline-roadnet-test.toml`。

### 第十三批数据库版本迁移包实施与验证（2026-09-26）

实施前核对：四个迁移模块只覆盖 schema v3→v4 Refine 重命名、v4→v5 artifacts、v5→v6 Refine 完成约束、v6→v7 Detect 完成约束，均为单一版本迁移操作，且按版本顺序整体执行。放入既有 `db` 领域下的 `migrations` 子包可保留数据库职责上下文，并形成必要的四模块组；四个文件依次惰性读取 `cli.init_db` 中的 DDL，`init_db.py` 也在 schema 升级分支内惰性导入它们。本批仅在新包中将 `schema` 和 `cli.init_db` 的相对导入层级调整一级；保留版本号、事务边界和迁移行为。Git 基线对象已核对，工作树内未发现以这四个源码路径为键的固定摘要记录；历史迁移/验证文档原文不改。

实施范围：将四模块迁入 `track.v2.db.migrations.refine_v4`、`artifacts_v5`、`refine_completion_v6`、`detect_completion_v7`，新增 `migrations/__init__.py`；更新 `cli/init_db.py` 的七处延迟导入及四份迁移/注册测试导入，删除旧模块，不留转发。`detect_completion_migration.py` 与 `cli/init_db.py` 均含 Q89 既有未提交修改；本批仅更改迁移导入路径及新文件所需的相对导入，保留现有 schema v8、source row key 等其他差异。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_artifact_migration.py tests/v2/test_completion_migration.py tests/v2/test_detect_completion_migration.py tests/v2/test_refine_migration_postgres.py tests/v2/test_artifact_registry_postgres.py tests/v2/test_init_db_schema.py`。结果：38 passed、18 skipped；跳过项来自未设置 `ROADNET_TEST_DSN` 的 PostgreSQL 集成测试。容器中四个新模块均可导入，四个旧路径的 `find_spec` 均为 `None`；`python -m pipeline init-db --help` 成功。测试使用项目冒烟配置 `pipeline-roadnet-test.toml`。现行源码/测试无旧迁移模块导入。

本批迁移四个基线模块；Detect candidates 六模块批次也已完成。其他并行候选仍在核实/实施，完整 Spec 尚未完成整体验收；源码总数与全局剩余候选数待并行批次稳定后统一复核。本轮未提交。

### Pipeline 数据库查询职责拆分（2026-09-26）

按实际调用方与数据域拆分 `db.queries`：空间窗口/扫描迭代迁入 `candidates.query_source_windows`，Detect/Clip 结果查询迁入 `intersections.query_results`，Refine 点流和 Run 读取迁入 `trajectories.query_refine_points`，Replay 快照读取迁入 `validation.query_replay_inputs`。跨 Detect/Refine reader 共用的 `SpatialWindow` 值类型移入根模块 `projection.py`。这修订了原建议：不将所有查询集中放入一个新建的 `queries` 技术包，也不把 Refine 点读取误归为 Run 元数据。按 Q4/Q5，快照 reader 虽尚未由当前运行路径调用，仍保留在 validation 域以对应其 Replay 输入职责。

主验证命令：`docker compose -f infra/docker/compose.dev.yml run --rm -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_spatial_queries_projection.py tests/v2/test_spatial_workspace.py tests/v2/test_detect.py tests/v2/test_clip_cursor_plan.py tests/v2/test_clip_fitting_streaming.py tests/v2/test_refine_parquet_reader.py tests/v2/test_refine_parquet_postgres.py tests/v2/test_projection_query_postgis.py tests/v2/test_detect_parquet_export_postgres.py tests/v2/test_cross_track_same_crs_benchmark.py`；173 passed、10 skipped。补充消费者命令：`docker compose -f infra/docker/compose.dev.yml run --rm -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_pipeline_store.py tests/v2/test_clip.py tests/v2/test_replay.py tests/v2/test_raster_mvp.py`；43 passed、0 skipped、8 rasterio deprecation warnings。四个新模块可导入，旧 `track.v2.db.queries` 的 `find_spec` 为 `None`，当前 src/tests/scripts 无旧导入。测试使用项目冒烟配置 `pipeline-roadnet-test.toml`。

### Pipeline orchestration 模块迁入 orchestration 包（2026-09-26）

按 Q4/Q5 核实，`pipeline.py` 负责依序调用阶段运行器和汇总执行结果，`pipeline_events.py` 定义 Pipeline 核心发出的类型化事件；它们共同构成编排域且 CLI reporter 只依赖事件契约。分别迁入 `track.v2.orchestration.run_pipeline` 与 `track.v2.orchestration.events`，更新全部现行源码、测试、CLI 与当前架构文档引用，删除两个旧模块，不留转发。根层公开的 `PipelineInput`、`PipelineResult` 继续通过 lazy export 暴露并保持对象 identity；命令入口和命令名不变。

冒烟配置下定向命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_pipeline_api.py tests/v2/test_pipeline_events.py tests/v2/test_run_pipeline_lifecycle.py tests/v2/test_pipeline_config_wiring.py tests/v2/test_turn_event_wiring.py tests/v2/test_heartbeat.py tests/v2/test_run_cli.py tests/v2/test_lazy_imports.py tests/v2/test_public_api.py`；79 passed。`python -m pipeline run --help` 通过；容器内新路径均可导入、旧 `track.v2.pipeline` 和 `track.v2.pipeline_events` 的 `find_spec` 均为 `None`。首次测试发现 identity 表仍引用旧 `pipeline` 模块路径，更新预期后复跑通过。现行 src/tests 无旧模块导入。本批使用项目冒烟配置 `pipeline-roadnet-test.toml`。

### Parser 与 Refine 迁入 trajectories 数据域（2026-09-26）

按职责拆分 Parser：数据源连接/读取迁入 `track.v2.trajectories.read_source`，字段解析与逐车流式处理迁入 `track.v2.trajectories.parse_rows`；Refine 算法迁入 `track.v2.trajectories.refine_trajectories`。同步更新现行消费者、根层 lazy exports 和 identity 测试、CLI 与阶段运行器，删除 `track.v2.parser`、`track.v2.refine`，不留转发。保留 Pipeline root export identity。

联合验证命令：`docker compose --project-directory infra/docker --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests/v2/test_lazy_imports.py tests/v2/test_public_api.py tests/v2/test_parser.py tests/v2/test_refine.py tests/v2/test_refine_point_copy.py tests/v2/test_completion_postgres.py tests/v2/test_run_pipeline_lifecycle.py tests/v2/test_detect.py tests/v2/test_pipeline_config_wiring.py`；178 passed、12 skipped。三个新路径和 root lazy exports 可导入，旧两个根路径的 `find_spec` 为 `None`；`import_trajectories --help` 与 `git diff --check` 通过，现行 src/tests/scripts 无旧模块引用。PostgreSQL 环境条件的跳过数按测试结果记录，不作额外推断。使用项目冒烟配置 `pipeline-roadnet-test.toml`。

### Fitting 算法迁入 centerlines 数据域（2026-09-26）

`fitting.py` 的主要输出是连接交叉口对的道路中心线；`fit_centerlines` 与 `fit_centerline_variants` 是同一拟合算法及其变体，不需再拆成多个职责模块。按 Q4/Q5 将模块迁入 `track.v2.centerlines.fit_centerlines`，新增 `centerlines/__init__.py`，更新 Fitting 阶段运行器、Pipeline 根层 lazy exports、现行测试导入及 patch 路径，并删除旧根模块。根层公开函数仍保持对象 identity，Stage 模块名 `stages.fitting` 不变。

冒烟配置定向命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_fitting_network.py tests/v2/test_clip_fitting_streaming.py tests/v2/test_run_pipeline_lifecycle.py tests/v2/test_pipeline_config_wiring.py tests/v2/test_lazy_imports.py tests/v2/test_public_api.py`；52 passed。`python -m pipeline run --help` 成功；新模块可导入，旧 `track.v2.fitting` 的 `find_spec` 为 `None`，公开两个拟合函数与新模块对象 identity 一致。现行 src/tests/scripts 无旧模块导入，`git diff --check` 通过。测试使用项目冒烟配置 `pipeline-roadnet-test.toml`。

### 当前工作树命名实施终验（2026-09-26）

建议表复核结果：129 个基线项逐项对应；31 个 `manual_*` 基线文件已从主工作树删除，98 个非手工基线项中 67 个已迁移、拆分或核实移除，31 个有职责依据的条目保留，待实施或待核实项为 0。忽略的独立旧工作树副本按 Q11 保留。当前 `apps/backend/src` 有 17 个 `.py` 文件，`services/pipeline/src` 有 122 个，共 139 个；Pipeline 计数包含本轮拆分模块及包初始化文件。主工作树的源码/测试/脚本旧导入扫描无命中，Spec 和建议文档的相对 Markdown 链接无断链。

此前审计发现四个现行脚本仍导入已删除的 `cli._startup`，其中两个 CV 脚本还引用旧 `db.queries`。现已更新两个 Pipeline 工具脚本与两个 CV 脚本至 `cli._load_config`，CV 查询改用 `intersections.query_results`，保留 Q89 现有配置差异。冒烟配置验证命令：`docker compose --project-directory infra/docker --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest scripts/cv_human_check/tests/test_cv_preview.py scripts/cv_human_check/tests/test_export_intersections.py`；23 passed。四脚本的 py_compile/import 检查、当前脚本旧路径扫描与 `git diff --check` 通过。

仓库全量 `make test` 通过：Compose 路径测试 5 passed；Backend 83 passed；Pipeline 使用全量配置 `pipeline.toml`，结果为 1,047 passed、67 skipped、8 warnings。此为实施与验证完成记录，不代表用户已对完整 Spec 作明确全文确认；文档状态仍为“待确认”。本 QA 不改写此前各轮原始问答、历史验证计数或基准哈希证据。


### 第十一批 candidates 调度与网格模块迁移（2026-09-26）

实施范围：将 `detect_candidates.py` 迁移为 `track.v2.candidates.collect_candidates`；按职责将混合职责的 `grid_cross_track_candidates.py` 拆为 `track.v2.candidates.grid`、`grid_segments` 与 `detect_grid_crossings`。同步更新候选调度、Detect、空间工作区、Refine Parquet reader、网格测试及现行文档引用，删除原调度和混合网格模块，无兼容转发。保留 Q89 中对 `RefineDatasetReader` 的注解差异。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_candidate_patches.py tests/v2/test_cross_candidate_relation.py tests/v2/test_cross_candidate_relation_integration.py tests/v2/test_turn_event_relation.py tests/v2/test_turn_event_wiring.py tests/v2/test_turn_writer_streaming.py tests/v2/test_dbscan_compact.py tests/v2/test_cross_track_same_crs_benchmark.py tests/v2/test_grid_candidates.py tests/v2/test_grid_crossings.py tests/v2/test_grid_partition.py tests/v2/test_grid_segments.py tests/v2/test_grid_scheduler.py tests/v2/test_grid_detect_wiring.py tests/v2/test_refine_parquet_reader.py tests/v2/test_spatial_workspace.py tests/v2/test_detect.py`。结果：343 passed、2 skipped。容器中 `collect_candidates` 和三个网格新模块可导入，两个旧根路径的 `find_spec` 为 `None`；src/tests/scripts 的现行导入无旧路径。`docs/detect-candidate-patches.md` 的两个现行脚本路径已更新。`pipeline-validation-summary-20260915.md` 保留原文。测试使用项目冒烟配置 `pipeline-roadnet-test.toml`。

候选的六模块和本批两个基线映射均已完成；其他并行工作树仍有候选批次实施中。当前全局源码总数和剩余候选数待并行改动稳定后统一复核，完整 Spec 尚未整体验收；不提交。

### Backend 地图 API 职责拆分与验证（2026-09-26）

实施范围：复核 `backend.map_api` 后按实际职责拆成 `backend.api.map_routes`（路由入口）、`backend.stores.map_layers`（SQL、Run 目录及存储上下文）和 `backend.replay.build_clip_features`（Clip 投影）；并抽出由多个模块共享的 `backend.map_errors`、`backend.map_features` 与 `backend.replay.track_points`。更新 `main.py` 入口和所有现行消费者，删除旧 `map_api.py`，未改动有日期的历史设计文档中的旧路径。此结果将原先 1 个混合模块拆为 6 个职责模块，并新增 `stores` 包。

验证命令：`docker compose --project-directory /data/workspace/Multimodal-GEO-roadnet/infra/docker --env-file /data/workspace/Multimodal-GEO-roadnet/.env -f /data/workspace/Multimodal-GEO-roadnet/infra/docker/compose.dev.yml run --rm --no-deps backend pytest tests/test_map_api.py tests/test_map_database_selection.py tests/test_map_store.py tests/test_intersection_replay.py tests/test_replay_segments.py tests/test_run_catalog.py tests/test_streaming_api_fields.py tests/test_health.py`。结果：67 passed in 0.64s。现行源码、测试和入口中旧模块引用为零；历史 `pipeline-20m-memory-budget-spec.md` 与 `pipeline-streaming-implementation.md` 保留原文。Compose 使用仓库文件，未修改 Compose 配置。

### Intersections 结果与关系模块迁移及 Q8 函数命名复核（2026-09-26）

实施范围：Detect Parquet 导出器/读取器、写入来源关联和成员序列模块成组迁入 `track.v2.intersections.results`、`track.v2.intersections.relations` 与 `track.v2.intersections.member_sequences`。按 Q4 现行消费者归属，`member_sequences` 属于 Detect 的 intersections 数据域，不作为跨阶段共享关系模块。更新现行消费者并删除旧根模块；基线表保留旧路径作为审查来源。

Q8 复核补充：Spec 中曾以裸调用 `export_detect_parquet` 说明按职责命名的要求。该导出接口本身必须表达导出的数据域和目标格式，已命名为 `export_detect_results_to_parquet()`；新模块路径为 `track.v2.intersections.results.export_to_parquet`。此变更同步到调用方和相关测试，不改动历史 Spec/QA 原文。

改名后的组件验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_detect_parquet_export.py tests/v2/test_detect_parquet_export_postgres.py tests/v2/test_detect_parquet_reader.py`；结果 16 passed、2 skipped。该记录只覆盖导出/读取组件；包含 DB 网格点导入的 intersections 完整联合回归在下一次批次验证后补记。测试配置为项目冒烟配置 `pipeline-roadnet-test.toml`。

### DB 游标规划与网格点读取模块迁移（2026-09-26）

实施前核对：`db.cursors` 封装完整消费 server-side cursor 的扫描规划，可独立命名为 `plan_full_scan_cursor`；`db.grid_points` 读取 Detect 网格内的 Refine 点及子轨迹邻居，作为数据库操作边界命名为 `fetch_refine_points_in_grid`。两项保留在既有 `db` 包，不新建单文件包。同期检查 `db.schema` 除运行时 schema guard 外还导出多个服务和迁移共同依赖的 `SCHEMA_COMPONENT`、`SCHEMA_VERSION` 契约常量，因此保留 `track.v2.db.schema`；`schema_guard` 会缩窄该模块职责。保留 grid/schema 文件中 Q89 既有差异，仅同步本批相关导入。

实施范围：将 `track.v2.db.cursors` 迁移为 `track.v2.db.plan_full_scan_cursor`，将 `track.v2.db.grid_points` 迁移为 `track.v2.db.fetch_refine_points_in_grid`；更新 Pipeline 当前消费者、网格测试和候选调度导入，删除旧文件，不留兼容转发。旧路径仅在日期明确的历史验证记录或命名基线表中保留。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_full_scan_cursor.py tests/v2/test_clip_cursor_plan.py tests/v2/test_grid_points.py tests/v2/test_grid_scheduler.py tests/v2/test_grid_crossings.py tests/v2/test_grid_segments.py tests/v2/test_grid_detect_wiring.py tests/v2/test_spatial_workspace.py tests/v2/test_network_replay.py`。结果：30 passed、10 skipped；跳过项来自未设置 `ROADNET_TEST_DSN` 的 PostgreSQL 集成条件。容器中两个新模块可导入，旧路径 `track.v2.db.cursors` 和 `track.v2.db.grid_points` 的 `find_spec` 均为 `None`。测试使用项目冒烟配置 `pipeline-roadnet-test.toml`。当前源码/测试/脚本中没有旧模块导入；日期明确的历史验证文档保留旧路径原文。

本批修改没有触碰并行 replay 模块迁移。`db/__init__.py` 现行 SQL 分层说明中关于 replay 路径的注释将在该批稳定后按新职责更新；历史记录不改。

Intersections 联合复测补记（2026-09-26）：在 DB 网格点模块迁移完成后，使用上述冒烟配置复跑完整 intersections/Clip/候选回归集。命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_detect_parquet_export.py tests/v2/test_detect_parquet_export_postgres.py tests/v2/test_detect_parquet_reader.py tests/v2/test_clip.py tests/v2/test_clip_fitting_streaming.py tests/v2/test_member_sequences.py tests/v2/test_writer_relation.py tests/v2/test_detect_writer_streaming.py tests/v2/test_turn_writer_streaming.py tests/v2/test_cross_candidate_relation.py tests/v2/test_cross_candidate_relation_integration.py tests/v2/test_refine_parquet_reader.py tests/v2/test_detect.py tests/v2/test_lazy_imports.py tests/v2/test_grid_candidates.py tests/v2/test_grid_crossings.py tests/v2/test_grid_partition.py tests/v2/test_grid_segments.py tests/v2/test_grid_scheduler.py tests/v2/test_grid_detect_wiring.py tests/v2/test_grid_points.py tests/v2/test_pipeline_config_wiring.py tests/v2/test_run_pipeline_lifecycle.py tests/v2/test_run_cli.py`。结果：375 passed、6 skipped。验证覆盖 Q8 函数名、stage 调用和 patch 路径以及新 DB 导入；现行 src/tests/scripts/docs 中旧导出函数名及四个 intersections 旧模块均无引用。Q89 SQL 和 `db_writer` 的整数转换改动保持原样。该结果取代本节此前关于等待联合回归的状态说明。

### Pipeline replay 职责拆分与 CLI JSON 输出模块实施（2026-09-26）

`track.v2.replay` 同时包含持久 Run/中心线血缘回放，以及从派生 cluster/refine/subtrack/point 引用解析并重建内存轨迹点的功能，按 Q5 不适合整体迁入单一 validation 模块。现已拆为 `track.v2.validation.replay_lineage` 与 `track.v2.validation.resolve_point_references`，分别承载持久结果血缘服务和内存点引用解析。更新 Pipeline 根懒导出、CLI、Backend 调用和测试引用，删除旧 `track.v2.replay`。验证：Pipeline 冒烟配置下 `tests/v2/test_replay.py`、`tests/v2/test_lazy_imports.py` 共 13 passed；Backend `tests/test_intersection_replay.py`、`tests/test_streaming_api_fields.py`、`tests/test_health.py` 共 17 passed；`python -m pipeline replay --help` 通过。新路径和根懒导出 identity 检查通过，旧根路径不存在。`db/__init__.py` 现行分层说明已同步为新 replay 路径，并避免声称该服务完全不执行 SQL；历史文档路径保留原文。

`track.v2.output` 只包含 `print_json`/`error_payload` 两个 CLI JSON helper，由 rasterize 和 replay 命令使用，现迁入既有 `track.v2.cli.json_output`；`track.v2.downstream` 是无源码/测试/脚本消费者的 `NotImplementedError` 占位模块，已核实后删除。冒烟配置下 `tests/v2/test_raster_mvp.py` 和 `tests/v2/test_replay.py` 19 passed、8 warnings；rasterize/replay 的 CLI help 成功；新路径可导入，两个旧根路径均无法解析。当前文档和代码引用已更新；历史基线表保留旧文件名并标注实施状态。

### 根层模块 Q7 复核及 Detect 性能计时迁移（2026-09-26）

`delete_run.py` 是唯一 Run 删除服务，当前生产调用仅来自 CLI；没有适合共同迁入 `runs` 包的邻居。`projection.py` 被 Detect、Refine、Clip、Fitting、db_writer、refine_parquet 和 Pipeline 编排共同使用，根模块职责明确，单独建立 `spatial` 包没有相邻模块。两者经 Q7 复核标为保留。

`_perf.py` 只由 Detect 使用，作为单一性能计时职责迁入 `track.v2.intersections.performance_timing`，并更新 Detect、候选模块及相关测试的现行导入，删除旧文件。冒烟命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_detect.py tests/v2/test_candidate_patches.py tests/v2/test_cross_track_same_crs_benchmark.py`；结果 115 passed、1 skipped。新 `PerfTracker` 路径导入成功，旧 `_perf` 路径 `find_spec` 为 `None`，当前 src/tests/scripts/docs 中无旧模块引用。两份有哈希的历史基准证据未改。

### 五阶段运行器迁入 stages 包（2026-09-26）

实施前核对：五个模块分别提供 Refine、Detect、Clip、Fitting、Network 的阶段执行与资源边界；Pipeline 编排器按顺序调用这些入口，职责稳定且与已有 `stages.support`、`stages.types` 契约同属阶段执行层。因此成组迁入 `track.v2.stages.refine`、`detect`、`clip`、`fitting`、`network`，无需再建新包。同步更新 `pipeline.py`、依赖调用和测试替身，更新当前阶段运行器计划文档路径，删除五个旧文件，不留转发。保留 Detect 导出函数 Q8 改名及其他 Q89 既有内容。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_pipeline_events.py tests/v2/test_run_pipeline_lifecycle.py tests/v2/test_pipeline_config_wiring.py tests/v2/test_turn_event_wiring.py tests/v2/test_clip_fitting_streaming.py tests/v2/test_detect_writer_streaming.py tests/v2/test_pipeline_api.py`；结果 79 passed、1 skipped。项目容器中五个新路径均可导入，五个旧路径的 `find_spec` 均为 `None`；现行 src/tests 中旧模块导入为零。第一次验证发现生命周期测试里动态拼接的 patch 名仍指向旧路径，修正该测试替身后复跑通过。测试使用项目冒烟配置 `pipeline-roadnet-test.toml`。

### Clip 算法与 Clip/Fitting 输入关系迁入 clips 数据域（2026-09-26）

实施前按 Q4/Q5 复核：`clip.py` 消费已解析 Detect 顺序并生成道路片段，职责是 Clip 数据域算法入口；`clip_relation.py` 持有可重复读取的 DuckDB Clip 数据关系，作为 Clip 到 Fitting 之间的输入边界。两个模块构成内聚的 Clip 数据域批次，不采用单文件技术型 `relations` 包。将它们分别迁入 `track.v2.clips.extract_clips` 与 `track.v2.clips.clip_input`，新增 `clips/__init__.py`；同步 `fitting.py`、`db.pipeline_store`、Pipeline 根懒导出、测试和现行性能优化说明，删除旧模块路径。Pipeline 根懒导出继续保留公开函数对象 identity。历史 benchmark/baseline 文档中的固定旧路径保留。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_clip.py tests/v2/test_clip_relation.py tests/v2/test_clip_fitting_streaming.py tests/v2/test_detect_parquet_export.py tests/v2/test_detect_parquet_export_postgres.py tests/v2/test_fitting_network.py tests/v2/test_clip_cli.py tests/v2/test_lazy_imports.py tests/v2/test_public_api.py`。结果 77 passed、2 skipped；`test_lazy_imports.py` 验证现有 Pipeline 公共导出保持对象 identity。新路径可导入，旧 `track.v2.clip` 和 `track.v2.clip_relation` 不存在；当前源码和测试中无旧模块导入。首次运行先发现 `db.pipeline_store` 仍引用旧 `clip` 路径，更新该单行导入后复跑通过；第二次发现 lazy-export identity 测试按旧模块路径解析，测试模块键已同步至 `clips.extract_clips`，其后 Detect 拆分同时把同一身份表模块键改至 `intersections.detect_intersections`。按项目约定使用冒烟配置 `pipeline-roadnet-test.toml`。

命名建议中早先将 `clip_relation` 暂留的评估反映当时未有 Clip/Fitting 数据域批次的状态；本批形成该边界包后，两项映射已实施。冲突审查记录同步说明根 `clip.py` 已迁入 `clips`，现不再阻止后续同名阶段子包决策。

### Network 算法迁入 road_network 包与摘要校验更新（2026-09-26）

实施前核对：`network.py` 的单一职责是由拟合中心线构造交叉口节点和道路边；消费者为 Network 阶段运行器、公开延迟导出和受控 Network replay 校验模块。`replay_network_from_fitting.py` 对算法文件执行固定 SHA-256 fail-closed 校验；迁移前 `network.py` 摘要为 `4986ebfd9cc1f481f488b86f22c59384c363aded58b391f240f47ab6d220fbef`，与现行 expected 一致。迁移到 `track.v2.road_network.build_road_network` 后，只因模型模块相对层级改变而调整了一处 `models` 导入；算法逻辑不变，但文件字节摘要因此更新为 `7946329a8869879c1c931929829090bb7c8f68a559af03e4cf827338011a9b9f`。同步更新 Network 阶段、validation 哈希校验路径、Pipeline lazy export 和对象 identity 测试；删除旧根模块。历史 QA 中的原路径/摘要不改。

验证命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_fitting_network.py tests/v2/test_network_replay.py tests/v2/test_run_pipeline_lifecycle.py tests/v2/test_pipeline_config_wiring.py tests/v2/test_lazy_imports.py tests/v2/test_public_api.py`；结果 51 passed、1 skipped。受控 replay 哈希校验、新旧 lazy export identity 和阶段调用均通过；旧 `track.v2.network` 路径不再存在。首轮全套测试遇到并行 db_writer 移动的短暂消费者导入中间态，等待 `db.pipeline_store` 同步后复跑通过。测试使用项目冒烟配置 `pipeline-roadnet-test.toml`。

### CLI artifact reconciliation、Detect 参考实现和数据库 writer 拆分验证补记（2026-09-26）

`track.v2.cli.artifacts` 是显式、可审计的不可变 Parquet 产物核对命令，已迁入 `track.v2.cli.reconcile_artifacts`；实际调用由 `pipeline/__main__.py` 命令分发，未涉及 `pyproject.toml` 独立入口。冒烟配置下 artifact CLI 测试 15 passed，CLI help 成功；新路径可导入，旧模块路径不存在。`track.v2._cross_track_legacy` 是 Detect 候选对等价基准使用的参考实现，迁入 `track.v2.diagnostics.reference_cross_track_candidates`；Detect/candidate oracle 测试 115 passed、1 skipped，新模块导入通过，旧路径及 src/tests/scripts/docs 当前导入均不存在。历史基准原路径保留。

`db_writer.py` 按写入边界拆为五个模块：`db.write_refine_results`、`db.write_detect_results`、`db.write_fitting_results`、`db.insert_trajectory_points` 与 `db.insert_batches`。前三者按结果域区分，后两者提供通用轨迹导入与 INSERT/COPY 操作；五者都保留在数据库层，不按算法阶段建立技术包。更新 `pipeline_store.py` 及所有现行调用/测试路径，删除旧 writer，不留转发。Q89 的 `source_row_key` 整数转换差异保留，事务和提交边界未改。定向命令：`docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest -q tests/v2/test_db_writer.py tests/v2/test_fit_persistence.py tests/v2/test_writer_relation.py tests/v2/test_turn_writer_streaming.py tests/v2/test_detect_writer_streaming.py tests/v2/test_cross_candidate_relation_integration.py tests/v2/test_pipeline_store.py tests/v2/test_run_pipeline_lifecycle.py tests/v2/test_pipeline_config_wiring.py tests/v2/test_refine_migration_postgres.py tests/v2/test_source_row_key_migration.py`；结果 140 passed、14 skipped。当前源码/测试无旧 `db_writer` 导入。
