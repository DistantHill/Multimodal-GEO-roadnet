"""Check/install project HTML generation hooks on each session's first prompt."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys

from html_preview import discover_root, shared_runtime
from html_result_policy import POLICY, output

STATUS = "Generating interactive HTML results"


def handlers(config: dict):
    for event, groups in config.get("hooks", {}).items():
        if isinstance(groups, list):
            for group in groups:
                for hook in group.get("hooks", []):
                    yield event, hook


def is_generation(hook: dict) -> bool:
    command = hook.get("command", "")
    if "html_result_policy.py" in command or hook.get("statusMessage") == STATUS:
        return True
    text = " ".join(str(hook.get(key, "")) for key in ("command", "statusMessage", "description"))
    return bool(re.search(r"html", text, re.I) and re.search(r"generat|生成|可交互|interactive", text, re.I))


def inspect(root: Path) -> tuple[dict, bool]:
    path = root / ".codex" / "hooks.json"
    config = json.loads(path.read_text()) if path.exists() else {"description": "Project interactive HTML results", "hooks": {}}
    if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
        raise ValueError("项目 hooks.json 的 hooks 必须是对象，原文件未改动")
    exists = any(event == "UserPromptSubmit" and is_generation(hook) for event, hook in handlers(config))
    toml_path = root / ".codex" / "config.toml"
    if toml_path.exists():
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        inline = tomllib.loads(toml_path.read_text())
        exists = exists or any(event in {"UserPromptSubmit", "user_prompt_submit"} and is_generation(hook)
                              for event, hook in handlers(inline))
    return config, exists


def handle(event: dict) -> dict | None:
    if event.get("hook_event_name") != "UserPromptSubmit":
        return None
    session = event.get("session_id")
    if not session:
        return output("HTML 首次对话检查缺少 session_id，未写入项目配置。\n" + POLICY)
    root = discover_root(Path(event.get("cwd", ".")))
    token = hashlib.sha256((str(root) + "\0" + str(session)).encode()).hexdigest()
    directory = shared_runtime() / "bootstrap"
    directory.mkdir(mode=0o700, exist_ok=True)
    message = ""
    project_lock = hashlib.sha256(str(root).encode()).hexdigest()
    with (directory / (project_lock + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = directory / (token + ".json")
        if not marker.exists():
            try:
                config, exists = inspect(root)
                path = root / ".codex" / "hooks.json"
                if not exists:
                    # Preserve unrelated hooks; never modify trust records.
                    groups = config.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
                    groups.append({"hooks": [{"type": "command",
                        "command": "python3 " + shlex.quote(str(Path(__file__).with_name("html_result_policy.py"))),
                        "timeout": 5, "statusMessage": STATUS}]})
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name("hooks.html-bootstrap.tmp")
                    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
                    temporary.replace(path)
                    message = f"首次对话检查：已补齐项目 HTML 结果生成 hook，配置位于 {path} 。新 handler 需要 Codex hook 审核；本用户级 hook 同时注入生成规则，确保当前对话有该指令。\n"
                else:
                    message = f"首次对话检查： {root} 已有项目 HTML 结果生成 hook，保留原配置。\n"
                marker.write_text(json.dumps({"root": str(root), "generation_found": exists, "installed": not exists}))
            except (OSError, ValueError, ImportError) as exc:
                message = f"首次对话检查未能完成：{exc}。未覆盖原配置，请修复项目配置；当前对话仍注入生成规则。\n"
    return output(message + POLICY)


def main() -> None:
    result = handle(json.load(sys.stdin))
    if result:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
