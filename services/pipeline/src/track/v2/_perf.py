"""Lightweight performance timing framework for the v2 intersect2 pipeline.

Private helper module — mirrors ``_geometry.py`` style (underscore-private
internals, bilingual docstrings). Provides three zero-friction timing hooks:

  - ``@timed``        function-level auto-timing decorator
  - ``TimedStage``    context-manager block timing (``with TimedStage("x"):``)
  - ``timed_iter``    per-iteration traversal timing

All timers feed one hierarchical tree; ``PerfTracker.report()`` renders a
nested breakdown with per-node totals, averages, min/max and parent shares.

ZH: v2 intersect2 流水线的轻量性能计时框架。私有辅助模块，风格对齐
    ``_geometry.py``。提供三个零摩擦计时入口：函数装饰器 ``@timed``、
    上下文管理器 ``TimedStage``、逐迭代遍历钩子 ``timed_iter``。所有计时
    汇总到同一棵层级树，``PerfTracker.report()`` 输出嵌套占比报告。

迭代深度限制 (iteration-depth limit):
  - depth 0 = stages (``TimedStage`` / ``PerfTracker.stage``)
  - depth 1 = first ``timed_iter`` nesting
  - depth 2 = second ``timed_iter`` nesting
  - depth >= 3 → ``timed_iter`` 退化为原 iterable 直通（零开销）
  - at the depth-1 node, one reminder is logged when deeper levels are skipped.
"""

from __future__ import annotations

import ast
import functools
import inspect
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_DEFAULT_MAX_ITER_DEPTH = 3
# 内部迭代深度上限 / max tracked iteration depth


def _fmt_time(seconds: float) -> str:
    """Format seconds into a compact human-readable duration.

    Uses ASCII-only unit suffixes (``us`` for microseconds) so the report
    survives GBK/Windows consoles that cannot encode U+00B5.

    ZH: 把秒数格式化为紧凑可读的时长（ns/us/ms/s）。单位后缀保持纯 ASCII，
        避免在 GBK/Windows 控制台（无法编码 µ）上报错。
    EN: Format seconds into a compact readable duration (ns/us/ms/s).
    """
    if seconds < 1e-6:
        return f"{seconds * 1e9:.0f}ns"
    if seconds < 1e-3:
        return f"{seconds * 1e6:.1f}us"
    if seconds < 1.0:
        return f"{seconds * 1e3:.1f}ms"
    return f"{seconds:.3f}s"


@dataclass
class _PerfNode:
    """One node in the hierarchical timing tree.

    Children merge by name — repeated same-name sibling blocks aggregate
    ``call_count`` / ``total_elapsed`` / ``min`` / ``max``.

    ZH: 计时树单节点。children 按 name 合并——同名兄弟块会聚合
        call_count / total_elapsed / min / max。
    """

    name: str
    depth: int
    total_elapsed: float = 0.0
    call_count: int = 0
    min_elapsed: float = float("inf")
    max_elapsed: float = 0.0
    total_items: int | None = None      # timed_iter 的 total 提示 / expected item count
    skipped_depth: int | None = None    # 最深被跳过的迭代深度 / deepest skipped depth
    children: dict[str, _PerfNode] = field(default_factory=dict)


