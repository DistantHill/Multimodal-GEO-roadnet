#include "profiling.hpp"

#include <pybind11/stl.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <dlfcn.h>
#include <sys/resource.h>
#include <unistd.h>
#endif
#if defined(__APPLE__)
#include <mach/mach.h>
#endif

namespace py = pybind11;
namespace fs = std::filesystem;

namespace {

using Clock = std::chrono::steady_clock;
using Bytes = std::uint64_t;

struct Snapshot {
    Clock::time_point at = Clock::now();
    std::optional<Bytes> rss, tree_rss, lifetime_peak;
    std::optional<Bytes> cgroup_current, cgroup_peak, cgroup_limit, cgroup_swap;
    std::string cgroup_path;
    std::map<std::string, Bytes> cgroup_events;
    double cpu_user_s = 0.0, cpu_system_s = 0.0;
    unsigned process_count = 1, thread_count = 0, unreadable_processes = 0;
    bool children_supported = false;
    std::string children_method = "unsupported";
};

std::optional<Bytes> read_number(const fs::path& path) {
    std::ifstream stream(path);
    std::string value;
    if (!(stream >> value) || value == "max") return std::nullopt;
    try {
        std::size_t used = 0;
        auto parsed = std::stoull(value, &used);
        if (used == value.size()) return parsed;
    } catch (const std::exception&) {}
    return std::nullopt;
}

#if defined(__linux__)
std::optional<Bytes> process_rss(long pid) {
    std::ifstream stream("/proc/" + (pid == 0 ? std::string("self") : std::to_string(pid)) + "/statm");
    Bytes virtual_pages = 0, resident_pages = 0;
    if (!(stream >> virtual_pages >> resident_pages)) return std::nullopt;
    const auto page_size = ::sysconf(_SC_PAGESIZE);
    if (page_size <= 0) return std::nullopt;
    return resident_pages * static_cast<Bytes>(page_size);
}

long proc_visible_pid() {
    // /proc may have been mounted in an ancestor PID namespace. In that case
    // getpid() is not the PID used by /proc/<pid>; /proc/self remains correct.
    std::ifstream status("/proc/self/status");
    for (std::string line; std::getline(status, line);) {
        if (line.rfind("Tgid:", 0) == 0) {
            std::istringstream field(line.substr(5));
            long pid = 0;
            if (field >> pid && pid > 0) return pid;
        }
    }
    return ::getpid();
}

std::vector<long> children(long pid, unsigned* threads = nullptr, bool* available = nullptr) {
    // A child may have been forked by a non-leader thread. Inspect every task.
    std::set<long> result;
    std::error_code error;
    const fs::path tasks = fs::path("/proc") / std::to_string(pid) / "task";
    for (fs::directory_iterator it(tasks, error), end; !error && it != end; it.increment(error)) {
        if (threads) ++*threads;
        std::ifstream stream(it->path() / "children");
        if (available && stream.is_open()) *available = true;
        long child = 0;
        while (stream >> child) result.insert(child);
    }
    return {result.begin(), result.end()};
}

std::map<long, std::vector<long>> children_by_ppid() {
    // /task/<tid>/children requires CONFIG_CHECKPOINT_RESTORE and is absent on
    // some kernels. One process-table pass is a portable Linux fallback.
    std::map<long, std::vector<long>> result;
    std::error_code error;
    for (fs::directory_iterator it("/proc", error), end; !error && it != end; it.increment(error)) {
        const auto filename = it->path().filename().string();
        if (filename.empty() || !std::all_of(filename.begin(), filename.end(), [](char c) { return c >= '0' && c <= '9'; }))
            continue;
        std::ifstream stream(it->path() / "stat");
        std::string line;
        if (!std::getline(stream, line)) continue;
        const auto close = line.rfind(')');
        if (close == std::string::npos) continue;
        std::istringstream fields(line.substr(close + 1));
        char state = 0;
        long ppid = 0;
        if (fields >> state >> ppid) {
            try { result[ppid].push_back(std::stol(filename)); }
            catch (const std::exception&) {}
        }
    }
    return result;
}

std::string unescape_mount(std::string value) {
    for (const auto& pair : std::vector<std::pair<std::string, std::string>>{
             {"\\040", " "}, {"\\011", "\t"}, {"\\012", "\n"}, {"\\134", "\\"}}) {
        std::size_t at = 0;
        while ((at = value.find(pair.first, at)) != std::string::npos) {
            value.replace(at, pair.first.size(), pair.second);
            at += pair.second.size();
        }
    }
    return value;
}

fs::path current_cgroup() {
    std::ifstream membership("/proc/self/cgroup");
    std::string line, member;
    while (std::getline(membership, line)) {
        if (line.rfind("0::", 0) == 0) { member = line.substr(3); break; }
    }
    if (member.empty()) return {};
    std::ifstream mounts("/proc/self/mountinfo");
    while (std::getline(mounts, line)) {
        const auto separator = line.find(" - cgroup2 ");
        if (separator == std::string::npos) continue;
        std::istringstream fields(line.substr(0, separator));
        std::vector<std::string> values;
        for (std::string value; fields >> value;) values.push_back(value);
        if (values.size() < 5) continue;
        const auto root = unescape_mount(values[3]);
        const fs::path mount = unescape_mount(values[4]);
        std::string relative;
        if (member == root || member == "/") relative = "";
        else if (root == "/") relative = member.substr(1);
        else if (member.rfind(root + "/", 0) == 0) relative = member.substr(root.size() + 1);
        else continue;
        // Never let a namespaced /proc entry escape its cgroup mount.
        const fs::path rel(relative);
        bool parent_component = false;
        for (const auto& part : rel) if (part == "..") parent_component = true;
        if (parent_component) continue;
        const auto candidate = mount / rel;
        if (read_number(candidate / "memory.current")) return candidate;
    }
    return {};
}
#endif

Snapshot sample(bool include_children, const fs::path& cgroup, bool full_sample = true) {
    Snapshot value;
#if defined(__unix__) || defined(__APPLE__)
    rusage usage{};
    if (::getrusage(RUSAGE_SELF, &usage) == 0) {
        value.cpu_user_s = usage.ru_utime.tv_sec + usage.ru_utime.tv_usec * 1e-6;
        value.cpu_system_s = usage.ru_stime.tv_sec + usage.ru_stime.tv_usec * 1e-6;
#if defined(__APPLE__)
        value.lifetime_peak = static_cast<Bytes>(usage.ru_maxrss);
#else
        value.lifetime_peak = static_cast<Bytes>(usage.ru_maxrss) * 1024;
#endif
    }
#endif
#if defined(__linux__)
    value.rss = process_rss(0);
    value.children_supported = true;
    if (full_sample || !include_children) value.tree_rss = value.rss;
    value.children_method = full_sample ? "self_only" : "not_sampled_at_phase_boundary";
    if (full_sample) {
      const long root_pid = proc_visible_pid();
      bool child_files_available = false;
      auto direct = children(root_pid, &value.thread_count, &child_files_available);
      if (include_children) {
        std::map<long, std::vector<long>> by_ppid;
        if (!child_files_available) {
            by_ppid = children_by_ppid();
            direct = by_ppid[root_pid];
        }
        value.children_method = child_files_available ? "task_children" : "proc_ppid_scan";
        std::vector<long> pending = direct;
        std::set<long> seen{root_pid};
        while (!pending.empty()) {
            const long pid = pending.back();
            pending.pop_back();
            if (!seen.insert(pid).second) continue;
            const auto rss = process_rss(pid);
            if (rss && value.tree_rss) *value.tree_rss += *rss;
            else ++value.unreadable_processes;
            const auto descendants = child_files_available ? children(pid) : by_ppid[pid];
            pending.insert(pending.end(), descendants.begin(), descendants.end());
        }
        value.process_count = static_cast<unsigned>(seen.size());
      }
    }
    if (full_sample && !cgroup.empty()) {
        value.cgroup_path = cgroup.string();
        value.cgroup_current = read_number(cgroup / "memory.current");
        value.cgroup_peak = read_number(cgroup / "memory.peak");
        value.cgroup_limit = read_number(cgroup / "memory.max");
        value.cgroup_swap = read_number(cgroup / "memory.swap.current");
        std::ifstream events(cgroup / "memory.events");
        std::string name;
        Bytes count = 0;
        while (events >> name >> count) value.cgroup_events[name] = count;
    }
#elif defined(__APPLE__)
    mach_task_basic_info info{};
    mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
    if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO,
                  reinterpret_cast<task_info_t>(&info), &count) == KERN_SUCCESS) {
        value.rss = static_cast<Bytes>(info.resident_size);
        value.tree_rss = value.rss;
    }
