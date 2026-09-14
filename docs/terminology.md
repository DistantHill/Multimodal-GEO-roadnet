# 项目术语与写作规范

本文件是项目级术语的统一维护位置，供各设计说明和其他项目文档引用，不在每份
设计说明中复制术语表。新增或调整通用术语时更新本文件；各文档仅保留必要的
上下文定义，并在首次使用术语时写作“中文翻译（英文原名）”，后续可只用中文。
内存约束使用内存预算或内存上限；
验收使用验收标准；仅强调可执行验证规则时使用验证契约，不将其与验收标准混用。
不采用商务或口语式表述，不引入额外的内存约束术语。

以上为用户要求保存的项目级写作偏好，同样适用于技术说明，不代表跨项目或账号级记忆。
术语表仅维护相关词条，保留英文原名，不堆砌无关术语。设计确认、实施授权和验收确认
分别说明，不能相互替代。

正文禁用“合同”“签字”“硬合同”等替代词；`verification contract` 与
`acceptance criteria` 均不能译为“验收契约”。不将 `memory contract` 或
`acceptance contract` 当作通用标准表述。优先采用 `memory budget`、`memory limit`
和 `acceptance criteria`；确需保留 `memory contract` 时，仅写“内存契约（memory contract）”，
首次定义其具体含义，不能替代内存预算、内存上限或验收标准。

代码标识符、接口、命令、文件路径和历史问答引文不作翻译性改写。产品、语言和库的
专名保持拼写，用中文说明类别，不另造中文品牌名。历史审计仅规范用词，保留数值、
结论和适用版本；各设计说明中的历史问答与更正原文保留原貌，不作为正文用词示范。

## 核心术语

| 规范表述 | 使用范围 |
| --- | --- |
| 内存预算（memory budget） | 运行资源分配与规划 |
| 内存上限（memory limit） | 不可超出的内存边界 |
| 验收标准（acceptance criteria） | 判断交付是否达标的条件 |
| 验证契约（verification contract） | 可执行检查及其输入、断言与输出规则 |
| 强制约束（hard constraint） | 必须满足的条件 |
| 验收确认（acceptance sign-off） | 依据证据确认通过验收 |
| 新建 Pipeline 父控制组（fresh Pipeline parent cgroup） | 新建、独立计量的父级控制组 |
| 阶段子控制组（stage child cgroup） | 父级之下的阶段计量边界 |
| 问答（questions and answers，QA） | 历史问题、回答、更正与确认 |
| 规格说明（specification，Spec） | 项目设计文档；“设计说明”与“规格说明”指同类文档 |
| 常驻内存集大小（resident set size，RSS） | 进程驻留内存指标，不替代控制组峰值 |
| 内存不足（out of memory，OOM） | 资源不足；事件接口名称仍原样保留 |
| 命令行接口（command-line interface，CLI） | 命令行运行入口 |
| 应用程序编程接口（application programming interface，API） | 代码/服务调用接口 |
| 数据定义语言（data definition language，DDL） | 数据库结构定义 |
| 结构化查询语言（Structured Query Language，SQL） | 数据库查询语言 |
| 坐标参考系（coordinate reference system，CRS） | 算法空间坐标解释 |
| 服务级别协议（service-level agreement，SLA） | 服务目标约定；具体性能指标由对应设计说明定义并验证 |
| 中央处理器（central processing unit，CPU） | 可用核心与处理器资源 |
| 图形处理器（graphics processing unit，GPU） | 用于并行计算的加速设备，是否启用由具体方案确定 |
| 输入/输出（input/output，I/O） | 读写工作量与性能 |
| 操作系统（operating system，OS） | 进程和内存管理环境 |
| 用户界面（user interface，UI） | 项目的人机交互界面 |
| 超文本传输协议（Hypertext Transfer Protocol，HTTP） | 服务接口协议 |
| 标识符（identifier，ID） | 行/运行身份；具体字段 `id` 保留 |
| 扩展二进制几何表示（Extended Well-Known Binary，EWKB） | 几何序列化格式 |
| 带噪声的基于密度的空间聚类（Density-Based Spatial Clustering of Applications with Noise，DBSCAN） | 跨分区聚类语义 |
| 局部热点统计量（G*） | 本项目统计量名称 |
| 通用搜索树索引（Generalized Search Tree，GiST） | 数据库空间索引 |
| 超大属性存储技术（The Oversized-Attribute Storage Technique，TOAST） | 数据库大字段存储 |
| JavaScript 对象表示法（JavaScript Object Notation，JSON） | 数据交换格式 |
| 二进制 JSON 类型（jsonb，代码类型 `JSONB`） | 数据库配置存储类型 |
| 逗号分隔值（comma-separated values，CSV） | 表格文件格式 |
| 字节（byte，B；复数 bytes） | 数据大小单位 |
| 十进制千兆字节（gigabyte，GB）/兆字节（megabyte，MB） | 1 GB = 1,000,000,000 B；1 MB = 1,000,000 B |
| 二进制吉比字节（gibibyte，GiB）/米比字节（mebibyte，MiB） | 1 GiB = 2³⁰ B；1 MiB = 2²⁰ B |
| 百万（million，M）/千（thousand，k） | 行数等规模简记，不是内存单位 |
| 米（metre，m）/毫秒（millisecond，ms） | 几何误差和时间单位 |

