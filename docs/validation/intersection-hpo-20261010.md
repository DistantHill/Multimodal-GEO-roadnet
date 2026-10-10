# 转角阈值调参框架验证记录

日期：2026-10-10。术语遵循 [项目术语与写作规范](../terminology.md) 。

## 实施范围

三个 gpt-6.1-sol / medium 子代理分别实现数据清单、运行适配和调参协调，主代理完成集成修复与独立验收。
算法只搜索 `turn_threshold` 的 `min_turn_deg`，保持严格 `abs(turn_deg) > min_turn_deg`。
严格人工区域，不外扩；沿用最终交叉口面与现有评价器。实验存档和 Optuna 持久化使用 PostgreSQL `roadnet_eval`。
SQLite 未实施，普通生产配置未更改，G* 代码没有删除。

## 自动化证据

1. 变更前 `make test`：Backend 147 passed / 6 skipped；Pipeline 1433 passed / 97 skipped。
2. 安装锁定依赖和 GDAL 后，使用独立 `roadnet-hpo-validation` 容器镜像执行 `make test`：
   Compose/Make 23 passed；Backend 147 passed / 6 skipped；Pipeline 1471 passed / 97 skipped。
3. 最后补充配置导出和 CLI 错误处理后，容器定向回归覆盖全部 HPO 测试及配置边界检查：46 passed。
4. 实际 Optuna PostgreSQL 持久化验证：在 `roadnet_eval` 独立临时 schema 中执行四个合成评分 trial，
   删除本地导出后从数据库恢复，完成 L3 晋级和独立测试阶段。仅本次临时 schema 在结束后删除。
   合成评分仅验证流程，不代表算法效果。
5. SQL hook 扫描确认新 HPO 模块没有自定义 SQL；原实验空间查询未改动。
6. `git diff --check` 通过。

完整容器回归命令（覆盖镜像文件为本次临时验证配置）：

```sh
make test 'COMPOSE_DEV=docker compose --env-file .env -f infra/docker/compose.dev.yml -f /tmp/roadnet-hpo-test.yml'
```

该覆盖只将 Pipeline 镜像替换为 `roadnet-hpo-validation`。验证镜像基于已有开发镜像，
加入与本次 Dockerfile 相同的 `gdal-bin` 及更新后的哈希依赖锁；代码仍由开发 Compose 挂载。
常规运行应重新构建 Pipeline 镜像以获得这些依赖。
本次运行的是 pytest 回归，没有启动 Pipeline 冒烟或全量数据作业。

## 用户文件实测

附件与 Windows 文件的 SHA-256 一致：
`2eda35081410c025487031cd6b489ed47a9f1c42faa5173672c05ed21dc3c052`。
文件大小 1,974,272 字节，图层 `labeled_intersections`，源 CRS EPSG:4326，1716 条记录。
保留 fid、OBJECTID、坐标及其他原始属性，不覆盖原文件。

源 WKB 包含 1712 个 MultiPolygon 和 4 个 MultiSurface。使用 GDAL `CONVERT_TO_LINEAR`、
`OGR_ARC_STEPSIZE=1` 转换为评价器支持的线性面，全部 1716 个要素通过读取与有效性校验。
宿主机 GDAL 3.4.1、验证容器 GDAL 3.10.3 均实际读取成功；版本及转换策略进入来源记录。
当前所有要素缺少 `type`，按用户要求保留为“未分类”，不自动猜测类别。
预留十字路口、T字路口、Y字路口、环岛、匝道汇入/分流、复杂交叉口、其他。

## 尚未完成的真实实验

用户说明另有区域和负样本 GPKG，尚未提供其路径或附件。没有推断评价边界、负样本完整性、
调参/独立测试分组，也没有把未分类标签当作已完成类型标注。
真实 Pilot、最佳阈值、真实 F1 与成本比较尚未运行；需要这些输入及明确的数据快照/配置。
跨 trial 的候选特征持久缓存未实现；目前复用已完成阶段与同一执行器拥有的 Refine。
CPU 总成本、完整 L3 反事实成本及加速比未测量，报告将明确标为不可用。