#endif
    value.at = Clock::now();
    return value;
}

fs::path cgroup_path() {
#if defined(__linux__)
    return current_cgroup();
#else
    return {};
#endif
}

py::dict as_dict(const Snapshot& value) {
    py::dict result;
    result["process_rss_bytes"] = py::cast(value.rss);
    result["process_tree_rss_sum_bytes"] = py::cast(value.tree_rss);
    result["process_lifetime_peak_rss_bytes"] = py::cast(value.lifetime_peak);
    result["process_cpu_user_s"] = value.cpu_user_s;
    result["process_cpu_system_s"] = value.cpu_system_s;
    result["process_count"] = value.process_count;
    result["thread_count"] = value.thread_count;
    result["unreadable_processes"] = value.unreadable_processes;
    result["children_supported"] = value.children_supported;
    result["children_method"] = value.children_method;
    result["cgroup_path"] = value.cgroup_path.empty() ? py::none() : py::cast(value.cgroup_path);
    result["cgroup_current_bytes"] = py::cast(value.cgroup_current);
    result["cgroup_lifetime_peak_bytes"] = py::cast(value.cgroup_peak);
    result["cgroup_limit_bytes"] = py::cast(value.cgroup_limit);
    result["cgroup_swap_bytes"] = py::cast(value.cgroup_swap);
    result["cgroup_events"] = py::cast(value.cgroup_events);
    result["cgroup_scope"] = "shared_or_unknown";
    return result;
}