## 项目名称、角色与接口标识

| 中文说明（原名） | 说明 |
| --- | --- |
| 数据处理流水线（Pipeline） | 项目数据处理运行单元 |
| 解析阶段（Parser） | 项目阶段名称 |
| 精炼阶段（Refine） | 原 Cleaner / Clean；当前代码、配置和数据库统一使用 Refine，历史问答保留原名 |
| 检测阶段（Detect） | 项目阶段名称 |
| 片段提取阶段（Clip） | 大写为阶段名，小写 clip 指片段 |
| 拟合阶段（Fitting） | 项目阶段名称 |
| 路网构建阶段（Network） | 项目阶段名称 |
| 后端服务（Backend） | 不在请求处理器中承载长时间数据处理任务 |
| 访谈技能（grilling） | 设计访谈及其记录流程 |
| 工作单元（worker） | 含义由上下文明确；[流式并行设计](pipeline-20m-memory-budget-spec.md)的 Q12 特指一对多调度者，数据库语境可指计算工作单元 |
| 计算进程（executor process；代码标识 `executor_process`） | 进程池中的实际计算者 |
| 数据库（PostgreSQL，简称 PG；通用简称 DB） | 专名保留 |
| 编程语言（Python、C++、Rust） | 专名保留 |
| Python 参考实现（CPython） | 专名保留 |
| 数值计算库（NumPy）、坐标转换库（pyproj）、数据库客户端库（libpq） | 专名保留 |
| 构建工具（setuptools、Makefile）、容器编排工具（Compose） | 原工具名与文件名保留 |
| 并行计算框架（Dask、Ray） | 可选计算框架，是否采用由具体方案确定 |
| 轻量标记语言（Markdown） | 文档格式名称保留 |
| 内存接口与事件标识 | `memory.max`、`memory.peak`、`memory.swap.max`、`memory.swap.current`、`memory.events`、`oom`、`oom_kill` 原样保留 |
| 点类型代码（p/i/c） | 已解析点序列中的类型代码，保持代码原名 |
| 任务目标行数（n）、计算进程数（W）、每进程内核线程数（T） | 自动调参的变量标识；W 不指调度工作单元数量 |
| 平面坐标（XY） | 坐标分量标识 |
| 其他代码、字段与路径 | `cpu.max`、`VmHWM`、`COPY`、`semantic_config`、`execution_config`、`DATA_ROOT` 及全部实际代码标识原样保留 |

## 通用术语索引

以下词条均以中文在前、英文原名在后列出；复数或大小写变体合并记录。

