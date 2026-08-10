"""Logger module: rich-powered terminal output + log.txt mirror + Live status panels.

ZH: 基于 rich 的日志模块。替换内置 print()，所有输出同时写入终端（彩色/rich 渲染）
    和 ``generated/log.txt``（纯文本 UTF-8）。提供 log_*() 结构化日志 API 和
    LiveStatus 实时状态面板。

EN: Rich-based logger module. Replaces builtins.print() so all output is mirrored to
    both the terminal (rich-coloured) and ``generated/log.txt`` (plain UTF-8).
    Provides log_*() structured logging and LiveStatus real-time status panels.

Usage / 用法::

    from ..logger import init_logger, shutdown_logger

    def main() -> int:
        init_logger()
        try:
            print("这条同时出现在终端和 log.txt / This appears on both terminal and log.txt")
        finally:
            shutdown_logger()
"""

from __future__ import annotations

import builtins
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

# ── Module-level state ──────────────────────────────────────────────────

_term_console: Console | None = None       # rich Console → terminal (colors + ANSI)
_log_file: TextIO | None = None             # plain-text log file handle
_live: Live | None = None                   # active Live display
_level: str = "INFO"                        # current log level
_initialized: bool = False                  # True after first init_logger()
_original_print = builtins.print            # preserved original builtins.print
_original_stdout = sys.stdout               # preserved original sys.stdout

_LEVELS: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARN": 30,
    "ERROR": 40,
    "FATAL": 50,
}

# ANSI/terminal control regex — stripped from log file output.
# Covers: CSI sequences (cursor/erase), OSC sequences (title), and NUL bytes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")       # CSI: ESC[ ... letter
_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")       # OSC: ESC] ... BEL or ESC\
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # non-printable except \t, \n

# Rich Live display uses cursor-up + erase sequences for panel refresh.
# These are CSI sequences ending in A (cursor up) or J/K (erase).
# Full CSI regex above covers all of them (including rich's hide/show cursor).


def _strip_ansi(text: str) -> str:
    """ZH: 移除 ANSI/终端控制序列和不可打印字符，用于写入纯文本 log 文件。
    EN: Remove ANSI/terminal control sequences and non-printable chars for log file."""
    text = _ANSI_RE.sub("", text)
    text = _OSC_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return text


# ── Tee stream ──────────────────────────────────────────────────────────

class _TeeStream:
    """ZH: file-like 对象，write() 同时输出到终端 rich Console 和 log 文件。
        若 Live 处于活动状态，终端输出通过 Live.console 以正确交错。
    EN: File-like that duplexes write() to the terminal rich Console and log file.
        When Live is active, terminal writes go through Live.console for proper
        interleaving."""

    def write(self, text: str | bytes) -> int:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        n = len(text)

        # ── Terminal output ──
        if _term_console is not None:
            if _live is not None:
                _live.console.out(text, end="")
            else:
                _term_console.out(text, end="")

        # ── Log file (plain text, no ANSI) ──
        if _log_file is not None:
            _log_file.write(_strip_ansi(text))
            _log_file.flush()

        return n

    def flush(self) -> None:
        # Console 无 flush 方法，仅 flush 文件句柄
        if _log_file is not None:
            _log_file.flush()

    def isatty(self) -> bool:
        """ZH: 返回 False 避免某些库尝试使用终端控制序列。
        EN: Return False so callers don't assume terminal control."""
        return False


# ── Replacement builtins.print ──────────────────────────────────────────

def _rich_print(*args, file=None, sep=" ", end="\n", flush=False) -> None:
    """ZH: 全局替换 builtins.print。若提供 file= 参数则走原始 print，
        否则走 tee 同步到终端和 log 文件。
    EN: Global replacement for builtins.print. Passes through to original print
        when file= is given; otherwise tees to both terminal and log file."""
    if file is not None:
        _original_print(*args, file=file, sep=sep, end=end, flush=flush)
        return
    msg = sep.join(str(a) for a in args) + end
    _tee.write(msg)
    if flush:
        _tee.flush()


# ── Log API ─────────────────────────────────────────────────────────────

def _should_log(level: str) -> bool:
    """ZH: 检查给定级别是否满足当前最低日志级别。
    EN: Check whether the given level meets the current minimum log level."""
    return _LEVELS.get(level, 20) >= _LEVELS.get(_level, 20)