void maximum(std::optional<Bytes>& destination, const std::optional<Bytes>& value) {
    if (value && (!destination || *value > *destination)) destination = value;
}

struct Span {
    std::string name;
    Snapshot start, last;
    std::optional<Bytes> rss_peak, tree_peak, cgroup_peak;
    unsigned process_peak = 1, thread_peak = 0;
    std::uint64_t samples = 0;
    void observe(const Snapshot& snapshot) {
        last = snapshot;
        maximum(rss_peak, snapshot.rss);
        maximum(tree_peak, snapshot.tree_rss);
        maximum(cgroup_peak, snapshot.cgroup_current);
        process_peak = std::max(process_peak, snapshot.process_count);
        thread_peak = std::max(thread_peak, snapshot.thread_count);
        ++samples;
    }
    py::dict to_dict() const {
        py::dict result;
        result["name"] = name;
        result["wall_s"] = std::chrono::duration<double>(last.at - start.at).count();
        result["process_cpu_user_s"] = std::max(0.0, last.cpu_user_s - start.cpu_user_s);
        result["process_cpu_system_s"] = std::max(0.0, last.cpu_system_s - start.cpu_system_s);
        result["rss_sampled_peak_bytes"] = py::cast(rss_peak);
        result["process_tree_rss_sampled_peak_bytes"] = py::cast(tree_peak);
        result["cgroup_current_sampled_peak_bytes"] = py::cast(cgroup_peak);
        result["process_lifetime_peak_rss_bytes"] = py::cast(last.lifetime_peak);
        result["process_count_peak"] = process_peak;
        result["thread_count_peak"] = thread_peak;
        result["samples"] = samples;
        result["start"] = as_dict(start);
        result["end"] = as_dict(last);
        return result;
    }
};