class PerfTracker:
    """Hierarchical performance tracker for one pipeline run.

    ZH: 单次流水线运行的层级性能追踪器。内部维护节点栈与迭代深度计数，
        线程安全不做要求（单线程流水线）。
    EN: Hierarchical perf tracker for one pipeline run. Maintains an internal
        node stack and iteration-depth counter. Thread-safety not required.
    """

    def __init__(self, name: str = "intersect2") -> None:
        self.name = name
        self._root = _PerfNode(name=name, depth=-1)
        self._stack: list[_PerfNode] = []
        self._iter_depth = 0

    # ── internals ──

    def _begin(self, name: str, depth: int) -> _PerfNode:
        parent = self._stack[-1] if self._stack else self._root
        node = parent.children.get(name)
        if node is None:
            node = _PerfNode(name=name, depth=depth)
            parent.children[name] = node
        self._stack.append(node)
        return node

    def _accumulate(self, node: _PerfNode, elapsed: float) -> None:
        node.total_elapsed += elapsed
        node.call_count += 1
        if elapsed < node.min_elapsed:
            node.min_elapsed = elapsed
        if elapsed > node.max_elapsed:
            node.max_elapsed = elapsed

    def _leave(self, node: _PerfNode, *, is_iter: bool) -> None:
        if is_iter and self._iter_depth > 0:
            self._iter_depth -= 1
        if self._stack and self._stack[-1] is node:
            self._stack.pop()

    def _note_skip(self, own_depth: int) -> None:
        """Log one depth-limit reminder at the depth-1 node (first timed_iter).

        ZH: 在 depth-1 节点（第一个 timed_iter）上记录一次深度超限提醒。
        EN: Record a one-time depth-limit reminder on the depth-1 node.
        """
        depth1 = next((n for n in self._stack if n.depth == 1), None)
        if depth1 is None or depth1.skipped_depth is not None:
            return
        depth1.skipped_depth = own_depth
        print(
            f"[perf] {depth1.name}: 内部迭代深度 {own_depth} 层，"
            f"默认追踪 {_DEFAULT_MAX_ITER_DEPTH} 层，更深层级未记录",
            file=sys.stderr,
        )

    # ── public interfaces ──

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time a named block; stages sit at iteration-depth 0.

        ZH: 对命名代码块计时；stage 位于迭代深度 0。
        EN: Time a named block; stages sit at iteration-depth 0.
        """
        node = self._begin(name, depth=self._iter_depth)
        start = time.perf_counter()
        try:
            yield
        finally:
            self._accumulate(node, time.perf_counter() - start)
            self._leave(node, is_iter=False)

    def timed_iter(
        self,
        iterable: Iterable[T],
        *,
        name: str,
        total: int | None = None,
        track: bool = True,
    ) -> Iterable[T]:
        """Time each iteration of ``iterable``; auto-passthrough past depth limit.

        ZH: 对 iterable 的每次迭代计时。``track=False`` 完全跳过（零开销直通）；
            迭代深度 > 3 时自动退化为原 iterable（零开销直通）。``total``
            可提示预期条目数，用于报告展示。
        EN: Time each iteration of ``iterable``. ``track=False`` skips entirely
            (zero-overhead passthrough); depth > 3 auto-degenerates to the raw
            iterable (zero overhead). ``total`` hints the expected item count.
        """
        own_depth = self._iter_depth + 1
        if not track or own_depth > _DEFAULT_MAX_ITER_DEPTH:
            if track:
                self._note_skip(own_depth)
            return iterable
        node = self._begin(name, depth=own_depth)
        if node.total_items is None and total is not None:
            node.total_items = total
        self._iter_depth += 1
        return self._iter_timed(iterable, node)

    def _iter_timed(self, iterable: Iterable[T], node: _PerfNode) -> Iterator[T]:
        start = time.perf_counter()
        try:
            for item in iterable:
                yield item
                elapsed = time.perf_counter() - start
                start = time.perf_counter()
                self._accumulate(node, elapsed)
        finally:
            self._leave(node, is_iter=True)

    # ── reporting ──

    def report(self) -> str:
        """Render the hierarchical timing breakdown as a string.

        ZH: 将层级计时树渲染为字符串：每节点展示 total/calls/avg/min/max
            及占父节点百分比。
        EN: Render the hierarchical timing tree: per-node total/calls/avg/
            min/max and % of parent.
        """
        lines: list[str] = []
        top = list(self._root.children.values())
        root_total = sum(child.total_elapsed for child in top)
        lines.append(f"[perf] {self.name}  {_fmt_time(root_total)}")
        for child in sorted(top, key=lambda n: n.name):
            self._render_node(child, lines, parent_total=root_total, indent=2)
        return "\n".join(lines)

    def _render_node(
        self,
        node: _PerfNode,
        lines: list[str],
        parent_total: float,
        indent: int,
    ) -> None:
        pad = " " * indent
        total = node.total_elapsed
        avg = total / node.call_count if node.call_count else 0.0
        pct = (total / parent_total * 100.0) if parent_total > 0 else 0.0
        min_s = _fmt_time(node.min_elapsed) if node.call_count else "-"
        max_s = _fmt_time(node.max_elapsed) if node.call_count else "-"
        line = (
            f"{pad}{node.name}: total={_fmt_time(total)} "
            f"calls={node.call_count} avg={_fmt_time(avg)} "
            f"min={min_s} max={max_s} ({pct:.1f}% of parent)"
        )
        if node.total_items is not None:
            line += f" items={node.total_items}"
        if node.skipped_depth is not None:
            line += " [深层迭代未记录]"
        lines.append(line)
        for child in sorted(node.children.values(), key=lambda n: n.name):
            self._render_node(child, lines, parent_total=total, indent=indent + 2)


# ── Module-level sugar ────────────────────────────────────────────────
# 默认 tracker 使用 thread-local，单线程流水线也保持隔离 / default tracker
# is thread-local so each thread keeps an isolated tree.

_default_local = threading.local()


def _default_tracker() -> PerfTracker:
    """Return this thread's default PerfTracker (created lazily)."""
    tracker = getattr(_default_local, "tracker", None)
    if tracker is None:
        tracker = _default_local.tracker = PerfTracker()
    return tracker


