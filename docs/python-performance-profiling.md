# Python 性能分析快速使用

本文说明如何在开发容器和调试容器中用 `py-spy` 与 `Scalene` 分析 Pipeline。
性能剖析器（profiler）不需要改业务代码；它们会启动目标程序并收集调用栈或逐行采样。

## 使用开发容器

开发环境构建时会安装 `py-spy` 和 `Scalene`。分析 Pipeline 的一次运行时，先替换
`public.day01` 为实际源表：

```sh
PROFILE_ID=20260923_172906_194381

# py-spy：采样并生成火焰图
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  py-spy record --duration 30 -o "/data/logs/${PROFILE_ID}_hotspots.svg" -- \
  python -m pipeline run --source-table public.day01

# Scalene：生成逐行 CPU 报告
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  scalene run --cpu-only -o "/data/logs/${PROFILE_ID}_scalene-profile.json" \
  -m pipeline run --source-table public.day01

# 在终端查看 Scalene 报告
docker compose --env-file .env -f infra/docker/compose.dev.yml run --rm --no-deps pipeline \
  scalene view --cli "/data/logs/${PROFILE_ID}_scalene-profile.json"
```

每次分析时将 `PROFILE_ID` 换成唯一标识，例如时间戳。`py-spy` 的 SVG 和 Scalene 的 JSON
会写到宿主机配置的报告目录（默认位于 `${DATA_ROOT}/logs`，可用 `PROFILE_OUTPUT_DIR`
覆盖）。Backend 和 Pipeline 容器都挂载了该目录。也可以把 `pipeline` 替换为 `backend`，
并将 `python -m pipeline ...` 换成要分析的 Backend Python 命令。

## 使用调试容器

`compose.debug.yml` 是 `compose.dev.yml` 的覆盖配置，会选择 Dockerfile 中的 `debug`
构建阶段（后端和 Pipeline 容器均以非 root 用户运行）。构建和启动方式如下：

```sh
PROFILE_ID=20260923_172906_194381

docker compose --env-file .env \
  -f infra/docker/compose.dev.yml -f infra/docker/compose.debug.yml \
  build backend pipeline

docker compose --env-file .env \
  -f infra/docker/compose.dev.yml -f infra/docker/compose.debug.yml \
  run --rm --no-deps pipeline \
  py-spy record --duration 30 -o "/data/logs/${PROFILE_ID}_hotspots.svg" -- \
  python -m pipeline run --source-table public.day01

docker compose --env-file .env \
  -f infra/docker/compose.dev.yml -f infra/docker/compose.debug.yml \
  run --rm --no-deps pipeline \
  scalene run --cpu-only -o "/data/logs/${PROFILE_ID}_scalene-profile.json" \
  -m pipeline run --source-table public.day01
```

调试容器的 `/data` 由 Compose 挂载到运行数据目录，因此分析报告会保留在宿主机。
宿主机日志目录需要允许容器 UID 1000 写入。
`py-spy` 附加到正在运行的进程需要操作系统允许读取目标进程；在 Docker 中通常需要
`SYS_PTRACE`。以上示例由 `py-spy` 启动目标进程，不需要附加权限。

## 如何看报告

- `py-spy` 的火焰图按采样到的调用栈汇总，方框越宽表示越多样本经过该函数；它用于找函数级热点。
- Scalene 的 CLI 报告按源码行展示 CPU 和内存活动；`--cpu-only` 仅收集 CPU 数据。
- 两者都采用采样估算，短时间或低频调用的结果可能不稳定。先对较长、可复现的运行采样，再针对热点做局部测量。
