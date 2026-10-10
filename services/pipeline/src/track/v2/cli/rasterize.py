"""Rasterize one completed Refine Run."""

from __future__ import annotations

import argparse
import uuid

from ..config import connect
from ..db.schema import require_current_schema
from .json_output import error_payload, print_json
from ..raster.models import RasterRunRequest
from ..raster.run_rasterization import run_rasterization
from ._load_config import load_startup_config


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a UUID") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Rasterize kept and injected Refine points")
    parser.add_argument("--refine-id", type=_uuid, required=True)
    parser.add_argument("--bounds", type=float, nargs=4,
                        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
                        help="Optional processing-CRS bounds, expanded outward to the "
                             "global pixel grid at the configured resolution")
    parser.add_argument("--time-start", help="Inclusive ISO timestamp with timezone; empty uses Refine start")
    parser.add_argument("--time-end", help="Exclusive ISO timestamp with timezone; empty uses Refine end")
    parser.add_argument("--ask-password", action="store_true")
    args = parser.parse_args()
    try:
        config = load_startup_config(ask_password=args.ask_password)
        request = RasterRunRequest(args.refine_id, config.paths.data_root,
                                   tuple(args.bounds) if args.bounds else None,
                                   time_start=args.time_start, time_end=args.time_end)
        with connect(config.postgres) as conn:
            require_current_schema(conn)
            result = run_rasterization(conn, request, config.raster)
        print_json({"raster_run_id": result.raster_run_id,
                    "output_directory": str(result.output_directory),
                    "input_point_count": result.input_point_count,
                    "accepted_point_count": result.accepted_point_count,
                    "excluded_point_count": result.excluded_point_count,
                    "grid": result.grid.as_dict()})
        return 0
    except (RuntimeError, ValueError, OSError, OverflowError) as exc:
        print_json(error_payload("rasterize_failed", str(exc)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
