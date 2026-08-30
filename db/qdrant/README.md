# Qdrant

Qdrant 的真实向量数据由 Compose 的 `qdrant_data` named volume 管理，仓库中不保存 `/qdrant/storage`。

当前工作区未提供 collection 定义或初始化脚本；后续只应将声明式 collection 配置和幂等初始化逻辑放入本目录。