@contextmanager
def TimedStage(
    name: str, *, tracker: PerfTracker | None = None
) -> Iterator[None]:
    """Time a block via the default tracker (or an explicit one).

    ZH: 便捷上下文管理器——对代码块计时。默认用线程本地 tracker，
        也可传入显式 tracker。
    EN: Convenience context manager — times a block. Uses the thread-local
        default tracker unless an explicit one is given.
    """
    tr = tracker if tracker is not None else _default_tracker()
    with tr.stage(name):
        yield


def timed_iter(
    iterable: Iterable[T],
    *,
    name: str,
    total: int | None = None,
    track: bool = True,
    tracker: PerfTracker | None = None,
) -> Iterable[T]:
    """Per-iteration timing on the default tracker (or an explicit one).

    ZH: 基于默认 tracker（或显式 tracker）的逐迭代计时便捷函数。
    EN: Per-iteration timing convenience on the default (or explicit) tracker.
    """
    tr = tracker if tracker is not None else _default_tracker()
    return tr.timed_iter(iterable, name=name, total=total, track=track)


def timed(
    func: Callable | None = None, *, tracker: PerfTracker | None = None
) -> Callable:
    """Function-level auto-timing decorator.

    Usable as bare ``@timed`` or ``@timed(tracker=my_tracker)``.

    ZH: 函数级自动计时装饰器。支持裸 ``@timed`` 或 ``@timed(tracker=t)``。
    EN: Function-level auto-timing decorator. Supports both ``@timed`` and
        ``@timed(tracker=t)``.
    """

    def _wrap(f: Callable) -> Callable:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tr = tracker if tracker is not None else _default_tracker()
            with tr.stage(f.__qualname__):
                return f(*args, **kwargs)

        return wrapper

    if func is not None:
        return _wrap(func)
    return _wrap


# ═══════════════════════════════════════════════════════════════════════════
# AST-level for-loop injection — zero-invasion iteration timing
# ═══════════════════════════════════════════════════════════════════════════
# ZH: AST 级 for 循环注入——零侵入迭代计时。``@track_for_loops`` 装饰器解析
#     函数源码，将匹配的 ``For`` 节点的 ``iter`` 替换为 ``_track_iter(...)``
#     调用，编译新 code object 并返回。不是猴子补丁——不替换任何全局对象。
# EN: AST-level for-loop injection — zero-invasion iteration timing.
#     ``@track_for_loops`` parses the function source, replaces matching
#     ``For.iter`` with ``_track_iter(...)`` calls, compiles a new code
#     object, and returns it. Not monkey-patching — no global objects replaced.


def _track_iter(
    perf: PerfTracker | None,
    iterable: Iterable[T],
    *,
    name: str,
    total: int | None = None,
) -> Iterable[T]:
    """AST-injected iteration timer — zero-overhead when *perf* is None.

    Called by code transformed via ``@track_for_loops``.  If *perf* is
    ``None`` (not profiling), returns *iterable* directly with zero overhead.
    Otherwise delegates to ``perf.timed_iter()``.

    ZH: AST 注入的迭代计时器。*perf* 为 None 时零开销直通原 iterable；
        否则委托给 ``perf.timed_iter()``。由 ``@track_for_loops`` 转换后
        的代码调用。
    """
    if perf is None:
        return iterable
    return perf.timed_iter(iterable, name=name, total=total)


def _is_track_for_loops_call(node: ast.expr) -> bool:
    """Check whether an AST expression node is a ``track_for_loops(...)`` call."""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "track_for_loops":
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr == "track_for_loops":
            return True
    return False


