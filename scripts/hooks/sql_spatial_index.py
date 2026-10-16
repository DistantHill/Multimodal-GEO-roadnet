"""Codex SQL-change review gate. Standard library only; never executes SQL."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SQL_START = re.compile(r'^(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|COPY|EXPLAIN|SET|DO|GRANT|REVOKE)\b', re.I)
REVIEW_FILE = 'docs/sql-index-reviews.json'
SUFFIXES = {'.py', '.sql', '.js', '.ts', '.jsx', '.tsx', '.sh'}


def sql_units(text: str) -> list[tuple[int, str]]:
    """Split SQL files, preserving semicolons in quoted/dollar-quoted bodies."""
    tokens = re.finditer(r"--[^\n]*|/\*.*?\*/|'(?:''|\\.|[^'])*'|\"(?:\"\"|[^\"])*\"|(?P<dollar>\$(?:[A-Za-z_]\w*)?\$).*?(?P=dollar)|;", text, re.S)
    start = 0
    units = []
    for token in tokens:
        if token.group() == ';':
            units.append((text.count('\n', 0, start) + 1, text[start:token.end()]))
            start = token.end()
    if text[start:].strip():
        units.append((text.count('\n', 0, start) + 1, text[start:]))
    return units


def is_sql(value: str) -> bool:
    value = re.sub(r'--[^\n]*|/\*.*?\*/', '', value, flags=re.S).strip()
    return bool(SQL_START.match(value))


def extract(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding='utf-8')
    if path.suffix == '.sql':
        return [(line, value) for line, value in sql_units(text) if is_sql(value)]
    if path.suffix == '.py':
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # Incomplete edits containing SQL still require review; do not skip.
            return [(1, text)] if re.search(r'\b(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b', text, re.I) else []
        result = []
        def walk(node):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left = node
                while isinstance(left, ast.BinOp) and isinstance(left.op, ast.Add):
                    left = left.left
                prefix = left.value if isinstance(left, ast.Constant) else (
                    left.values[0].value if isinstance(left, ast.JoinedStr)
                    and isinstance(left.values[0], ast.Constant) else '')
                if isinstance(prefix, str) and is_sql(prefix):
                    result.append((node.lineno, ast.unparse(node)))
                    return
            if isinstance(node, ast.JoinedStr):
                value = ''.join(part.value if isinstance(part, ast.Constant) else '{' + ast.unparse(part) + '}' for part in node.values)
                if is_sql(value):
                    result.append((node.lineno, value))
                return
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and is_sql(node.value):
                result.append((node.lineno, node.value))
            for child in ast.iter_child_nodes(node):
                walk(child)
        walk(tree)
        return result
    # JS/TS templates, ordinary strings and shell heredocs. This is a static
    # detector, not a parser for arbitrary runtime-generated queries.
    literals = re.finditer(r'`[^`]*`|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|<<[\'\"]?(\w+)[\'\"]?[^\n]*\n(.*?)\n\1\b', text, re.S)
    result = []
    for match in literals:
        value = match.group(2) if match.group(1) else match.group()[1:-1]
        if is_sql(value):
            result.append((text.count('\n', 0, match.start()) + 1, value))
    return result


def scan(root: Path) -> dict[str, dict]:
    names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard']).decode().split('\0')
    found = {}
    for name in set(names):
        path = root / name
        if not name or path.suffix not in SUFFIXES or not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        for line, value in extract(path):
            digest = hashlib.sha256(value.encode()).hexdigest()
            key = name + '#' + digest
            found[key] = {'path': name, 'line': line, 'sha256': digest}
    return found


def plan_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from plan_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from plan_nodes(child)


def external_plan_root(root: Path) -> Path:
    """Use the same DATA_ROOT resolution as the user's artifact-routing hook."""
    source = Path(__file__).with_name('artifact_routing.py')
    spec = importlib.util.spec_from_file_location('_sql_artifact_routing', source)
    routing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routing)
    configured, _ = routing.data_root(root, dict(os.environ))
    base = configured if configured is not None else routing.FALLBACK_ROOT / routing.project_name(root)
    return (base / 'codex-artifacts').resolve()


