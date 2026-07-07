"""Initialize the track.v2 PostgreSQL database schema.

ZH: 连接到已部署的 PostgreSQL 数据库，创建流水线所需的全部数据表。
通过 --ask-password 在 ps/CMD 窗口中交互式输入密码。

EN: Connect to the deployed PostgreSQL database and create all pipeline tables.
Use --ask-password for interactive password entry in ps/CMD windows.

Usage / 用法::

    python -m track.v2.cli.init_db --ask-password
    python -m track.v2.cli.init_db --host 127.0.0.1 --port 5432 --database postgres --user postgres --ask-password
"""

from __future__ import annotations

import argparse
import getpass
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus


# ── Minimal PostgreSQL config & connection (self-contained; no io.postgres dependency) ──

@dataclass(frozen=True)
class _PostgresConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "postgres"
    user: str = "postgres"
    password: str = ""
    connect_timeout: int = 10
    sslmode: str | None = None

    def dsn(self) -> str:
        auth = quote_plus(self.user)
        if self.password:
            auth += f":{quote_plus(self.password)}"
        host = quote_plus(self.host)
        database = quote_plus(self.database)
        query = f"connect_timeout={self.connect_timeout}"
        if self.sslmode:
            query += f"&sslmode={quote_plus(self.sslmode)}"
        return f"postgresql://{auth}@{host}:{self.port}/{database}?{query}"

    def safe_dsn(self) -> str:
        auth = quote_plus(self.user)
        if self.password:
            auth += ":***"
        host = quote_plus(self.host)
        database = quote_plus(self.database)
        query = f"connect_timeout={self.connect_timeout}"
        if self.sslmode:
            query += f"&sslmode={quote_plus(self.sslmode)}"
        return f"postgresql://{auth}@{host}:{self.port}/{database}?{query}"


def _connect(config: _PostgresConfig):
    """Open a PostgreSQL connection."""
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg. Install with: pip install 'psycopg[binary]'"
        ) from exc
    return psycopg.connect(config.dsn())

# ── Default connection parameters (from project CLAUDE.md) ──────────
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5432
DEFAULT_DATABASE = "postgres"
DEFAULT_USER = "postgres"

# ── DDL statements in pipeline dependency order ─────────────────────
# ZH: 按流水线阶段顺序排列，先创建被引用表，再创建引用表。
# EN: Ordered by pipeline stage and FK dependency — referenced tables first.

