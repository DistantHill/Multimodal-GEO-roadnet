"""薄壳测试脚本共享 helper —— 统一默认连接到 track_test 数据库。

ZH: 所有 manual_bench_*/manual_test_* 脚本通过此模块获取数据库连接参数。
    默认数据库为 ``track_test``（由 ``manual_setup_test_db.py`` 创建），
    而不是 .env 中的生产库。如需临时切回生产库，设环境变量
    ``TRACK_BENCH_DB=postgres``。

EN: Shared helper for thin benchmark/test scripts. All manual_bench_*/manual_test_*
    scripts get DB connection params through this module. Default database is
    ``track_test`` (created by ``manual_setup_test_db.py``), not the production
    database in .env. Override via ``TRACK_BENCH_DB=postgres`` env var.

Usage::

    from _thin_bench_helpers import connect_test_db, load_test_env, TEST_DB_NAME

    env = load_test_env()
    conn = connect_test_db()
"""

from __future__ import annotations

import os
import sys

import psycopg

# ── 确保项目根目录在 sys.path 中 ────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ═══════════════════════════════════════════════════════════════════════════
# 默认测试数据库名
# ZH: 可通过环境变量 TRACK_BENCH_DB 覆盖（如临时切回 postgres 库查数据）
# ═══════════════════════════════════════════════════════════════════════════
TEST_DB_NAME: str = os.environ.get("TRACK_BENCH_DB", "track_test")


def _find_env_path() -> str:
    """自动定位 track/v2/.env 文件。"""
    # _thin_bench_helpers.py 在 manual_thin_benchmark/ 目录下
    bench_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(bench_dir, "..", ".env")
    if not os.path.isfile(env_path):
        raise FileNotFoundError(
            f".env not found at {env_path}. "
            f"Ensure track/v2/.env exists with PostgreSQL connection params."
        )
    return env_path


def load_test_env(env_path: str | None = None) -> dict[str, str]:
    """加载 .env 连接参数，但将 database 覆盖为 TEST_DB_NAME。

    ZH: 返回的字典中 ``TRACK_PG_DATABASE`` 固定为 ``track_test``（或被
        TRACK_BENCH_DB 覆盖），其余参数（host/port/user/password）来自 .env。
    """
    if env_path is None:
        env_path = _find_env_path()

    env: dict[str, str] = {}
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    # ── 覆盖：强制使用测试库 ──
    env["TRACK_PG_DATABASE"] = TEST_DB_NAME
    return env


def connect_test_db() -> psycopg.Connection:
    """连接到测试数据库的快捷方法。

    ZH: 等价于 ``psycopg.connect(load_test_env() 构造的 DSN)``。
    """
    env = load_test_env()
    return psycopg.connect(
        host=env["TRACK_PG_HOST"],
        port=env["TRACK_PG_PORT"],
        dbname=env["TRACK_PG_DATABASE"],
        user=env["TRACK_PG_USER"],
        password=env["TRACK_PG_PASSWORD"],
        connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
    )
