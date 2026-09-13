from datetime import datetime, timezone
from dataclasses import replace
import json

import pytest

from track.v2.resource_budget import (
    InsufficientMemoryError,
    MemoryEstimates,
    SnapshotProbe,
    collect_run_environment,
    plan_budget,
)


def estimates(**overrides):
    return MemoryEstimates(**{
        "coordinator_bytes": 100,
        "process_baseline_bytes": 200,
        "peak_bytes_per_row": 10,
        "writer_bytes": 50,
        "buffer_bytes": 50,
        "safety_margin_bytes": 100,
        **overrides,
    })


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1000", None])
def test_limit_is_strict_positive_integer_bytes(value):
    with pytest.raises(ValueError, match="memory_limit_bytes"):
        plan_budget(value, estimates(), available_cpu_count=4)


@pytest.mark.parametrize("field,value", [
    ("coordinator_bytes", True), ("process_baseline_bytes", -1),
    ("peak_bytes_per_row", 0), ("writer_bytes", 1.5),
    ("buffer_bytes", "1"), ("safety_margin_bytes", -1),
    ("thread_extra_bytes", False),
])
def test_estimates_are_validated(field, value):
    with pytest.raises(ValueError, match=field):
        estimates(**{field: value})


@pytest.mark.parametrize("field,value", [
    ("available_cpu_count", 0), ("available_cpu_count", True),
    ("native_thread_limit", 0), ("preferred_patch_rows", False),
])
def test_scheduling_inputs_are_validated(field, value):
    kwargs = {"available_cpu_count": 8, field: value}
    with pytest.raises(ValueError, match=field):
        plan_budget(1000, estimates(), **kwargs)


def test_budget_accounts_for_all_concurrent_allocations():
    plan = plan_budget(
        4001, estimates(thread_extra_bytes=50), available_cpu_count=12,
        native_thread_limit=2, preferred_patch_rows=100,
    )
    assert plan.executor_processes == 2
    assert plan.native_threads_per_process == 2
    assert plan.patch_target_rows == 155
    assert plan.all_process_baselines_bytes == 400
    assert plan.all_thread_extras_bytes == 200
    assert plan.all_patch_working_bytes == 3100
    assert plan.estimated_peak_bytes == 4000
    assert plan.unallocated_bytes == 1
    assert plan.to_dict()["executor_processes"] == 2


def test_tiny_budget_reduces_concurrency_and_patch_size():
    plan = plan_budget(601, estimates(), available_cpu_count=64)
    assert plan.executor_processes == 1
    assert plan.patch_target_rows == 10
    assert plan.estimated_peak_bytes < 601


def test_thread_baselines_can_reduce_thread_count():
    plan = plan_budget(
        751, estimates(thread_extra_bytes=100), available_cpu_count=12,
        native_thread_limit=8,
    )
    assert plan.native_threads_per_process == 2
    assert plan.patch_target_rows == 5


def test_strict_limit_rejects_exact_minimum_without_headroom():
    with pytest.raises(InsufficientMemoryError, match="511 bytes"):
        plan_budget(510, estimates(), available_cpu_count=4)
    plan = plan_budget(511, estimates(), available_cpu_count=4)
    assert plan.patch_target_rows == 1


@pytest.mark.parametrize("budget", [600, 1500, 5000, 100000])
@pytest.mark.parametrize("cpus", [1, 3, 16])
@pytest.mark.parametrize("threads", [1, 2, 8])
def test_plan_invariants(budget, cpus, threads):
    plan = plan_budget(
        budget, estimates(thread_extra_bytes=10), available_cpu_count=cpus,
        native_thread_limit=threads, preferred_patch_rows=10,
    )
    assert 1 <= plan.executor_processes * plan.native_threads_per_process <= cpus
    assert plan.patch_target_rows >= 1
    assert plan.estimated_peak_bytes < budget
    assert plan.estimated_peak_bytes == sum([
        plan.coordinator_bytes, plan.writer_bytes, plan.buffer_bytes,
        plan.safety_margin_bytes, plan.all_process_baselines_bytes,
        plan.all_thread_extras_bytes, plan.all_patch_working_bytes,
    ])


def fake_probe(files=None, **kwargs):
    files = files or {}
    resolved = {"/sys/dev/block/8:1": "/sys/devices/pci/block/sda/sda1"}
    return SnapshotProbe(
        read_text=files.__getitem__, cpu_count=kwargs.pop("cpu_count", lambda: 12),
        read_link=kwargs.pop("read_link", lambda path: "cgroup:[4026531835]"),
        resolve_path=kwargs.pop("resolve_path", resolved.__getitem__),
        affinity=kwargs.pop("affinity", lambda: [0, 1, 2, 3]),
        python_version=lambda: "3.12.0",
        now=lambda: datetime(2026, 9, 13, tzinfo=timezone.utc), **kwargs,
    )


