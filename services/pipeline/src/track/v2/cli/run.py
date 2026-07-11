"""End-to-end pipeline runner: Parser 0 → Cleaner 1 → Intersect2 → PostgreSQL.

ZH: 端到端流水线 CLI 入口。读取 CSV 或 PostgreSQL 表，依次运行三个流水线阶段，
    所有产物写入 PostgreSQL 数据库。CLI 仅负责参数解析和委托。
    --csv 和 --pg-table 互斥——必须指定其中一个作为数据源。

EN: End-to-end pipeline CLI. Reads CSV or a PostgreSQL table, runs all three
    pipeline stages, writes all outputs to PostgreSQL. CLI handles argument
    parsing only. --csv and --pg-table are mutually exclusive — exactly one
    must be provided as the data source.

Usage / 用法::

    python -m track.v2.cli.run --csv track/data/guiji_20230301_top10w.csv --ask-password
    python -m track.v2.cli.run --csv data.csv --host 127.0.0.1 --port 15432 --database trajectory --user track_user --ask-password
    python -m track.v2.cli.run --pg-table trajectory_points --host 127.0.0.1 --port 15432 --database trajectory --user track_user --ask-password
"""

from __future__ import annotations

import argparse
import getpass
import time

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..db_writer import (
    fetch_trajectory_point_ids,
    insert_candidate_entry_exit_points,
    insert_candidate_intersection_events,
    insert_candidate_intersections,
    insert_entry_exit_points,
    insert_excluded_points,
    insert_injected_points,
    insert_intersections,
    insert_kept_trajectory_points,
    insert_trajectory_points,
    insert_turn_events,
)
from ..models import CsvSource, ExcludedIndex, PostgresSource
from .. import get_database, detect_intersections, IntersectConfig
from ..cleaner import clean_vehicle
from ..sources import StreamVehicleSource
from ..db_writer import mark_excluded_points


# ── Pipeline runner ──────────────────────────────────────────────────