DDL_STATEMENTS: list[tuple[str, str]] = [
    # ── Parser 0 ──
    ("trajectory_points", """
        CREATE TABLE IF NOT EXISTS trajectory_points (
            id              BIGSERIAL PRIMARY KEY,
            source_id       TEXT NOT NULL,
            source_row_key  TEXT NOT NULL,
            stable          BOOLEAN NOT NULL DEFAULT TRUE,
            vehicle_id      TEXT NOT NULL,
            timestamp       DOUBLE PRECISION NOT NULL,
            lon             DOUBLE PRECISION NOT NULL,
            lat             DOUBLE PRECISION NOT NULL,
            heading         DOUBLE PRECISION NOT NULL DEFAULT 0,
            speed           DOUBLE PRECISION NOT NULL DEFAULT 0,
            extra           JSONB NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(source_id, source_row_key)
        )
    """),
    # ── Cleaner 1 ──
    ("excluded_points", """
        CREATE TABLE IF NOT EXISTS excluded_points (
            id              BIGSERIAL PRIMARY KEY,
            source_id       TEXT NOT NULL,
            source_row_key  TEXT NOT NULL,
            reason          TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("injected_points", """
        CREATE TABLE IF NOT EXISTS injected_points (
            id                  BIGSERIAL PRIMARY KEY,
            vehicle_id          TEXT NOT NULL,
            timestamp           DOUBLE PRECISION NOT NULL,
            lon                 DOUBLE PRECISION NOT NULL,
            lat                 DOUBLE PRECISION NOT NULL,
            x                   DOUBLE PRECISION NOT NULL,
            y                   DOUBLE PRECISION NOT NULL,
            heading             DOUBLE PRECISION NOT NULL,
            speed               DOUBLE PRECISION NOT NULL DEFAULT 0,
            from_source_id      TEXT NOT NULL,
            from_source_row_key TEXT NOT NULL,
            to_source_id        TEXT NOT NULL,
            to_source_row_key   TEXT NOT NULL,
            generated_reason    TEXT NOT NULL DEFAULT 'orthogonal_heading',
            origin_lon          DOUBLE PRECISION NOT NULL,
            origin_lat          DOUBLE PRECISION NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    # ── Intersect2 ──
    ("turn_events", """
        CREATE TABLE IF NOT EXISTS turn_events (
            id                  BIGSERIAL PRIMARY KEY,
            event_id            INTEGER NOT NULL,
            vehicle_id          TEXT NOT NULL,
            timestamp           DOUBLE PRECISION NOT NULL,
            lon                 DOUBLE PRECISION NOT NULL,
            lat                 DOUBLE PRECISION NOT NULL,
            x                   DOUBLE PRECISION NOT NULL,
            y                   DOUBLE PRECISION NOT NULL,
            in_bearing          DOUBLE PRECISION NOT NULL,
            out_bearing         DOUBLE PRECISION NOT NULL,
            turn_deg            DOUBLE PRECISION NOT NULL,
            score               DOUBLE PRECISION NOT NULL,
            source              TEXT NOT NULL DEFAULT 'turn',
            tp_source_id        TEXT,
            tp_source_row_key   TEXT,
            origin_lon          DOUBLE PRECISION NOT NULL,
            origin_lat          DOUBLE PRECISION NOT NULL,
            g_star_z            DOUBLE PRECISION,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("candidate_intersections", """
        CREATE TABLE IF NOT EXISTS candidate_intersections (
            id                  BIGSERIAL PRIMARY KEY,
            cluster_id          INTEGER NOT NULL,
            centroid_x          DOUBLE PRECISION NOT NULL,
            centroid_y          DOUBLE PRECISION NOT NULL,
            centroid_lon        DOUBLE PRECISION NOT NULL,
            centroid_lat        DOUBLE PRECISION NOT NULL,
            radius_m            DOUBLE PRECISION NOT NULL,
            source              TEXT NOT NULL DEFAULT 'turn',
            sources             JSONB NOT NULL DEFAULT '[]',
            source_counts       JSONB NOT NULL DEFAULT '{}',
            source_ratios       JSONB NOT NULL DEFAULT '{}',
            critical_sources    JSONB NOT NULL DEFAULT '[]',
            turn_event_count    INTEGER NOT NULL DEFAULT 0,
            orthogonal_count    INTEGER NOT NULL DEFAULT 0,
            total_event_count   INTEGER NOT NULL DEFAULT 0,
            origin_lon          DOUBLE PRECISION NOT NULL,
            origin_lat          DOUBLE PRECISION NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("candidate_intersection_events", """
        CREATE TABLE IF NOT EXISTS candidate_intersection_events (
            id                          BIGSERIAL PRIMARY KEY,
            candidate_intersection_id   BIGINT NOT NULL REFERENCES candidate_intersections(id),
            turn_event_id               BIGINT NOT NULL REFERENCES turn_events(id),
            UNIQUE(candidate_intersection_id, turn_event_id)
        )
    """),
    ("candidate_entry_exit_points", """
        CREATE TABLE IF NOT EXISTS candidate_entry_exit_points (
            id                          BIGSERIAL PRIMARY KEY,
            candidate_intersection_id   BIGINT NOT NULL REFERENCES candidate_intersections(id),
            cluster_id                  INTEGER NOT NULL,
            vehicle_id                  TEXT NOT NULL,
            segment_index               INTEGER NOT NULL,
            crossing_type               TEXT NOT NULL,
            crossing_x                  DOUBLE PRECISION NOT NULL,
            crossing_y                  DOUBLE PRECISION NOT NULL,
            crossing_lon                DOUBLE PRECISION NOT NULL,
            crossing_lat                DOUBLE PRECISION NOT NULL,
            branch_angle_deg            DOUBLE PRECISION,
            origin_lon                  DOUBLE PRECISION NOT NULL,
            origin_lat                  DOUBLE PRECISION NOT NULL,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("intersections", """
        CREATE TABLE IF NOT EXISTS intersections (
            id                          BIGSERIAL PRIMARY KEY,
            candidate_intersection_id   BIGINT NOT NULL REFERENCES candidate_intersections(id),
            cluster_id                  INTEGER NOT NULL,
            branch_centers_deg          JSONB NOT NULL DEFAULT '[]',
            transition_counts           JSONB NOT NULL DEFAULT '{}',
            observation_count           INTEGER NOT NULL,
            is_complete                 BOOLEAN NOT NULL DEFAULT TRUE,
            reason                      TEXT NOT NULL,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("entry_exit_points", """
        CREATE TABLE IF NOT EXISTS entry_exit_points (
            id                  BIGSERIAL PRIMARY KEY,
            intersection_id     BIGINT NOT NULL REFERENCES intersections(id),
            cluster_id          INTEGER NOT NULL,
            vehicle_id          TEXT NOT NULL,
            entry_branch_angle  DOUBLE PRECISION NOT NULL,
            exit_branch_angle   DOUBLE PRECISION NOT NULL,
            inbound_bearing     DOUBLE PRECISION NOT NULL,
            outbound_bearing    DOUBLE PRECISION NOT NULL,
            turn_deg            DOUBLE PRECISION NOT NULL,
            entry_crossing_id   BIGINT REFERENCES candidate_entry_exit_points(id),
            exit_crossing_id    BIGINT REFERENCES candidate_entry_exit_points(id),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
]

# ── Index DDL (separate for cleaner error reporting) ────────────────

INDEX_STATEMENTS: list[tuple[str, str]] = [
    ("idx_tp_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_tp_vehicle_ts ON trajectory_points(vehicle_id, timestamp)"),
    ("idx_tp_source_row",      "CREATE INDEX IF NOT EXISTS idx_tp_source_row ON trajectory_points(source_id, source_row_key)"),
    ("idx_ep_reason",          "CREATE INDEX IF NOT EXISTS idx_ep_reason ON excluded_points(reason)"),
    ("idx_ep_source_row",      "CREATE INDEX IF NOT EXISTS idx_ep_source_row ON excluded_points(source_id, source_row_key)"),
    ("idx_ip_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_ip_vehicle_ts ON injected_points(vehicle_id, timestamp)"),
    ("idx_te_event_id",        "CREATE INDEX IF NOT EXISTS idx_te_event_id ON turn_events(event_id)"),
    ("idx_te_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_te_vehicle_ts ON turn_events(vehicle_id, timestamp)"),
    ("idx_te_source",          "CREATE INDEX IF NOT EXISTS idx_te_source ON turn_events(source)"),
    ("idx_te_xy",              "CREATE INDEX IF NOT EXISTS idx_te_xy ON turn_events(x, y)"),
    ("idx_ci_cluster_id",      "CREATE INDEX IF NOT EXISTS idx_ci_cluster_id ON candidate_intersections(cluster_id)"),
    ("idx_ci_source",          "CREATE INDEX IF NOT EXISTS idx_ci_source ON candidate_intersections(source)"),
    ("idx_ci_centroid",        "CREATE INDEX IF NOT EXISTS idx_ci_centroid ON candidate_intersections(centroid_lon, centroid_lat)"),
    ("idx_cie_ci",             "CREATE INDEX IF NOT EXISTS idx_cie_ci ON candidate_intersection_events(candidate_intersection_id)"),
    ("idx_cie_te",             "CREATE INDEX IF NOT EXISTS idx_cie_te ON candidate_intersection_events(turn_event_id)"),
    ("idx_ceep_cluster",       "CREATE INDEX IF NOT EXISTS idx_ceep_cluster ON candidate_entry_exit_points(cluster_id, vehicle_id)"),
    ("idx_ceep_type",          "CREATE INDEX IF NOT EXISTS idx_ceep_type ON candidate_entry_exit_points(crossing_type)"),
    ("idx_int_cluster_id",     "CREATE INDEX IF NOT EXISTS idx_int_cluster_id ON intersections(cluster_id)"),
    ("idx_int_complete",       "CREATE INDEX IF NOT EXISTS idx_int_complete ON intersections(is_complete)"),
    ("idx_eep_cluster_vehicle","CREATE INDEX IF NOT EXISTS idx_eep_cluster_vehicle ON entry_exit_points(cluster_id, vehicle_id)"),
    ("idx_eep_intersection",   "CREATE INDEX IF NOT EXISTS idx_eep_intersection ON entry_exit_points(intersection_id)"),
]


def create_tables(config: _PostgresConfig) -> int:
    """Execute all DDL statements and return the number of failures.

    ZH: 按序执行所有建表 + 索引 DDL，打印每步结果，返回失败数。
    EN: Execute all table + index DDL in order, print each result, return failure count.
    """
    failures = 0
    tables_created = 0
    tables_existed = 0
    indexes_created = 0

    with _connect(config) as conn:
        with conn.cursor() as cur:
            # ── Create tables ──
            print("\n── 创建数据表 / Creating Tables ──")
            for name, ddl in DDL_STATEMENTS:
                try:
                    cur.execute(ddl)
                    # Check if table was newly created or already existed
                    status = cur.statusmessage
                    print(f"  [OK] {name:40s} {status}")
                    tables_created += 1
                except Exception as exc:
                    print(f"  [FAIL] {name:40s} {exc}")
                    failures += 1

            # ── Create indexes ──
            print("\n── 创建索引 / Creating Indexes ──")
            for name, ddl in INDEX_STATEMENTS:
                try:
                    cur.execute(ddl)
                    status = cur.statusmessage
                    print(f"  [OK] {name:40s} {status}")
                    indexes_created += 1
                except Exception as exc:
                    print(f"  [FAIL] {name:40s} {exc}")
                    failures += 1

        conn.commit()

    print(f"\n── 汇总 / Summary ──")
    print(f"  表 / Tables:  {tables_created} processed")
    print(f"  索引 / Indexes: {indexes_created} processed")
    if failures:
        print(f"  [FAIL] 失败 / Failures: {failures}")
    else:
        print(f"  [OK] 全部成功 / All succeeded")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize track.v2 PostgreSQL database schema."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument(
        "--ask-password", action="store_true",
        help="Prompt for password interactively (secure, no echo)."
    )
    parser.add_argument("--connect-timeout", type=int, default=10)
    parser.add_argument("--sslmode", default=None)
    args = parser.parse_args()

    # ── Resolve password ──
    if args.ask_password:
        password = getpass.getpass("PostgreSQL password: ")
    else:
        password = os.getenv("TRACK_PG_PASSWORD", "")

    config = _PostgresConfig(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=password,
        connect_timeout=args.connect_timeout,
        sslmode=args.sslmode,
    )

    print(f"连接目标 / Target: {config.safe_dsn()}")

    try:
        failures = create_tables(config)
    except RuntimeError as exc:
        print(str(exc))
        return 2
    except Exception as exc:
        print(f"[FAIL] 连接或执行失败 / Connection or execution failed: {exc}")
        return 1

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
