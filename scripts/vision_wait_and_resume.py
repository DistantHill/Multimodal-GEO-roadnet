#!/usr/bin/env python3
"""Wait outside Codex for the Vision build, then queue this session's next task."""

import argparse
import fcntl
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
IMAGE = "roadnet-vision-yolov8:8.3.0-cu117"


def run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, timeout=timeout)


def build_running(pid: int) -> bool:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except FileNotFoundError:
        return False
    return b"vision-build" in command and any(
        Path(arg.decode()).name == "make" for arg in command if arg
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--build-pid", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--build-command", nargs=argparse.REMAINDER,
                        default=["make", "vision-build"],
                        help="Build command and arguments; place this option last")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval must be positive")
    if not args.build_command:
        parser.error("--build-command must not be empty")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    lock = (args.state_dir / "watcher.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex executable not found")

    def status(phase: str, **extra: object) -> None:
        record = {"phase": phase, "time": datetime.now(timezone.utc).isoformat(),
                  "thread": args.thread, "image": IMAGE, **extra}
        temporary = args.state_dir / "status.tmp"
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(args.state_dir / "status.json")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    if (args.state_dir / "queued.json").exists():
        print("Continuation already queued; exiting.", flush=True)
        return
    status("waiting", build_pid=args.build_pid)
    deadline = time.monotonic() + 6 * 3600
    rebuild_attempts = 0
    try:
        while True:
            if run(["docker", "image", "inspect", IMAGE]).returncode == 0:
                outcome = "ready"
                detail = "Vision 镜像已构建完成。"
                break
            if not build_running(args.build_pid):
                if rebuild_attempts < 3:
                    rebuild_attempts += 1
                    status("retrying_build", attempt=rebuild_attempts,
                           command=args.build_command)
                    time.sleep(args.interval)
                    with (args.state_dir / "build.log").open("a") as log:
                        subprocess.run(args.build_command, cwd=REPO,
                                       stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, timeout=2 * 3600)
                    continue
                outcome = "build_failed"
                detail = "Vision 构建退出且后台重试三次后仍无目标镜像；请检查失败原因。"
                break
            if time.monotonic() >= deadline:
                outcome = "timeout"
                detail = "Vision 后台等待已超过六小时，目标镜像仍不存在；请检查构建状态。"
                break
            time.sleep(args.interval)
    except Exception as exc:
        outcome = "watch_error"
        detail = f"Vision 后台监控发生异常：{exc}"
    status(outcome)
    message = (
        "这是用户授权的 Vision 后台脚本触发的续接通知。" + detail
        + " 原任务：使用 /data/service-data/vision/models/yolo/yolov8n.pt "
        "部署项目 Vision 并跑通一张图片识别 demo。不要恢复会话内长时间轮询。"
        "若镜像已就绪，继续用项目 Compose 的 vision 服务运行 GPU 单图识别并验证输出。"
        "演示脚本已准备在 /tmp/roadnet-vision-demo.py ，可通过 "
        "docker compose --env-file .env -f infra/docker/compose.dev.yml --profile vision "
        "run --rm -T vision python - < /tmp/roadnet-vision-demo.py 执行。"
        "它使用 Ultralytics 自带 bus.jpg，将标注图片、标签和 summary.json "
        "保存到 /data/tmp/road-network/vision/output 下。"
        "保留现有用户改动，完成后报告真实结果和文件位置。"
        f" 后台状态文件位于 {args.state_dir / 'status.json'} 。"
    )
    status("queueing", build_outcome=outcome)
    try:
        queued = run([codex, "queue", "--thread", args.thread,
                      "--cd", str(REPO), "--message", message], timeout=120)
        (args.state_dir / "queue.log").write_text(queued.stdout + queued.stderr)
        if queued.returncode:
            raise RuntimeError(f"codex queue exited {queued.returncode}; see queue.log")
    except Exception as exc:
        status("queue_failed", build_outcome=outcome, error=str(exc))
        raise
    (args.state_dir / "queued.json").write_text(json.dumps({"thread": args.thread,
                                                           "outcome": outcome}) + "\n")
    status("queued", build_outcome=outcome)


if __name__ == "__main__":
    main()
