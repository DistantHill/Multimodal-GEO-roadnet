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
from pathlib import Path

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..logger import init_logger, shutdown_logger, LiveStatus
from ..db_writer import (
    create_pipeline_run,
    fetch_trajectory_point_ids,
    finish_pipeline_run,
    insert_candidate_entry_exit_points,
    insert_candidate_intersection_events,
    insert_candidate_intersections,
    insert_entry_exit_points,
    insert_excluded_points,
    insert_injected_points,
    insert_tracks_cross,
    insert_intersections,
    insert_kept_trajectory_points,
    reset_active_points,
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
) -> dict[str, int | str]:
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

    stats: dict[str, int | str] = {}
    t_start = time.perf_counter()
    is_pg_source = pg_source_table is not None

    if csv_path is not None:
        source = CsvSource(path=csv_path)
        source_label = csv_path
        source_id = str(Path(csv_path).resolve())
        source_kind = "csv"
    else:
        source = PostgresSource(
            config=db_config,
            query=f"SELECT * FROM {pg_source_table} ORDER BY vehicle_id, timestamp",
            source_id=pg_source_table,
            primary_key_field="id",
        )
        source_label = f"postgresql://{db_config.database}/{pg_source_table}"
        source_id = str(pg_source_table)
        source_kind = "postgres_table"

    with pg_connect(db_config) as conn:
        run_id = create_pipeline_run(
            conn,
            source_id=source_id,
            source_kind=source_kind,
            parameters={"target_table": table},
        )
        stats["run_id"] = run_id
        # Keep the run record even if a later pipeline statement rolls back.
        # A non-completed row is an explicit interrupted/failed-run record.
        conn.commit()
        reset_active_points(conn, table=table)
        # ── Stage 1+2: Streamed read → insert → clean (per vehicle) ──
        print("── Parser 0 + Cleaner 1: 流式读取&清洗 / Streaming read & clean ──")
        print(f"  数据源 / Source: {source_label}")
        handle = get_database(source)

        # ZH: 快速统计车辆总数，用于进度条。CSV 扫 vehicle_id 列，PG 走 COUNT DISTINCT。
        # EN: Quick vehicle count for progress bar. CSV scans vehicle_id column, PG uses COUNT DISTINCT.
        t_count = time.perf_counter()
        total_vehicles = handle.count_vehicle_ids()
        print(f"  车辆总数 / Total vehicles: {total_vehicles:,} ({time.perf_counter() - t_count:.1f}s)")
        stream = StreamVehicleSource(handle)

        total_raw = 0
        total_injected = 0
        tp_id_map: dict[tuple[str, str], int] = {}
        all_excluded: list = []
        all_kept_subtracks: list[list] = []
        all_injected_points: list = []

        live_clean = LiveStatus("Parser 0 + Cleaner 1: 流式清洗 / Streaming Clean")
        live_clean.start()

        t_read = time.perf_counter()
        vehicle_count = 0
        last_update_t = t_read
        for vehicle_points in stream.iter_vehicle_tracks():
            total_raw += len(vehicle_points)
            vehicle_count += 1

            if not is_pg_source or pg_source_table != table:
                # Persist both CSV rows and project-table rows as replayable
                # raw points.  Reading the target table itself is the sole
                # case where the rows already live in that table.
                batch_map = insert_trajectory_points(conn, vehicle_points, table=table)
                tp_id_map.update(batch_map)

            # Clean this vehicle
            vresult = clean_vehicle(vehicle_points)
            total_injected += vresult.injected_count

            # Mark excluded points as is_active=FALSE in DB
            mark_excluded_points(conn, vresult.excluded, table=table, tp_id_map=tp_id_map)
            all_excluded.extend(vresult.excluded)
            all_kept_subtracks.extend(vresult.kept_subtracks)

            # Collect injected points for batch insert
            for p in vresult.active:
                if p.point_ref.generated_reason == "orthogonal_heading":
                    all_injected_points.append(p)

            now = time.perf_counter()
            if vehicle_count == 1 or vehicle_count % 500 == 0 or (now - last_update_t) >= 2.0:
                elapsed = now - t_read
                rate = vehicle_count / elapsed if elapsed > 0 else 0
                pct = vehicle_count / total_vehicles if total_vehicles > 0 else 0
                live_clean.update(
                    progress=pct,
                    detail=f"{vehicle_count:,}/{total_vehicles:,} 车辆, {total_raw:,} 点",
                    elapsed=elapsed,
                    throughput=f"{rate:.1f} 车辆/s",
                )
                last_update_t = now

        # Final 100% update
        elapsed_read = time.perf_counter() - t_read
        live_clean.update(
            progress=1.0,
            detail=f"{vehicle_count:,}/{total_vehicles:,} 车辆, {total_raw:,} 点",
            elapsed=elapsed_read,
            throughput=f"{vehicle_count / elapsed_read:.1f} 车辆/s" if elapsed_read > 0 else "",
        )
        live_clean.stop()
        stream.close()
        handle.close()
        print(f"  读取&清洗 / Read&Clean: {total_raw:,} 点/points, "
              f"{vehicle_count:,} 车辆/vehicles ({elapsed_read:.1f}s)")

        stats["trajectory_points"] = total_raw

        if is_pg_source:
            # Points already in DB — fetch existing ID mapping for FK references.
            # ZH: PG 表模式下，数据可能含多种 source_id，用 pg_source_table 过滤
            #     会漏掉所有行。当 pg_source_table == table 时不传 source_id，
            #     查询该表全部点。
            # EN: In PG-table mode the table may contain multiple source_ids.
            #     When pg_source_table == table, query all points without filtering.
            t0 = time.perf_counter()
            _fetch_src = pg_source_table if pg_source_table != table else None
            tp_id_map = fetch_trajectory_point_ids(conn, _fetch_src, table=table)
            print(f"  ID 映射 / ID map: {len(tp_id_map):,} entries ({time.perf_counter() - t0:.1f}s)")

        # ── Batch-write cleaner outputs ──────────────────────────────
        if not is_pg_source or pg_source_table != table:
            # Imports have just been inserted; read the complete map so repeat
            # runs keep their raw-point references stable.
            tp_id_map = fetch_trajectory_point_ids(conn, source_id, table=table)

        excluded_index = ExcludedIndex(all_excluded)
        print(f"  排除 / Excluded: {len(all_excluded):,}")
        print(f"  注入 / Injected: {total_injected:,}")

        t0 = time.perf_counter()
        insert_excluded_points(conn, excluded_index, tp_id_map, run_id=run_id)
        stats["excluded_points"] = len(all_excluded)
        print(f"  excluded_points: {len(all_excluded):,} rows ({time.perf_counter() - t0:.1f}s)")

        if all_kept_subtracks:
            t0 = time.perf_counter()
            insert_kept_trajectory_points(conn, all_kept_subtracks, tp_id_map, run_id=run_id)
            kept_count = sum(len(st) for st in all_kept_subtracks)
            stats["kept_trajectory_points"] = kept_count
            print(f"  kept_trajectory_points: {kept_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        if all_injected_points:
            t0 = time.perf_counter()
            insert_injected_points(conn, all_injected_points, run_id=run_id)
            stats["injected_points"] = total_injected
            print(f"  injected_points: {total_injected:,} rows ({time.perf_counter() - t0:.1f}s)")

        # ── Stage 3: Intersect2 (DB mode — spatial sliding windows) ──
        print("\n── Intersect2: 交叉口检测 / Intersection Detection (DB 滑窗模式) ──")
        # ZH: source_id 作为 intersect2 查询中 %s::text AS source_id 的参数值。
        #     PG 模式用表名，CSV 模式用文件路径。
        # EN: source_id used as the parameter for %s::text AS source_id in
        #     intersect2 queries — table name for PG, file path for CSV.
        intersect_source_id = pg_source_table if is_pg_source else source_id
        live_intersect = LiveStatus("Intersect2: Cross-track Detection")
        live_intersect.start()
        icfg = IntersectConfig(
            query_active_from_db=True,
            db_table=table,
            source_id=intersect_source_id,
            run_id=run_id,
            live_status=live_intersect,
        )
        iresult = detect_intersections(conn=conn, config=icfg)
        live_intersect.stop()

        print(f"  转向事件 / Turn events: {iresult.report.turn_event_count:,}")
        print(f"  热点事件 / Hotspot events: {iresult.report.hotspot_event_count:,}")
        print(f"  候选交叉口 / Clusters: {iresult.report.cluster_count:,}")
        print(f"  完整交叉口 / Complete intersections: {iresult.report.complete_intersection_count:,}")

        t0 = time.perf_counter()
        tracks_cross_count = insert_tracks_cross(conn, iresult.cross_track_candidates, run_id=run_id)
        stats["tracks_cross"] = tracks_cross_count
        print(f"  tracks_cross: {tracks_cross_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        te_id_map = insert_turn_events(conn, iresult.turn_events, run_id=run_id)
        stats["turn_events"] = len(iresult.turn_events)
        print(f"  turn_events: {len(iresult.turn_events):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        ci_id_map = insert_candidate_intersections(conn, iresult.clusters, run_id=run_id)
        stats["candidate_intersections"] = len(iresult.clusters)
        print(f"  candidate_intersections: {len(iresult.clusters):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        insert_candidate_intersection_events(
            conn, iresult.clusters, ci_id_map, te_id_map, run_id=run_id,
        )
        stats["candidate_intersection_events"] = sum(len(c.events) for c in iresult.clusters)
        print(f"  candidate_intersection_events: {stats['candidate_intersection_events']:,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        ceep_id_map = insert_candidate_entry_exit_points(
            conn, iresult.clusters, ci_id_map, run_id=run_id,
        )
        ceep_count = sum(len(c.crossing_points) for c in iresult.clusters)
        stats["candidate_entry_exit_points"] = ceep_count
        print(f"  candidate_entry_exit_points: {ceep_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        clusters_by_id = {c.cluster_id: c for c in iresult.clusters}
        t0 = time.perf_counter()
        int_id_map = insert_intersections(
            conn, iresult.topologies, ci_id_map, clusters_by_id, run_id=run_id,
        )
        stats["intersections"] = len(int_id_map)
        print(f"  intersections: {len(int_id_map):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        insert_entry_exit_points(
            conn, iresult.observations, int_id_map, ceep_id_map, run_id=run_id,
        )
        entry_exit_count = sum(
            1 for observation in iresult.observations
            if observation.cluster_id in int_id_map
        )
        stats["entry_exit_points"] = entry_exit_count
        print(f"  entry_exit_points: {entry_exit_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        finish_pipeline_run(conn, run_id, status="completed")
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
        "--table", default=None,
        help="目标表名 / Target table name for trajectory_points "
             "(default: same as --pg-table for PG mode, 'trajectory_points' for CSV mode)."
    )
    args = parser.parse_args()

    # ZH: 若未显式指定 --table，PG 模式默认写到源表（原地工作），CSV 模式写到 trajectory_points。
    # EN: If --table is not set, default to the PG source table (work in-place)
    #     or 'trajectory_points' for CSV imports.
    if args.table is None:
        args.table = args.pg_table or "trajectory_points"

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
    init_logger()
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
    finally:
        shutdown_logger()

    # ── Summary ──
    print("\n── 写入统计 / Write Summary ──")
    for table in [
        "trajectory_points", "excluded_points", "kept_trajectory_points",
        "injected_points", "tracks_cross", "turn_events", "candidate_intersections",
        "candidate_intersection_events", "candidate_entry_exit_points",
        "intersections", "entry_exit_points",
    ]:
        if table in stats:
            print(f"  {table:40s} {stats[table]:>10,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