def run_pipeline(
    db_config: PostgresConfig,
    *,
    csv_path: str | None = None,
    pg_source_table: str | None = None,
    table: str = "trajectory_points",
) -> dict[str, int]:
    """Run the full pipeline and write all outputs to PostgreSQL.

    ZH: 运行完整流水线并将所有阶段产物写入数据库。返回各表写入行数统计。
        ``csv_path`` 和 ``pg_source_table`` 必须且只能指定一个。
        ``table`` 指定 trajectory_points 的目标表名。
    EN: Run the full pipeline and write all stage outputs to the database.
        Returns per-table row counts. Exactly one of ``csv_path`` or
        ``pg_source_table`` must be provided. ``table`` specifies the target
        table name for trajectory_points.
    """
    import psycopg

    if csv_path is not None and pg_source_table is not None:
        raise ValueError("只能指定 --csv 或 --pg-table 中的一个 / csv_path and pg_source_table are mutually exclusive")
    if csv_path is None and pg_source_table is None:
        raise ValueError("必须指定 --csv 或 --pg-table / either csv_path or pg_source_table must be provided")

    stats: dict[str, int] = {}
    t_start = time.perf_counter()
    is_pg_source = pg_source_table is not None

    if csv_path is not None:
        source = CsvSource(path=csv_path)
        source_label = csv_path
    else:
        source = PostgresSource(
            config=db_config,
            query=f"SELECT * FROM {pg_source_table} ORDER BY vehicle_id, timestamp",
            source_id=pg_source_table,
            primary_key_field="id",
        )
        source_label = f"postgresql://{db_config.database}/{pg_source_table}"

    with pg_connect(db_config) as conn:
        # ── Stage 1+2: Streamed read → insert → clean (per vehicle) ──
        print("── Parser 0 + Cleaner 1: 流式读取&清洗 / Streaming read & clean ──")
        print(f"  数据源 / Source: {source_label}")
        handle = get_database(source)
        stream = StreamVehicleSource(handle)

        total_raw = 0
        total_injected = 0
        tp_id_map: dict[tuple[str, str], int] = {}
        all_excluded: list = []
        all_kept_subtracks: list[list] = []
        all_injected_points: list = []

        t_read = time.perf_counter()
        vehicle_count = 0
        for vehicle_points in stream.iter_vehicle_tracks():
            total_raw += len(vehicle_points)
            vehicle_count += 1

            if not is_pg_source:
                # CSV: insert raw points to DB
                batch_map = insert_trajectory_points(conn, vehicle_points, table=table)
                tp_id_map.update(batch_map)

            # Clean this vehicle
            vresult = clean_vehicle(vehicle_points)
            total_injected += vresult.injected_count

            # Mark excluded points as is_active=FALSE in DB
            mark_excluded_points(conn, vresult.excluded, table=table)
            all_excluded.extend(vresult.excluded)
            all_kept_subtracks.extend(vresult.kept_subtracks)

            # Collect injected points for batch insert
            for p in vresult.active:
                if p.point_ref.generated_reason == "orthogonal_heading":
                    all_injected_points.append(p)

            if vehicle_count % 500 == 0:
                elapsed = time.perf_counter() - t_read
                print(f"    ... {vehicle_count:,} 车辆/vehicles, {total_raw:,} 点/points ({elapsed:.1f}s)")

        stream.close()
        handle.close()
        elapsed_read = time.perf_counter() - t_read
        print(f"  读取&清洗 / Read&Clean: {total_raw:,} 点/points, "
              f"{vehicle_count:,} 车辆/vehicles ({elapsed_read:.1f}s)")

        stats["trajectory_points"] = total_raw

        if is_pg_source:
            # Points already in DB — fetch existing ID mapping for FK references
            t0 = time.perf_counter()
            tp_id_map = fetch_trajectory_point_ids(conn, pg_source_table, table=table)
            print(f"  ID 映射 / ID map: {len(tp_id_map):,} entries ({time.perf_counter() - t0:.1f}s)")

        # ── Batch-write cleaner outputs ──────────────────────────────
        excluded_index = ExcludedIndex(all_excluded)
        print(f"  排除 / Excluded: {len(all_excluded):,}")
        print(f"  注入 / Injected: {total_injected:,}")

        t0 = time.perf_counter()
        insert_excluded_points(conn, excluded_index, tp_id_map)
        stats["excluded_points"] = len(all_excluded)
        print(f"  excluded_points: {len(all_excluded):,} rows ({time.perf_counter() - t0:.1f}s)")

        if all_kept_subtracks:
            t0 = time.perf_counter()
            insert_kept_trajectory_points(conn, all_kept_subtracks, tp_id_map)
            kept_count = sum(len(st) for st in all_kept_subtracks)
            stats["kept_trajectory_points"] = kept_count
            print(f"  kept_trajectory_points: {kept_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        if all_injected_points:
            t0 = time.perf_counter()
            insert_injected_points(conn, all_injected_points)
            stats["injected_points"] = total_injected
            print(f"  injected_points: {total_injected:,} rows ({time.perf_counter() - t0:.1f}s)")

        # ── Stage 3: Intersect2 (DB mode — spatial sliding windows) ──
        print("\n── Intersect2: 交叉口检测 / Intersection Detection (DB 滑窗模式) ──")
        icfg = IntersectConfig(
            query_active_from_db=True,
            db_table=table,
        )
        iresult = detect_intersections(conn=conn, config=icfg)

        print(f"  转向事件 / Turn events: {iresult.report.turn_event_count:,}")
        print(f"  热点事件 / Hotspot events: {iresult.report.hotspot_event_count:,}")
        print(f"  候选交叉口 / Clusters: {iresult.report.cluster_count:,}")
        print(f"  完整交叉口 / Complete intersections: {iresult.report.complete_intersection_count:,}")

        t0 = time.perf_counter()
        te_id_map = insert_turn_events(conn, iresult.turn_events)
        stats["turn_events"] = len(iresult.turn_events)
        print(f"  turn_events: {len(iresult.turn_events):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        ci_id_map = insert_candidate_intersections(conn, iresult.clusters)
        stats["candidate_intersections"] = len(iresult.clusters)
        print(f"  candidate_intersections: {len(iresult.clusters):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        insert_candidate_intersection_events(conn, iresult.clusters, ci_id_map, te_id_map)
        stats["candidate_intersection_events"] = sum(len(c.events) for c in iresult.clusters)
        print(f"  candidate_intersection_events: {stats['candidate_intersection_events']:,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        ceep_id_map = insert_candidate_entry_exit_points(conn, iresult.clusters, ci_id_map)
        ceep_count = sum(len(c.crossing_points) for c in iresult.clusters)
        stats["candidate_entry_exit_points"] = ceep_count
        print(f"  candidate_entry_exit_points: {ceep_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        clusters_by_id = {c.cluster_id: c for c in iresult.clusters}
        t0 = time.perf_counter()
        int_id_map = insert_intersections(conn, iresult.topologies, ci_id_map, clusters_by_id)
        stats["intersections"] = len(iresult.topologies)
        print(f"  intersections: {len(iresult.topologies):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        insert_entry_exit_points(conn, iresult.observations, int_id_map)
        stats["entry_exit_points"] = len(iresult.observations)
        print(f"  entry_exit_points: {len(iresult.observations):,} rows ({time.perf_counter() - t0:.1f}s)")

        conn.commit()
        print(f"\n  提交 / Committed.")

    elapsed = time.perf_counter() - t_start
    stats["_elapsed_s"] = int(elapsed)
    print(f"\n── 完成 / Done ({elapsed:.1f}s) ──")
    return stats


# ── CLI ───────────────────────────────────────────────────────────────


def main() -> int:
    # ZH: 从 .env 加载默认连接参数，CLI 参数可覆盖
    # EN: Load defaults from .env; CLI args take precedence
    _env = default_postgres_config()

    parser = argparse.ArgumentParser(
        description="Run track.v2 pipeline and write all outputs to PostgreSQL."
    )
    # ── 互斥数据源 / Mutually exclusive data sources ──
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--csv", metavar="PATH",
        help="从 CSV 文件读取输入轨迹点 / Read input trajectory points from a CSV file."
    )
    source_group.add_argument(
        "--pg-table", metavar="TABLE",
        help="从 PostgreSQL 表读取输入轨迹点 / Read input trajectory points from a PostgreSQL table."
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
        "--table", default="trajectory_points",
        help="目标表名 / Target table name for trajectory_points (default: trajectory_points)."
    )
    args = parser.parse_args()

    # ── Resolve password: CLI prompt > .env > empty ──
    if args.ask_password:
        password = getpass.getpass("PostgreSQL password: ")
    else:
        password = _env.password

    db_config = PostgresConfig(
        host=args.host,
        port=args.port,
        database=args.database,
        user=args.user,
        password=password,
        connect_timeout=args.connect_timeout,
        sslmode=args.sslmode,
    )

    source_label = args.csv or f"postgresql://{db_config.database}/{args.pg_table}"
    print(f"数据源 / Source: {source_label}")
    print(f"连接目标 / Target: {db_config.safe_dsn()}")

    try:
        stats = run_pipeline(
            db_config,
            csv_path=args.csv,
            pg_source_table=args.pg_table,
            table=args.table,
        )
    except RuntimeError as exc:
        print(str(exc))
        return 2
    except Exception as exc:
        print(f"[FAIL] 流水线失败 / Pipeline failed: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    # ── Summary ──
    print("\n── 写入统计 / Write Summary ──")
    for table in [
        "trajectory_points", "excluded_points", "kept_trajectory_points",
        "injected_points", "turn_events", "candidate_intersections",
        "candidate_intersection_events", "candidate_entry_exit_points",
        "intersections", "entry_exit_points",
    ]:
        if table in stats:
            print(f"  {table:40s} {stats[table]:>10,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
