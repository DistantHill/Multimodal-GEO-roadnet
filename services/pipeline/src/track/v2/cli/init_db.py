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
import re
from pathlib import Path
from typing import Any

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..db.legacy_migration import load_legacy_clean_map, migrate_legacy_pipeline_runs
from ..logger import init_logger, shutdown_logger

# ── DDL statements in pipeline dependency order ─────────────────────
# ZH: 按流水线阶段顺序排列，先创建被引用表，再创建引用表。
# EN: Ordered by pipeline stage and FK dependency — referenced tables first.

DDL_STATEMENTS: list[tuple[str, str]] = [
    ("clean_runs", """
        CREATE TABLE IF NOT EXISTS clean_runs (
            id              UUID PRIMARY KEY,
            source_id       TEXT NOT NULL,
            source_kind     TEXT NOT NULL,
            parameters      JSONB NOT NULL DEFAULT '{}',
            status          TEXT NOT NULL,
            started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at     TIMESTAMPTZ
        )
    """),
    ("detect_runs", """
        CREATE TABLE IF NOT EXISTS detect_runs (
            id              UUID PRIMARY KEY,
            clean_id        UUID NOT NULL REFERENCES clean_runs(id),
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
            clean_id        UUID NOT NULL REFERENCES clean_runs(id),
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
            clean_id                UUID NOT NULL REFERENCES clean_runs(id),
            trajectory_point_id     BIGINT NOT NULL,
            source_id               TEXT NOT NULL,
            source_row_key          TEXT NOT NULL,
            vehicle_id              TEXT NOT NULL,
            subtrack_index          INTEGER NOT NULL DEFAULT 1,
            sequence_in_subtrack    INTEGER NOT NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """),
    ("injected_points", """
        CREATE TABLE IF NOT EXISTS injected_points (
            id                  BIGSERIAL PRIMARY KEY,
            clean_id            UUID NOT NULL REFERENCES clean_runs(id),
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
            detect_id           UUID NOT NULL REFERENCES detect_runs(id),
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
            detect_id           UUID NOT NULL REFERENCES detect_runs(id),
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
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (id, detect_id)
        )
    """),
    ("candidate_intersections", """
        CREATE TABLE IF NOT EXISTS candidate_intersections (
            id                  BIGSERIAL PRIMARY KEY,
            detect_id           UUID NOT NULL REFERENCES detect_runs(id),
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
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (id, detect_id)
        )
    """),
    ("candidate_intersection_events", """
        CREATE TABLE IF NOT EXISTS candidate_intersection_events (
            id                          BIGSERIAL PRIMARY KEY,
            detect_id                   UUID NOT NULL REFERENCES detect_runs(id),
            candidate_intersection_id   BIGINT NOT NULL,
            event_kind                  TEXT NOT NULL DEFAULT 'turn',
            turn_event_id               BIGINT,
            from_source_id              TEXT,
            from_source_row_key         TEXT,
            to_source_id                TEXT,
            to_source_row_key           TEXT,
            FOREIGN KEY (candidate_intersection_id, detect_id)
                REFERENCES candidate_intersections(id, detect_id),
            FOREIGN KEY (turn_event_id, detect_id)
                REFERENCES turn_events(id, detect_id)
        )
    """),
    ("candidate_entry_exit_points", """
        CREATE TABLE IF NOT EXISTS candidate_entry_exit_points (
            id                          BIGSERIAL PRIMARY KEY,
            detect_id                   UUID NOT NULL REFERENCES detect_runs(id),
            candidate_intersection_id   BIGINT NOT NULL,
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
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (id, detect_id),
            FOREIGN KEY (candidate_intersection_id, detect_id)
                REFERENCES candidate_intersections(id, detect_id)
        )
    """),
    ("intersections", """
        CREATE TABLE IF NOT EXISTS intersections (
            id                          BIGSERIAL PRIMARY KEY,
            detect_id                   UUID NOT NULL REFERENCES detect_runs(id),
            candidate_intersection_id   BIGINT NOT NULL,
            cluster_id                  INTEGER NOT NULL,
            branch_centers_deg          JSONB NOT NULL DEFAULT '[]',
            transition_counts           JSONB NOT NULL DEFAULT '{}',
            observation_count           INTEGER NOT NULL,
            is_complete                 BOOLEAN NOT NULL DEFAULT TRUE,
            reason                      TEXT NOT NULL,
            clip_radius_m               DOUBLE PRECISION NOT NULL,
            geom                        geometry(Polygon, 4326),  -- app-layer: derived from candidate_intersections
            created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (id, detect_id),
            FOREIGN KEY (candidate_intersection_id, detect_id)
                REFERENCES candidate_intersections(id, detect_id)
        )
    """),
    ("entry_exit_points", """
        CREATE TABLE IF NOT EXISTS entry_exit_points (
            id                  BIGSERIAL PRIMARY KEY,
            detect_id           UUID NOT NULL REFERENCES detect_runs(id),
            intersection_id     BIGINT NOT NULL,
            cluster_id          INTEGER NOT NULL,
            vehicle_id          TEXT NOT NULL,
            entry_branch_angle  DOUBLE PRECISION NOT NULL,
            exit_branch_angle   DOUBLE PRECISION NOT NULL,
            inbound_bearing     DOUBLE PRECISION NOT NULL,
            outbound_bearing    DOUBLE PRECISION NOT NULL,
            turn_deg            DOUBLE PRECISION NOT NULL,
            entry_crossing_id   BIGINT,
            exit_crossing_id    BIGINT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            FOREIGN KEY (intersection_id, detect_id)
                REFERENCES intersections(id, detect_id),
            FOREIGN KEY (entry_crossing_id, detect_id)
                REFERENCES candidate_entry_exit_points(id, detect_id),
            FOREIGN KEY (exit_crossing_id, detect_id)
                REFERENCES candidate_entry_exit_points(id, detect_id)
        )
    """),
]

