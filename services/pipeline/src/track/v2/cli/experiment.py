"""Single entrypoint for a local experiment workspace and archived history."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

from ..experiments.config import load_workspace
from ..experiments.runner import archive_local, evaluate_local, run_batch


def parameter_comments() -> dict[tuple[str, ...], str]:
    """Reuse the maintained Pipeline template descriptions for base parameters."""
    comments = {}
    template = Path(__file__).resolve().parents[4] / "config" / "pipeline.toml"
    section: tuple[str, ...] = ()
    last_key = None
    for line in template.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = tuple(stripped.strip("[]").split("."))
            last_key = None
        elif "=" in line and not stripped.startswith("#"):
            key = line.split("=", 1)[0].strip()
            last_key = (*section, key)
            parts = re.split(r"\s+#", line, maxsplit=1)
            if len(parts) == 2:
                comments[last_key] = parts[1].strip()
        elif stripped.startswith("#") and last_key is not None:
            comments[last_key] = (comments.get(last_key, "") + " " + stripped[1:].strip()).strip()
    return comments


EXPERIMENT_ALGORITHM_COMMENTS = {
    "candidates": "本次候选算法；算法内 params 定义实例，output_instances 选择输出。",
    "cluster_algorithm": "本次聚类算法，参数由 cluster 表配置。",
}


EXPERIMENT_COMMENTS = {
    "experiment": {"case": "本次使用的案例名称，对应 cases 下的分组。",
                   "schemes": "本次依次运行的参数方案名称，对应 schemes 下的分组。",
                   "workspace": "实验产物目录，必须位于配置的数据根目录内。",
                   "note": "本次实验的备注。"},
    "cases": {"source_table": "轨迹来源表。", "time_start": "开始时间，包含；必须带时区。",
              "time_end": "结束时间，不包含；必须带时区。",
              "region_file": "WGS84 多边形 GeoJSON 文件，相对本 TOML；与 bbox 二选一。",
              "bbox": "区域两个对角顶点 [[经度, 纬度], [经度, 纬度]]；与 region_file 二选一。",
              "truth_file": "可选的交叉口真值 GeoJSON 文件，相对本 TOML；未配置则不评价。"},
    "evaluation": {"mode": "匹配方式：center_distance（中心距离）、iou（交并比）、both（同时满足）。",
                   "distance_m": "中心距离阈值，单位米；center_distance 或 both 时必填。",
                   "iou_threshold": "交并比阈值；iou 或 both 时必填。"},
    "archive": {"database": "实验评价存档数据库，独立于开发数据库。"},
}


def toml_text(payload: dict[str, Any]) -> str:
    """Emit the simple tables/scalars supported by experiment configuration."""
    def scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, (list, tuple)):
            return "[" + ", ".join(scalar(item) for item in value) + "]"
        if hasattr(value, "isoformat"):
            return json.dumps(value.isoformat())
        raise ValueError(f"unsupported TOML value: {type(value).__name__}")
    comments = parameter_comments()

    def key_text(key: str) -> str:
        return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key, ensure_ascii=False)

    def description(prefix: tuple[str, ...], key: str) -> str | None:
        if prefix and prefix[0] in EXPERIMENT_COMMENTS:
            result = EXPERIMENT_COMMENTS[prefix[0]].get(key)
            if result:
                return result
        if prefix == ("detect",) or (len(prefix) == 3 and prefix[0] == "schemes" and prefix[2] == "detect"):
            if key in EXPERIMENT_ALGORITHM_COMMENTS:
                return EXPERIMENT_ALGORITHM_COMMENTS[key]
        if prefix and prefix[0] == "schemes":
            return comments.get((*prefix[2:], key))
        if prefix == ("archive",):
            return comments.get(("postgres", key))
        return comments.get((*prefix, key))

    lines = []
    def table(values: dict[str, Any], prefix: tuple[str, ...]) -> None:
        if prefix and (not values or any(not isinstance(v, dict) for v in values.values())):
            lines.append("[" + ".".join(key_text(part) for part in prefix) + "]")
        for key, value in values.items():
            if not isinstance(value, dict):
                line = f"{key_text(key)} = {scalar(value)}"
                comment = description(prefix, key)
                if comment:
                    # Column numbers are one-based; leave at least one separating space.
                    if len(line) >= 69:
                        lines.extend((line, " " * 69 + "# " + comment))
                    else:
                        lines.append(line.ljust(69) + "# " + comment)
                else:
                    lines.append(line)
        if len(prefix) == 2 and prefix[0] == "cases" and values.get("region_file") == "":
            lines.append(" " * 69 + '# 必填空间区域：填写 region_file；或删除该行，取消下一行注释并填写两个对角顶点。')
            lines.append(" " * 69 + '# bbox = [[经度1, 纬度1], [经度2, 纬度2]]')
        lines.append("")
        for key, value in values.items():
            if isinstance(value, dict):
                table(value, (*prefix, key))
    table(payload, ())
    return "\n".join(lines)


def initialize_workspace(path: Path, base: Path) -> None:
    if path.exists():
        return
    payload = tomllib.loads(base.read_text(encoding="utf-8"))
    if not path.parent.resolve().is_relative_to(Path(payload["paths"]["data_root"]).resolve()):
        raise ValueError("workspace configuration must be under configured paths.data_root")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (base.parent / payload["parser"]["field_specs_path"]).resolve()
    shutil.copyfile(fields, path.parent / "field_specs.toml")
    payload["parser"]["field_specs_path"] = "field_specs.toml"
    payload["experiment"] = {"case": "example", "schemes": ["turn", "joint"]}
    payload["archive"] = {"database": "roadnet_eval"}
    payload["cases"] = {"example": {"source_table": "trajectory_points", "time_start": "",
                                    "time_end": "", "region_file": "", "bbox": []}}
    joint = copy.deepcopy(payload["detect"])
    single = copy.deepcopy(joint)
    single["candidates"] = [joint["candidates"][0]]
    payload["schemes"] = {"turn": {"detect": single}, "joint": {"detect": joint}}
    payload["evaluation"] = {"mode": "center_distance", "distance_m": 30.0}
    path.write_text("# Fill case time_start/time_end and region_file or bbox before running.\n" + toml_text(payload), encoding="utf-8")


def restore_snapshot(snapshot: dict, *, migrate: bool = False,
                     turn_algorithm: str | None = None, cluster_algorithm: str | None = None) -> dict:
    """Copy a historical draft, explicitly converting legacy Detect before reuse."""
    from ..config.migrate_detect import convert_legacy_detect
    restored = copy.deepcopy(snapshot)
    legacy_base = copy.deepcopy(restored.get("detect", {}))
    sections = [restored]
    sections.extend(restored.get("schemes", {}).values())
    for section in sections:
        raw = section.get("detect")
        if raw is None or "candidates" in raw:
            continue
        if not migrate:
            raise ValueError("legacy Detect snapshot requires restore --migrate-detect")
        if section is not restored:
            from ..experiments.config import merge_parameters
            raw = merge_parameters(legacy_base, raw)
        turn = raw.get("turn_candidate_algorithm", turn_algorithm)
        cluster = raw.get("clustering_algorithm", cluster_algorithm)
        if turn is None or cluster is None:
            raise ValueError("legacy snapshot requires explicit turn and cluster algorithms")
        section["detect"] = convert_legacy_detect(raw, turn_algorithm=turn, cluster_algorithm=cluster)
    return restored


def cleanup_experiment(workspace: Any, experiment_id: str) -> dict[str, Any]:
    from ..config import PostgresConfig, connect
    from ..delete_run import DeleteRunService, REFINE_ARTIFACT_TABLES, DETECT_ARTIFACT_TABLES
    from pipeline.experiments.archive import ArchiveStore, connect_archive
    with connect_archive(workspace.archive_settings) as archived_conn:
        archive = ArchiveStore(archived_conn)
        archive.initialize()
        guard = archive.cleanup_guard(experiment_id)
        if not guard["archived"]:
            raise ValueError("complete archive is required before cleanup")
        record = archive.get_experiment(experiment_id)
        postgres_values = record["parameters"]["execution_config"]["postgres"]
        postgres = PostgresConfig(**postgres_values, password=workspace.configs[workspace.schemes[0]].postgres.password)
        root = Path(record["parameters"]["execution_config"]["paths"]["parquet_directory"]).resolve()
        removed_paths = []
        with connect(postgres) as conn:
            service = DeleteRunService(conn)
            with conn.cursor() as cur:
                cur.execute("SET LOCAL track_v2.allow_completed_run_delete = 'on'")
                detect_id = record.get("detect_id")
                refine_id = record.get("refine_id")
                def retire_artifacts(column, identifier):
                    cur.execute(f"SELECT id,relative_path FROM pipeline_artifacts WHERE {column}=%s FOR UPDATE", (identifier,))
                    for artifact_id, relative_path in cur.fetchall():
                        target = (root / relative_path).resolve()
                        if not target.is_relative_to(root) or target == root:
                            raise ValueError("unsafe artifact path in registry")
                        removed_paths.append(target)
                        cur.execute("INSERT INTO pipeline_artifact_events(artifact_id,state,reason) "
                                    "SELECT %s,'deleted','experiment intermediate cleanup' WHERE NOT EXISTS "
                                    "(SELECT 1 FROM pipeline_artifact_events WHERE artifact_id=%s AND state='deleted')",
                                    (artifact_id,artifact_id))
                if detect_id:
                    cur.execute("SELECT id FROM detect_runs WHERE id=%s FOR UPDATE", (detect_id,))
                    if cur.fetchone() is not None:
                        cur.execute("SELECT 1 FROM fit_runs WHERE detect_id=%s LIMIT 1", (detect_id,))
                        if cur.fetchone() is not None:
                            raise ValueError("Detect has downstream Fitting runs; clean them through their owning workflow first")
                        retire_artifacts("detect_id", detect_id)
                        service.preview_detect(detect_id, lock=True)
                        cur.execute("DELETE FROM centerline_source_clips WHERE centerline_id IN "
                                    "(SELECT id FROM centerlines WHERE detect_id=%s)", (detect_id,))
                        for table in DETECT_ARTIFACT_TABLES[1:]:
                            service._delete_rows(table, "detect_id", detect_id)
                        # Keep immutable Run, candidate instance parameters/counts,
                        # and registry ownership as history.
                if refine_id and guard["can_delete_refine"]:
                    cur.execute("SELECT id FROM refine_runs WHERE id=%s FOR UPDATE", (refine_id,))
                    exists = cur.fetchone() is not None
                    cur.execute("""SELECT 1 FROM detect_runs d WHERE d.refine_id=%s AND d.id IS DISTINCT FROM %s::uuid
                        AND (d.status='running' OR EXISTS (SELECT 1 FROM resolved_subtrack_points p WHERE p.detect_id=d.id)
                             OR EXISTS (SELECT 1 FROM pipeline_artifacts a WHERE a.detect_id=d.id
                                AND NOT EXISTS (SELECT 1 FROM pipeline_artifact_events e WHERE e.artifact_id=a.id AND e.state='deleted')))
                        LIMIT 1""", (refine_id, detect_id))
                    if exists and cur.fetchone() is None:
                        retire_artifacts("refine_id", refine_id)
                        for table in REFINE_ARTIFACT_TABLES:
                            service._delete_rows(table, "refine_id", refine_id)
            conn.commit()
        # Paths remain discoverable in the registry if filesystem deletion fails;
        # rerunning cleanup retries removal before marking the archive cleaned.
        for target in removed_paths:
            if target.exists():
                shutil.rmtree(target)
        archive.mark_cleaned(experiment_id)
    return {"id": experiment_id, "intermediate_state": "cleaned"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Local TOML experiment workspace")
    parser.add_argument("--config", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--base-config", type=Path, required=True)
    initialize.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    for command in ("run", "list", "restore", "evaluate", "retry-archive", "cleanup"):
        sub = commands.add_parser(command)
        # Accept --config either side of the command.
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if command == "run":
            sub.add_argument("--case")
            sub.add_argument("--scheme", action="append")
            sub.add_argument("--refine-id")
            sub.add_argument("--independent-instances", action="store_true")
        elif command == "list":
            sub.add_argument("--case")
            sub.add_argument("--limit", type=int, default=100)
        else:
            sub.add_argument("experiment_id")
            if command == "restore":
                sub.add_argument("--output", type=Path, required=True)
                sub.add_argument("--migrate-detect", action="store_true")
                sub.add_argument("--legacy-turn-algorithm")
                sub.add_argument("--legacy-cluster-algorithm")
            if command == "evaluate":
                sub.add_argument("--truth-file", type=Path)
    args = parser.parse_args()
    config_path = args.config or Path(os.environ.get("EXPERIMENT_CONFIG_PATH", "/data/experiments/experiment.toml"))
    try:
        if args.command == "init":
            initialize_workspace(config_path, args.base_config)
            workspace = load_workspace(config_path, password=os.environ.get("POSTGRES_PASSWORD", ""), validate_run=False)
            from pipeline.experiments.archive import ArchiveStore, connect_archive, ensure_archive_database
            ensure_archive_database(workspace.archive_settings)
            with connect_archive(workspace.archive_settings) as conn:
                ArchiveStore(conn).initialize()
            print(f"Workspace configuration: {config_path}")
            return 0
        workspace = load_workspace(config_path, password=os.environ.get("POSTGRES_PASSWORD", ""),
                                   case_name=getattr(args, "case", None), schemes=getattr(args, "scheme", None),
                                   validate_run=args.command == "run")
        if args.command == "run":
            records = run_batch(workspace, explicit_refine_id=args.refine_id, independent_instances=args.independent_instances)
            return 0 if all(record["status"] == "completed" and record["archive_state"] == "archived" and "archive_error" not in record and "evaluation_error" not in record for record in records) else 1
        if args.command == "retry-archive":
            result = archive_local(workspace, workspace.root / "runs" / args.experiment_id)
        elif args.command == "evaluate":
            result = evaluate_local(workspace, args.experiment_id, truth_file=args.truth_file)
        elif args.command == "cleanup":
            result = cleanup_experiment(workspace, args.experiment_id)
        else:
            from pipeline.experiments.archive import ArchiveStore, connect_archive
            with connect_archive(workspace.archive_settings) as conn:
                archive = ArchiveStore(conn)
                archive.initialize()
                if args.command == "list":
                    result = archive.list_experiments(limit=args.limit, case_name=args.case) if args.case else archive.list_experiments(limit=args.limit)
                else:
                    record = archive.get_experiment(args.experiment_id)
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(toml_text(restore_snapshot(record["draft_snapshot"], migrate=args.migrate_detect, turn_algorithm=args.legacy_turn_algorithm, cluster_algorithm=args.legacy_cluster_algorithm)), encoding="utf-8")
                    result = {"configuration": str(args.output)}
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print(f"[FAIL] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