class NativeMonitor {
  public:
    NativeMonitor(unsigned interval_ms, bool include_children)
        : interval_(interval_ms), include_children_(include_children), cgroup_(cgroup_path()) {
        if (interval_ms < 1 || interval_ms > 60000)
            throw std::invalid_argument("sample_interval_ms must be in [1, 60000]");
        latest_ = sample(include_children_, cgroup_);
        if (!latest_.rss) throw std::runtime_error("Native RSS measurement is unavailable on this platform");
        worker_ = std::thread([this] { loop(); });
    }
    ~NativeMonitor() { close(); }
    NativeMonitor(const NativeMonitor&) = delete;
    NativeMonitor& operator=(const NativeMonitor&) = delete;

    std::uint64_t begin(const std::string& name, bool full_sample) {
        auto value = sample(include_children_, cgroup_, full_sample);
        std::lock_guard<std::mutex> lock(mutex_);
        ensure_open();
        observe(value);
        Span span;
        span.name = name;
        span.start = value;
        span.observe(value);
        const auto id = ++next_id_;
        spans_.emplace(id, std::move(span));
        return id;
    }

    py::dict end(std::uint64_t id, bool full_sample) {
        auto value = sample(include_children_, cgroup_, full_sample);
        Span finished;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ensure_open();
            auto found = spans_.find(id);
            if (found == spans_.end()) throw std::invalid_argument("Unknown resource span");
            observe(value);
            finished = std::move(found->second);
            spans_.erase(found);
        }
        return finished.to_dict();
    }

    py::dict peek(std::uint64_t id) {
        // Cooperative checks may run for every vehicle: keep them cheap. Tree
        // and cgroup samples still arrive on the independent interval thread.
        auto value = sample(include_children_, cgroup_, false);
        Span copy;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ensure_open();
            observe(value);
            const auto found = spans_.find(id);
            if (found == spans_.end()) throw std::invalid_argument("Unknown resource span");
            copy = found->second;
        }
        return copy.to_dict();
    }

    py::dict latest() {
        auto value = sample(include_children_, cgroup_);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ensure_open();
            observe(value);
        }
        return as_dict(value);
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        condition_.notify_all();
        if (worker_.joinable()) worker_.join();
    }

  private:
    void ensure_open() const {
        if (stopping_) throw std::runtime_error("Resource monitor is closed");
    }
    void observe(const Snapshot& value) {
        // Sampling can race a synchronous boundary read; never move time backwards.
        if (value.at < latest_.at) return;
        latest_ = value;
        for (auto& entry : spans_) entry.second.observe(value);
    }
    void loop() noexcept {
        std::unique_lock<std::mutex> lock(mutex_);
        while (!condition_.wait_for(lock, interval_, [this] { return stopping_; })) {
            lock.unlock();
            try {
                auto value = sample(include_children_, cgroup_);
                lock.lock();
                if (!stopping_) observe(value);
            } catch (...) {
                // Keep the observer alive; boundary reads still report unavailable values.
                if (!lock.owns_lock()) lock.lock();
            }
        }
    }

    std::chrono::milliseconds interval_;
    bool include_children_, stopping_ = false;
    fs::path cgroup_;
    std::thread worker_;
    std::mutex mutex_;
    std::condition_variable condition_;
    std::uint64_t next_id_ = 0;
    Snapshot latest_;
    std::map<std::uint64_t, Span> spans_;
};

struct HeapProfiler {
    using Start = void (*)(const char*);
    using Dump = void (*)(const char*);
    using Stop = void (*)();
    using Running = int (*)();
    Start start = nullptr;
    Dump dump = nullptr;
    Stop stop = nullptr;
    Running running = nullptr;
    std::string allocator_library;
    bool allocator_owns_malloc = false;
    bool available() const {
        return start && dump && stop && running && allocator_owns_malloc;
    }
};

