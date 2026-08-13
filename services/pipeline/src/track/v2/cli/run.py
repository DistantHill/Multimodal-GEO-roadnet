"""Pipeline runner: Parser 0 → Cleaner 1 → Intersect2 → Clip → Fitting → Network.

ZH: 端到端流水线 CLI 入口。完整模式读取 CSV 或 PostgreSQL 表并创建 Clean Run，
    也可按 --clean-id 复用一个已完成 Clean Run；每次都创建新的 Detect Run。

EN: End-to-end pipeline CLI. Full mode creates a Clean Run from CSV or a
    PostgreSQL table; --clean-id reuses one completed Clean Run. Every
    invocation creates a new Detect Run.

Usage / 用法::

    python -m track.v2.cli.run --csv track/data/guiji_20230301_top10w.csv --ask-password
    python -m track.v2.cli.run --csv data.csv --host 127.0.0.1 --port 15432 --database trajectory --user track_user --ask-password
    python -m track.v2.cli.run --pg-table trajectory_points --host 127.0.0.1 --port 15432 --database trajectory --user track_user --ask-password
    python -m track.v2.cli.run --clean-id UUID --ask-password
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
import uuid
from pathlib import Path

import psycopg

from ..config import PostgresConfig, connect as pg_connect, default_postgres_config
from ..logger import init_logger, shutdown_logger, LiveStatus
from ..db_writer import (
    create_clean_run,
    create_detect_run,
    create_fit_run,
    fetch_clean_run,
    finish_clean_run,
    finish_detect_run,
    finish_fit_run,
    insert_candidate_entry_exit_points,
    insert_candidate_intersection_events,
    insert_candidate_intersections,
    insert_entry_exit_points,
    insert_injected_points,
    insert_tracks_cross,
    insert_intersections,
    insert_centerline_bundle,
    insert_kept_trajectory_points,
    insert_trajectory_points,
    insert_turn_events,
)
from ..models import (
    CleanerConfig,
    ClipConfig,
    ClipResult,
    CsvSource,
    FitResult,
    FittingConfig,
    NetworkResult,
    PostgresSource,
)
from ..clip import extract_clips_for_detect
from ..fitting import fit_centerline_variants
from ..network import build_network
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
    clean_id: str | None = None,
    table: str = "trajectory_points",
) -> dict[str, int | str | list[int]]:
    """Run the full pipeline, persisting replay inputs and reporting Clip.

    ZH: 运行完整流水线并将所有阶段产物写入数据库。返回各表写入行数统计。
        ``csv_path``、``pg_source_table`` 和 ``clean_id`` 必须且只能指定一个。
        ``table`` 指定 trajectory_points 的目标表名。
    EN: Run the full pipeline, persist Parser/Cleaner/Intersect artifacts, and
        compute the in-memory Clip result. Exactly one of ``csv_path``,
        ``pg_source_table``, or ``clean_id`` must be provided. ``table`` specifies the target
        table name for trajectory_points.
    """
    if sum(value is not None for value in (csv_path, pg_source_table, clean_id)) != 1:
        raise ValueError("exactly one of csv_path, pg_source_table, or clean_id is required")

    stats: dict[str, int | str | list[int]] = {}
    t_start = time.perf_counter()
    reuse_clean = clean_id is not None
    is_pg_source = pg_source_table is not None

    if csv_path is not None:
        source = CsvSource(path=csv_path)
        source_label = csv_path
        source_id = str(Path(csv_path).resolve())
        source_kind = "csv"
    elif pg_source_table is not None:
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
        if reuse_clean:
            clean_record = fetch_clean_run(conn, str(clean_id))
            if clean_record.status != "completed":
                raise ValueError(f"Clean Run {clean_id} is not completed")
            table = str(clean_record.parameters["target_table"])
            source_id = clean_record.source_id
            source_kind = clean_record.source_kind
            is_pg_source = source_kind == "postgres_table"
        else:
            clean_id = create_clean_run(
                conn,
                source_id=source_id,
                source_kind=source_kind,
                parameters={
                    "target_table": table,
                    "cleaner_max_gap_s": CleanerConfig().max_gap_s,
                },
            )
            conn.commit()
        stats["clean_id"] = str(clean_id)

        if reuse_clean:
            total_raw = 0
            total_injected = 0
            total_excluded = 0
            tp_id_map: dict[tuple[str, str], int] = {}
            all_cleaned_subtracks: list = []
        else:
            assert clean_id is not None
        # ── Stage 1+2: Streamed read → insert → clean (per vehicle) ──
            print("── Parser 0 + Cleaner 1: 流式读取&清洗 / Streaming read & clean ──")
            print(f"  数据源 / Source: {source_label}")
            handle = get_database(source)

        # ZH: 快速统计总行数，用于行计数进度条。CSV 扫全文件，PG 走 COUNT(*)。
        # EN: Quick row count for row-based progress bar. CSV full scan, PG COUNT(*).
            t_count = time.perf_counter()
            total_rows = handle.count_rows()
            print(f"  总行数 / Total rows: {total_rows:,} ({time.perf_counter() - t_count:.1f}s)")
            total_raw = 0
            total_injected = 0
            tp_id_map = {}
            total_excluded = 0
            all_cleaned_subtracks = []
        # ZH: 批量累积排除点，每 N 辆车 flush 一次 UPDATE，消除逐车往返开销。
        # EN: Batch excluded points; flush UPDATE every N vehicles to eliminate per-vehicle round-trips.
            _pending_excluded: list = []
            _MARK_FLUSH_EVERY_N = 200

            stream = StreamVehicleSource(handle)
            live_clean = LiveStatus("Parser 0 + Cleaner 1: 流式清洗 / Streaming Clean")
            live_clean.start()

            t_read = time.perf_counter()
            vehicle_count = 0
            last_update_t = t_read
            for vehicle_points in stream.iter_vehicle_tracks():
                total_raw += len(vehicle_points)
                vehicle_count += 1
                if not is_pg_source:
                    batch_map = insert_trajectory_points(conn, vehicle_points, table=table)
                    tp_id_map.update(batch_map)
                vresult = clean_vehicle(vehicle_points)
                total_injected += vresult.injected_count
                _pending_excluded.extend(vresult.excluded)
                total_excluded += len(vresult.excluded)
                if len(_pending_excluded) >= 2000 or \
                (vehicle_count %  _MARK_FLUSH_EVERY_N) == 0:
                    # ZH: 自定义 --pg-table 的原地更新直接用 source_row_key
                    #     （即源表自身的主键），因为 tp_id_map 此时引用的是
                    #     trajectory_points 的 id，与源表主键不同。
                    # EN: Custom --pg-table
                    #     in-place updates use source_row_key directly
                    #     (the source table's own PK), since tp_id_map
                    #     references trajectory_points ids which differ.
                    _mark_map = tp_id_map if table == "trajectory_points" else {}
                    mark_excluded_points(conn, _pending_excluded, table=table, tp_id_map=_mark_map)
                    _pending_excluded.clear()
                all_cleaned_subtracks.extend(vresult.subtracks)

                now = time.perf_counter()
                if vehicle_count == 1 or vehicle_count % 500 == 0 or (now - last_update_t) >= 2.0:
                    elapsed = now - t_read
                    rate = total_raw / elapsed if elapsed > 0 else 0
                    pct = total_raw / total_rows if total_rows > 0 else 0
                    live_clean.update(
                        progress=pct,
                        detail=f"{total_raw:,}/{total_rows:,} 点, {vehicle_count:,} 车辆",
                        elapsed=elapsed,
                        throughput=f"{rate:.0f} 点/s",
                    )
                    last_update_t = now

            # Final 100% update
            # Flush remaining pending excluded points
            if _pending_excluded:
                _mark_map = tp_id_map if table == "trajectory_points" else {}
                mark_excluded_points(conn, _pending_excluded, table=table, tp_id_map=_mark_map)
                _pending_excluded.clear()
            elapsed_read = time.perf_counter() - t_read
            live_clean.update(
                progress=1.0,
                detail=f"{total_raw:,}/{total_rows:,} 点, {vehicle_count:,} 车辆",
                elapsed=elapsed_read,
                throughput=f"{total_raw / elapsed_read:.0f} 点/s" if elapsed_read > 0 else "",
            )
            live_clean.stop()
            stream.close()
            handle.close()
            print(f"  读取&清洗 / Read&Clean: {total_raw:,} 点/points, "
                  f"{vehicle_count:,} 车辆/vehicles ({elapsed_read:.1f}s)")

            stats["trajectory_points"] = total_raw

        # ── Batch-write cleaner outputs ──────────────────────────────

        print(f"  排除 / Excluded: {total_excluded:,}")
        print(f"  注入 / Injected: {total_injected:,}")

        if all_cleaned_subtracks:
            t0 = time.perf_counter()
            insert_kept_trajectory_points(conn, all_cleaned_subtracks, tp_id_map, clean_id=clean_id)
            kept_count = sum(len(st.kept) for st in all_cleaned_subtracks)
            stats["kept_trajectory_points"] = kept_count
            print(f"  kept_trajectory_points: {kept_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        if total_injected:
            t0 = time.perf_counter()
            insert_injected_points(conn, all_cleaned_subtracks, clean_id=clean_id)
            stats["injected_points"] = total_injected
            print(f"  injected_points: {total_injected:,} rows ({time.perf_counter() - t0:.1f}s)")

        # ZH: 即使 Intersect2 失败清洗结果也不会丢失。
        # EN: they survive an Intersect2 failure.
        if not reuse_clean:
            finish_clean_run(conn, clean_id, status="completed")
            conn.commit()

        detect_id = create_detect_run(
            conn,
            clean_id=clean_id,
            parameters={"target_table": table},
        )
        stats["detect_id"] = detect_id
        conn.commit()

        # ── Stage 3: Intersect2 (DB mode — spatial sliding windows) ──
        print("\n── Intersect2: 交叉口检测 / Intersection Detection (DB 滑窗模式) ──")
        # ZH: source_id 作为 intersect2 查询中 %s::text AS source_id 的参数值。
        #     PG 模式用表名，CSV 模式用文件路径。
        # EN: source_id used as the parameter for %s::text AS source_id in
        #     intersect2 queries — table name for PG, file path for CSV.
        intersect_source_id = source_id
        live_intersect = LiveStatus("Intersect2: Cross-track Detection")
        live_intersect.start()
        icfg = IntersectConfig(
            query_active_from_db=True,
            db_table=table,
            kept_table="kept_trajectory_points",
            source_id=intersect_source_id,
            clean_id=clean_id,
            live_status=live_intersect,
        )
        iresult = detect_intersections(conn=conn, config=icfg)
        live_intersect.stop()

        print(f"  转向事件 / Turn events: {iresult.report.turn_event_count:,}")
        print(f"  热点事件 / Hotspot events: {iresult.report.hotspot_event_count:,}")
        print(f"  候选交叉口 / Clusters: {iresult.report.cluster_count:,}")
        print(f"  完整交叉口 / Complete intersections: {iresult.report.complete_intersection_count:,}")

        t0 = time.perf_counter()
        tracks_cross_count = insert_tracks_cross(conn, iresult.cross_track_candidates, detect_id=detect_id)
        stats["tracks_cross"] = tracks_cross_count
        print(f"  tracks_cross: {tracks_cross_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        te_id_map = insert_turn_events(conn, iresult.turn_events, detect_id=detect_id)
        stats["turn_events"] = len(iresult.turn_events)
        print(f"  turn_events: {len(iresult.turn_events):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        ci_id_map = insert_candidate_intersections(conn, iresult.clusters, detect_id=detect_id)
        stats["candidate_intersections"] = len(iresult.clusters)
        print(f"  candidate_intersections: {len(iresult.clusters):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        insert_candidate_intersection_events(
            conn, iresult.clusters, ci_id_map, te_id_map, detect_id=detect_id,
        )
        stats["candidate_intersection_events"] = sum(len(c.events) for c in iresult.clusters)
        print(f"  candidate_intersection_events: {stats['candidate_intersection_events']:,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        ceep_id_map = insert_candidate_entry_exit_points(
            conn, iresult.clusters, ci_id_map, detect_id=detect_id,
        )
        ceep_count = sum(len(c.crossing_points) for c in iresult.clusters)
        stats["candidate_entry_exit_points"] = ceep_count
        print(f"  candidate_entry_exit_points: {ceep_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        clusters_by_id = {c.cluster_id: c for c in iresult.clusters}
        t0 = time.perf_counter()
        int_id_map = insert_intersections(
            conn, iresult.topologies, ci_id_map, clusters_by_id, detect_id=detect_id,
        )
        stats["intersections"] = len(int_id_map)
        print(f"  intersections: {len(int_id_map):,} rows ({time.perf_counter() - t0:.1f}s)")

        t0 = time.perf_counter()
        insert_entry_exit_points(
            conn, iresult.observations, int_id_map, ceep_id_map, detect_id=detect_id,
        )
        entry_exit_count = sum(
            1 for observation in iresult.observations
            if observation.cluster_id in int_id_map
        )
        stats["entry_exit_points"] = entry_exit_count
        print(f"  entry_exit_points: {entry_exit_count:,} rows ({time.perf_counter() - t0:.1f}s)")

        finish_detect_run(conn, detect_id, status="completed")
        conn.commit()

        # ── Stage 4: Clip (pure result, replayable from this Detect Run) ────────────
        print("\n── Clip: 路段切分 / Track clipping ──")
        clip_config = ClipConfig()
        clip_result = extract_clips_for_detect(
            conn, detect_id, config=clip_config,
        )
        _add_clip_stats(stats, clip_result)
        _print_clip_report(clip_result)

        # ── Stage 5: Fitting (one centerline per connected pair) ────────────
        print("\n── Fitting: 中心线拟合 / Centerline fitting ──")
        fitting_config = FittingConfig()
        fit_id = create_fit_run(
            conn,
            detect_id=detect_id,
            clip_config=clip_config,
            fitting_config=fitting_config,
        )
        stats["fit_id"] = fit_id
        conn.commit()
        try:
            fit_bundle = fit_centerline_variants(clip_result, config=fitting_config)
            centerline_ids = insert_centerline_bundle(
                conn, fit_bundle, clip_result, fit_id=fit_id, detect_id=detect_id,
            )
            report = {
                "combined": {
                    "report": fit_bundle.combined.report.as_dict(),
                    "issues": [issue.as_dict() for issue in fit_bundle.combined.issues],
                },
                "a_to_b": {
                    "report": fit_bundle.a_to_b.report.as_dict(),
                    "issues": [issue.as_dict() for issue in fit_bundle.a_to_b.issues],
                },
                "b_to_a": {
                    "report": fit_bundle.b_to_a.report.as_dict(),
                    "issues": [issue.as_dict() for issue in fit_bundle.b_to_a.issues],
                },
            }
            finish_fit_run(conn, fit_id, status="completed", report=report)
            conn.commit()
        except (psycopg.Error, RuntimeError, ValueError):
            conn.rollback()
            finish_fit_run(conn, fit_id, status="failed")
            conn.commit()
            raise
        stats["centerlines"] = len(centerline_ids)
        stats["centerline_ids"] = list(centerline_ids)
        _add_fit_stats(stats, fit_bundle.combined)
        fit_bundle.combined.report.format_print(title="  Fitting 报告 / Fitting report")

        # ── Stage 6: Network (intersections as nodes, centerlines as edges) ─
        print("\n── Network: 路网组装 / Network assembly ──")
        network_result = build_network(fit_bundle.combined, iresult)
        _add_network_stats(stats, network_result)
        network_result.report.format_print(title="  Network 报告 / Network report")

        print(f"\n  提交 / Committed.")

    elapsed = time.perf_counter() - t_start
    stats["_elapsed_s"] = int(elapsed)
    print(f"\n── 完成 / Done ({elapsed:.1f}s) ──")
    return stats


def _add_clip_stats(stats: dict[str, int | str | list[int]], result: ClipResult) -> None:
    stats.update(
        {
            f"clip_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


def _print_clip_report(result: ClipResult) -> None:
    result.report.format_print(title="  Clip 报告 / Clip report")


def _add_fit_stats(stats: dict[str, int | str | list[int]], result: FitResult) -> None:
    stats.update(
        {
            f"fitting_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


def _add_network_stats(stats: dict[str, int | str | list[int]], result: NetworkResult) -> None:
    stats.update(
        {
            f"network_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


# ── CLI ───────────────────────────────────────────────────────────────


def _uuid_arg(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def main() -> int:
    # ZH: 强制 stdout/stderr 走 UTF-8，确保 pipe 环境（conda run 等）下中文不乱码。
    # EN: Force stdout/stderr to UTF-8 so CJK text survives pipe environments (conda run etc.).
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    # ZH: 从 .env 加载默认连接参数，CLI 参数可覆盖
    # EN: Load defaults from .env; CLI args take precedence
    _env = default_postgres_config()

    parser = argparse.ArgumentParser(
        description="Run track.v2 through Network."
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
    source_group.add_argument(
        "--clean-id", metavar="UUID", type=_uuid_arg,
        help="复用一个已完成 Clean Run，只创建新的 Detect Run / Reuse one completed Clean Run."
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

    if args.clean_id and args.table is not None:
        parser.error("--clean-id 不可覆盖 --table / cannot override --table")

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

    source_label = (
        f"clean:{args.clean_id}" if args.clean_id
        else args.csv or f"postgresql://{db_config.database}/{args.pg_table}"
    )
    init_logger()
    print(f"数据源 / Source: {source_label}")
    print(f"连接目标 / Target: {db_config.safe_dsn()}")

    try:
        stats = run_pipeline(
            db_config,
            csv_path=args.csv,
            pg_source_table=args.pg_table,
            clean_id=str(args.clean_id) if args.clean_id else None,
            table=args.table,
        )
        _print_write_summary(stats)
        return 0
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


def _print_write_summary(stats: dict[str, int | str | list[int]]) -> None:
    """Print model-driven Clip fields plus persisted table row counts."""
    print("\n── 写入统计 / Write Summary ──")
    for table in [
        "trajectory_points", "kept_trajectory_points",
        "injected_points", "tracks_cross", "turn_events", "candidate_intersections",
        "candidate_intersection_events", "candidate_entry_exit_points",
        "intersections", "entry_exit_points",
    ]:
        if table in stats:
            print(f"  {table:40s} {stats[table]:>10,} rows")
    for name, value in stats.items():
        if name.startswith("clip_") and isinstance(value, int):
            print(f"  {name:40s} {value:>10,}")


if __name__ == "__main__":
    raise SystemExit(main())
