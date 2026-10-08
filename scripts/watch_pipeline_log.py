#!/usr/bin/env python3
"""Record Pipeline log tails in the background until its launcher exits."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import time


def process_identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except FileNotFoundError:
        return None
    return None if fields[0] == "Z" else fields[19]


def tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            chunks = []
            newlines = 0
            while position and newlines <= lines:
                size = min(position, 8192)
                position -= size
                handle.seek(position)
                chunk = handle.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
        return b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-lines:]
    except FileNotFoundError:
        return [f"Log does not exist yet: {path}"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--result-log", type=Path, required=True,
                        help="Captured make output used to identify completion")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=600)
    parser.add_argument("--lines", type=int, default=12)
    args = parser.parse_args()
    if args.interval <= 0 or args.lines <= 0 or args.pid <= 0:
        parser.error("pid, interval and lines must be positive")

    identity = process_identity(args.pid)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", buffering=1) as output:
        while True:
            running = identity is not None and process_identity(args.pid) == identity
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            output.write(f"\n[{timestamp}] pid={args.pid} running={running}\n")
            output.write("\n".join(tail(args.log, args.lines)) + "\n")
            if not running:
                result = "\n".join(tail(args.result_log, 100))
                output.write("\n--- Final make output ---\n" + result + "\n")
                failed = bool(re.search(r"Traceback|\[FAIL\]|make(?:\[\d+\])?: \*\*\*", result))
                completed = bool(re.search(r"^\s*_elapsed_s\s+[\d.]+\s+s\s*$", result, re.M))
                status = "FAILED" if failed else "COMPLETED" if completed else "UNKNOWN"
                output.write(f"RESULT={status} (inferred from log; process has exited)\n")
                return 1 if failed else 0 if completed else 2
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