class _ForIterTransformer(ast.NodeTransformer):
    """Replace matching For-loop iterables with ``_track_iter(...)`` calls.

    ZH: 将匹配的 For 循环 iter 替换为 ``_track_iter(...)`` 调用。
        按 AST 嵌套深度计数，超过 max_depth 的循环不转换。
    """

    def __init__(
        self,
        patterns: dict[str, str],
        max_depth: int,
        track_iter_name: str = "_track_iter",
    ) -> None:
        self._patterns = patterns      # {source_fragment: node_name}
        self._max_depth = max_depth
        self._track_iter = track_iter_name
        self._current_depth = 0
        self._matched: set[str] = set()   # 每个 pattern 最多匹配一次

    def visit_For(self, node: ast.For) -> ast.For:
        self._current_depth += 1

        if self._current_depth <= self._max_depth:
            iter_src = ast.unparse(node.iter)
            for pattern, name in self._patterns.items():
                if pattern not in self._matched and pattern in iter_src:
                    self._matched.add(pattern)
                    node.iter = ast.Call(
                        func=ast.Name(id=self._track_iter, ctx=ast.Load()),
                        args=[
                            ast.Name(id="perf", ctx=ast.Load()),
                            node.iter,
                        ],
                        keywords=[
                            ast.keyword(arg="name", value=ast.Constant(value=name)),
                        ],
                    )
                    break  # 一个 For 节点只匹配第一个 pattern

        node = self.generic_visit(node)
        self._current_depth -= 1
        return node


def track_for_loops(
    patterns: dict[str, str],
    *,
    depth: int = _DEFAULT_MAX_ITER_DEPTH,
) -> Callable:
    """Decorator: auto-inject ``_track_iter`` calls into matching for-loops.

    Instead of manually wrapping every for-loop::

        for vid, track in _perf_iter(perf, tracks.items(), name="tracks_turn"):

    you write clean loops and declare the patterns once::

        @track_for_loops({"tracks.items": "tracks_turn"}, depth=3)
        def my_func(..., perf=None):
            for vid, track in tracks.items():   # ← auto-wrapped by AST injection
                ...

    **How it works**

    1.  ``inspect.getsource(func)`` → raw source
    2.  ``ast.parse(source)`` → AST tree
    3.  Strip ``@track_for_loops(...)`` from the FunctionDef decorator list
        (otherwise exec would re-apply it, causing infinite recursion)
    4.  ``_ForIterTransformer`` walks the AST, replacing matching For nodes
    5.  ``compile()`` + ``exec()`` into a namespace with ``_track_iter`` injected
    6.  Return the new function from the namespace

    This is **not monkey-patching** — no global objects are replaced.
    Each decorated function gets its own transformed code object.

    **Pattern matching**

    *patterns* keys are matched via ``in`` against ``ast.unparse(node.iter)``.
    First match wins; each key matches at most once.

    ZH: 装饰器——自动往匹配的 for 循环注入 ``_track_iter`` 调用。
        不是猴子补丁——不替换任何全局对象，每个函数独立转换。

    Parameters
    ----------
    patterns : dict[str, str]
        ``{source_fragment: node_name}``.  *source_fragment* 是 for 循环
        iter 表达式源码的子串（如 ``"tracks.items"`` 匹配 ``tracks.items()``）。
    depth : int
        最大转换深度（默认 3，即 depth 1/2/3 的 For 循环被转换，
        depth >= 4 的保持原样）。
    """

    def _decorator(func: Callable) -> Callable:
        try:
            source = inspect.getsource(func)
        except OSError:
            return func  # 拿不到源码（交互环境等），静默跳过

        tree = ast.parse(source)

        # ── 1. Strip @track_for_loops decorators ──
        #    exec 时会重新执行装饰器 → 无限递归, 必须清除
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = [
                    d for d in node.decorator_list
                    if not _is_track_for_loops_call(d)
                ]

        # ── 2. Transform For nodes ──
        transformer = _ForIterTransformer(patterns, max_depth=depth)
        new_tree = transformer.visit(tree)
        ast.fix_missing_locations(new_tree)

        # ── 3. Compile + exec with _track_iter injected ──
        namespace: dict[str, Any] = dict(func.__globals__)
        namespace["_track_iter"] = _track_iter
        code = compile(new_tree, func.__code__.co_filename, "exec")
        exec(code, namespace)

        new_func: Callable = namespace[func.__name__]
        new_func.__module__ = func.__module__
        new_func.__qualname__ = func.__qualname__
        new_func.__doc__ = func.__doc__
        new_func.__wrapped__ = func  # 保留原始引用，方便调试

        return new_func

    return _decorator
