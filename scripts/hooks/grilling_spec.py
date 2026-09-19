"""Inject the repository's grilling workflow on each user prompt; no file writes."""

import json
from pathlib import Path
import sys


def main() -> None:
    event = json.load(sys.stdin)
    root = Path(__file__).resolve().parents[2]
    spec_template = root / "docs" / "templates" / "grilling-spec.md"
    cwd = Path(event.get("cwd", ".")).resolve()
    if event.get("hook_event_name") != "UserPromptSubmit" or not cwd.is_relative_to(root):
        return

    heading = "## Grilling → Spec Hook"
    instructions = (root / "AGENTS.md").read_text(encoding="utf-8")
    section = instructions.split(heading, 1)[1].split("\n## ", 1)[0].strip()
    context = (
        "项目 grilling Spec 生命周期 hook。以下规则仅在用户实际调用 grilling，"
        "或当前消息属于正在进行的 grilling 访谈（回答、补充、更正、确认）时适用。"
        "后续回答无需再次包含 @grilling。普通任务、引用示例及配置 hook 本身不启动访谈。"
        "请依据对话语义判断；本 hook 没有创建或更新任何 Spec，文档操作由你执行。\n\n"
        f"Spec 模板位置：`{spec_template}`。\n\n"
        + section
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }}, ensure_ascii=False))


if __name__ == "__main__":
    main()
