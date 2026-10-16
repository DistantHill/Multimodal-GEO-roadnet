"""Plan non-HTML runtime artifact locations on every user prompt; no file writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
from string import Template
import subprocess
import sys

FALLBACK_ROOT = Path('/data/tmp')
CATEGORIES = {
    'evidence': ('测试证据、验证结果 JSON', r'测试|验证|证据|评估|\b(?:tests?|pytest|unittest|benchmark|validation|explain)\b'),
    'logs': ('运行日志、轮询及监控记录', r'日志|轮询|监控|\b(?:logs?|poll(?:ing)?|watch|monitor(?:ing)?)\b'),
    'metrics': ('性能指标、测量结果', r'性能|指标|耗时|内存|基准|\b(?:benchmark|metrics?|profil(?:e|ing))\b'),
    'screenshots': ('截图及调试图片', r'截图|截屏|\b(?:screenshots?|screens?)\b'),
    'exports': ('导出文件', r'导出|\b(?:exports?|csv|parquet)\b'),
}


def discover_root(cwd: Path) -> Path:
    result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True, timeout=3)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else cwd.resolve()


def project_name(root: Path) -> str:
    return re.sub(r'[^\w.-]+', '-', root.name).strip('.-')[:64] or 'project'


def data_root(root: Path, env: dict[str, str]) -> tuple[Path | None, str]:
    value = ''
    source = '未配置 DATA_ROOT'
    for key in ('DATA_ROOT', 'data_root'):
        if env.get(key, '').strip():
            value, source = env[key].strip(), '环境变量 ' + key
            break
    if not value:
        dotenv = root / '.env'
        if dotenv.is_file():
            values = {}
            for line in dotenv.read_text(encoding='utf-8').splitlines():
                match = re.match(r'^\s*(?:export\s+)?(DATA_ROOT|data_root)\s*=\s*(.*)$', line)
                if match:
                    # Parse only the requested setting. Never source shell code.
                    tokens = shlex.split(match.group(2), comments=True)
                    values[match.group(1)] = tokens[0] if len(tokens) == 1 else ''
            for key in ('DATA_ROOT', 'data_root'):
                if values.get(key):
                    value, source = values[key], '项目 .env 中的 ' + key
                    break
    if not value:
        return None, source
    value = Template(value).safe_substitute(env)
    if '$' in value or '`' in value:
        return None, source + ' 含未解析表达式，使用仓库外备用目录'
    path = Path(value).expanduser()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if path.is_relative_to(root):
        return None, source + ' 位于仓库内，使用仓库外备用目录'
    return path, source


def make_plan(event: dict, env: dict[str, str] | None = None) -> dict:
    env = dict(os.environ) if env is None else env
    root = discover_root(Path(event.get('cwd', '.')))
    configured, source = data_root(root, env)
    base = configured if configured is not None else FALLBACK_ROOT / project_name(root)
    project_key = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    session_key = hashlib.sha256(str(event.get('session_id') or 'session').encode()).hexdigest()[:12]
    run = base / 'codex-artifacts' / project_key / session_key
    prompt = str(event.get('prompt', ''))
    categories = {key: label for key, (label, pattern) in CATEGORIES.items() if re.search(pattern, prompt, re.I)}
    categories['scratch'] = '开发过程临时文件、调试及中间结果'
    return {'project_root': str(root), 'data_root': str(configured) if configured else None,
            'source': source, 'run_root': str(run),
            'artifacts': [{'kind': key, 'description': label, 'directory': str(run / key)}
                          for key, label in categories.items()]}


def handle(event: dict) -> dict | None:
    if event.get('hook_event_name') != 'UserPromptSubmit':
        return None
    try:
        plan = make_plan(event)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        context = f'非 HTML 临时产物目录规划失败：{exc}。在修复 DATA_ROOT 配置前，不要将运行证据或临时文件退回仓库；使用 /data/tmp/ 下的项目临时目录。'
    else:
        lines = [
            '本轮非 HTML 产物目录规划（用户级 hook）：',
            f"项目： {plan['project_root']} 。目录来源：{plan['source']}。",
            f"本会话运行目录： {plan['run_root']} 。",
            '以下类别由当前提示词关键词建议；请结合完整会话和实际工具操作补充类别，但保持目录优先级：',
        ]
        lines += [f"- {item['description']}： {item['directory']} 。" for item in plan['artifacts']]
        lines += [
            '优先项目 DATA_ROOT（进程环境优先，其次项目 .env）；未配置或指向仓库内部时，使用 /data/tmp/<项目名>/codex-artifacts/<项目标识>/<会话标识>/ 下的分类目录。',
            '测试证据 JSON、轮询记录、运行日志、截图、导出数据及开发临时文件不要随意写入 pwd 或 Git 工作区；需要时才创建目录，结束时报告实际证据位置。',
            '源码、测试代码、正式文档和必须版本管理的配置仍放仓库；受专门审查流程约束的文件继续遵从其规则，不得跳过验证。',
            '本 hook 仅规划非 HTML 运行产物；不移动现有文件，不改变 HTML 保存、生成或预览规则。',
        ]
        context = '\n'.join(lines)
    return {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': context}}


def main() -> None:
    result = handle(json.load(sys.stdin))
    if result:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
