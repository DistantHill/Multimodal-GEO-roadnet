"""Project-level instruction to produce interactive HTML and use the shared preview."""
from __future__ import annotations

import json
import sys

POLICY = """HTML 结果生成与统一预览规则：每次完成用户任务、给出实质性结果时，生成或更新一个可交互 HTML 产物。内容以本次真实结果为依据；根据结果提供搜索、筛选、折叠、排序或场景切换等有用交互，不仅把回复文字包进 HTML。每份 HTML 在 head 中加入 <meta name="codex-preview" content="artifact">，让 hook 即使在文件没有变化或清单重建后也能识别并收录。HTML 及其配套 JSON、图片、CSS/JS 统一保存到仓库外的 /data/tmp/codex/html-preview/ 下，使用当前项目持久登记的 storage_root 保存区，且保存区必须位于上述根目录内；禁止把这些生成产物写入 pwd、Git 工作区或仓库内的 docs/validation 等目录。保存前先确认项目外部保存区已登记；未配置时先配置项目专属保存区，不得回退到仓库。最终回复简要说明结果并提供统一预览链接。HTML 的登记和服务启动由用户级 html_preview.py 在 PostToolUse / Stop 处理，多个项目共用 18765 端口，链接包含项目标识。无需每个文件新建服务或 SSH 隧道。若没有获得登记链接，读取用户级预览脚本或使用它显式登记：--root 指定原项目根目录，--publish 指定外部保存区内 HTML 的绝对路径；不要把保存区当作项目根目录。确认服务响应后再提供链接。用户明确要求其他格式、仅需补充信息或权限确认时，遵从用户本次指令；不得为生成 HTML 改变任务结论或编造结果。"""


def output(context: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


def main() -> None:
    event = json.load(sys.stdin)
    if event.get("hook_event_name") == "UserPromptSubmit":
        print(json.dumps(output(POLICY), ensure_ascii=False))


if __name__ == "__main__":
    main()