| 规范表述 | 规范表述 | 规范表述 |
| --- | --- | --- |
| 内存窗口回退路径（memory-window fallback） | 服务端命名游标（server-side named cursor） | 命名游标（named cursor） |
| 未命名游标（unnamed cursor） | 客户端游标（client cursor） | 数据库客户端结果缓冲区（DB client result buffer） |
| 数据库客户端缓冲区（DB client buffer） | 内存分配代理测量（allocation proxy） | 原生内存分配器（native allocator） |
| 原生缓冲区（native buffer） | 原生堆内存（native heap） | 原生临时工作区（native scratch） |
| 浅层大小（shallow size） | 单个列表槽位（one list slot） | 点数据工作区（point workspace） |
| 采样矩阵（sample matrix） | 定向坐标元组（oriented coordinate tuples） | 定向经纬度元组（oriented lon/lat tuples） |
| 坐标元组（coordinate tuples） | 连续静止点段（stationary run） | 静止点集合（stationary set） |
| 正交点注入（orthogonal injection） | 源数据引用（source reference） | 原始插入标识映射（raw insert ID map） |
| 行键（row key） | 行元组（row tuple） | 包装对象列表（wrapper list） |
| 簇包装对象（cluster wrapper） | 点对象图（point graph） | 内部点引用（interior point references） |
| 内部点元组（interior tuple） | 全局网格归属记录（global grid memberships） | 簇成员归属记录（cluster memberships / cluster membership） |
| 网格归属记录（grid memberships） | 候选穿越点（candidate crossing points / candidate crossings） | 候选簇（candidate clusters） |
| 候选事件（candidate events） | 跨轨迹候选（cross-track candidates / cross candidates / cross candidate） | 穿越点（crossing points） |
| 完整交叉口（complete intersections） | 完整进出观测（complete entry/exit observations） | 完整进出记录（complete entry/exit） |
| 转向事件（turn events） | 热点事件（hotspot events） | 保留点（kept points） |
| 注入点（injected points） | 有效点（active points） | 片段内点（points in clips / clip points） |
| 可用片段内点（eligible clip points） | 可用片段（eligible clips / eligible clip） | 边界片段（boundary clips / boundary clip） |
| 合并中心线（combined centerlines） | 合并边（combined edges / union edges / union edge） | 有向中心线（directed centerlines） |
| 中心线清单（centerline manifests） | 清单行（manifest rows） | 路网节点（network nodes） |
| 紧凑交叉口摘要（compact intersections） | 紧凑摘要（compact summary） | 全量物化输出（eager output） |
| 全量物化结果（eager result） | 全量参考实现（eager oracle） | 全量物化接口（eager API） |
| 零剔除（zero-exclusion） | 自复制（self-copy） | 回看上下文（look-behind） |
| 读取批量（fetch size） | 读取字节数（fetch bytes） | 批次读取字节数（fetch batch bytes） |
| 读取批次（fetch batch） | 源数据查询（source query） | 源数据行（source row） |
| 输出行（output rows） | 局部计数（local counts） | 核心点标记（core flags） |
| 活动集合（active sets） | 规范归属分块（canonical owner tile） | 规范归属者（canonical owner） |
| 归属记录数量保护阈值（membership guard） | 跨轨迹保护阈值（Cross-track guard） | 归属网格（home cell） |
| 半径邻域缓冲区（radius halo） | 边界点（border point） | 并查集（union-find） |
| 合并临时工作区（union scratch） | 车辆哈希分片（vehicle hash shard） | 车辆分片（vehicle shard） |
| 拓扑分片（topology shard） | 拓扑点数组（topology point arrays） | GiST 包围盒（GiST bbox） |
| 运行隔离的（run-scoped） | 按配对索引的磁盘暂存（pair-indexed spool） | 临时文件暂存（temp-file spool） |
| 临时文件（temp file / temp files） | 文件页缓存（page cache / page-cache） | 驻留页（resident pages） |
| 峰值驻留（peak resident） | 内存映射窗口（mmap window） | 高水位监测（high-water telemetry） |
| 高水位（high-water） | 硬性上限（hard limit） | 父任务（parent task） |
| 父任务块（parentpatch） | 表视图（table view） | 子任务块（subpatch） |
| 子轨迹（subtrack / subtracks） | 任务块（patch） | 检查点（checkpoint） |
| 尝试（attempt） | 语义配置（semantic_config） | 执行配置（execution_config） |
| 未提交差异（dirty diff） | 来源追溯信息（provenance） | 部署上线（rollout） |
| 命名卷（named volume） | 运行时（runtime） | 编排器（orchestrator） |
| 执行器（executor） | 运行器（runner） | 写入器（writer） |
| 读取器（reader） | 加载器（loader） | 解析器（parser） |
| 生成器（generator） | 可迭代对象（iterable） | 迭代器（iterator） |
| 工作区（workspace） | 临时工作区（scratch） | 内存分配器（allocator） |
| 内存分配（allocation） | 堆内存（heap） | 内存分配区域（arena） |
| 预读（readahead） | 回收（reclaim） | 内存映射（mmap） |
| 内存映射数组（memmap） | 磁盘暂存（spool） | 外存溢写（spill） |
| 中间暂存（staging） | 回退路径（fallback） | 保护阈值（guard） |
| 溢出（overflow） | 有界（bounded） | 全量物化（eager） |
| 惰性（lazy） | 批量写出（flush） | 读取（fetch） |
| 解码（decode） | 性能剖析（profiling） | 监测（telemetry） |
| 推算（projected） | 实测验证（verified） | 测试驱动程序（harness） |
| 摘要（digest） | 基准测试（benchmark） | 冒烟测试（smoke） |
| 重置（reset） | 数据库结构（schema） | 分区（partition） |
| 分片（shard） | 空间分块（tile） | 邻域缓冲区（halo） |
| 空间网格（grid） | 包围盒（bbox） | 归属者（owner） |
| 线段对（segment pair） | 线段（segment / segments） | 跨轨迹（cross-track / cross） |
| 穿越（crossing） | 拓扑（topology / Topology） | 簇（cluster） |
| 连通分量（component） | 待处理集合（frontier） | 均值（mean） |
| 方差（variance） | 中位数（median） | 标准分数（z-score） |
| 核心（core） | 边界（border） | 噪声（noise） |
| 链式连接（chain） | 连通交叉口对（connected pair） | 配对（pair） |
| 轨迹（track / tracks） | 转向（turn / turns） | 正交（ortho） |
| 片段（clip / clips） | 中心线（centerline / centerlines） | 清单（manifest / manifests） |
| 交叉口（intersections / intersection） | 观测（observations） | 事件（events / event） |
| 归属记录（memberships / membership） | 端点（endpoints / endpoint） | 节点（nodes / node） |
| 边（edges / edge） | 孤立节点（isolated） | 变体（variant） |
| 阶段（stage / Stage / Phase） | 运行批次（Run） | 时间间隔（gap） |
| 待处理（pending） | 待判定（undecided） | 静止（stationary） |
| 剔除（excluded / exclusion） | 剔除记录（exclusions） | 移除（removed） |
| 保留（kept） | 注入（injected） | 有效（active） |
| 计数（counts） | 标签（labels） | 已分组（grouped） |
| 成员（members） | 源数据（source） | 车辆（vehicle） |
| 点（point / points） | 行（row / rows） | 映射（map / maps） |
| 列表（list / lists） | 元组（tuple / tuples） | 字典（dict） |
| 集合（set） | 浮点数（float / floats） | 整数（int） |
| 字符串（strings） | 缓冲区（buffer） | 数据类（dataclass） |
| 槽位（slots） | 空（empty） | 独立（standalone） |
| 窗口（window） | 桶（buckets） | 批次（batch） |
| 数据块（chunk） | 数据流（stream） | 导入（import） |
| 问题记录（issue） | 未知（unknown） | 计算中（working） |
| 已完成（complete / completed） | 已失败（failed） | 运行中（running） |
| 续跑（resume） | 租约（lease） | 心跳（heartbeat） |
| 协作锁（advisory） | 手动（manual） | 原始（raw） |
| 输出（output） | 最大分组（max group） | 最大存活点数（max live points） |
| 数据库增长量（database growth） | 精确源数据行数（exact source rows） | 实际下游数量（actual downstream counts） |
| 规划（planned） | 外存溢写次数（spill count） | 边界范围（bounds） |
| 几何（geometry） | 顺序（order） | 时间戳（timestamp） |
| 方向角（heading） | 速度（speed） | 经度（lon） |
| 纬度（lat） | 锚点（anchor） | 类型（kind） |
| 表（table） | 代理执行环境（agent） | 开发（dev） |
| 生产（prod） | 上限（cap） | 采样堆叠数组（sample stack） |
| 持久化（persistence） | 易读容量格式（pretty-size） | 合并（combined / union） |
| 已计算（computed） | 已解析（resolved） | 未命名（unnamed） |
| 控制组（cgroup） | 结果（result） | 原生（native） |
| 游标（cursor） | 数据库清理（vacuum） | 插入（insert） |
| 堆叠数组（stack） | 拆分（split） | 清洗（clean） |
| 交换空间（swap） | 视图（view） | 最大值（max） |
