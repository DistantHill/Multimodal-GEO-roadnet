# 历史运行边界

参数试验台重构以前的基线提交为
[`e2f5a8ce64e11ee6d161cf245878219d140f5efe`](https://github.com/DistantHill/Multimodal-GEO-roadnet/tree/e2f5a8ce64e11ee6d161cf245878219d140f5efe)。
该提交的 [README](https://github.com/DistantHill/Multimodal-GEO-roadnet/blob/e2f5a8ce64e11ee6d161cf245878219d140f5efe/README.md)、
Makefile、Dockerfile 与 Compose 配套，记录了旧 Web GIS / QGIS / PostgreSQL 流水线。

当前分支保留 `apps/`、`infra/`、`db/`、`services/vision/`、`track.v2`、历史 Spec 和测试
供迁移参考，不将这些服务接入新的核心命令。新的根 Makefile 和 Pipeline Dockerfile
专用于参数试验台，不能与历史 Compose 的 `serve` / `bootstrap` 命令混用。
需要复现旧栈时，使用该基线的独立 checkout，不在活动试验台里混合两套配置。

原 `pipeline.__main__` 作为 `pipeline.legacy` 保留，便于查看旧入口或在安装
`roadnet-pipeline[legacy]` 及其原运行依赖后执行迁移诊断。该入口没有在本次环境完成
整栈回归，不构成对旧 UI/API、旧数据库 schema 或旧配置的兼容性保证。

历史 Spec 中记录的规模和资源目标继续属于原目标，本次合成验收不替代这些目标的
正式验证。新活动入口、参数和产物以根 `pipeline.toml` 及
[参数试验台设计](parameter-workbench.md)为准。
