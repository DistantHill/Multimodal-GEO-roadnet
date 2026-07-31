"""薄壳脚本：创建 track_test 独立测试数据库环境。

ZH: 在本地 PostgreSQL 实例上创建 ``track_test`` 数据库，然后调用
    ``init_db`` 的建表逻辑初始化全部流水线数据表（含索引和 PostGIS 扩展）。
    测试库完全独立于生产库，可随时 DROP 重建。

EN: Creates a ``track_test`` database on the local PostgreSQL instance, then
    runs ``init_db`` table creation (DDL + indexes + PostGIS extension).
    Fully isolated — can be DROP'd and recreated at any time.

用法 / Usage::

    python track/v2/manual_thin_benchmark/manual_setup_test_db.py
    python track/v2/manual_thin_benchmark/manual_setup_test_db.py --reset

PostgreSQL 层级 / PostgreSQL Hierarchy::

    Instance (localhost:5432)
    ├── Database "postgres"        ← 系统维护库
    ├── Database "track_test"      ← ★ 测试环境（本脚本创建）
    │   └── Schema "public"
    │       ├── trajectory_points
    │       ├── turn_events
    │       ├── candidate_intersections
    │       ├── intersections
    │       └── ... (共 12 张表 + 索引)
    └── Role "postgres"            ← 连接用户
"""

from __future__ import annotations

import os
import sys

# 确保项目根目录在 sys.path 中，免去 pip install -e .
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import psycopg

# ── 复用 init_db 的内部函数（不通过 CLI argparse，直接调用） ──
from track.v2.cli.init_db import create_tables, reset_tables
from track.v2.config import PostgresConfig

# ── 测试数据库名从共享 helper 获取（统一入口） ──
from track.v2.manual_thin_benchmark._thin_bench_helpers import load_test_env, TEST_DB_NAME


def _env_config(database: str) -> PostgresConfig:
    """用 .env 参数 + 指定 database 名构造 PostgresConfig。"""
    env = load_test_env()
    return PostgresConfig(
        host=env.get("TRACK_PG_HOST", "127.0.0.1"),
        port=int(env.get("TRACK_PG_PORT", "5432")),
        database=database,
        user=env.get("TRACK_PG_USER", "postgres"),
        password=env.get("TRACK_PG_PASSWORD", ""),
        connect_timeout=int(env.get("TRACK_PG_CONNECT_TIMEOUT", "10")),
        sslmode=env.get("TRACK_PG_SSLMODE") or None,
    )


def _connect_admin_db() -> psycopg.Connection:
    """连接到 postgres 维护库（用于 CREATE/DROP DATABASE）。

    ZH: CREATE DATABASE 不能在目标库中执行，必须连到其他库（如 postgres）。
    """
    cfg = _env_config("postgres")
    return psycopg.connect(cfg.dsn())


def database_exists(name: str) -> bool:
    """检查指定名称的 database 是否已存在。"""
    conn = _connect_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def create_database(name: str) -> bool:
    """创建数据库（如果不存在）。返回 True 表示新建，False 表示已存在。"""
    if database_exists(name):
        print(f"  [SKIP] 数据库已存在 / Database exists: {name}")
        return False

    conn = _connect_admin_db()
    conn.autocommit = True  # CREATE DATABASE 必须在 autocommit 模式下
    try:
        with conn.cursor() as cur:
            # 用 quote_ident 防止注入（虽然 name 是硬编码常量）
            cur.execute(f"CREATE DATABASE {name} ENCODING 'UTF8'")
            print(f"  [OK] 数据库已创建 / Created database: {name}")
    finally:
        conn.close()
    return True


def drop_database(name: str) -> bool:
    """删除数据库（如果存在）。用于 --full-reset 模式。"""
    if not database_exists(name):
        print(f"  [SKIP] 数据库不存在 / Database does not exist: {name}")
        return False

    conn = _connect_admin_db()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # 先踢掉所有连接到目标库的会话
            cur.execute("""
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = %s
                  AND pid <> pg_backend_pid()
            """, (name,))
            cur.execute(f"DROP DATABASE {name}")
            print(f"  [OK] 数据库已删除 / Dropped database: {name}")
    finally:
        conn.close()
    return True


def install_postgis(config: PostgresConfig) -> None:
    """在目标数据库中启用 PostGIS 扩展。

    ZH: CREATE EXTENSION 必须在 autocommit 模式下执行（不能包在事务里）。
        先检查是否已安装，避免重复操作。
    """
    conn = psycopg.connect(config.dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # 先检查是否已安装
            cur.execute("SELECT extname FROM pg_extension WHERE extname = 'postgis'")
            if cur.fetchone():
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
                ver = cur.fetchone()[0]
                print(f"  [SKIP] PostGIS v{ver} 已安装 / already installed")
            else:
                cur.execute("CREATE EXTENSION postgis")
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
                ver = cur.fetchone()[0]
                print(f"  [OK] PostGIS v{ver} 安装成功 / installed")
    finally:
        conn.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="track.v2 测试数据库环境初始化 / Test DB environment setup"
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="先 DROP 全部表再重建（不删库）/ Drop all tables then recreate (keep database)."
    )
    parser.add_argument(
        "--full-reset", action="store_true",
        help="DROP 整个数据库再完全重建 / Drop the entire database and recreate from scratch."
    )
    parser.add_argument(
        "--db-name", default=TEST_DB_NAME,
        help=f"测试数据库名称（默认: {TEST_DB_NAME}）/ Test database name."
    )
    args = parser.parse_args()
    db_name: str = args.db_name

    env = load_test_env()
    host = env.get("TRACK_PG_HOST", "127.0.0.1")
    port = env.get("TRACK_PG_PORT", "5432")

    print("=" * 66)
    print("  track.v2 测试环境初始化 / Test Environment Setup")
    print(f"  实例 / Instance:  {host}:{port}")
    print(f"  测试库 / Test DB:  {db_name}")
    print("=" * 66)

    # ── Step 0: 完全重置模式 —— 删库重来 ──
    if args.full_reset:
        print("\n── [Step 0] 完全重置 / Full Reset ──")
        drop_database(db_name)

    # ── Step 1: 创建数据库 ──
    print("\n── [Step 1] 创建数据库 / Create Database ──")
    create_database(db_name)

    # ── Step 2: 安装 PostGIS ──
    print("\n── [Step 2] 安装 PostGIS 扩展 / Install PostGIS ──")
    test_cfg = _env_config(db_name)
    install_postgis(test_cfg)

    # ── Step 3: 建表 + 索引（复用 init_db 内部逻辑） ──
    if args.reset:
        print("\n── [Step 3a] 重置表 / Reset Tables ──")
        reset_failures = reset_tables(test_cfg)
        if reset_failures > 0:
            print("[FAIL] 重置失败 / Reset failed, aborting.")
            return 1

    print("\n── [Step 3b] 创建表和索引 / Create Tables & Indexes ──")
    failures = create_tables(test_cfg)
    if failures:
        print(f"\n[FAIL] {failures} 项失败 / failures.")
        return 1

    # ── 验证 ──
    print("\n── 验证 / Verification ──")
    conn = psycopg.connect(test_cfg.dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)
            tables = [row[0] for row in cur.fetchall()]
            print(f"  表 / Tables ({len(tables)}):")
            for t in tables:
                print(f"    - {t}")
    finally:
        conn.close()

    print(f"\n{'=' * 66}")
    print(f"  [OK] 测试环境就绪 / Test environment ready: {test_cfg.safe_dsn()}")
    print(f"  连接字符串 / DSN: {test_cfg.safe_dsn()}")
    print(f"{'=' * 66}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
