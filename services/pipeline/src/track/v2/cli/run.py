"""Pipeline runner: Parser 0 → Cleaner 1 → Intersect2 → Clip.

ZH: 端到端流水线 CLI 入口。读取 CSV 或 PostgreSQL 表，运行四个阶段；前三阶段
    的产物写入 PostgreSQL，Clip 返回纯内存结果。也可按 run_id 单独重放 Clip。
    --csv、--pg-table 和 --replay-clips 互斥。

EN: End-to-end pipeline CLI. It reads CSV or PostgreSQL, runs four stages,
    persists the first three stages, and returns Clip as a pure in-memory
    result. Clip can also be replayed by run_id. --csv, --pg-table, and
    --replay-clips are mutually exclusive.

Usage / 用法::

    python -m track.v2.cli.run --csv track/data/guiji_20230301_top10w.csv --ask-password
    python -m track.v2.cli.run --csv data.csv --host 127.0.0.1 --port 15432 --database trajectory --user track_user --ask-password
    python -m track.v2.cli.run --pg-table trajectory_points --host 127.0.0.1 --port 15432 --database trajectory --user track_user --ask-password
    python -m track.v2.cli.run --replay-clips RUN_ID --ask-password
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
    finish_pipeline_run,
    insert_candidate_entry_exit_points,
    insert_candidate_intersection_events,
    insert_candidate_intersections,
    insert_entry_exit_points,
    insert_injected_points,
    insert_tracks_cross,
    insert_intersections,
    insert_kept_trajectory_points,
    insert_trajectory_points,
    insert_turn_events,
)
from ..models import CleanerConfig, ClipResult, CsvSource, PostgresSource
from ..clip import extract_clips_for_run
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
    skip_clean: bool = False,
) -> dict[str, int | str]:
    """Run the full pipeline, persisting replay inputs and reporting Clip.

    ZH: 运行完整流水线并将所有阶段产物写入数据库。返回各表写入行数统计。
        ``csv_path`` 和 ``pg_source_table`` 必须且只能指定一个。
        ``table`` 指定 trajectory_points 的目标表名。
        ``skip_clean`` 跳过 Parser0+Cleaner1，直接从 is_active 列读取已清洗结果。
    EN: Run the full pipeline, persist Parser/Cleaner/Intersect artifacts, and
        compute the in-memory Clip result. Returns row/result counts. Exactly one of ``csv_path`` or
        ``pg_source_table`` must be provided. ``table`` specifies the target
        table name for trajectory_points.
        ``skip_clean`` skips Parser0+Cleaner1 and reads pre-cleaned results
        from the is_active column directly.
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
            parameters={
                "target_table": table,
                "skip_clean": skip_clean,
                "cleaner_max_gap_s": CleanerConfig().max_gap_s,
            },
        )
        stats["run_id"] = run_id
        # Keep the run record even if a later pipeline statement rolls back.
        # A non-completed row is an explicit interrupted/failed-run record.
        conn.commit()
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
        tp_id_map: dict[tuple[str, str], int] = {}
        total_excluded = 0
        all_kept_subtracks: list[list] = []
        all_injected_points: list = []
        # ZH: 批量累积排除点，每 N 辆车 flush 一次 UPDATE，消除逐车往返开销。
        # EN: Batch excluded points; flush UPDATE every N vehicles to eliminate per-vehicle round-trips.
        _pending_excluded: list = []
        _MARK_FLUSH_EVERY_N = 200

        if skip_clean:
            # ZH: 跳过 Parser0+Cleaner1——is_active 已在之前运行中持久化，
            #     直接复用已有清洗结果，省去逐行 parse_row + clean_vehicle + mark_excluded。
            # EN: Skip Parser0+Cleaner1 — is_active is already persisted from a
            #     prior run; reuse existing clean results directly.
            handle.close()
            total_raw = total_rows
            vehicle_count = 0
            stats["trajectory_points"] = total_raw
            print(f"  跳过清洗 / Skip clean: {total_raw:,} 点 (is_active 已持久化/persisted)")
        else:
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
                    batch_map = insert_trajectory_points(conn, vehicle_points, table="trajectory_points")
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
                all_kept_subtracks.extend(vresult.kept_subtracks)

                # Collect injected points for batch insert
                for p in vresult.active:
                    if p.point_ref.generated_reason == "orthogonal_heading":
                        all_injected_points.append(p)

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

        # ZH: 即使 Intersect2 失败清洗结果也不会丢失。
        # EN: they survive an Intersect2 failure.
        conn.commit()

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
            run_id=None if skip_clean else run_id,
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

        # ── Stage 4: Clip (pure result, replayable from this run) ────────────
        print("\n── Clip: 路段切分 / Track clipping ──")
        clip_result = extract_clips_for_run(conn, run_id, allow_running=True)
        _add_clip_stats(stats, clip_result)
        _print_clip_report(clip_result)

        finish_pipeline_run(conn, run_id, status="completed")
        conn.commit()
        print(f"\n  提交 / Committed.")

    elapsed = time.perf_counter() - t_start
    stats["_elapsed_s"] = int(elapsed)
    print(f"\n── 完成 / Done ({elapsed:.1f}s) ──")
    return stats


def _add_clip_stats(stats: dict[str, int | str], result: ClipResult) -> None:
    stats.update(
        {
            f"clip_{name}": value
            for name, value in result.report.as_dict().items()
            if isinstance(value, int)
        }
    )


def _print_clip_report(result: ClipResult) -> None:
    result.report.format_print(title="  Clip 报告 / Clip report")


def replay_clips(db_config: PostgresConfig, run_id: str) -> ClipResult:
    """Replay the pure Clip stage from one completed pipeline run."""
    with pg_connect(db_config) as conn:
        return extract_clips_for_run(conn, run_id)


# ── CLI ───────────────────────────────────────────────────────────────


def main() -> int:
    # ZH: 从 .env 加载默认连接参数，CLI 参数可覆盖
    # EN: Load defaults from .env; CLI args take precedence
    _env = default_postgres_config()

    parser = argparse.ArgumentParser(
        description="Run track.v2 through Clip, or replay Clip from a completed run."
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
        "--replay-clips", metavar="RUN_ID",
        help="从已完成的流水线运行重放 Clip / Replay Clip from a completed pipeline run."
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
    parser.add_argument(
        "--skip-clean", action="store_true",
        help="跳过 Parser0+Cleaner1，直接从 trajectory_points.is_active 读取已清洗结果进入 Intersect2。"
             "仅支持 --pg-table 模式 / Requires --pg-table mode."
    )
    args = parser.parse_args()

    # ZH: --skip-clean 仅对 PG 源有效
    # EN: --skip-clean is only valid with --pg-table
    if args.skip_clean and not args.pg_table:
        parser.error("--skip-clean 仅支持 --pg-table 模式 / requires --pg-table")

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
        f"pipeline-run:{args.replay_clips}"
        if args.replay_clips
        else args.csv or f"postgresql://{db_config.database}/{args.pg_table}"
    )
    init_logger()
    print(f"数据源 / Source: {source_label}")
    print(f"连接目标 / Target: {db_config.safe_dsn()}")

    try:
        if args.replay_clips:
            clip_result = replay_clips(db_config, args.replay_clips)
            print("\n── Clip 回放 / Clip Replay ──")
            _print_clip_report(clip_result)
            return 0
        stats = run_pipeline(
            db_config,
            csv_path=args.csv,
            pg_source_table=args.pg_table,
            table=args.table,
            skip_clean=args.skip_clean,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
