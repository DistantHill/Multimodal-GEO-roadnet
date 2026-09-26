"""Read-only, credential-free run environment snapshots.

This module does not configure cgroups or connect to a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
from typing import Callable, Mapping, Sequence


def _integer(name: str, value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _affinity() -> Sequence[int]:
    return sorted(os.sched_getaffinity(0))


def _resolve_path(path: str) -> str:
    return str(Path(path).resolve(strict=True))


@dataclass(frozen=True)
class SnapshotProbe:
    """Inject host access for tests or a container-specific inspection view."""

    read_text: Callable[[str], str] = _read_text
    read_link: Callable[[str], str] = os.readlink
    resolve_path: Callable[[str], str] = _resolve_path
    cpu_count: Callable[[], int | None] = os.cpu_count
    affinity: Callable[[], Sequence[int]] = _affinity
    python_version: Callable[[], str] = platform.python_version
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _unknown(reason: str) -> dict[str, str]:
    return {"status": "unknown", "reason": reason}


def _read(probe: SnapshotProbe, path: str) -> str | None:
    try:
        return probe.read_text(path).strip()
    except (OSError, UnicodeError, KeyError):
        return None


def _unescape_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _mounts(probe: SnapshotProbe) -> list[dict[str, str]]:
    records = []
    for line in (_read(probe, "/proc/self/mountinfo") or "").splitlines():
        left, separator, right = line.partition(" - ")
        fields, details = left.split(), right.split()
        if separator and len(fields) >= 6 and len(details) >= 3:
            records.append({
                "device_id": fields[2], "root": _unescape_path(fields[3]),
                "mount_point": _unescape_path(fields[4]), "filesystem": details[0],
                # Do not record mount options: network credentials can occur there.
                "source": _unescape_path(details[1]),
            })
    return records


def _relative(path: str, root: str) -> PurePosixPath | None:
    try:
        return PurePosixPath(path).relative_to(PurePosixPath(root))
    except ValueError:
        return None


def _limit(value: str | None) -> int | str | dict[str, str]:
    if value == "max":
        return "unlimited"
    if value is not None and value.isdecimal():
        return int(value)
    return _unknown("unreadable or invalid limit")


def _initial_cgroup_namespace(probe: SnapshotProbe) -> bool | None:
    # Linux reserves 0xEFFFFFFB for the initial cgroup namespace. Unlike PID 1
    # comparison, this also distinguishes a container's private cgroup namespace.
    # https://github.com/torvalds/linux/blob/v6.1/include/linux/proc_ns.h
    # Now also CGROUP_NS_INIT_INO in include/uapi/linux/nsfs.h.
    try:
        target = probe.read_link("/proc/self/ns/cgroup")
    except (OSError, KeyError, AttributeError, NotImplementedError):
        return None
    match = re.fullmatch(r"cgroup:\[(\d+)\]", target)
    return int(match[1]) == 0xEFFFFFFB if match else None


def _controller_limit(
    probe: SnapshotProbe, path: str, *, global_root: bool, root_default: str,
) -> str | None:
    # The three max interfaces do not exist on the global cgroup v2 root.
    # Only ENOENT is expected there; permission/I/O failures are not unlimited.
    # KeyError represents a missing path in injected dictionary probes.
    try:
        return probe.read_text(path).strip()
    except (FileNotFoundError, KeyError):
        return root_default if global_root else None
    except (OSError, UnicodeError):
        return None


def _cgroup(probe: SnapshotProbe, mounts: list[dict[str, str]]) -> dict:
    group = None
    memberships = (_read(probe, "/proc/self/cgroup") or "").splitlines()
    if any(line.strip() and not line.startswith("0::") for line in memberships):
        return _unknown("cgroup v1 or hybrid membership is not supported")
    for line in memberships:
        if line.startswith("0::"):
            group = line[3:]
            break
    if group is None:
        return _unknown("cgroup v2 membership unavailable; v1 is not supported")
    candidates = [
        mount for mount in mounts
        if mount["filesystem"] == "cgroup2" and _relative(group, mount["root"]) is not None
    ]
    if not candidates or ".." in PurePosixPath(group).parts:
        return _unknown("cgroup v2 membership cannot be resolved to a visible mount")
    mount = max(candidates, key=lambda entry: len(entry["root"]))
    relative = _relative(group, mount["root"])
    leaf = PurePosixPath(mount["mount_point"]) / relative
    root = PurePosixPath(mount["mount_point"])
    initial_namespace = _initial_cgroup_namespace(probe)
    whole_hierarchy_visible = initial_namespace is True and mount["root"] == "/"
    limits = []
    current = leaf
    while True:
        global_root = whole_hierarchy_visible and current == root
        cpu_raw = _controller_limit(
            probe, str(current / "cpu.max"), global_root=global_root,
            root_default="max 100000",
        )
        parts = (cpu_raw or "").split()
        cpu: float | str | dict = _unknown("cpu.max unreadable or invalid")
        if len(parts) == 2 and parts[1].isdecimal() and int(parts[1]) > 0:
            if parts[0] == "max":
                cpu = "unlimited"
            elif parts[0].isdecimal() and int(parts[0]) > 0:
                cpu = int(parts[0]) / int(parts[1])
        limits.append({
            "path": str(current), "cpu_quota_cores": cpu,
            "global_root": global_root,
            "memory_limit_bytes": _limit(_controller_limit(
                probe, str(current / "memory.max"), global_root=global_root, root_default="max",
            )),
            "swap_limit_bytes": _limit(_controller_limit(
                probe, str(current / "memory.swap.max"), global_root=global_root, root_default="max",
            )),
        })
        if current == root:
            break
        current = current.parent
    result = {
        "status": "observed", "version": 2, "path": group, "ancestors": limits,
        "initial_namespace": initial_namespace if initial_namespace is not None
        else _unknown("cgroup namespace identity unavailable"),
        "whole_hierarchy_visible": whole_hierarchy_visible,
    }
    for field in ("cpu_quota_cores", "memory_limit_bytes", "swap_limit_bytes"):
        numeric = [entry[field] for entry in limits if isinstance(entry[field], (int, float))]
        known = min(numeric) if numeric else (
            "unlimited" if all(entry[field] == "unlimited" for entry in limits)
            else _unknown("no numeric ancestor limit observed")
        )
        result[f"observed_{field}"] = known
        result[f"effective_{field}"] = (
            _unknown("host ancestors may be hidden; complete hierarchy not verified")
            if not whole_hierarchy_visible else
            _unknown("one or more visible ancestor limits unreadable")
            if any(isinstance(entry[field], dict) for entry in limits) else known
        )
    result["visibility_note"] = (
        "Initial cgroup namespace and global mount root verified; absent root max interfaces do not apply."
        if whole_hierarchy_visible else
        "Only visible ancestors are inspected; host ancestors may be hidden."
    )
    return result


def _cpu(probe: SnapshotProbe, cgroup: dict) -> dict:
    cpu_info = _read(probe, "/proc/cpuinfo")
    models, physical_cores = set(), set()
    for block in (cpu_info or "").split("\n\n"):
        fields = dict(
            (key.strip(), value.strip()) for line in block.splitlines()
            for key, separator, value in [line.partition(":")] if separator
        )
        model = fields.get("model name") or fields.get("Hardware")
        if model:
            models.add(model)
        if "physical id" in fields and "core id" in fields:
            physical_cores.add((fields["physical id"], fields["core id"]))
    try:
        count = probe.cpu_count()
    except OSError:
        count = None
    try:
        affinity = sorted(set(probe.affinity()))
    except (OSError, AttributeError, NotImplementedError):
        affinity = None
    bounds = [value for value in (count, len(affinity) if affinity else None) if value]
    quota = cgroup.get("effective_cpu_quota_cores")
    observed_quota = cgroup.get("observed_cpu_quota_cores")
    if isinstance(observed_quota, (int, float)):
        bounds.append(observed_quota)
    complete = bool(count and affinity) and isinstance(quota, (int, float, str))
    capacity = min(bounds) if bounds else None
    return {
        "models": sorted(models) or _unknown("CPU model unavailable"),
        "logical_count": count or _unknown("logical CPU count unavailable"),
        "physical_core_count": len(physical_cores) or _unknown("physical core IDs unavailable"),
        "affinity": affinity or _unknown("CPU affinity unavailable"),
        "observed_capacity_cores": capacity if capacity is not None else _unknown("no CPU bounds"),
        "available_cpu_count": math.floor(capacity) if complete else _unknown("incomplete CPU bounds"),
    }


def _disk(probe: SnapshotProbe, path: str, mounts: list[dict[str, str]]) -> dict:
    if not PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise ValueError("runtime paths must be absolute and contain no '..'")
    matches = [mount for mount in mounts if _relative(path, mount["mount_point"]) is not None]
    if not matches:
        return {"path": path, **_unknown("mount information unavailable")}
    mount = max(matches, key=lambda entry: len(entry["mount_point"]))
    device_id = mount["device_id"]
    base = f"/sys/dev/block/{device_id}"
    source = mount["source"]
    # Only local device paths are recorded, never a remote URI or credential.
    source_value = source if source.startswith("/dev/") else _unknown("not a local device path")
    partition = _read(probe, f"{base}/partition")
    metadata_base = base
    try:
        resolved = PurePosixPath(probe.resolve_path(base))
        if not resolved.is_absolute() or ".." in resolved.parts:
            raise ValueError("invalid resolved sysfs path")
        # Both sda1 and nvme0n1p1 have a partition marker and a parent disk
        # directory in sysfs. Device-name suffix heuristics are not reliable.
        metadata_base = str(resolved.parent if partition is not None else resolved)
    except (OSError, KeyError, ValueError):
        if partition is not None:
            metadata_base = None
    if partition is not None and (not partition.isdecimal() or int(partition) < 1):
        metadata_base = None
    rotational = _read(probe, f"{metadata_base}/queue/rotational") if metadata_base else None
    model = _read(probe, f"{metadata_base}/device/model") if metadata_base else None
    return {
        "path": path, "mount_point": mount["mount_point"],
        "filesystem": mount["filesystem"], "device_id": device_id,
        "source": source_value,
        "partition_index": int(partition) if partition and partition.isdecimal() and int(partition) > 0
        else _unknown("partition marker absent or invalid"),
        "physical_device_source": metadata_base or _unknown("partition parent device unresolved"),
        "model": model or _unknown("device model unavailable"),
        "rotational": int(rotational) if rotational in ("0", "1") else _unknown("media type unavailable"),
    }


_EXECUTION_PARAMETERS = frozenset({
    "memory_limit_bytes", "fetch_rows", "insert_rows",
})


def collect_run_environment(
    *,
    effective_parameters: Mapping[str, int],
    runtime_paths: Sequence[str] = (),
    probe: SnapshotProbe | None = None,
) -> dict:
    """Return a fresh JSON-compatible snapshot for one Run or resume attempt.

    Only explicit numeric execution fields are accepted; this is not a generic
    configuration/environment dump. Callers separately persist sanitized
    semantic configuration, database metadata, and measured run performance.
    No synthetic hardware performance results or benchmark claims are emitted.
    """
    parameters = dict(effective_parameters)
    for key, value in parameters.items():
        if key not in _EXECUTION_PARAMETERS:
            raise ValueError("unsupported execution parameter; use explicit non-secret fields")
        _integer(key, value)
    probe = probe or SnapshotProbe()
    mounts = _mounts(probe)
    cgroup = _cgroup(probe, mounts)
    meminfo = _read(probe, "/proc/meminfo") or ""
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB$", meminfo, re.MULTILINE)
    return {
        "snapshot_version": 1,
        "captured_at": probe.now().isoformat(),
        "python_version": probe.python_version(),
        "cpu": _cpu(probe, cgroup),
        "host_memory_bytes": int(match[1]) * 1024 if match else _unknown("MemTotal unavailable"),
        "cgroup": cgroup,
        "disks": [_disk(probe, path, mounts) for path in runtime_paths],
        "effective_parameters": parameters,
        "performance_measurements": _unknown("collected separately during execution"),
    }
