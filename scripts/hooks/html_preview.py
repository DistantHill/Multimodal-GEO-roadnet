"""Serve changed project HTML locally for a Windows-initiated SSH forward."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import html
import io
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urlsplit
from urllib.request import urlopen

SERVICE_ID = "roadnet-html-preview-v2"
PORT = 18765
PREVIEW_MARKER = '<meta name="codex-preview" content="artifact">'
EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__"}
ASSETS = {".css", ".js", ".mjs", ".json", ".geojson", ".png", ".jpg", ".jpeg",
          ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".csv"}


def shared_runtime() -> Path:
    directory = Path(tempfile.gettempdir()) / f"roadnet-html-preview-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory



def registry_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    directory = base / "codex-html-preview"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Migrate every old project atomically, preserving newer durable records.
    with (directory / "migration.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for legacy in (shared_runtime() / "projects").glob("*/state.json"):
            target = directory / "projects" / legacy.parent.name / "state.json"
            if target.exists():
                continue
            state = json.loads(legacy.read_text())
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = target.with_name("migration.new")
            temporary.write_text(json.dumps(state))
            temporary.replace(target)
    return directory


class PreviewMarkerParser(HTMLParser):
    marked = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if (tag.lower() == "meta" and str(values.get("name", "")).lower() == "codex-preview"
                and str(values.get("content", "")).lower() == "artifact"):
            self.marked = True


def is_marked(path: Path) -> bool:
    parser = PreviewMarkerParser()
    with path.open(encoding="utf-8", errors="replace") as stream:
        parser.feed(stream.read())
    return parser.marked


def mark_artifact(path: Path) -> None:
    if is_marked(path):
        return
    stat = path.stat()
    text = path.read_text(encoding="utf-8")
    closing = re.search(r"</head\s*>", text, re.I)
    opening = re.search(r"<html\b[^>]*>", text, re.I)
    offset = closing.start() if closing else opening.end() if opening else 0
    updated = text[:offset] + PREVIEW_MARKER + "\n" + text[offset:]
    temporary = path.with_name(path.name + ".preview.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.chmod(stat.st_mode & 0o777)
    os.utime(temporary, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    temporary.replace(path)


def project_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]


def runtime(root: Path) -> Path:
    directory = registry_root() / "projects" / project_id(root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def discover_root(cwd: Path) -> Path:
    result = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=3)
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else cwd.resolve()


def project_url(root: Path, name: str) -> str:
    return f"/p/{project_id(root)}/{quote(name)}"


def read_state(directory: Path) -> dict:
    path = directory / "state.json"
    if path.exists():
        return json.loads(path.read_text())
    # Import pre-persistence registrations without deleting the old state.
    legacy = shared_runtime() / "projects" / directory.name / "state.json"
    if legacy.exists():
        return json.loads(legacy.read_text())
    return {"known": {}, "published": []}


def write_state(directory: Path, state: dict) -> None:
    path = directory / "state.new"
    path.write_text(json.dumps(state))
    path.replace(directory / "state.json")


def scan(root: Path) -> dict[str, str]:
    found = {}
    for parent, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in EXCLUDED and not name.startswith(".")]
        for name in files:
            path = Path(parent) / name
            if path == Path(__file__).with_name("preview_home.html"):
                continue
            if path.suffix.lower() not in {".html", ".htm"} or not path.resolve().is_relative_to(root):
                continue
            try:
                found[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                continue
    return found


def allowed_file(root: Path, name: str, published: list[str]) -> bool:
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return False
    relative = path.relative_to(root)
    if any(part.startswith(".") for part in relative.parts):
        return False
    if relative.as_posix() in published:
        return True
    return path.suffix.lower() in ASSETS and any(
        path.is_relative_to((root / item).parent) for item in published
    )


def projects(directory: Path) -> list[dict]:
    result = []
    for path in sorted((directory / "projects").glob("*/state.json")):
        state = json.loads(path.read_text())
        if "root" not in state:
            continue
        root = Path(state["root"])
        content_root = Path(state.get("storage_root", state["root"]))
        files = []
        for name in state["published"]:
            if not allowed_file(content_root, name, state["published"]):
                continue
            file = content_root / name
            try:
                stat = file.stat()
                with file.open(encoding="utf-8", errors="replace") as stream:
                    heading = re.search(r"<title[^>]*>(.*?)</title>", stream.read(16384), re.I | re.S)
                title = html.unescape(re.sub(r"<[^>]+>", "", heading.group(1))).strip() if heading else file.stem
                files.append({"name": name, "title": title or file.stem, "size": stat.st_size,
                              "updated": stat.st_mtime, "url": project_url(root, name)})
            except FileNotFoundError:
                continue
        if files:
            result.append({"id": project_id(root), "name": root.name, "files": files})
    return result


def create_server(directory: Path, port: int = PORT) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def send_head(self):
            route = urlsplit(self.path).path
            content_type = "text/html; charset=utf-8"
            if route == "/__roadnet_health__":
                body = SERVICE_ID.encode()
                content_type = "text/plain; charset=utf-8"
            elif route == "/":
                body = Path(__file__).with_name("preview_home.html").read_bytes()
            elif route == "/api/projects":
                body = json.dumps(projects(directory), ensure_ascii=False).encode()
                content_type = "application/json; charset=utf-8"
            elif route.startswith("/p/"):
                parts = route.split("/", 3)
                if len(parts) != 4 or not re.fullmatch(r"[a-f0-9]{16}", parts[2]):
                    self.send_error(404)
                    return None
                state = read_state(directory / "projects" / parts[2])
                name = unquote(parts[3])
                root = Path(state.get("storage_root", state.get("root", "/nonexistent")))
                if not allowed_file(root, name, state["published"]):
                    self.send_error(404)
                    return None
                self.directory = str(root)
                self.path = "/" + quote(name)
                return super().send_head()
            else:
                # Keep existing single-project links when exactly one project owns the file.
                name = unquote(route).lstrip("/")
                owners = [item for item in projects(directory)
                          if any(file["name"] == name for file in item["files"])]
                if len(owners) == 1:
                    self.send_response(302)
                    self.send_header("Location", f"/p/{owners[0]['id']}/{quote(name)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_error(404)
                return None
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return io.BytesIO(body)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(directory: Path, port: int = PORT) -> None:
    with create_server(registry_root(), port) as server:
        marker = directory / "server.ready.json"
        identity = {"pid": os.getpid(), "service_id": SERVICE_ID,
                    "start": process_start(os.getpid()), "script": str(Path(__file__).resolve()),
                    "pid_namespace": os.readlink("/proc/self/ns/pid"),
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
        stopped = threading.Event()

        def heartbeat():
            while True:
                temporary = directory / "server.ready.tmp"
                temporary.write_text(json.dumps({**identity, "heartbeat": time.time()}))
                temporary.replace(marker)
                if stopped.wait(2):
                    return

        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            server.serve_forever()
        finally:
            stopped.set()
            worker.join(timeout=3)


def process_start(pid: int) -> str:
    # /proc stat's comm may contain spaces or parentheses; field 22 is starttime.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def ready_process() -> bool:
    """Validate the post-bind marker when a sandbox denies loopback sockets."""
    try:
        ready = json.loads((shared_runtime() / "server.ready.json").read_text())
        pid = int(ready["pid"])
        if ready["service_id"] != SERVICE_ID:
            return False
        if (ready.get("pid_namespace") and ready["pid_namespace"] != os.readlink("/proc/self/ns/pid")):
            # Codex also isolates /proc: use a fresh post-bind heartbeat from
            # the host service, never a bare stale PID file.
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return ready.get("boot_id") == boot_id and 0 <= time.time() - ready.get("heartbeat", 0) <= 10
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return (ready["start"] == process_start(pid)
                and b"--serve" in args and ready["script"].encode() in args)
    except (OSError, ValueError, KeyError, IndexError):
        return False


def healthy(root: Path | None = None) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{PORT}/__roadnet_health__", timeout=1) as response:
            return response.read().decode() == SERVICE_ID
    except OSError as exc:
        cause = getattr(exc, "reason", exc)
        if isinstance(cause, OSError) and cause.errno in {errno.EPERM, errno.EACCES}:
            return ready_process()
        return False


def ensure_preview(root: Path, directory: Path) -> None:
    shared = shared_runtime()
    with (shared / "server.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if healthy():
            return
        unit = Path.home() / ".config/systemd/user/html-preview.service"
        if unit.exists():
            result = subprocess.run(["systemctl", "--user", "start", "html-preview.service"],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode:
                raise RuntimeError("无法联系用户服务管理器；请在沙箱外执行 systemctl --user start html-preview.service")
            for _ in range(20):
                if healthy():
                    return
                time.sleep(0.1)
            raise RuntimeError("用户服务启动后未就绪；执行 journalctl --user -u html-preview.service -n 30 查看实际错误")
        log_path = shared / "server.log"
        offset = log_path.stat().st_size if log_path.exists() else 0
        with log_path.open("a") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        (shared / "server.pid").write_text(str(process.pid))
        for _ in range(20):
            if healthy():
                return
            if process.poll() is not None:
                break
            time.sleep(0.1)
        with log_path.open() as log:
            log.seek(offset)
            details = log.read().strip().splitlines()
        detail = details[-1] if details else "启动后未通过健康检查"
        if "Operation not permitted" in detail or "Permission denied" in detail:
            raise RuntimeError("沙箱禁止创建网络 socket；请在沙箱外执行 systemctl --user start html-preview.service")
        raise RuntimeError(f"统一预览服务启动失败：{detail}")


def handle(event: dict, root: Path, publish: str | None = None) -> str | None:
    kind = event.get("hook_event_name")
    if kind not in {"SessionStart", "PostToolUse", "Stop"} or not Path(event.get("cwd", ".")).resolve().is_relative_to(root):
        return None
    directory = runtime(root)
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(directory)
        content_root = Path(state.get("storage_root", str(root))).resolve()
        if publish:
            file = (content_root / publish).resolve()
            if not file.is_relative_to(content_root) or file.suffix.lower() not in {".html", ".htm"} or not file.is_file():
                raise ValueError("发布文件必须是本项目产物保存区内已存在的 HTML")
            if any(part.startswith(".") for part in file.relative_to(content_root).parts):
                raise ValueError("不发布隐藏目录中的 HTML")
            mark_artifact(file)
        state["root"] = str(root.resolve())
        current = scan(content_root)
        marked = {name for name in current if is_marked(content_root / name)}
        restored = marked - set(state["published"])
        state["published"] = sorted((set(state["published"]) | marked) & current.keys())
        if kind == "SessionStart":
            # Keep pending changes across resumed/compacted sessions.
            if not (directory / "state.json").exists():
                state["known"] = current
            write_state(directory, state)
            try:
                ensure_preview(root, directory)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                return f"HTML 本机预览服务启动失败：{exc}"
            return None
        changed = sorted({name for name, digest in current.items() if state["known"].get(name) != digest} | restored)
        state["published"] = sorted(set(state["published"]) & current.keys())
        if publish:
            name = file.relative_to(content_root).as_posix()
            if name not in current:
                raise ValueError("发布文件必须是本项目产物保存区内已存在的 HTML")
            changed = sorted(set(changed + [name]))
        if not changed:
            state["known"] = current
            write_state(directory, state)
            try:
                ensure_preview(root, directory)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                return f"HTML 本机预览服务启动失败，下次工具执行时重试：{exc}"
            return None
        for name in changed:
            mark_artifact(content_root / name)
        if changed:
            current = scan(content_root)
        state["published"] = sorted(set(state["published"] + changed) & current.keys())
        write_state(directory, state)
        try:
            ensure_preview(root, directory)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return f"HTML 本机预览服务启动失败，下次工具执行时重试：{exc}"
        state["known"] = current
        write_state(directory, state)
        return ("HTML 本机预览服务已就绪。以下 Windows 浏览器链接需要 Windows 发起的 SSH 本地端口转发；"
                "hook 不检查 Windows 转发是否已建立。未配置时在 Windows 执行：\n"
                f"ssh -N -L {PORT}:127.0.0.1:{PORT} reid@100.120.6.3\n"
                "请在最终回复提供对应浏览器链接：\n"
                + "\n".join(f"http://localhost:{PORT}{project_url(root, name)}" for name in changed))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--publish")
    parser.add_argument("--root", type=Path, help="Project root; defaults to the event cwd Git root")
    args = parser.parse_args()
    if args.serve:
        serve(shared_runtime())
        return
    event = ({"hook_event_name": "PostToolUse", "cwd": str(args.root or Path.cwd())}
             if args.publish else json.load(sys.stdin))
    root = (args.root or discover_root(Path(event.get("cwd", ".")))).resolve()
    try:
        message = handle(event, root, args.publish)
    except Exception as exc:
        message = f"HTML 预览 hook 失败：{exc}"
    if message:
        if args.publish:
            print(message)
        elif event["hook_event_name"] == "Stop":
            print(json.dumps({"systemMessage": message}, ensure_ascii=False))
        else:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": event["hook_event_name"], "additionalContext": message,
            }}, ensure_ascii=False))


if __name__ == "__main__":
    main()