def _emit(level: str, *args, sep: str = " ", **kwargs) -> None:
    """ZH: 发送一条带时间戳+级别的日志，同时写入终端和 log 文件。
        终端用 rich 样式着色；log 文件用 `[timestamp] [LEVEL] msg` 格式。

    EN: Emit a timestamped log line to both terminal and log file.
        Terminal: rich-coloured by level.  Log file: ``[ts] [LEVEL] msg``."""
    if not _should_log(level):
        return

    msg = sep.join(str(a) for a in args)

    # Log file format: [2026-07-30 14:30:01.123] [INFO ] message
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S") + f".{now.microsecond // 1000:03d}"
    file_line = f"[{ts}] [{level:<5}] {msg}\n"

    # Terminal: rich-coloured by level with prefix
    style_map = {
        "DEBUG": "dim",
        "INFO": "green",
        "WARN": "yellow",
        "ERROR": "red",
        "FATAL": "bold white on red",
    }
    style = style_map.get(level, "")

    if _term_console is not None:
        prefix = Text(f"[{level}] ", style=style)
        body = Text(msg)
        if _live is not None:
            _live.console.print(prefix + body)
        else:
            _term_console.print(prefix + body)

    # Write to log file (always plain text with timestamp)
    if _log_file is not None:
        _log_file.write(file_line)
        _log_file.flush()


def log_info(*args, sep: str = " ", **kwargs) -> None:
    """ZH: INFO 级别日志。EN: Log at INFO level."""
    _emit("INFO", *args, sep=sep, **kwargs)


def log_warn(*args, sep: str = " ", **kwargs) -> None:
    """ZH: WARN 级别日志。EN: Log at WARN level."""
    _emit("WARN", *args, sep=sep, **kwargs)


def log_error(*args, sep: str = " ", **kwargs) -> None:
    """ZH: ERROR 级别日志。EN: Log at ERROR level."""
    _emit("ERROR", *args, sep=sep, **kwargs)


def log_debug(*args, sep: str = " ", **kwargs) -> None:
    """ZH: DEBUG 级别日志。EN: Log at DEBUG level."""
    _emit("DEBUG", *args, sep=sep, **kwargs)


def log_fatal(*args, sep: str = " ", **kwargs) -> None:
    """ZH: FATAL 级别日志。EN: Log at FATAL level."""
    _emit("FATAL", *args, sep=sep, **kwargs)


def set_level(level: str) -> None:
    """ZH: 运行时修改最低日志级别（DEBUG/INFO/WARN/ERROR/FATAL）。
    EN: Change the minimum log level at runtime."""
    global _level
    upper = level.upper()
    if upper not in _LEVELS:
        print(f"[WARN] 未知日志级别 / Unknown log level: {level!r}, 保持 / keeping {_level}")
        return
    _level = upper


# ── Init / Shutdown ─────────────────────────────────────────────────────

_tee = _TeeStream()  # singleton tee instance


def init_logger(
    log_path: str | Path | None = None,
    *,
    level: str = "INFO",
) -> Path:
    """ZH: 初始化日志系统。替换 builtins.print 和 sys.stdout，打开 log 文件。
        可多次调用——第二次起仅更新日志级别和 Live 引用，不重复创建句柄。

    EN: Initialise the logging system. Replaces builtins.print and sys.stdout,
        opens the log file. Idempotent — subsequent calls only update level/Live.

    Args:
        log_path: log 文件路径；None → 读 TRACK_LOG_PATH 环境变量 → 默认 generated/log.txt
        level: 最低日志级别

    Returns:
        日志文件的 Path
    """
    global _term_console, _log_file, _initialized, _level

    # ── Log file (only on first init) ──
    if not _initialized:
        # Resolve path
        if log_path is None:
            log_path = os.environ.get("TRACK_LOG_PATH", "generated/log.txt")
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        _log_file = open(str(log_path), "w", encoding="utf-8", buffering=1)

        # ZH: 强制 stdout/stderr 走 UTF-8 编码，避免 pipe 环境（conda run 等）下
        #     退化为系统 ANSI 编码（如 CP936）导致中文乱码。
        # EN: Force stdout/stderr to UTF-8 so pipe environments (conda run, etc.)
        #     don't fall back to the system ANSI codepage (e.g. CP936) and mangle CJK text.
        _original_stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

        # ZH: Console 必须绑定原始 stdout，避免 tee 写回自身导致无限递归。
        # EN: Console must bind to the real stdout so tee writes don't loop back.
        _term_console = Console(file=_original_stdout, highlight=False)

        # Replace builtins AFTER creating console (console captures original stdout)
        builtins.print = _rich_print
        sys.stdout = _tee  # type: ignore[assignment]

        _initialized = True

    # Always update level
    upper = level.upper()
    if upper in _LEVELS:
        _level = upper

    return log_path if isinstance(log_path, Path) else Path(str(log_path))


def shutdown_logger() -> None:
    """ZH: 关闭日志系统：停止 Live、flush 日志、恢复原始 print/stdout、关闭文件。
    EN: Shut down the logging system: stop Live, flush log, restore print/stdout,
        close file handle."""
    global _initialized, _live

    # Stop live display if active
    if _live is not None:
        _live.stop()
        _live = None

    # Flush file buffer
    if _log_file is not None:
        _log_file.flush()

    # Restore originals
    builtins.print = _original_print
    sys.stdout = _original_stdout

    # Close log file
    if _log_file is not None:
        _log_file.close()
        # _log_file = None  -- keep reference for diagnostics

    _initialized = False


def flush_logger() -> None:
    """ZH: 强制刷新 log 文件缓冲。EN: Force-flush the log file buffer."""
    if _log_file is not None:
        _log_file.flush()


# ── Live status panel ───────────────────────────────────────────────────

class LiveStatus:
    """ZH: 预构建的流水线段实时状态面板。封装 rich.live.Live，显示阶段名、进度条、
        计数、耗时和吞吐速率。若不传 progress，则仅显示统计信息而无进度条，
        适合总量未知的流式处理场景。

    EN: Pre-built pipeline-stage live status panel. Wraps rich.live.Live
        to show stage name, progress bar, counts, elapsed time, and throughput.
        When progress is never set, shows stats-only mode for streaming with
        unknown total counts.

    Usage / 用法::

        # 有进度条 / With progress bar
        status = LiveStatus("清洗 / Cleaning")
        status.start()
        for batch in batches:
            status.update(progress=0.52, detail="1,203 / 2,314 车辆", elapsed=45.2)
        status.stop()

        # 无进度条（流式，总量未知）/ Stats-only (streaming, unknown total)
        status = LiveStatus("清洗 / Cleaning")
        status.start()
        for vehicle in stream:
            status.update(detail=f"{n:,} 车辆, {pts:,} 点", elapsed=t, throughput=f"{rate:.1f} 车辆/s")
        status.stop()
    """

    def __init__(self, stage_name: str) -> None:
        """ZH: 创建 LiveStatus 面板。
        EN: Create a LiveStatus panel.

        Args:
            stage_name: 阶段名称（建议中英双语，如 "清洗 / Cleaning"）
        """
        self._stage_name = stage_name
        self._progress: float | None = None       # None = no progress bar
        self._detail: str = ""
        self._sub_detail: str = ""                # nested/secondary progress line
        self._elapsed: float = 0.0
        self._throughput: str = ""
        self._live: Live | None = None

    def update(
        self,
        progress: float | None = None,
        detail: str = "",
        sub_detail: str = "",
        elapsed: float | None = None,
        throughput: str = "",
        stage: str | None = None,
    ) -> None:
        """ZH: 更新面板内容。progress=None 时不显示进度条。
        EN: Update the panel content. When progress is None, hide the progress bar.

        Args:
            progress: 进度 0.0–1.0，None 表示隐藏进度条
            detail: 计数描述，如 "1,203 / 2,314 车辆"。空字符串不覆盖已有值。
            sub_detail: 嵌套进度行（如窗口内的跨车求交进度）。
                       空字符串会清除该行——与 detail 不同。
            elapsed: 已用秒数
            throughput: 吞吐描述，如 "26.6 车辆/s"
            stage: 动态阶段名；非 None 时覆盖 __init__ 的 stage_name
        """
        if progress is not None:
            self._progress = max(0.0, min(1.0, progress))
        if detail:
            self._detail = detail
        # sub_detail: always assign — empty string clears the row
        self._sub_detail = sub_detail
        if elapsed is not None:
            self._elapsed = elapsed
        if throughput:
            self._throughput = throughput
        if stage is not None:
            self._stage_name = stage

        if self._live is not None:
            self._live.update(self._render())

    def start(self) -> None:
        """ZH: 启动 Live 面板刷新（refresh_per_second=4）。
        EN: Start the live panel rendering (refresh_per_second=4)."""
        global _live
        if _term_console is None:
            return
        self._live = Live(
            self._render(),
            console=_term_console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.start()
        _live = self._live

    def stop(self) -> None:
        """ZH: 停止 Live 面板，恢复普通终端输出。
        EN: Stop the live panel and restore normal terminal output."""
        global _live
        if self._live is not None:
            self._live.stop()
            self._live = None
        _live = None

    def _render(self):
        """ZH: 渲染当前状态的 rich renderable。
        EN: Render the current state as a rich renderable."""
        table = Table.grid(padding=(0, 2))
        table.add_column(justify="left")

        # Stage name header
        table.add_row(Text(f"[{self._stage_name}]", style="bold cyan"))

        # Progress bar row (only if progress was explicitly set)
        if self._progress is not None:
            progress = Progress(
                TextColumn("[bold]{task.description}"),
                BarColumn(bar_width=40, complete_style="green", finished_style="green"),
                TextColumn("{task.percentage:>3.0f}%"),
                expand=False,
            )
            progress.add_task("", total=100, completed=self._progress * 100)
            table.add_row(progress)

        # Stats row
        parts = []
        if self._detail:
            parts.append(self._detail)
        if self._elapsed > 0:
            parts.append(f"T: {self._elapsed:.1f}s")
        if self._throughput:
            parts.append(f"> {self._throughput}")
        if parts:
            table.add_row(Text("  |  ".join(parts), style="dim"))

        # Sub-detail row (nested progress, e.g. cross-track inside a spatial window)
        if self._sub_detail:
            table.add_row(Text(self._sub_detail, style="dim italic"))

        return Panel(
            table,
            border_style="bright_black",
            padding=(1, 2),
        )