def host_files():
    return {
        "/proc/self/cgroup": "0::/run/worker\n",
        "/proc/self/mountinfo": (
            "1 0 0:1 / / rw - overlay overlay rw,secret=never-log\n"
            "2 1 0:2 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
            "3 1 8:1 / /data rw - ext4 /dev/sda1 rw\n"
        ),
        "/proc/cpuinfo": (
            "processor : 0\nmodel name : Fake CPU\nphysical id : 0\ncore id : 0\n\n"
            "processor : 1\nmodel name : Fake CPU\nphysical id : 0\ncore id : 0\n"
        ),
        "/proc/meminfo": "MemTotal: 1000000 kB\n",
        "/sys/fs/cgroup/cpu.max": "max 100000",
        "/sys/fs/cgroup/memory.max": "2000000",
        "/sys/fs/cgroup/memory.swap.max": "0",
        "/sys/fs/cgroup/run/cpu.max": "250000 100000",
        "/sys/fs/cgroup/run/memory.max": "1000000",
        "/sys/fs/cgroup/run/memory.swap.max": "0",
        "/sys/fs/cgroup/run/worker/cpu.max": "400000 100000",
        "/sys/fs/cgroup/run/worker/memory.max": "max",
        "/sys/fs/cgroup/run/worker/memory.swap.max": "max",
        "/sys/dev/block/8:1/partition": "1",
        "/sys/devices/pci/block/sda/device/model": "Example SSD\n",
        "/sys/devices/pci/block/sda/queue/rotational": "0",
    }


def test_snapshot_resolves_global_mount_root_and_ancestor_limits():
    snapshot = collect_run_environment(
        effective_parameters={"executor_processes": 2, "patch_target_rows": 100},
        runtime_paths=["/data/run-1"], probe=fake_probe(host_files()),
    )
    assert snapshot["cpu"]["available_cpu_count"] == 2
    assert snapshot["cpu"]["models"] == ["Fake CPU"]
    assert snapshot["cpu"]["physical_core_count"] == 1
    assert snapshot["cgroup"]["effective_cpu_quota_cores"] == 2.5
    assert snapshot["cgroup"]["effective_memory_limit_bytes"] == 1000000
    assert snapshot["cgroup"]["effective_swap_limit_bytes"] == 0
    assert len(snapshot["cgroup"]["ancestors"]) == 3
    assert snapshot["host_memory_bytes"] == 1024000000
    assert snapshot["python_version"] == "3.12.0"
    assert snapshot["captured_at"] == "2026-09-13T00:00:00+00:00"
    assert snapshot["disks"][0]["model"] == "Example SSD"
    assert snapshot["disks"][0]["source"] == "/dev/sda1"
    assert snapshot["disks"][0]["rotational"] == 0
    assert snapshot["disks"][0]["device_id"] == "8:1"
    assert snapshot["disks"][0]["partition_index"] == 1
    assert snapshot["disks"][0]["physical_device_source"] == "/sys/devices/pci/block/sda"
    assert "secret" not in json.dumps(snapshot)


def test_missing_probe_files_are_explicit_unknowns():
    snapshot = collect_run_environment(
        effective_parameters={}, runtime_paths=["/data"], probe=fake_probe(),
    )
    assert snapshot["cgroup"]["status"] == "unknown"
    assert snapshot["cpu"]["models"]["status"] == "unknown"
    assert snapshot["cpu"]["available_cpu_count"]["status"] == "unknown"
    assert snapshot["disks"][0]["status"] == "unknown"
    assert snapshot["host_memory_bytes"]["status"] == "unknown"
    assert snapshot["performance_measurements"]["status"] == "unknown"