HeapProfiler heap_profiler() {
    HeapProfiler api;
#if defined(__unix__) || defined(__APPLE__)
    api.start = reinterpret_cast<HeapProfiler::Start>(dlsym(RTLD_DEFAULT, "HeapProfilerStart"));
    api.dump = reinterpret_cast<HeapProfiler::Dump>(dlsym(RTLD_DEFAULT, "HeapProfilerDump"));
    api.stop = reinterpret_cast<HeapProfiler::Stop>(dlsym(RTLD_DEFAULT, "HeapProfilerStop"));
    api.running = reinterpret_cast<HeapProfiler::Running>(dlsym(RTLD_DEFAULT, "IsHeapProfilerRunning"));
    // malloc's exported address can be the main executable's PLT trampoline,
    // so dladdr(malloc) alone cannot establish which allocator actually owns it.
    // Verify a real allocation with gperftools' documented ownership C shim.
    // https://github.com/gperftools/gperftools/blob/gperftools-2.15/src/gperftools/malloc_extension_c.h
    using Malloc = void* (*)(std::size_t);
    using Free = void (*)(void*);
    using Ownership = int (*)(const void*);
    const auto allocate = reinterpret_cast<Malloc>(dlsym(RTLD_DEFAULT, "malloc"));
    const auto deallocate = reinterpret_cast<Free>(dlsym(RTLD_DEFAULT, "free"));
    const auto ownership = reinterpret_cast<Ownership>(dlsym(RTLD_DEFAULT, "MallocExtension_GetOwnership"));
    if (allocate && deallocate && ownership) {
        if (void* probe = allocate(64)) {
            api.allocator_owns_malloc = ownership(probe) == 1; // MallocExtension_kOwned
            deallocate(probe);
        }
    }
    Dl_info info{};
    void* allocator_symbol = dlsym(RTLD_DEFAULT, api.allocator_owns_malloc ? "tc_malloc" : "malloc");
    if (dladdr(allocator_symbol, &info) && info.dli_fname) api.allocator_library = info.dli_fname;
#endif
    return api;
}

HeapProfiler require_heap_profiler() {
    const auto api = heap_profiler();
    if (!api.available()) throw std::runtime_error(
        "gperftools heap profiling requires its full libtcmalloc (not libtcmalloc_minimal) "
        "preloaded before Python starts. Install libgoogle-perftools-dev, then launch with "
        "LD_PRELOAD=/absolute/path/to/libtcmalloc.so and resources.heap_profile=true. "
        "Setting LD_PRELOAD after process startup does not work.");
    return api;
}

} // namespace

void bind_profiling(py::module_& module) {
    module.def("resource_snapshot", [](bool children) {
        return as_dict(sample(children, cgroup_path()));
    }, py::arg("include_children") = true);
    py::class_<NativeMonitor>(module, "NativeMonitor")
        .def(py::init<unsigned, bool>(), py::arg("sample_interval_ms") = 100,
             py::arg("include_children") = true)
        .def("begin", &NativeMonitor::begin, py::arg("name"), py::arg("full_sample") = true)
        .def("end", &NativeMonitor::end, py::arg("id"), py::arg("full_sample") = true)
        .def("peek", &NativeMonitor::peek)
        .def("latest", &NativeMonitor::latest)
        .def("close", &NativeMonitor::close);
    module.def("heap_profiler_status", [] {
        const auto api = heap_profiler();
        py::dict result;
        result["available"] = api.available();
        result["running"] = api.running ? static_cast<bool>(api.running()) : false;
        result["allocator_library"] = api.allocator_library;
        result["allocator_owns_malloc"] = api.allocator_owns_malloc;
        result["library"] = "gperftools";
        return result;
    });
    module.def("heap_profile_start", [](const std::string& prefix) {
        const auto api = require_heap_profiler();
        if (api.running()) throw std::runtime_error(
            "A gperftools heap profile is already active. Remove HEAPPROFILE and let the workbench own it.");
        api.start(prefix.c_str());
        if (!api.running()) throw std::runtime_error("gperftools did not start heap profiling");
    });
    module.def("heap_profile_dump", [](const std::string& reason) {
        const auto api = require_heap_profiler();
        if (!api.running()) throw std::runtime_error("gperftools heap profile is not active");
        api.dump(reason.c_str());
    });
    module.def("heap_profile_stop", [] {
        const auto api = require_heap_profiler();
        if (api.running()) api.stop();
    });
}