# ── Index DDL (separate for cleaner error reporting) ────────────────

INDEX_STATEMENTS: list[tuple[str, str]] = [
    ("idx_clean_runs_source", "CREATE INDEX IF NOT EXISTS idx_clean_runs_source ON clean_runs(source_id, started_at DESC)"),
    ("idx_detect_runs_clean", "CREATE INDEX IF NOT EXISTS idx_detect_runs_clean ON detect_runs(clean_id, started_at DESC)"),
    # ── trajectory_points ──
    ("idx_tp_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_tp_vehicle_ts ON trajectory_points(vehicle_id, timestamp)"),
    ("idx_tp_geom",            "CREATE INDEX IF NOT EXISTS idx_tp_geom ON trajectory_points USING GIST (geom)"),
    ("idx_tp_active",          "CREATE INDEX IF NOT EXISTS idx_tp_active ON trajectory_points(is_active) WHERE is_active = TRUE"),
    # ── excluded_points ──
    ("idx_ep_reason",          "CREATE INDEX IF NOT EXISTS idx_ep_reason ON excluded_points(reason)"),
    ("idx_ep_geom",            "CREATE INDEX IF NOT EXISTS idx_ep_geom ON excluded_points USING GIST (geom)"),
    ("idx_ep_clean",           "CREATE INDEX IF NOT EXISTS idx_ep_clean ON excluded_points(clean_id)"),
    # ── kept_trajectory_points ──
    ("idx_ktp_tp_id",          "CREATE INDEX IF NOT EXISTS idx_ktp_tp_id ON kept_trajectory_points(trajectory_point_id)"),
    ("idx_ktp_vehicle_sub",    "CREATE INDEX IF NOT EXISTS idx_ktp_vehicle_sub ON kept_trajectory_points(clean_id, vehicle_id, subtrack_index, sequence_in_subtrack)"),
    # ── injected_points ──
    ("idx_ip_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_ip_vehicle_ts ON injected_points(clean_id, vehicle_id, timestamp)"),
    ("idx_ip_geom",            "CREATE INDEX IF NOT EXISTS idx_ip_geom ON injected_points USING GIST (geom)"),
    # ── tracks_cross ──
    ("idx_tc_vehicle_ts",      "CREATE INDEX IF NOT EXISTS idx_tc_vehicle_ts ON tracks_cross(detect_id, vehicle_id, timestamp)"),
    ("idx_tc_geom",            "CREATE INDEX IF NOT EXISTS idx_tc_geom ON tracks_cross USING GIST (geom)"),
    ("idx_tc_segment_pair",    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tc_segment_pair ON tracks_cross (detect_id, vehicle_id, from_source_id, from_source_row_key, to_source_id, to_source_row_key)"),
    # ── turn_events ──
    ("idx_te_event_id",        "CREATE INDEX IF NOT EXISTS idx_te_event_id ON turn_events(event_id)"),
    ("idx_te_vehicle_ts",      "CREATE UNIQUE INDEX IF NOT EXISTS idx_te_vehicle_ts ON turn_events(detect_id, vehicle_id, timestamp)"),
    ("idx_te_detect_id",       "CREATE INDEX IF NOT EXISTS idx_te_detect_id ON turn_events(detect_id)"),
    ("idx_te_source",          "CREATE INDEX IF NOT EXISTS idx_te_source ON turn_events(source)"),
    ("idx_te_xy",              "CREATE INDEX IF NOT EXISTS idx_te_xy ON turn_events(x, y)"),
    ("idx_te_geom",            "CREATE INDEX IF NOT EXISTS idx_te_geom ON turn_events USING GIST (geom)"),
    # ── candidate_intersections ──
    ("idx_ci_cluster_id",      "CREATE UNIQUE INDEX IF NOT EXISTS idx_ci_cluster_id ON candidate_intersections(detect_id, cluster_id)"),
    ("idx_ci_source",          "CREATE INDEX IF NOT EXISTS idx_ci_source ON candidate_intersections(source)"),
    ("idx_ci_centroid",        "CREATE INDEX IF NOT EXISTS idx_ci_centroid ON candidate_intersections(centroid_lon, centroid_lat)"),
    ("idx_ci_geom",            "CREATE INDEX IF NOT EXISTS idx_ci_geom ON candidate_intersections USING GIST (geom)"),
    # ── candidate_intersection_events ──
    ("idx_cie_ci",             "CREATE INDEX IF NOT EXISTS idx_cie_ci ON candidate_intersection_events(detect_id, candidate_intersection_id)"),
    ("idx_cie_te",             "CREATE INDEX IF NOT EXISTS idx_cie_te ON candidate_intersection_events(turn_event_id)"),
    # ── candidate_entry_exit_points ──
    ("idx_ceep_cluster",       "CREATE INDEX IF NOT EXISTS idx_ceep_cluster ON candidate_entry_exit_points(detect_id, cluster_id, vehicle_id)"),
    ("idx_ceep_type",          "CREATE INDEX IF NOT EXISTS idx_ceep_type ON candidate_entry_exit_points(crossing_type)"),
    ("idx_ceep_geom",          "CREATE INDEX IF NOT EXISTS idx_ceep_geom ON candidate_entry_exit_points USING GIST (geom)"),
    # ── intersections ──
    ("idx_int_cluster_id",     "CREATE UNIQUE INDEX IF NOT EXISTS idx_int_cluster_id ON intersections(detect_id, cluster_id)"),
    ("idx_int_complete",       "CREATE INDEX IF NOT EXISTS idx_int_complete ON intersections(is_complete)"),
    ("idx_int_geom",           "CREATE INDEX IF NOT EXISTS idx_int_geom ON intersections USING GIST (geom)"),
    # ── entry_exit_points ──
    ("idx_eep_cluster_vehicle","CREATE INDEX IF NOT EXISTS idx_eep_cluster_vehicle ON entry_exit_points(detect_id, cluster_id, vehicle_id)"),
    ("idx_eep_intersection",   "CREATE INDEX IF NOT EXISTS idx_eep_intersection ON entry_exit_points(intersection_id)"),
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
    drop_order = [*reversed(DDL_STATEMENTS), ("pipeline_runs", "")]
    tables_dropped = 0

    with pg_connect(config) as conn:
        with conn.cursor() as cur:
            print("\n── 重置数据表 / Resetting Tables ──")
            for name, _ in drop_order:
                sql = f"DROP TABLE IF EXISTS {name} CASCADE"
                cur.execute(sql)
                status = cur.statusmessage
                print(f"  [OK] DROP {name:40s} {status}")
                tables_dropped += 1
        conn.commit()

    print(f"\n  已删除 / Dropped: {tables_dropped} 表/tables")
    return 0


# ── Auto column migration helpers ────────────────────────────────────
# ZH: 从 CREATE TABLE DDL 中解析列定义，对比 information_schema.columns，
#     自动生成 ALTER TABLE ADD COLUMN IF NOT EXISTS，消除手动维护迁移列表的需求。
# EN: Parse column definitions from CREATE TABLE DDL, diff against
#     information_schema.columns, and auto-generate ALTER TABLE ADD COLUMN
#     IF NOT EXISTS — eliminating the need for a hand-maintained migration list.


def _strip_sql_comments(text: str) -> str:
    """Remove SQL single-line comments (``-- ...``) from *text*.

    ZH: 去掉 ``--`` 注释，防止注释中的括号和逗号干扰 DDL 解析。
    EN: Strip ``--`` comments so their parens/commas don't break the DDL parser.
    """
    return re.sub(r"--[^\n]*", "", text)


def _matching_paren(text: str, open_idx: int) -> int:
    """Return the index of the closing paren that matches ``text[open_idx]``."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unmatched parenthesis in DDL")


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split *text* by *sep*, but only when paren-depth is zero.

    ZH: 按分隔符切分，但跳过括号内的分隔符。
        保护 ``geometry(Point, 4326)``、``ST_MakePoint(lon, lat)`` 等片段。
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def parse_ddl_columns(ddl: str) -> dict[str, str]:
    """Parse a ``CREATE TABLE`` DDL string, returning ``{column_name: definition}``.

    ZH: 从 CREATE TABLE DDL 中提取列定义。跳过 GENERATED 列、SERIAL 类型
        和表级约束（PRIMARY KEY / UNIQUE / CHECK / FOREIGN KEY / CONSTRAINT）。

    EN: Extract column definitions from a CREATE TABLE DDL. Skips GENERATED
        columns, SERIAL types, and table-level constraints.
    """
    text = _strip_sql_comments(ddl)
    try:
        open_idx = text.index("(")
    except ValueError:
        return {}
    close_idx = _matching_paren(text, open_idx)
    body = text[open_idx + 1 : close_idx]

    columns: dict[str, str] = {}
    for part in _split_top_level(body):
        part = part.strip()
        if not part:
            continue
        up = part.upper().lstrip()

        # Skip table-level constraints
        if up.startswith(
            ("PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY", "CONSTRAINT")
        ):
            continue

        # Split into column-name and the rest
        first_space = part.find(" ")
        if first_space == -1:
            continue
        col_name = part[:first_space].strip()
        col_def = part[first_space + 1 :].strip()
        if not col_def:
            continue

        columns[col_name] = col_def

    return columns


def column_to_alter_fragment(col_name: str, col_def: str) -> str | None:
    """Convert a CREATE TABLE column definition into an ALTER TABLE-safe fragment.

    ZH: 将 CREATE TABLE 的列定义转为 ALTER TABLE ADD COLUMN 安全形式。
        - GENERATED ALWAYS AS / SERIAL → 返回 None（无法通过 ALTER 添加）
        - NOT NULL 无 DEFAULT → 去除 NOT NULL（现有行会是 NULL）
        - PRIMARY KEY / UNIQUE → 去除（有数据的表无法加约束）

    Returns the ALTER-safe type + constraint fragment, or *None* to skip.
    """
    defn = col_def.strip()
    if not defn:
        return None

    # Skip generated (computed) columns — can't ALTER ADD
    if re.search(r"\bGENERATED\s+ALWAYS\s+AS\b", defn, re.IGNORECASE):
        return None

    # Skip SERIAL types — PostgreSQL auto-creates the sequence
    type_token = defn.split(maxsplit=1)[0].upper()
    if type_token in ("SERIAL", "SMALLSERIAL", "BIGSERIAL"):
        return None

    # NOT NULL without DEFAULT → strip NOT NULL (existing rows get NULL)
    has_not_null = bool(re.search(r"\bNOT\s+NULL\b", defn, re.IGNORECASE))
    has_default = bool(re.search(r"\bDEFAULT\b", defn, re.IGNORECASE))
    if has_not_null and not has_default:
        defn = re.sub(r"\bNOT\s+NULL\b", "", defn, flags=re.IGNORECASE)

    # Strip inline PRIMARY KEY / UNIQUE (can't add to populated table)
    defn = re.sub(r"\bPRIMARY\s+KEY\b", "", defn, flags=re.IGNORECASE)
    defn = re.sub(r"\bUNIQUE\b", "", defn, flags=re.IGNORECASE)

    # Collapse whitespace
    return re.sub(r"\s+", " ", defn).strip()


def build_auto_migrations(
    table_name: str, ddl: str, existing: set[str]
) -> list[tuple[str, str | None, str | None]]:
    """Diff DDL columns against existing columns; return migration entries.

    ZH: 对比 DDL 和数据库实际列，返回 ``[(column_name, fragment|None, sql|None)]``。
        纯函数，不接触数据库——是测试核心。
    """
    expected = parse_ddl_columns(ddl)
    out: list[tuple[str, str | None, str | None]] = []
    for col, defn in expected.items():
        if col in existing:
            continue
        fragment = column_to_alter_fragment(col, defn)
        sql = (
            None
            if fragment is None
            else f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col} {fragment}"
        )
        out.append((col, fragment, sql))
    return out


def _fetch_existing_columns(cur: Any, table_name: str) -> set[str]:
    """Query *information_schema.columns* for the current column set."""
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s",
        (table_name,),
    )
    return {row[0] for row in cur.fetchall()}


def _auto_add_missing_columns(cur: Any, table_name: str, ddl: str) -> int:
    """Compare DDL vs live columns and add any missing ones.

    ZH: 对比 DDL 与实际列，动态执行 ``ALTER TABLE ADD COLUMN IF NOT EXISTS``。
        跳过无法通过 ALTER 添加的列（GENERATED / SERIAL）。
        返回失败数（0 = 全部成功或无需变更）。
    """
    existing = _fetch_existing_columns(cur, table_name)
    for col, fragment, sql in build_auto_migrations(table_name, ddl, existing):
        if fragment is None:
            print(
                f"  [SKIP] auto-add {table_name}.{col:28s} "
                f"generated/serial — cannot ALTER"
            )
            continue
        cur.execute(sql)
        print(
            f"  [OK] auto-add {table_name}.{col:28s} "
            f"{cur.statusmessage}"
        )
    return 0


def _run_migrations(cur: Any) -> None:
    """Structural changes that cannot be auto-detected from the DDL.

    ZH: DDL 无法表达的结构性变更（DROP NOT NULL / DROP INDEX / DROP FK）。
        可自动检测的列添加已由 ``_auto_add_missing_columns`` 处理。
    """
    # ZH: 仅保留 DDL 无法表达的结构性变更。
    #     列添加已由 _auto_add_missing_columns 自动处理。
    # EN: Only structural changes that the DDL cannot express.
    #     Column additions are handled automatically by _auto_add_missing_columns.
    migrations: list[tuple[str, str]] = [
        (
            "candidate_event_turn_nullable",
            "ALTER TABLE candidate_intersection_events "
            "ALTER COLUMN turn_event_id DROP NOT NULL",
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
        cur.execute(sql)
        print(f"  [OK] migration {label:30s} {cur.statusmessage}")


def create_tables(
    config: PostgresConfig,
    *,
    legacy_clean_map: dict[str, str] | None = None,
) -> int:
    """Create or migrate the complete schema in one transaction.

    ZH: 任一步失败都向上传播，由连接上下文回滚整个事务。
    EN: Any error propagates so the connection context rolls back every change.
    """
    tables_created = 0
    indexes_created = 0

    with pg_connect(config) as conn:
        with conn.cursor() as cur:
            migrate_legacy_pipeline_runs(
                cur,
                legacy_clean_map or {},
                registry_ddl=dict(DDL_STATEMENTS),
                index_statements=INDEX_STATEMENTS,
            )
            # ── Create tables ──
            print("\n── 创建数据表 / Creating Tables ──")
            for name, ddl in DDL_STATEMENTS:
                cur.execute(ddl)
                status = cur.statusmessage
                print(f"  [OK] {name:40s} {status}")
                tables_created += 1

            # ── Auto column migrations: diff DDL vs information_schema ──
            print("\n── 自动列迁移 / Auto Column Migrations ──")
            for name, ddl in DDL_STATEMENTS:
                _auto_add_missing_columns(cur, name, ddl)

            # ── Structural migrations (DROP NOT NULL / DROP INDEX / DROP FK) ──
            print("\n── 结构性迁移 / Structural Migrations ──")
            _run_migrations(cur)

            # ── Create indexes ──
            print("\n── 创建索引 / Creating Indexes ──")
            for name, ddl in INDEX_STATEMENTS:
                cur.execute(ddl)
                status = cur.statusmessage
                print(f"  [OK] {name:40s} {status}")
                indexes_created += 1

        conn.commit()

    print(f"\n── 汇总 / Summary ──")
    print(f"  表 / Tables:  {tables_created} processed")
    print(f"  索引 / Indexes: {indexes_created} processed")
    print(f"  [OK] 全部成功 / All succeeded")
    return 0


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
    parser.add_argument(
        "--legacy-clean-map", type=Path,
        help="JSON object mapping ambiguous legacy Detect UUIDs to Clean UUIDs.",
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
        legacy_clean_map = (
            load_legacy_clean_map(args.legacy_clean_map)
            if args.legacy_clean_map is not None
            else {}
        )
        failures = create_tables(config, legacy_clean_map=legacy_clean_map)
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