@pytest.mark.parametrize("device_id,device,disk,partition", [
    ("8:1", "sda1", "sda", 1),
    ("259:2", "nvme0n1p2", "nvme0n1", 2),
])
def test_partition_uses_resolved_parent_disk_metadata(device_id, device, disk, partition):
    parent = f"/sys/devices/controller/block/{disk}"
    files = {
        "/proc/self/mountinfo": f"1 0 {device_id} / /data rw - ext4 /dev/{device} rw",
        f"/sys/dev/block/{device_id}/partition": str(partition),
        f"{parent}/device/model": "Parent disk model",
        f"{parent}/queue/rotational": "0",
        # Deliberately misleading partition metadata must never win.
        f"/sys/dev/block/{device_id}/device/model": "Wrong partition model",
        f"/sys/dev/block/{device_id}/queue/rotational": "1",
    }
    probe = fake_probe(files, resolve_path=lambda path: f"{parent}/{device}")
    snapshot = collect_run_environment(effective_parameters={}, runtime_paths=["/data/run"], probe=probe)
    observed = snapshot["disks"][0]
    assert observed["device_id"] == device_id
    assert observed["source"] == f"/dev/{device}"
    assert observed["partition_index"] == partition
    assert observed["physical_device_source"] == parent
    assert observed["model"] == "Parent disk model"
    assert observed["rotational"] == 0


def test_unresolved_partition_does_not_claim_partition_metadata_is_disk_metadata():
    files = host_files()
    files["/sys/dev/block/8:1/device/model"] = "Not the parent disk"

    def unresolved(path):
        raise FileNotFoundError(path)

    snapshot = collect_run_environment(
        effective_parameters={}, runtime_paths=["/data"],
        probe=fake_probe(files, resolve_path=unresolved),
    )
    observed = snapshot["disks"][0]
    assert observed["device_id"] == "8:1"
    assert observed["partition_index"] == 1
    assert observed["physical_device_source"]["status"] == "unknown"
    assert observed["model"]["status"] == "unknown"
    assert observed["rotational"]["status"] == "unknown"


def test_whole_disk_reads_its_own_metadata():
    parent = "/sys/devices/controller/block/sda"
    files = {
        "/proc/self/mountinfo": "1 0 8:0 / /data rw - ext4 /dev/sda rw",
        f"{parent}/device/model": "Whole disk",
        f"{parent}/queue/rotational": "1",
    }
    snapshot = collect_run_environment(
        effective_parameters={}, runtime_paths=["/data"],
        probe=fake_probe(files, resolve_path=lambda path: parent),
    )
    assert snapshot["disks"][0]["physical_device_source"] == parent
    assert snapshot["disks"][0]["model"] == "Whole disk"
    assert snapshot["disks"][0]["rotational"] == 1


def test_missing_ancestor_does_not_claim_known_effective_limit():
    files = host_files()
    del files["/sys/fs/cgroup/run/cpu.max"]
    snapshot = collect_run_environment(effective_parameters={}, probe=fake_probe(files))
    assert snapshot["cgroup"]["effective_cpu_quota_cores"]["status"] == "unknown"
    assert snapshot["cgroup"]["observed_cpu_quota_cores"] == 4
    assert snapshot["cpu"]["observed_capacity_cores"] == 4
    assert snapshot["cpu"]["available_cpu_count"]["status"] == "unknown"


@pytest.mark.parametrize("value", ["bad", "1 0", "-1 100000", "max 0", "0 100000"])
def test_malformed_cpu_quota_is_unknown(value):
    files = host_files()
    files["/sys/fs/cgroup/run/cpu.max"] = value
    snapshot = collect_run_environment(effective_parameters={}, probe=fake_probe(files))
    assert snapshot["cgroup"]["effective_cpu_quota_cores"]["status"] == "unknown"


def test_fractional_cpu_quota_is_not_rounded_up():
    files = host_files()
    files["/sys/fs/cgroup/run/cpu.max"] = "50000 100000"
    snapshot = collect_run_environment(effective_parameters={}, probe=fake_probe(files))
    assert snapshot["cpu"]["available_cpu_count"] == 0
    with pytest.raises(ValueError, match="available_cpu_count"):
        plan_budget(1000, estimates(), available_cpu_count=snapshot["cpu"]["available_cpu_count"])


def test_cgroup_namespace_root_is_resolved_without_host_path_assumptions():
    files = {
        "/proc/self/cgroup": "0::/",
        "/proc/self/mountinfo": "2 1 0:2 / /custom/cgroup rw - cgroup2 cgroup rw",
        "/custom/cgroup/cpu.max": "max 100000",
        "/custom/cgroup/memory.max": "max",
        "/custom/cgroup/memory.swap.max": "0",
    }
    snapshot = collect_run_environment(
        effective_parameters={},
        probe=fake_probe(files, read_link=lambda path: "cgroup:[4026532999]"),
    )
    assert snapshot["cpu"]["available_cpu_count"]["status"] == "unknown"
    assert snapshot["cgroup"]["observed_memory_limit_bytes"] == "unlimited"
    assert snapshot["cgroup"]["effective_memory_limit_bytes"]["status"] == "unknown"
    assert len(snapshot["cgroup"]["ancestors"]) == 1


