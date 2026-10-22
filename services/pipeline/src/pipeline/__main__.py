"""One CLI, one selected TOML; algorithm flags and environment overrides absent."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Road-network parameter experiment workbench")
    parser.add_argument("command", choices=("run", "tune", "preview-labels", "list-labels", "check-config", "inspect"))
    parser.add_argument("--config", type=Path, default=Path("pipeline.toml"), help="The only parameter/input configuration")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "check-config":
            from .resources import _native_module, validate_heap_profiler
            native = _native_module()
            result = {"status": "VALID", "config": str(args.config.resolve()),
                      "native_extension": native.__file__, "processing_crs": config["input"]["processing_crs"],
                      "output_root": config["output"]["root"], "database_enabled": config["database"]["enabled"]}
            if config["resources"]["heap_profile"]:
                result["heap_profiler"] = validate_heap_profiler()
            if config["labels"]:
                from .labels import load_labels
                labels = load_labels(config["labels"], config["input"]["processing_crs"], args.config.resolve().parent)
                result["labels"] = labels.metadata
            if config["tuning"]["search_space"]:
                from .tune import validate_search_space
                result["search_space"] = validate_search_space(config)
        elif args.command == "list-labels":
            from .labels import list_layers
            paths = sorted({item["path"] for item in config["labels"].values()
                            if isinstance(item, dict) and "path" in item})
            result = {path: list_layers(path) for path in paths}
        elif args.command == "preview-labels":
            from .labels import load_labels, prepare_labels
            labels = load_labels(config["labels"], config["input"]["processing_crs"], args.config.resolve().parent)
            result = prepare_labels(labels, Path(config["output"]["root"]) / "labels")
        elif args.command == "inspect":
            latest = json.loads((Path(config["output"]["root"]) / "latest.json").read_text())
            result = json.loads((Path(latest["run_dir"]) / "run.json").read_text())
        elif args.command == "run":
            from .runner import run_pipeline
            run = run_pipeline(config, args.config)
            result = {"status": "COMPLETE", "run_id": run["run_id"], "run_dir": run["run_dir"],
                      "counts": run["manifest"]["counts"], "resources": run["metrics"]}
        else:
            from .runner import monitor_options, run_isolated, run_pipeline
            from .resources import ResourceMonitor
            from .source import prepare_source
            from .tune import tune
            from .labels import load_labels
            labels = load_labels(config["labels"], config["input"]["processing_crs"], args.config.resolve().parent)
            with ResourceMonitor(Path(config["output"]["root"]) / "input-resources", **monitor_options(config)) as monitor:
                with monitor.stage("input"):
                    source = prepare_source(config, Path(config["output"]["root"]), monitor)
            callback = lambda effective, region, directory: run_isolated(effective, args.config,
                source=source, region=region, output_dir=directory)
            result = tune(config, args.config, callback, source.identity, labels=labels)
            best = run_pipeline(result["best_config"], Path(result["best_config_path"]), source=source)
            result["best_full_run"] = {"run_id": best["run_id"], "run_dir": best["run_dir"], "counts": best["manifest"]["counts"]}
            from .artifacts import write_json
            write_json(Path(result["best_config_path"]).parent / "best-full-run.json", result["best_full_run"])
            result.pop("best_config", None)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(f"ERROR [{type(error).__name__}]: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