def review_error(root: Path, record: dict) -> str | None:
    if not isinstance(record, dict):
        return '缺少 SQL 空间索引检查记录'
    if record.get('status') == 'not-applicable':
        if not str(record.get('reason', '')).strip():
            return '不适用空间索引时必须说明具体原因'
        return None
    if record.get('status') != 'uses-index':
        return 'status 必须为 uses-index 或 not-applicable'
    indexes = record.get('indexes')
    if not isinstance(indexes, list) or not indexes or not all(isinstance(name, str) and name for name in indexes):
        return '必须列出实际使用的空间索引'
    if not str(record.get('crs', '')).strip():
        return '必须记录空间索引坐标系 crs'
    plan_name = record.get('plan')
    if not isinstance(plan_name, str) or not plan_name:
        return '必须提供 EXPLAIN (FORMAT JSON) 计划文件'
    plan_path = (root / plan_name).resolve()
    external = not plan_path.is_relative_to(root)
    if external and not plan_path.is_relative_to(external_plan_root(root)):
        return '外部计划文件必须位于项目 DATA_ROOT 或备用目录下的 codex-artifacts 目录内'
    try:
        contents = plan_path.read_bytes()
        plan = json.loads(contents)
    except (OSError, ValueError):
        return '计划文件不存在或不是有效 JSON'
    if external and record.get('plan_sha256') != hashlib.sha256(contents).hexdigest():
        return '外部计划文件必须提供与实际内容一致的 plan_sha256'
    # Merely mentioning an index in SQL or a Filter is not index access.
    used = {node.get('Index Name') for node in plan_nodes(plan)
            if node.get('Node Type') in {'Index Scan', 'Index Only Scan', 'Bitmap Index Scan'}
            and re.search(r'&&|\s@\s|\s~\s|<->|<<|>>|&<|&>|\b(?:st_|geom|geometry)', str(node.get('Index Cond', node.get('Order By', ''))), re.I)}
    if not set(indexes).issubset(used):
        return '计划中未找到所声明空间索引的扫描节点及空间索引条件'
    return None


@contextmanager
def locked_state(root: Path, session: str):
    token = hashlib.sha256((str(root) + '\0' + session).encode()).hexdigest()
    directory = Path(tempfile.gettempdir()) / 'roadnet-sql-index-hooks'
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / (token + '.json')
    with (directory / (token + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else None
        yield state, path


def handle(event: dict, root: Path) -> dict | None:
    if not Path(event.get('cwd', '.')).resolve().is_relative_to(root):
        return None
    kind = event.get('hook_event_name')
    if kind not in {'SessionStart', 'PreToolUse', 'PostToolUse', 'Stop'}:
        return None
    session = event.get('session_id')
    if not session:
        return {'decision': 'block', 'reason': 'SQL 索引 hook 缺少 session_id，无法建立变更基线'}
    with locked_state(root, session) as (state, path):
        current = scan(root)
        if state is None:
            state = {'known': list(current), 'pending': []}
            path.write_text(json.dumps(state))
            if kind in {'PostToolUse', 'Stop'}:
                return {'decision': 'block', 'reason': 'SQL 索引 hook 缺少写入前基线；本次变更需人工核对，随后继续正常监测'}
        if kind in {'SessionStart', 'PreToolUse'}:
            return None
        pending = (set(state['pending']) | (set(current) - set(state['known']))) & set(current)
        try:
            reviews = json.loads((root / REVIEW_FILE).read_text())
            if not isinstance(reviews, dict):
                raise ValueError('review file must be an object')
        except (OSError, ValueError):
            reviews = {}
        failures = []
        remaining = []
        for key in sorted(pending):
            error = review_error(root, reviews.get(key))
            if error:
                remaining.append(key)
                item = current[key]
                failures.append(f" {root / item['path']}:{item['line']} ：{error}\nreview key: {key} ")
        path.write_text(json.dumps({'known': list(current), 'pending': remaining}))
        if failures:
            reason = ('新增或修改 SQL 尚未完成空间索引检查。请核对现有索引及 CRS，'
                      '在 docs/sql-index-reviews.json 中按 review key 提交 uses-index + indexes + crs + plan，'
                      '或 not-applicable + reason。SQL 内容再次变化必须重新检查。\n' + '\n'.join(failures[:12]))
            return {'decision': 'block', 'reason': reason}
    return None


def main():
    root = Path(__file__).resolve().parents[2]
    if sys.argv[1:] == ['--list']:
        print(json.dumps(scan(root), ensure_ascii=False, indent=2))
        return
    try:
        result = handle(json.load(sys.stdin), root)
    except Exception as exc:
        result = {'decision': 'block', 'reason': f'SQL 空间索引 hook 检查失败：{exc}'}
    if result:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