def root_files():
    return {
        "/proc/self/cgroup": "0::/",
        "/proc/self/mountinfo": "2 1 0:2 / /sys/fs/cgroup ro - cgroup2 cgroup2 rw,nsdelegate",
        "/sys/fs/cgroup/cgroup.controllers": "cpuset cpu io memory pids",
    }


def test_real_global_root_missing_max_interfaces_are_not_applicable():
    files = root_files()

    def read(path):
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    snapshot = collect_run_environment(
        effective_parameters={}, probe=replace(fake_probe(files), read_text=read),
    )
    observed = snapshot["cgroup"]
    assert observed["initial_namespace"] is True
    assert observed["whole_hierarchy_visible"] is True
    assert observed["ancestors"][0]["global_root"] is True
    for field in ("cpu_quota_cores", "memory_limit_bytes", "swap_limit_bytes"):
        assert observed[f"effective_{field}"] == "unlimited"
    assert snapshot["cpu"]["available_cpu_count"] == 4


def test_global_root_absence_does_not_obscure_real_child_limits():
    files = host_files()
    for name in ("cpu.max", "memory.max", "memory.swap.max"):
        del files[f"/sys/fs/cgroup/{name}"]
    snapshot = collect_run_environment(effective_parameters={}, probe=fake_probe(files))
    assert snapshot["cgroup"]["effective_cpu_quota_cores"] == 2.5
    assert snapshot["cgroup"]["effective_memory_limit_bytes"] == 1000000
    assert snapshot["cgroup"]["effective_swap_limit_bytes"] == 0
    assert snapshot["cpu"]["available_cpu_count"] == 2


@pytest.mark.parametrize("name,field", [
    ("cpu.max", "cpu_quota_cores"), ("memory.max", "memory_limit_bytes"),
    ("memory.swap.max", "swap_limit_bytes"),
])
def test_nonroot_missing_max_interfaces_are_still_unknown(name, field):
    files = host_files()
    del files[f"/sys/fs/cgroup/run/worker/{name}"]
    snapshot = collect_run_environment(effective_parameters={}, probe=fake_probe(files))
    assert snapshot["cgroup"][f"effective_{field}"]["status"] == "unknown"


@pytest.mark.parametrize("name,field", [
    ("cpu.max", "cpu_quota_cores"), ("memory.max", "memory_limit_bytes"),
    ("memory.swap.max", "swap_limit_bytes"),
])
@pytest.mark.parametrize("error", [PermissionError, OSError, UnicodeError])
def test_global_root_unreadable_is_not_equivalent_to_absent(name, field, error):
    files = root_files()

    def read(path):
        if path == f"/sys/fs/cgroup/{name}":
            raise error("unreadable")
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    snapshot = collect_run_environment(
        effective_parameters={}, probe=replace(fake_probe(files), read_text=read),
    )
    assert snapshot["cgroup"][f"effective_{field}"]["status"] == "unknown"


@pytest.mark.parametrize("identity", ["cgroup:[4026532999]", "malformed"])
def test_unverified_namespace_root_missing_interfaces_remain_unknown(identity):
    snapshot = collect_run_environment(
        effective_parameters={},
        probe=fake_probe(root_files(), read_link=lambda path: identity),
    )
    assert snapshot["cgroup"]["whole_hierarchy_visible"] is False
    assert snapshot["cgroup"]["ancestors"][0]["global_root"] is False
    assert snapshot["cgroup"]["observed_cpu_quota_cores"]["status"] == "unknown"
    assert snapshot["cpu"]["available_cpu_count"]["status"] == "unknown"


def test_denied_namespace_identity_does_not_guess_from_root_paths():
    def denied(path):
        raise PermissionError(path)

    snapshot = collect_run_environment(
        effective_parameters={}, probe=fake_probe(root_files(), read_link=denied),
    )
    assert snapshot["cgroup"]["initial_namespace"]["status"] == "unknown"
    assert snapshot["cgroup"]["effective_memory_limit_bytes"]["status"] == "unknown"


