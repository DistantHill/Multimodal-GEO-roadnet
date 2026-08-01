"""Initialize the track.v2 PostgreSQL database schema.

ZH: 连接到已部署的 PostgreSQL 数据库，创建流水线所需的全部数据表。
通过 --ask-password 在 ps/CMD 窗口中交互式输入密码。
--reset 会先清空所有表数据再重建（等价于 DROP + CREATE）。

EN: Connect to the deployed PostgreSQL database and create all pipeline tables.
Use --ask-password for interactive password entry in ps/CMD windows.
--reset drops all tables first, then recreates them (DROP + CREATE).

Usage / 用法::

    python -m track.v2.cli.init_db --ask-password
    python -m track.v2.cli.init_db --host 127.0.0.1 --port 5432 --database postgres --user postgres --ask-password
    python -m track.v2.cli.init_db --reset --ask-password
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..logger import init_logger, shutdown_logger

# ── DDL statements in pipeline dependency order ─────────────────────
# ZH: 按流水线阶段顺序排列，先创建被引用表，再创建引用表。
# EN: Ordered by pipeline stage and FK dependency — referenced tables first.

DDL_STATEMENTS: list[tuple[str, str]] = [
    ("pipeline_runs", """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id              UUID PRIMARY KEY,
            source_id       TEXT NOT NULL,
            source_kind     TEXT NOT NULL,
            parameters      JSONB NOT NULL DEFAULT '{}',
            status          TEXT NOT NULL,
            started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at     TIMESTAMPTZ
        )
    """),
    # ── Parser 0 ──
    ("trajectory_points", """
        CREATE TABLE IF NOT EXISTS trajectory_points (
            id              BIGSERIAL PRIMARY KEY,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            vehicle_id      TEXT NOT NULL,
            timestamp       DOUBLE PRECISION NOT NULL,
            lon             DOUBLE PRECISION NOT NULL,
            lat             DOUBLE PRECISION NOT NULL,
            heading         DOUBLE PRECISION NOT NULL DEFAULT 0,
            speed           DOUBLE PRECISION NOT NULL DEFAULT 0,
            geom            geometry(Point, 4326) GENERATED ALWAYS AS (
                                ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                            ) STORED,
            extra           JSONB NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    # ── Cleaner 1 ──
    ("excluded_points", """
        CREATE TABLE IF NOT EXISTS excluded_points (
            id              BIGSERIAL PRIMARY KEY,
            run_id          UUID NOT NULL REFERENCES pipeline_runs(id),
            source_id       TEXT NOT NULL,
            source_row_key  TEXT NOT NULL,
            reason          TEXT NOT NULL,
            lon             DOUBLE PRECISION NOT NULL,
            lat             DOUBLE PRECISION NOT NULL,
            geom            geometry(Point, 4326) GENERATED ALWAYS AS (
                                ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                            ) STORED,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("kept_trajectory_points", """
        CREATE TABLE IF NOT EXISTS kept_trajectory_points (
            id                      BIGSERIAL PRIMARY KEY,
            run_id                  UUID NOT NULL REFERENCES pipeline_runs(id),
            trajectory_point_id     BIGINT NOT NULL,
            vehicle_id              TEXT NOT NULL,
            subtrack_index          INTEGER NOT NULL DEFAULT 1,
            sequence_in_subtrack    INTEGER NOT NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("injected_points", """
        CREATE TABLE IF NOT EXISTS injected_points (
            id                  BIGSERIAL PRIMARY KEY,
            run_id              UUID NOT NULL REFERENCES pipeline_runs(id),
            vehicle_id          TEXT NOT NULL,
            timestamp           DOUBLE PRECISION NOT NULL,
            lon                 DOUBLE PRECISION NOT NULL,
            lat                 DOUBLE PRECISION NOT NULL,
            x                   DOUBLE PRECISION NOT NULL,
            y                   DOUBLE PRECISION NOT NULL,
            heading             DOUBLE PRECISION NOT NULL,
            speed               DOUBLE PRECISION NOT NULL DEFAULT 0,
            geom                geometry(Point, 4326) GENERATED ALWAYS AS (
                                    ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                                ) STORED,
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
    ("tracks_cross", """
        CREATE TABLE IF NOT EXISTS tracks_cross (
            id                  BIGSERIAL PRIMARY KEY,
            run_id              UUID NOT NULL REFERENCES pipeline_runs(id),
            vehicle_id          TEXT NOT NULL,
            timestamp           DOUBLE PRECISION NOT NULL,
            lon                 DOUBLE PRECISION NOT NULL,
            lat                 DOUBLE PRECISION NOT NULL,
            x                   DOUBLE PRECISION NOT NULL,
            y                   DOUBLE PRECISION NOT NULL,
            heading             DOUBLE PRECISION NOT NULL,
            speed               DOUBLE PRECISION NOT NULL DEFAULT 0,
            geom                geometry(Point, 4326) GENERATED ALWAYS AS (
                                    ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                                ) STORED,
            from_source_id      TEXT NOT NULL,
            from_source_row_key TEXT NOT NULL,
            to_source_id        TEXT NOT NULL,
            to_source_row_key   TEXT NOT NULL,
            generated_reason    TEXT NOT NULL DEFAULT 'orthogonal_cross_track',
            origin_lon          DOUBLE PRECISION NOT NULL,
            origin_lat          DOUBLE PRECISION NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    # ── Intersect2 ──
    ("turn_events", """
        CREATE TABLE IF NOT EXISTS turn_events (
            id                  BIGSERIAL PRIMARY KEY,
            run_id              UUID NOT NULL REFERENCES pipeline_runs(id),
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
            geom                geometry(Point, 4326) GENERATED ALWAYS AS (
                                    ST_SetSRID(ST_MakePoint(lon, lat), 4326)
                                ) STORED,
            tp_source_id        TEXT,
            tp_source_row_key   TEXT,
            from_source_id      TEXT,
            from_source_row_key TEXT,
            to_source_id        TEXT,
            to_source_row_key   TEXT,
            origin_lon          DOUBLE PRECISION NOT NULL,
            origin_lat          DOUBLE PRECISION NOT NULL,
            g_star_z            DOUBLE PRECISION,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("candidate_intersections", """
        CREATE TABLE IF NOT EXISTS candidate_intersections (
            id                  BIGSERIAL PRIMARY KEY,
            run_id              UUID NOT NULL REFERENCES pipeline_runs(id),
            cluster_id          INTEGER NOT NULL,
            centroid_x          DOUBLE PRECISION NOT NULL,
            centroid_y          DOUBLE PRECISION NOT NULL,
            centroid_lon        DOUBLE PRECISION NOT NULL,
            centroid_lat        DOUBLE PRECISION NOT NULL,
            radius_m            DOUBLE PRECISION NOT NULL,
            geom                geometry(Polygon, 4326),  -- app-layer: ST_Buffer(geography, radius_m)
            source              TEXT NOT NULL DEFAULT 'turn',
            sources             JSONB NOT NULL DEFAULT '[]',
            source_counts       JSONB NOT NULL DEFAULT '{}',
            source_ratios       JSONB NOT NULL DEFAULT '{}',
            critical_sources    JSONB NOT NULL DEFAULT '[]',
            turn_event_count    INTEGER NOT NULL DEFAULT 0,
            orthogonal_count    INTEGER NOT NULL DEFAULT 0,
            cross_track_count   INTEGER NOT NULL DEFAULT 0,
            total_event_count   INTEGER NOT NULL DEFAULT 0,
            origin_lon          DOUBLE PRECISION NOT NULL,
            origin_lat          DOUBLE PRECISION NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("candidate_intersection_events", """
        CREATE TABLE IF NOT EXISTS candidate_intersection_events (
            id                          BIGSERIAL PRIMARY KEY,
            run_id                      UUID NOT NULL REFERENCES pipeline_runs(id),
            candidate_intersection_id   BIGINT NOT NULL REFERENCES candidate_intersections(id),
            event_kind                  TEXT NOT NULL,
            turn_event_id               BIGINT REFERENCES turn_events(id),
            from_source_id              TEXT,
            from_source_row_key         TEXT,
            to_source_id                TEXT,
            to_source_row_key           TEXT
        )
    """),
    ("candidate_entry_exit_points", """
        CREATE TABLE IF NOT EXISTS candidate_entry_exit_points (
            id                          BIGSERIAL PRIMARY KEY,
            run_id                      UUID NOT NULL REFERENCES pipeline_runs(id),
            candidate_intersection_id   BIGINT NOT NULL REFERENCES candidate_intersections(id),
            cluster_id                  INTEGER NOT NULL,
            vehicle_id                  TEXT NOT NULL,
            segment_index               INTEGER NOT NULL,
            crossing_type               TEXT NOT NULL,
            crossing_x                  DOUBLE PRECISION NOT NULL,
            crossing_y                  DOUBLE PRECISION NOT NULL,
            crossing_lon                DOUBLE PRECISION NOT NULL,
            crossing_lat                DOUBLE PRECISION NOT NULL,
            geom                        geometry(Point, 4326) GENERATED ALWAYS AS (
                                            ST_SetSRID(ST_MakePoint(crossing_lon, crossing_lat), 4326)
                                        ) STORED,
            branch_angle_deg            DOUBLE PRECISION,
            origin_lon                  DOUBLE PRECISION NOT NULL,
            origin_lat                  DOUBLE PRECISION NOT NULL,
            from_source_id              TEXT,
            from_source_row_key         TEXT,
            to_source_id                TEXT,
            to_source_row_key           TEXT,
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("intersections", """
        CREATE TABLE IF NOT EXISTS intersections (
            id                          BIGSERIAL PRIMARY KEY,
            run_id                      UUID NOT NULL REFERENCES pipeline_runs(id),
            candidate_intersection_id   BIGINT NOT NULL REFERENCES candidate_intersections(id),
            cluster_id                  INTEGER NOT NULL,
            branch_centers_deg          JSONB NOT NULL DEFAULT '[]',
            transition_counts           JSONB NOT NULL DEFAULT '{}',
            observation_count           INTEGER NOT NULL,
            is_complete                 BOOLEAN NOT NULL DEFAULT TRUE,
            reason                      TEXT NOT NULL,
            geom                        geometry(Polygon, 4326),  -- app-layer: derived from candidate_intersections
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("entry_exit_points", """
        CREATE TABLE IF NOT EXISTS entry_exit_points (
            id                  BIGSERIAL PRIMARY KEY,
            run_id              UUID NOT NULL REFERENCES pipeline_runs(id),
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
    # ── trajectory_points ──
    ("idx_tp_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_tp_vehicle_ts ON trajectory_points(vehicle_id, timestamp)"),
    ("idx_tp_geom",            "CREATE INDEX IF NOT EXISTS idx_tp_geom ON trajectory_points USING GIST (geom)"),
    ("idx_tp_active",          "CREATE INDEX IF NOT EXISTS idx_tp_active ON trajectory_points(is_active) WHERE is_active = TRUE"),
    # ── excluded_points ──
    ("idx_ep_reason",          "CREATE INDEX IF NOT EXISTS idx_ep_reason ON excluded_points(reason)"),
    ("idx_ep_geom",            "CREATE INDEX IF NOT EXISTS idx_ep_geom ON excluded_points USING GIST (geom)"),
    # ── kept_trajectory_points ──
    ("idx_ktp_tp_id",          "CREATE INDEX IF NOT EXISTS idx_ktp_tp_id ON kept_trajectory_points(trajectory_point_id)"),
    ("idx_ktp_vehicle_sub",    "CREATE INDEX IF NOT EXISTS idx_ktp_vehicle_sub ON kept_trajectory_points(vehicle_id, subtrack_index, sequence_in_subtrack)"),
    # ── injected_points ──
    ("idx_ip_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_ip_vehicle_ts ON injected_points(vehicle_id, timestamp)"),
    ("idx_ip_geom",            "CREATE INDEX IF NOT EXISTS idx_ip_geom ON injected_points USING GIST (geom)"),
    # ── tracks_cross ──
    ("idx_tc_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_tc_vehicle_ts ON tracks_cross(vehicle_id, timestamp)"),
    ("idx_tc_geom",            "CREATE INDEX IF NOT EXISTS idx_tc_geom ON tracks_cross USING GIST (geom)"),
    ("idx_tc_segment_pair",    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tc_segment_pair ON tracks_cross (run_id, vehicle_id, from_source_id, from_source_row_key, to_source_id, to_source_row_key)"),
    # ── turn_events ──
    ("idx_te_event_id",        "CREATE INDEX IF NOT EXISTS idx_te_event_id ON turn_events(event_id)"),
    ("idx_te_vehicle_ts",      "CREATE UNIQUE INDEX IF NOT EXISTS idx_te_vehicle_ts ON turn_events(run_id, vehicle_id, timestamp)"),
    ("idx_te_source",          "CREATE INDEX IF NOT EXISTS idx_te_source ON turn_events(source)"),
    ("idx_te_xy",              "CREATE INDEX IF NOT EXISTS idx_te_xy ON turn_events(x, y)"),
    ("idx_te_geom",            "CREATE INDEX IF NOT EXISTS idx_te_geom ON turn_events USING GIST (geom)"),
    # ── candidate_intersections ──
    ("idx_ci_cluster_id",      "CREATE INDEX IF NOT EXISTS idx_ci_cluster_id ON candidate_intersections(cluster_id)"),
    ("idx_ci_source",          "CREATE INDEX IF NOT EXISTS idx_ci_source ON candidate_intersections(source)"),
    ("idx_ci_centroid",        "CREATE INDEX IF NOT EXISTS idx_ci_centroid ON candidate_intersections(centroid_lon, centroid_lat)"),
    ("idx_ci_geom",            "CREATE INDEX IF NOT EXISTS idx_ci_geom ON candidate_intersections USING GIST (geom)"),
    # ── candidate_intersection_events ──
    ("idx_cie_ci",             "CREATE INDEX IF NOT EXISTS idx_cie_ci ON candidate_intersection_events(candidate_intersection_id)"),
    ("idx_cie_te",             "CREATE INDEX IF NOT EXISTS idx_cie_te ON candidate_intersection_events(turn_event_id)"),
    # ── candidate_entry_exit_points ──
    ("idx_ceep_cluster",       "CREATE INDEX IF NOT EXISTS idx_ceep_cluster ON candidate_entry_exit_points(cluster_id, vehicle_id)"),
    ("idx_ceep_type",          "CREATE INDEX IF NOT EXISTS idx_ceep_type ON candidate_entry_exit_points(crossing_type)"),
    ("idx_ceep_geom",          "CREATE INDEX IF NOT EXISTS idx_ceep_geom ON candidate_entry_exit_points USING GIST (geom)"),
    # ── intersections ──
    ("idx_int_cluster_id",     "CREATE INDEX IF NOT EXISTS idx_int_cluster_id ON intersections(cluster_id)"),
    ("idx_int_complete",       "CREATE INDEX IF NOT EXISTS idx_int_complete ON intersections(is_complete)"),
    ("idx_int_geom",           "CREATE INDEX IF NOT EXISTS idx_int_geom ON intersections USING GIST (geom)"),
    # ── entry_exit_points ──
    ("idx_eep_cluster_vehicle","CREATE INDEX IF NOT EXISTS idx_eep_cluster_vehicle ON entry_exit_points(cluster_id, vehicle_id)"),
    ("idx_eep_intersection",   "CREATE INDEX IF NOT EXISTS idx_eep_intersection ON entry_exit_points(intersection_id)"),
    ("idx_pr_source",          "CREATE INDEX IF NOT EXISTS idx_pr_source ON pipeline_runs(source_id, started_at DESC)"),
]


def reset_tables(config: PostgresConfig) -> int:
    """Drop all pipeline tables in reverse dependency order.

    ZH: 按反向依赖顺序删除所有流水线数据表（先删引用表，再删被引用表），
        清除全部数据。如果表不存在则跳过（IF EXISTS）。
    EN: Drop all pipeline tables in reverse FK order — referencing tables
        first, referenced tables last. Skips nonexistent tables (IF EXISTS).
    """
    # ZH: 反向遍历 DDL 列表，确保引用表先被删除
    # EN: Reverse DDL order so referencing tables are dropped before their parents
    drop_order = reversed(DDL_STATEMENTS)
    failures = 0
    tables_dropped = 0

    with pg_connect(config) as conn:
        with conn.cursor() as cur:
            print("\n── 重置数据表 / Resetting Tables ──")
            for name, _ in drop_order:
                sql = f"DROP TABLE IF EXISTS {name} CASCADE"
                try:
                    cur.execute(sql)
                    status = cur.statusmessage
                    print(f"  [OK] DROP {name:40s} {status}")
                    tables_dropped += 1
                except Exception as exc:
                    print(f"  [FAIL] DROP {name:40s} {exc}")
                    failures += 1
        conn.commit()

    print(f"\n  已删除 / Dropped: {tables_dropped} 表/tables")
    if failures:
        print(f"  [FAIL] 失败 / Failures: {failures}")
    return failures


def _run_migrations(cur: Any) -> None:
    """Add columns/constraints that may be missing from existing tables.

    ZH: 对已存在的表，添加可能缺失的列或约束。
        ``CREATE TABLE IF NOT EXISTS`` 不会修改已有表结构，迁移步骤确保新列被添加。
    """
    migrations: list[tuple[str, str]] = [
        (
            "tp_is_active",
            "ALTER TABLE trajectory_points ADD COLUMN IF NOT EXISTS "
            "is_active BOOLEAN NOT NULL DEFAULT TRUE",
        ),
        (
            "ci_cross_track_count",
            "ALTER TABLE candidate_intersections ADD COLUMN IF NOT EXISTS "
            "cross_track_count INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "excluded_run_id",
            "ALTER TABLE excluded_points ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "kept_run_id",
            "ALTER TABLE kept_trajectory_points ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "injected_run_id",
            "ALTER TABLE injected_points ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "cross_run_id",
            "ALTER TABLE tracks_cross ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "turn_run_id",
            "ALTER TABLE turn_events ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "turn_source_pairs",
            "ALTER TABLE turn_events "
            "ADD COLUMN IF NOT EXISTS from_source_id TEXT, "
            "ADD COLUMN IF NOT EXISTS from_source_row_key TEXT, "
            "ADD COLUMN IF NOT EXISTS to_source_id TEXT, "
            "ADD COLUMN IF NOT EXISTS to_source_row_key TEXT",
        ),
        (
            "candidate_run_id",
            "ALTER TABLE candidate_intersections ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "candidate_event_run_id",
            "ALTER TABLE candidate_intersection_events ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "candidate_event_kind",
            "ALTER TABLE candidate_intersection_events ADD COLUMN IF NOT EXISTS "
            "event_kind TEXT NOT NULL DEFAULT 'turn'",
        ),
        (
            "candidate_event_source_pairs",
            "ALTER TABLE candidate_intersection_events "
            "ADD COLUMN IF NOT EXISTS from_source_id TEXT, "
            "ADD COLUMN IF NOT EXISTS from_source_row_key TEXT, "
            "ADD COLUMN IF NOT EXISTS to_source_id TEXT, "
            "ADD COLUMN IF NOT EXISTS to_source_row_key TEXT",
        ),
        (
            "candidate_event_turn_nullable",
            "ALTER TABLE candidate_intersection_events "
            "ALTER COLUMN turn_event_id DROP NOT NULL",
        ),
        (
            "crossing_run_id",
            "ALTER TABLE candidate_entry_exit_points ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "crossing_segment_refs",
            "ALTER TABLE candidate_entry_exit_points "
            "ADD COLUMN IF NOT EXISTS from_source_id TEXT, "
            "ADD COLUMN IF NOT EXISTS from_source_row_key TEXT, "
            "ADD COLUMN IF NOT EXISTS to_source_id TEXT, "
            "ADD COLUMN IF NOT EXISTS to_source_row_key TEXT",
        ),
        (
            "intersection_run_id",
            "ALTER TABLE intersections ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        (
            "entry_exit_run_id",
            "ALTER TABLE entry_exit_points ADD COLUMN IF NOT EXISTS "
            "run_id UUID REFERENCES pipeline_runs(id)",
        ),
        ("replace_cross_unique_index", "DROP INDEX IF EXISTS idx_tc_segment_pair"),
        ("replace_turn_unique_index", "DROP INDEX IF EXISTS idx_te_vehicle_ts"),
        (
            "ktp_drop_tp_fk",
            "DO $$ DECLARE cname text; BEGIN "
            "SELECT con.conname INTO cname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = 'kept_trajectory_points' "
            "AND con.contype = 'f' AND con.confrelid = 'trajectory_points'::regclass; "
            "IF cname IS NOT NULL THEN "
            "EXECUTE 'ALTER TABLE kept_trajectory_points DROP CONSTRAINT ' || cname; "
            "END IF; END $$",
        ),
    ]
    for label, sql in migrations:
        try:
            cur.execute(sql)
            print(f"  [OK] migration {label:30s} {cur.statusmessage}")
        except Exception as exc:
            print(f"  [SKIP] migration {label:30s} {exc}")


def create_tables(config: PostgresConfig) -> int:
    """Execute all DDL statements and return the number of failures.

    ZH: 按序执行所有建表 + 索引 DDL，打印每步结果，返回失败数。
    EN: Execute all table + index DDL in order, print each result, return failure count.
    """
    failures = 0
    tables_created = 0
    tables_existed = 0
    indexes_created = 0

    with pg_connect(config) as conn:
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

            # ── Migrations: add missing columns to existing tables ──
            print("\n── 迁移 / Migrations ──")
            _run_migrations(cur)

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
    # ZH: 从 .env 加载默认连接参数，CLI 参数可覆盖
    # EN: Load defaults from .env; CLI args take precedence
    _env = default_postgres_config()

    parser = argparse.ArgumentParser(
        description="Initialize track.v2 PostgreSQL database schema."
    )
    parser.add_argument("--host", default=_env.host)
    parser.add_argument("--port", type=int, default=_env.port)
    parser.add_argument("--database", default=_env.database)
    parser.add_argument("--user", default=_env.user)
    parser.add_argument(
        "--ask-password", action="store_true",
        help="Prompt for password interactively (secure, no echo). Overrides .env password."
    )
    parser.add_argument("--connect-timeout", type=int, default=_env.connect_timeout)
    parser.add_argument("--sslmode", default=_env.sslmode)
    parser.add_argument(
        "--reset", action="store_true",
        help="Drop all tables (CASCADE) before recreating them, clearing all data."
    )
    args = parser.parse_args()

    # ── Resolve password: CLI prompt > .env > empty ──
    if args.ask_password:
        password = getpass.getpass("PostgreSQL password: ")
    else:
        password = _env.password

    config = PostgresConfig(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=password,
        connect_timeout=args.connect_timeout,
        sslmode=args.sslmode,
    )

    print(f"连接目标 / Target: {config.safe_dsn()}")
    init_logger()

    try:
        if args.reset:
            reset_failures = reset_tables(config)
            if reset_failures > 0:
                print("[FAIL] 重置失败，终止操作 / Reset failed, aborting.")
                return 1
        failures = create_tables(config)
    except RuntimeError as exc:
        print(str(exc))
        return 2
    except Exception as exc:
        print(f"[FAIL] 连接或执行失败 / Connection or execution failed: {exc}")
        return 1
    finally:
        shutdown_logger()

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