def test_hidden_mount_ancestors_remain_unknown_despite_readable_visible_limits():
    files = host_files()
    files["/proc/self/cgroup"] = "0::/docker/run/worker"
    files["/proc/self/mountinfo"] = files["/proc/self/mountinfo"].replace(
        "0:2 / /sys/fs/cgroup", "0:2 /docker /sys/fs/cgroup",
    )
    snapshot = collect_run_environment(effective_parameters={}, probe=fake_probe(files))
    assert snapshot["cgroup"]["initial_namespace"] is True
    assert snapshot["cgroup"]["whole_hierarchy_visible"] is False
    assert snapshot["cgroup"]["observed_cpu_quota_cores"] == 2.5
    assert snapshot["cgroup"]["observed_memory_limit_bytes"] == 1000000
    assert snapshot["cgroup"]["effective_cpu_quota_cores"]["status"] == "unknown"
    assert snapshot["cgroup"]["effective_memory_limit_bytes"]["status"] == "unknown"
    assert snapshot["cpu"]["available_cpu_count"]["status"] == "unknown"


def test_hybrid_root_does_not_ignore_limits_in_unsupported_v1_controllers():
    files = root_files()
    files["/proc/self/cgroup"] += "\n2:cpu,cpuacct:/restricted\n3:memory:/restricted"
    snapshot = collect_run_environment(effective_parameters={}, probe=fake_probe(files))
    assert snapshot["cgroup"]["status"] == "unknown"
    assert "hybrid" in snapshot["cgroup"]["reason"]
    assert snapshot["cpu"]["available_cpu_count"]["status"] == "unknown"


def test_unsupported_cgroup_v1_and_unresolvable_paths_are_unknown():
    for membership in ("2:cpu:/worker", "0::/unmounted/run", "0::/../worker"):
        files = host_files()
        files["/proc/self/cgroup"] = membership
        files["/proc/self/mountinfo"] = files["/proc/self/mountinfo"].replace(
            "0:2 / /sys/fs/cgroup", "0:2 /docker /sys/fs/cgroup",
        )
        snapshot = collect_run_environment(effective_parameters={}, probe=fake_probe(files))
        assert snapshot["cgroup"]["status"] == "unknown"


def test_network_mount_credentials_and_options_are_not_collected():
    files = host_files()
    files["/proc/self/mountinfo"] += (
        "4 1 0:5 / /data/remote rw - cifs //user:password@host/share rw,password=secret\n"
    )
    snapshot = collect_run_environment(
        effective_parameters={}, runtime_paths=["/data/remote/run"], probe=fake_probe(files),
    )
    assert snapshot["disks"][0]["filesystem"] == "cifs"
    assert snapshot["disks"][0]["source"]["status"] == "unknown"
    assert "password" not in json.dumps(snapshot)
    assert "secret" not in json.dumps(snapshot)


def test_mount_path_escaping_and_longest_prefix():
    files = host_files()
    files["/proc/self/mountinfo"] += "4 1 8:2 / /data/my\\040disk rw - ext4 /dev/sdb rw\n"
    snapshot = collect_run_environment(
        effective_parameters={}, runtime_paths=["/data/my disk/run"], probe=fake_probe(files),
    )
    assert snapshot["disks"][0]["source"] == "/dev/sdb"


@pytest.mark.parametrize("path", ["relative", "/data/../secret"])
def test_runtime_paths_require_explicit_absolute_targets(path):
    with pytest.raises(ValueError, match="runtime paths"):
        collect_run_environment(effective_parameters={}, runtime_paths=[path], probe=fake_probe())


@pytest.mark.parametrize("parameters", [
    {"password": 123}, {"dsn": "postgres://secret"}, {"executor_processes": "secret"},
    {"patch_target_rows": True}, {"fetch_rows": -1},
])
def test_snapshot_accepts_only_numeric_execution_allowlist(parameters):
    with pytest.raises(ValueError):
        collect_run_environment(effective_parameters=parameters, probe=fake_probe())


def test_snapshots_do_not_share_mutable_input_or_output():
    parameters = {"executor_processes": 2}
    first = collect_run_environment(effective_parameters=parameters, probe=fake_probe(host_files()))
    parameters["executor_processes"] = 3
    second = collect_run_environment(effective_parameters=parameters, probe=fake_probe(host_files()))
    second["cpu"]["affinity"].append(8)
    assert first["effective_parameters"]["executor_processes"] == 2
    assert second["effective_parameters"]["executor_processes"] == 3
    assert first["cpu"]["affinity"] == [0, 1, 2, 3]


def test_unavailable_affinity_and_count_do_not_guess_capacity():
    def unavailable():
        raise OSError("denied")

    snapshot = collect_run_environment(
        effective_parameters={}, probe=fake_probe(host_files(), affinity=unavailable, cpu_count=unavailable),
    )
    assert snapshot["cpu"]["affinity"]["status"] == "unknown"
    assert snapshot["cpu"]["available_cpu_count"]["status"] == "unknown"
