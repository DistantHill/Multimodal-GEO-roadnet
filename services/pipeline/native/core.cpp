// Native numeric kernels for the parameter workbench. Geometry is projected XY in metres.
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "profiling.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <numeric>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace py = pybind11;
using F64 = py::array_t<double, py::array::c_style | py::array::forcecast>;
using I64 = py::array_t<std::int64_t, py::array::c_style | py::array::forcecast>;
using U8 = py::array_t<std::uint8_t, py::array::c_style | py::array::forcecast>;
namespace {
struct NativeResourceLimitExceeded : std::runtime_error {
    using std::runtime_error::runtime_error;
};

constexpr double pi = 3.14159265358979323846;
constexpr double eps = 1e-9;
constexpr std::int64_t unvisited = -2;

struct Point { double x, y; };
Point operator+(Point a, Point b) { return {a.x + b.x, a.y + b.y}; }
Point operator-(Point a, Point b) { return {a.x - b.x, a.y - b.y}; }
Point operator*(Point a, double scale) { return {a.x * scale, a.y * scale}; }
double dot(Point a, Point b) { return a.x * b.x + a.y * b.y; }
double cross(Point a, Point b) { return a.x * b.y - a.y * b.x; }
double norm(Point a) { return std::hypot(a.x, a.y); }
double distance(Point a, Point b) { return norm(a - b); }
bool finite(Point p) { return std::isfinite(p.x) && std::isfinite(p.y); }
double bearing(Point p) {
    double result = std::atan2(p.x, p.y) * 180.0 / pi;
    if (result < 0.0) result += 360.0;
    return result >= 360.0 ? 0.0 : result;
}
double angle_delta(double a, double b) {
    double value = std::fmod(b - a + 180.0, 360.0);
    return (value < 0.0 ? value + 360.0 : value) - 180.0;
}
Point direction(double heading) {
    const double radians = heading * pi / 180.0;
    return {std::sin(radians), std::cos(radians)};
}
Point point(const double* xy, std::size_t i) { return {xy[2 * i], xy[2 * i + 1]}; }
void append(std::vector<double>& xy, Point p) { xy.push_back(p.x); xy.push_back(p.y); }

template<class T> T option(const py::dict& config, const char* key, T fallback) {
    return config.contains(key) ? py::cast<T>(config[key]) : fallback;
}
void positive(double value, const char* name, bool zero_allowed = false) {
    if (!std::isfinite(value) || (zero_allowed ? value < 0.0 : value <= 0.0))
        throw std::invalid_argument(std::string(name) + " must be finite and " + (zero_allowed ? "non-negative" : "positive"));
}
void range(double value, double lo, double hi, const char* name) {
    if (!std::isfinite(value) || value < lo || value > hi)
        throw std::invalid_argument(std::string(name) + " is outside its valid range");
}
void vector_shape(const py::array& values, py::ssize_t length, const char* name) {
    if (values.ndim() != 1 || values.shape(0) != length)
        throw std::invalid_argument(std::string(name) + " must be a one-dimensional array of matching length");
}
void matrix_shape(const py::array& values, py::ssize_t columns, const char* name) {
    if (values.ndim() != 2 || values.shape(1) != columns)
        throw std::invalid_argument(std::string(name) + " has invalid shape");
}

template<class T> py::array array(std::vector<T>&& data, py::ssize_t columns = 0) {
    auto* owned = new std::vector<T>(std::move(data));
    py::capsule owner(owned, [](void* pointer) { delete static_cast<std::vector<T>*>(pointer); });
    if (columns) {
        return py::array(py::dtype::of<T>(),
                         {static_cast<py::ssize_t>(owned->size()) / columns, columns},
                         {static_cast<py::ssize_t>(sizeof(T)) * columns, static_cast<py::ssize_t>(sizeof(T))},
                         owned->data(), owner);
    }
    return py::array(py::dtype::of<T>(), {static_cast<py::ssize_t>(owned->size())},
                     {static_cast<py::ssize_t>(sizeof(T))}, owned->data(), owner);
}

struct Cell {
    std::int64_t x, y;
    bool operator==(const Cell& other) const { return x == other.x && y == other.y; }
};
struct CellHash {
    std::size_t operator()(const Cell& value) const {
        const auto a = std::hash<std::int64_t>{}(value.x);
        const auto b = std::hash<std::int64_t>{}(value.y);
        return a ^ (b + 0x9e3779b97f4a7c15ULL + (a << 6) + (a >> 2));
    }
};
Cell cell(Point p, double spacing) {
    const double x = std::floor(p.x / spacing), y = std::floor(p.y / spacing);
    // Avoid undefined float-to-integer conversions for corrupt or extreme coordinates.
    if (!std::isfinite(x) || !std::isfinite(y) || std::abs(x) > 9e15 || std::abs(y) > 9e15)
        throw std::invalid_argument("spatial cell index is outside the supported range");
    return {static_cast<std::int64_t>(x), static_cast<std::int64_t>(y)};
}
using Buckets = std::unordered_map<Cell, std::vector<std::size_t>, CellHash>;

struct RefineOptions {
    double max_gap_s, min_speed_mps, stationary_s, stationary_radius, max_speed;
    double min_heading, max_heading, max_extension;
    std::int64_t min_subtrack_points;
    bool inject;
    explicit RefineOptions(const py::dict& config)
        : max_gap_s(option(config, "max_gap_s", 120.0)),
          min_speed_mps(option(config, "min_speed_mps", 0.5)),
          stationary_s(option(config, "max_stationary_duration_s", 60.0)),
          stationary_radius(option(config, "stationary_radius_m", 15.0)),
          max_speed(option(config, "max_speed_mps", 70.0)),
          min_heading(option(config, "min_heading_delta_deg", 80.0)),
          max_heading(option(config, "max_heading_delta_deg", 100.0)),
          max_extension(option(config, "max_extension_m", 200.0)),
          min_subtrack_points(option<std::int64_t>(config, "min_subtrack_points", 2)),
          inject(option(config, "inject_orthogonal", true)) {
        positive(max_gap_s, "max_gap_s"); positive(min_speed_mps, "min_speed_mps", true);
        positive(stationary_s, "max_stationary_duration_s", true);
        positive(stationary_radius, "stationary_radius_m", true); positive(max_speed, "max_speed_mps");
        positive(max_extension, "max_extension_m", true);
        range(min_heading, 0, 180, "min_heading_delta_deg");
        range(max_heading, min_heading, 180, "max_heading_delta_deg");
        if (min_subtrack_points < 1) throw std::invalid_argument("min_subtrack_points must be positive");
    }
};

py::dict refine(F64 xy, F64 timestamp, F64 heading, F64 speed, const py::dict& config) {
    matrix_shape(xy, 2, "xy");
    const py::ssize_t size = xy.shape(0);
    vector_shape(timestamp, size, "timestamp"); vector_shape(heading, size, "heading"); vector_shape(speed, size, "speed");
    RefineOptions cfg(config);
    const auto* positions = xy.data(); const auto* times = timestamp.data();
    const auto* headings = heading.data(); const auto* speeds = speed.data();
    std::vector<double> out_xy, out_time, out_heading, out_speed, out_ratio, heading_cal;
    std::vector<std::int64_t> source_index, from_index, to_index, subtrack_index, sequence, excluded_index;
    std::vector<std::uint8_t> kind;
    std::vector<std::string> excluded_reason;
    {
        py::gil_scoped_release release;
        std::vector<std::size_t> valid;
        std::vector<std::uint8_t> removed(static_cast<std::size_t>(size), 0);
        auto exclude = [&](std::size_t index, const char* reason) {
            if (!removed[index]) {
                removed[index] = 1; excluded_index.push_back(static_cast<std::int64_t>(index)); excluded_reason.emplace_back(reason);
            }
        };
        for (py::ssize_t i = 0; i < size; ++i) {
            if (!finite(point(positions, i)) || !std::isfinite(times[i])) { exclude(i, "nonfinite_position_or_time"); continue; }
            if (!valid.empty()) {
                if (times[i] < times[valid.back()]) throw std::invalid_argument("refine requires timestamps sorted within a complete vehicle");
                if (times[i] == times[valid.back()]) { exclude(i, "duplicate_timestamp"); continue; }
            }
            valid.push_back(static_cast<std::size_t>(i));
        }
        // Only remove an isolated spike when its shortcut remains physically feasible.
        for (std::size_t k = 1; k + 1 < valid.size(); ++k) {
            const auto a = valid[k - 1], b = valid[k], c = valid[k + 1];
            if (removed[a] || removed[c]) continue;
            const double dt1 = times[b] - times[a], dt2 = times[c] - times[b];
            if (dt1 > cfg.max_gap_s || dt2 > cfg.max_gap_s) continue;
            if (distance(point(positions, a), point(positions, b)) / dt1 > cfg.max_speed &&
                distance(point(positions, b), point(positions, c)) / dt2 > cfg.max_speed &&
                distance(point(positions, a), point(positions, c)) / (dt1 + dt2) <= cfg.max_speed)
                exclude(b, "isolated_speed_spike");
        }
        valid.erase(std::remove_if(valid.begin(), valid.end(), [&](auto i) { return removed[i]; }), valid.end());
        std::vector<double> resolved_heading(static_cast<std::size_t>(size), 0), resolved_speed(static_cast<std::size_t>(size), 0);
        for (std::size_t k = 0; k < valid.size(); ++k) {
            const auto i = valid[k];
            if (std::isfinite(headings[i])) {
                resolved_heading[i] = std::fmod(headings[i], 360.0);
                if (resolved_heading[i] < 0) resolved_heading[i] += 360.0;
            } else if (k + 1 < valid.size() && times[valid[k + 1]] - times[i] <= cfg.max_gap_s) {
                resolved_heading[i] = bearing(point(positions, valid[k + 1]) - point(positions, i));
            } else if (k && times[i] - times[valid[k - 1]] <= cfg.max_gap_s) {
                resolved_heading[i] = bearing(point(positions, i) - point(positions, valid[k - 1]));
            }
            if (std::isfinite(speeds[i]) && speeds[i] >= 0) resolved_speed[i] = speeds[i];
            else if (k && times[i] - times[valid[k - 1]] <= cfg.max_gap_s)
                resolved_speed[i] = distance(point(positions, i), point(positions, valid[k - 1])) / (times[i] - times[valid[k - 1]]);
            else if (k + 1 < valid.size() && times[valid[k + 1]] - times[i] <= cfg.max_gap_s)
                resolved_speed[i] = distance(point(positions, i), point(positions, valid[k + 1])) / (times[valid[k + 1]] - times[i]);
        }
        std::int64_t partition = 0;
        std::size_t begin = 0;
        while (begin < valid.size()) {
            ++partition;
            std::size_t end = begin + 1;
            while (end < valid.size()) {
                const auto a = valid[end - 1], b = valid[end];
                const double dt = times[b] - times[a];
                if (dt > cfg.max_gap_s || distance(point(positions, a), point(positions, b)) / dt > cfg.max_speed) break;
                ++end;
            }
            for (std::size_t k = begin; k < end;) {
                if (resolved_speed[valid[k]] >= cfg.min_speed_mps) { ++k; continue; }
                const std::size_t first = k;
                while (k + 1 < end && resolved_speed[valid[k + 1]] < cfg.min_speed_mps &&
                       distance(point(positions, valid[first]), point(positions, valid[k + 1])) <= cfg.stationary_radius) ++k;
                if (k > first && times[valid[k]] - times[valid[first]] > cfg.stationary_s)
                    for (std::size_t j = first + 1; j < k; ++j) exclude(valid[j], "stationary_interior");
                ++k;
            }
            std::vector<std::size_t> kept;
            for (std::size_t k = begin; k < end; ++k) if (!removed[valid[k]]) kept.push_back(valid[k]);
            if (static_cast<std::int64_t>(kept.size()) < cfg.min_subtrack_points) {
                for (auto i : kept) exclude(i, "short_subtrack");
                begin = end; continue;
            }
            std::int64_t seq = 0;
            auto push = [&](Point p, double t, double h, double v, std::int64_t src,
                            std::int64_t a, std::int64_t b, double ratio, std::uint8_t type) {
                append(out_xy, p); out_time.push_back(t); out_heading.push_back(h); out_speed.push_back(v);
                source_index.push_back(src); from_index.push_back(a); to_index.push_back(b);
                out_ratio.push_back(ratio); kind.push_back(type); subtrack_index.push_back(partition); sequence.push_back(seq++);
            };
            for (std::size_t k = 0; k < kept.size(); ++k) {
                const auto a = kept[k]; const Point start = point(positions, a);
                push(start, times[a], resolved_heading[a], resolved_speed[a], a, a, a, 0, 0);
                if (!cfg.inject || k + 1 == kept.size()) continue;
                const auto b = kept[k + 1]; const Point finish = point(positions, b);
                const double delta = angle_delta(resolved_heading[a], resolved_heading[b]);
                if (std::abs(delta) < cfg.min_heading || std::abs(delta) > cfg.max_heading) continue;
                // Early sample forward, later sample backward. Both distances must be non-negative.
                const Point da = direction(resolved_heading[a]), db = direction(resolved_heading[b]) * -1.0;
                const double denominator = cross(da, db);
                if (std::abs(denominator) < eps) continue;
                const Point offset = finish - start;
                const double ta = cross(offset, db) / denominator, tb = cross(offset, da) / denominator;
                if (ta < -eps || tb < -eps || ta > cfg.max_extension || tb > cfg.max_extension || ta + tb <= eps) continue;
                const Point hit = start + da * std::max(ta, 0.0);
                // Do not manufacture a duplicate endpoint; it cannot add geometric information.
                if (distance(hit, start) <= eps || distance(hit, finish) <= eps) continue;
                const double ratio = std::max(ta, 0.0) / (std::max(ta, 0.0) + std::max(tb, 0.0));
                double h = std::fmod(resolved_heading[a] + delta * ratio + 360.0, 360.0);
                push(hit, times[a] + ratio * (times[b] - times[a]), h, 0.0, -1, a, b, ratio, 1);
            }
            begin = end;
        }
        heading_cal.assign(source_index.size(), std::numeric_limits<double>::quiet_NaN());
        double last = std::numeric_limits<double>::quiet_NaN();
        for (std::size_t i = 0; i < source_index.size(); ++i) {
            if (i == 0 || subtrack_index[i] != subtrack_index[i - 1]) last = std::numeric_limits<double>::quiet_NaN();
            if (i + 1 < source_index.size() && subtrack_index[i + 1] == subtrack_index[i]) {
                Point delta = point(out_xy.data(), i + 1) - point(out_xy.data(), i);
                if (norm(delta) > eps) { heading_cal[i] = bearing(delta); last = heading_cal[i]; }
            } else heading_cal[i] = last;
        }
    }
    py::dict result;
    result["xy"] = array(std::move(out_xy), 2); result["timestamp"] = array(std::move(out_time));
    result["heading"] = array(std::move(out_heading)); result["heading_cal"] = array(std::move(heading_cal));
    result["speed"] = array(std::move(out_speed)); result["source_index"] = array(std::move(source_index));
    result["from_index"] = array(std::move(from_index)); result["to_index"] = array(std::move(to_index));
    result["ratio_in_gap"] = array(std::move(out_ratio)); result["kind"] = array(std::move(kind));
    result["subtrack_index"] = array(std::move(subtrack_index)); result["sequence"] = array(std::move(sequence));
    result["excluded_index"] = array(std::move(excluded_index)); result["excluded_reason"] = py::cast(excluded_reason);
    return result;
}

py::dict turns(F64 xy, F64 timestamp, F64 heading, I64 subtrack, I64 sequence, const py::dict& config) {
    matrix_shape(xy, 2, "xy"); const auto n = xy.shape(0);
    vector_shape(timestamp, n, "timestamp"); vector_shape(heading, n, "heading");
    vector_shape(subtrack, n, "subtrack_index"); vector_shape(sequence, n, "sequence");
    const auto window = option<std::int64_t>(config, "turn_window", 2);
    const auto low = option(config, "min_turn_deg", 30.0), high = option(config, "max_turn_deg", 160.0);
    const auto leg = option(config, "min_leg_m", 5.0), movement = option(config, "min_movement_speed_mps", 0.5);
    if (window < 1 || window > 1000000) throw std::invalid_argument("turn_window must be between 1 and 1000000");
    range(low, 0, 180, "min_turn_deg"); range(high, low, 180, "max_turn_deg");
    positive(leg, "min_leg_m", true); positive(movement, "min_movement_speed_mps", true);
    const auto *positions = xy.data(), *times = timestamp.data(), *headings = heading.data();
    const auto *tracks = subtrack.data(), *seq = sequence.data();
    std::vector<std::int64_t> indices; std::vector<double> out_xy, angles, entry, exit;
    {
        py::gil_scoped_release release;
        std::vector<double> sine(n + 1, 0), cosine(n + 1, 0);
        std::vector<std::int64_t> missing(n + 1, 0), breaks(n + 1, 0);
        for (py::ssize_t i = 0; i < n; ++i) {
            const bool ok = std::isfinite(headings[i]);
            sine[i + 1] = sine[i] + (ok ? std::sin(headings[i] * pi / 180.0) : 0.0);
            cosine[i + 1] = cosine[i] + (ok ? std::cos(headings[i] * pi / 180.0) : 0.0);
            missing[i + 1] = missing[i] + (ok ? 0 : 1);
            breaks[i + 1] = breaks[i] + (i && (tracks[i] != tracks[i - 1] || seq[i] != seq[i - 1] + 1) ? 1 : 0);
        }
        for (py::ssize_t i = window; i + window < n; ++i) {
            const auto a = i - window, b = i + window;
            if (breaks[b + 1] != breaks[a + 1] || missing[b] != missing[a]) continue;
            const Point p = point(positions, i), before = point(positions, a), after = point(positions, b);
            if (!finite(p) || !finite(before) || !finite(after)) continue;
            const double incoming = distance(p, before), outgoing = distance(after, p), dt = times[b] - times[a];
            if (!std::isfinite(dt) || dt <= 0 || incoming < leg || outgoing < leg || (incoming + outgoing) / dt < movement) continue;
            Point in_sum{sine[i] - sine[a], cosine[i] - cosine[a]};
            Point out_sum{sine[b] - sine[i], cosine[b] - cosine[i]};
            if (norm(in_sum) < eps || norm(out_sum) < eps) continue;
            const double in = bearing(in_sum), out = bearing(out_sum), angle = angle_delta(in, out);
            if (std::abs(angle) < low || std::abs(angle) > high) continue;
            indices.push_back(i); append(out_xy, p); angles.push_back(angle); entry.push_back(in); exit.push_back(out);
        }
    }
    py::dict result;
    result["index"] = array(std::move(indices)); result["xy"] = array(std::move(out_xy), 2);
    result["turn_deg"] = array(std::move(angles)); result["entry_heading"] = array(std::move(entry));
    result["exit_heading"] = array(std::move(exit));
    return result;
}

struct Limits {
    std::int64_t candidates, pairs, memberships;
    explicit Limits(const py::dict& config)
        : candidates(option<std::int64_t>(config, "max_candidates", 1000000)),
          pairs(option<std::int64_t>(config, "max_pair_checks", 100000000)),
          memberships(option<std::int64_t>(config, "max_grid_memberships", 10000000)) {
        if (candidates < 1 || pairs < 1 || memberships < 1)
            throw std::invalid_argument("native resource limits must be positive");
    }
};
void check_limit(std::int64_t actual, std::int64_t limit, const char* key) {
    if (actual > limit) throw NativeResourceLimitExceeded(std::string("native resource limit exceeded: ") + key);
}

// An AABB grid avoids the all-pairs matrix; membership is explicitly bounded.
template<class Function>
void box_cells(Point a, Point b, double spacing, std::int64_t budget, Function callback) {
    const Cell lo = cell({std::min(a.x, b.x), std::min(a.y, b.y)}, spacing);
    const Cell hi = cell({std::max(a.x, b.x), std::max(a.y, b.y)}, spacing);
    const long double count = static_cast<long double>(hi.x - lo.x + 1) * (hi.y - lo.y + 1);
    if (count > static_cast<long double>(budget))
        throw NativeResourceLimitExceeded("native resource limit exceeded: max_grid_memberships (segment or query bounds)");
    for (auto x = lo.x; x <= hi.x; ++x)
        for (auto y = lo.y; y <= hi.y; ++y) callback(Cell{x, y});
}

py::dict crossings(F64 segments, I64 track_ids, const py::dict& config,
                   py::object headings_object, py::object speeds_object) {
    matrix_shape(segments, 4, "segments"); const auto n = segments.shape(0);
    vector_shape(track_ids, n, "track_ids");
    const double spacing = option(config, "cross_grid_size_m", 100.0);
    const double min_angle = option(config, "min_cross_angle_deg", 30.0);
    const double straightness = option(config, "cross_straightness_deg", 30.0);
    const double min_speed = option(config, "cross_min_speed_mps", 0.0);
    positive(spacing, "cross_grid_size_m"); range(min_angle, 0, 90, "min_cross_angle_deg");
    range(straightness, 0, 180, "cross_straightness_deg"); positive(min_speed, "cross_min_speed_mps", true);
    Limits limits(config);
    F64 headings, speeds; const double *h = nullptr, *v = nullptr;
    if (!headings_object.is_none()) {
        headings = F64::ensure(headings_object);
        if (!headings) throw std::invalid_argument("headings must be numeric");
        matrix_shape(headings, 2, "headings");
        if (headings.shape(0) != n) throw std::invalid_argument("headings length differs from segments");
        h = headings.data();
    }
    if (!speeds_object.is_none()) {
        speeds = F64::ensure(speeds_object);
        if (!speeds) throw std::invalid_argument("speeds must be numeric");
        matrix_shape(speeds, 2, "speeds");
        if (speeds.shape(0) != n) throw std::invalid_argument("speeds length differs from segments");
        v = speeds.data();
    }
    const auto* data = segments.data(); const auto* tracks = track_ids.data();
    std::vector<double> out_xy, angles; std::vector<std::int64_t> first, second;
    std::int64_t pair_checks = 0, memberships = 0, eligible_count = 0;
    {
        py::gil_scoped_release release;
        Buckets buckets;
        std::vector<std::int64_t> seen(n, -1);
        for (py::ssize_t i = 0; i < n; ++i) {
            Point a{data[4 * i], data[4 * i + 1]}, b{data[4 * i + 2], data[4 * i + 3]};
            if (!finite(a) || !finite(b)) throw std::invalid_argument("segment coordinates must be finite");
            const Point da = b - a; const double length_a = norm(da);
            if (length_a <= eps) continue;
            if (h && (!std::isfinite(h[2 * i]) || !std::isfinite(h[2 * i + 1]) ||
                      std::abs(angle_delta(h[2 * i], h[2 * i + 1])) > straightness)) continue;
            if (v && (!std::isfinite(v[2 * i]) || !std::isfinite(v[2 * i + 1]) ||
                      (v[2 * i] + v[2 * i + 1]) / 2.0 < min_speed)) continue;
            ++eligible_count;
            box_cells(a, b, spacing, limits.memberships, [&](Cell key) {
                auto iterator = buckets.find(key);
                if (iterator == buckets.end()) return;
                for (std::size_t j : iterator->second) {
                    if (seen[j] == i) continue;
                    seen[j] = i;
                    if (tracks[j] == tracks[i]) continue;
                    check_limit(++pair_checks, limits.pairs, "max_pair_checks");
                    Point c{data[4 * j], data[4 * j + 1]}, d{data[4 * j + 2], data[4 * j + 3]};
                    if (std::max(a.x, b.x) < std::min(c.x, d.x) || std::max(c.x, d.x) < std::min(a.x, b.x) ||
                        std::max(a.y, b.y) < std::min(c.y, d.y) || std::max(c.y, d.y) < std::min(a.y, b.y)) continue;
                    const Point db = d - c;
                    const double lengths = length_a * norm(db), determinant = cross(da, db);
                    if (std::abs(determinant) <= 1e-12 * lengths) continue;
                    const double cosine = std::clamp(std::abs(dot(da, db)) / lengths, 0.0, 1.0);
                    const double angle = std::acos(cosine) * 180.0 / pi;
                    if (angle + 1e-12 < min_angle) continue;
                    const Point offset = c - a;
                    const double t = cross(offset, db) / determinant, u = cross(offset, da) / determinant;
                    if (t < -1e-10 || t > 1.0 + 1e-10 || u < -1e-10 || u > 1.0 + 1e-10) continue;
                    check_limit(static_cast<std::int64_t>(first.size()) + 1, limits.candidates, "max_candidates");
                    append(out_xy, a + da * std::clamp(t, 0.0, 1.0)); first.push_back(j); second.push_back(i); angles.push_back(angle);
                }
            });
            box_cells(a, b, spacing, limits.memberships, [&](Cell key) {
                check_limit(++memberships, limits.memberships, "max_grid_memberships");
                buckets[key].push_back(i);
            });
        }
    }
    py::dict result;
    result["xy"] = array(std::move(out_xy), 2); result["segment_a"] = array(std::move(first));
    result["segment_b"] = array(std::move(second)); result["angle_deg"] = array(std::move(angles));
    result["pair_checks"] = pair_checks; result["grid_memberships"] = memberships; result["eligible_segments"] = eligible_count;
    return result;
}

struct UnionFind {
    std::vector<std::size_t> parent;
    explicit UnionFind(std::size_t n) : parent(n) { std::iota(parent.begin(), parent.end(), 0); }
    std::size_t find(std::size_t i) {
        while (parent[i] != i) { parent[i] = parent[parent[i]]; i = parent[i]; }
        return i;
    }
    void join(std::size_t a, std::size_t b) {
        a = find(a); b = find(b);
        if (a != b) parent[std::max(a, b)] = std::min(a, b);
    }
};

py::dict cluster(F64 xy, double radius, std::int64_t min_samples, std::int64_t max_pair_checks) {
    matrix_shape(xy, 2, "xy"); positive(radius, "eps_m");
    if (min_samples < 1 || max_pair_checks < 1) throw std::invalid_argument("min_samples and max_pair_checks must be positive");
    const auto n = static_cast<std::size_t>(xy.shape(0)); const auto* data = xy.data();
    std::vector<std::int64_t> labels(n, -1), counts;
    std::vector<double> centers;
    std::int64_t pair_checks = 0;
    {
        py::gil_scoped_release release;
        // Cell diameter <= eps: all points in a dense cell are core without enumerating its NxN pairs.
        const double spacing = radius / std::sqrt(2.0), squared = radius * radius;
        Buckets buckets; std::vector<Cell> keys; keys.reserve(n);
        std::unordered_map<Cell, std::size_t, CellHash> cell_ids;
        for (std::size_t i = 0; i < n; ++i) {
            const Point p = point(data, i);
            if (!finite(p)) throw std::invalid_argument("cluster coordinates must be finite");
            const Cell key = cell(p, spacing);
            auto insertion = cell_ids.emplace(key, keys.size());
            if (insertion.second) keys.push_back(key);
            buckets[key].push_back(i);
        }
        auto near = [&](std::size_t a, std::size_t b) {
            check_limit(++pair_checks, max_pair_checks, "max_pair_checks (DBSCAN)");
            const Point delta = point(data, a) - point(data, b);
            return dot(delta, delta) <= squared * (1.0 + 1e-14);
        };
        auto neighbors = [&](Cell key, auto callback) {
            for (std::int64_t dx = -2; dx <= 2; ++dx)
                for (std::int64_t dy = -2; dy <= 2; ++dy) {
                    auto found = buckets.find({key.x + dx, key.y + dy});
                    if (found != buckets.end() && !callback(found->first, found->second)) return;
                }
        };
        std::vector<std::uint8_t> core(n, 0);
        std::vector<std::vector<std::size_t>> core_cells(keys.size());
        for (std::size_t k = 0; k < keys.size(); ++k) {
            const auto& members = buckets.at(keys[k]);
            for (auto i : members) {
                if (members.size() >= static_cast<std::size_t>(min_samples)) core[i] = 1;
                else {
                    std::int64_t count = 0;
                    neighbors(keys[k], [&](Cell, const auto& other) {
                        for (auto j : other) {
                            if (near(i, j) && ++count >= min_samples) return false;
                        }
                        return true;
                    });
                    core[i] = count >= min_samples;
                }
                if (core[i]) core_cells[k].push_back(i);
            }
        }
        UnionFind components(n);
        std::vector<Point> minimum(keys.size()), maximum(keys.size());
        for (std::size_t k = 0; k < keys.size(); ++k) {
            if (core_cells[k].empty()) continue;
            minimum[k] = maximum[k] = point(data, core_cells[k][0]);
            for (auto i : core_cells[k]) {
                components.join(core_cells[k][0], i); const Point p = point(data, i);
                minimum[k] = {std::min(minimum[k].x, p.x), std::min(minimum[k].y, p.y)};
                maximum[k] = {std::max(maximum[k].x, p.x), std::max(maximum[k].y, p.y)};
            }
        }
        for (std::size_t k = 0; k < keys.size(); ++k) {
            if (core_cells[k].empty()) continue;
            neighbors(keys[k], [&](Cell key, const auto&) {
                const auto other = cell_ids.at(key);
                if (other <= k || core_cells[other].empty()) return true;
                const double dx = std::max({0.0, minimum[k].x - maximum[other].x, minimum[other].x - maximum[k].x});
                const double dy = std::max({0.0, minimum[k].y - maximum[other].y, minimum[other].y - maximum[k].y});
                if (dx * dx + dy * dy > squared) return true;
                // One connecting core pair joins the entire pair of cells; do not materialize all edges.
                bool connected = false;
                for (auto i : core_cells[k]) {
                    for (auto j : core_cells[other]) {
                        if (near(i, j)) { connected = true; break; }
                    }
                    if (connected) break;
                }
                if (connected) components.join(core_cells[k][0], core_cells[other][0]);
                return true;
            });
        }
        std::unordered_map<std::size_t, std::int64_t> cluster_ids;
        for (std::size_t i = 0; i < n; ++i) {
            if (!core[i]) continue;
            const auto root = components.find(i);
            auto found = cluster_ids.emplace(root, cluster_ids.size());
            labels[i] = found.first->second;
        }
        for (std::size_t i = 0; i < n; ++i) {
            if (core[i]) continue;
            const Cell key = cell(point(data, i), spacing);
            std::int64_t chosen = std::numeric_limits<std::int64_t>::max();
            neighbors(key, [&](Cell neighbor_key, const auto&) {
                const auto k = cell_ids.at(neighbor_key);
                if (core_cells[k].empty()) return true;
                const auto label = labels[core_cells[k][0]];
                if (label >= chosen) return true;
                for (auto j : core_cells[k]) if (near(i, j)) { chosen = label; break; }
                return true;
            });
            if (chosen != std::numeric_limits<std::int64_t>::max()) labels[i] = chosen;
        }
        centers.assign(2 * cluster_ids.size(), 0); counts.assign(cluster_ids.size(), 0);
        for (std::size_t i = 0; i < n; ++i) if (labels[i] >= 0) {
            const auto k = static_cast<std::size_t>(labels[i]);
            centers[2 * k] += data[2 * i]; centers[2 * k + 1] += data[2 * i + 1]; ++counts[k];
        }
        for (std::size_t k = 0; k < counts.size(); ++k) { centers[2 * k] /= counts[k]; centers[2 * k + 1] /= counts[k]; }
    }
    py::dict result; result["labels"] = array(std::move(labels)); result["centers"] = array(std::move(centers), 2);
    result["counts"] = array(std::move(counts)); result["pair_checks"] = pair_checks;
    return result;
}

py::dict topology(F64 centers, F64 segments, I64 track_ids, const py::dict& config) {
    matrix_shape(centers, 2, "centers"); matrix_shape(segments, 4, "segments");
    const auto n = segments.shape(0), count = centers.shape(0); vector_shape(track_ids, n, "track_ids");
    const auto radius = option(config, "topology_radius_m", 30.0);
    const auto tolerance = option(config, "arm_merge_angle_deg", 25.0);
    const auto spacing = option(config, "cross_grid_size_m", 100.0);
    const auto min_arms = option<std::int64_t>(config, "min_arms", 3);
    const auto min_vehicles = option<std::int64_t>(config, "min_vehicles", 2);
    positive(radius, "topology_radius_m"); positive(spacing, "cross_grid_size_m");
    range(tolerance, 0, 179.999, "arm_merge_angle_deg");
    if (min_arms < 1 || min_vehicles < 1) throw std::invalid_argument("min_arms and min_vehicles must be positive");
    Limits limits(config);
    const auto *data = segments.data(), *candidate = centers.data(); const auto* tracks = track_ids.data();
    std::vector<std::int64_t> arm_count, vehicle_count, offsets{0};
    std::vector<std::uint8_t> valid; std::vector<double> arms;
    std::int64_t memberships = 0, pair_checks = 0;
    {
        py::gil_scoped_release release;
        Buckets buckets;
        for (py::ssize_t i = 0; i < n; ++i) {
            Point a{data[4 * i], data[4 * i + 1]}, b{data[4 * i + 2], data[4 * i + 3]};
            if (!finite(a) || !finite(b)) throw std::invalid_argument("topology segment coordinates must be finite");
            if (distance(a, b) <= eps) continue;
            box_cells(a, b, spacing, limits.memberships, [&](Cell key) {
                check_limit(++memberships, limits.memberships, "max_grid_memberships"); buckets[key].push_back(i);
            });
        }
        std::vector<std::int64_t> seen(n, -1);
        for (py::ssize_t index = 0; index < count; ++index) {
            const Point center = point(candidate, index);
            if (!finite(center)) throw std::invalid_argument("topology centers must be finite");
            std::vector<double> bearings; std::unordered_set<std::int64_t> vehicles;
            box_cells({center.x - radius, center.y - radius}, {center.x + radius, center.y + radius}, spacing,
                      limits.memberships, [&](Cell key) {
                auto found = buckets.find(key); if (found == buckets.end()) return;
                for (auto j : found->second) {
                    if (seen[j] == index) continue;
                    seen[j] = index;
                    check_limit(++pair_checks, limits.pairs, "max_pair_checks (topology)");
                    const Point a{data[4 * j], data[4 * j + 1]}, b{data[4 * j + 2], data[4 * j + 3]}, d = b - a;
                    const double t = std::clamp(dot(center - a, d) / dot(d, d), 0.0, 1.0);
                    if (distance(center, a + d * t) > radius) continue;
                    vehicles.insert(tracks[j]);
                    // Only trajectories reaching the disk boundary support outward road arms.
                    if (distance(a, center) > radius) bearings.push_back(bearing(a - center));
                    if (distance(b, center) > radius) bearings.push_back(bearing(b - center));
                }
            });
            std::sort(bearings.begin(), bearings.end());
            std::vector<double> representatives;
            if (!bearings.empty()) {
                // Begin after the largest circular gap; each group's full angular span is bounded.
                std::size_t first = 0; double largest = -1;
                for (std::size_t k = 0; k < bearings.size(); ++k) {
                    const double next = (k + 1 < bearings.size() ? bearings[k + 1] : bearings[0] + 360.0);
                    if (next - bearings[k] > largest) { largest = next - bearings[k]; first = (k + 1) % bearings.size(); }
                }
                double start = 0, sine = 0, cosine = 0;
                for (std::size_t k = 0; k < bearings.size(); ++k) {
                    const auto original = (first + k) % bearings.size();
                    const double unwrapped = bearings[original] + (original < first ? 360.0 : 0.0);
                    if (k && unwrapped - start > tolerance) {
                        representatives.push_back(bearing({sine, cosine})); sine = cosine = 0; start = unwrapped;
                    } else if (!k) start = unwrapped;
                    sine += std::sin(unwrapped * pi / 180.0); cosine += std::cos(unwrapped * pi / 180.0);
                }
                representatives.push_back(bearing({sine, cosine}));
                std::sort(representatives.begin(), representatives.end());
            }
            arm_count.push_back(representatives.size()); vehicle_count.push_back(vehicles.size());
            valid.push_back(static_cast<std::int64_t>(representatives.size()) >= min_arms &&
                            static_cast<std::int64_t>(vehicles.size()) >= min_vehicles);
            arms.insert(arms.end(), representatives.begin(), representatives.end()); offsets.push_back(arms.size());
        }
    }
    py::dict result; result["arm_count"] = array(std::move(arm_count)); result["vehicle_count"] = array(std::move(vehicle_count));
    result["valid"] = array(std::move(valid)); result["arm_offsets"] = array(std::move(offsets));
    result["arm_bearings"] = array(std::move(arms)); result["pair_checks"] = pair_checks; result["grid_memberships"] = memberships;
    return result;
}

struct Polyline {
    std::vector<Point> points;
    std::vector<double> cumulative;
    std::size_t original;
    bool reversed;
    double length() const { return cumulative.empty() ? 0.0 : cumulative.back(); }
};
void extend(Polyline& line, Point p) {
    if (line.points.empty()) { line.points.push_back(p); line.cumulative.push_back(0); return; }
    const auto delta = distance(line.points.back(), p);
    if (delta > eps) { line.points.push_back(p); line.cumulative.push_back(line.cumulative.back() + delta); }
}
Point interpolate(const Polyline& line, double target) {
    target = std::clamp(target, 0.0, line.length());
    if (target <= 0) return line.points.front();
    if (target >= line.length()) return line.points.back();
    auto upper = std::lower_bound(line.cumulative.begin(), line.cumulative.end(), target);
    const auto b = static_cast<std::size_t>(upper - line.cumulative.begin()), a = b - 1;
    const double ratio = (target - line.cumulative[a]) / (line.cumulative[b] - line.cumulative[a]);
    return line.points[a] * (1.0 - ratio) + line.points[b] * ratio;
}
double median(std::vector<double>& values) {
    const auto middle = values.size() / 2;
    std::nth_element(values.begin(), values.begin() + middle, values.end());
    const auto hi = values[middle];
    if (values.size() % 2) return hi;
    return (hi + *std::max_element(values.begin(), values.begin() + middle)) / 2.0;
}

py::dict fit(F64 xy, I64 offsets, U8 reversed, const py::dict& config) {
    matrix_shape(xy, 2, "xy");
    if (offsets.ndim() != 1 || offsets.shape(0) < 1) throw std::invalid_argument("offsets must have K+1 entries");
    const auto k = offsets.shape(0) - 1, n = xy.shape(0); vector_shape(reversed, k, "reversed");
    const double sample_spacing = option(config, "fit_sample_spacing_m", 5.0);
    const double output_spacing = option(config, "point_spacing_m", 5.0);
    const auto minimum = option<std::int64_t>(config, "min_clips_per_pair", 1);
    const auto max_points = option<std::int64_t>(config, "max_fit_points", 1000000);
    positive(sample_spacing, "fit_sample_spacing_m"); positive(output_spacing, "point_spacing_m");
    if (minimum < 1 || max_points < 2) throw std::invalid_argument("min_clips_per_pair and max_fit_points are invalid");
    check_limit(n, max_points, "max_fit_points (input)");
    const auto* data = xy.data(); const auto* boundaries = offsets.data(); const auto* reverse = reversed.data();
    if (boundaries[0] != 0 || boundaries[k] != n) throw std::invalid_argument("offsets must start at 0 and end at len(xy)");
    for (py::ssize_t i = 0; i < k; ++i)
        if (boundaries[i + 1] < boundaries[i]) throw std::invalid_argument("offsets must be non-decreasing");
    std::vector<double> output; std::vector<std::int64_t> used;
    double length = 0; std::int64_t forward = 0, reverse_count = 0;
    std::string status = "insufficient_clips";
    {
        py::gil_scoped_release release;
        std::vector<Polyline> lines;
        for (py::ssize_t i = 0; i < k; ++i) {
            if (reverse[i] > 1) throw std::invalid_argument("reversed must contain only 0 or 1");
            Polyline line; line.original = i; line.reversed = reverse[i] != 0;
            for (auto j = boundaries[i]; j < boundaries[i + 1]; ++j) {
                const Point p = point(data, j); if (!finite(p)) throw std::invalid_argument("fit coordinates must be finite");
                extend(line, p);
            }
            if (line.length() <= eps) continue;
            used.push_back(i); if (line.reversed) ++reverse_count; else ++forward;
            lines.push_back(std::move(line));
        }
        if (static_cast<std::int64_t>(lines.size()) >= minimum) {
            std::vector<double> lengths; lengths.reserve(lines.size());
            for (const auto& line : lines) lengths.push_back(line.length());
            const double mid_length = median(lengths);
            const double raw_count = std::max(2.0, std::ceil(mid_length / sample_spacing) + 1.0);
            if (!std::isfinite(raw_count) || raw_count > max_points)
                throw NativeResourceLimitExceeded("native resource limit exceeded: max_fit_points (samples)");
            const auto sample_count = static_cast<std::size_t>(raw_count);
            Polyline fitted;
            std::vector<double> x(lines.size()), y(lines.size());
            for (std::size_t sample = 0; sample < sample_count; ++sample) {
                const double progress = static_cast<double>(sample) / (sample_count - 1);
                for (std::size_t i = 0; i < lines.size(); ++i) {
                    const auto& line = lines[i];
                    const Point p = interpolate(line, line.length() * (line.reversed ? 1.0 - progress : progress));
                    x[i] = p.x; y[i] = p.y;
                }
                extend(fitted, {median(x), median(y)});
            }
            status = "degenerate";
            if (fitted.length() > eps) {
                const double raw_output = std::ceil(fitted.length() / output_spacing) + 1.0;
                if (!std::isfinite(raw_output) || raw_output > max_points)
                    throw NativeResourceLimitExceeded("native resource limit exceeded: max_fit_points (output)");
                // Fixed metre spacing and an explicit final endpoint, matching the previous operator.
                Point previous = fitted.points.front(); append(output, previous);
                for (std::size_t i = 1; static_cast<double>(i) * output_spacing < fitted.length() - eps; ++i) {
                    Point p = interpolate(fitted, static_cast<double>(i) * output_spacing);
                    length += distance(previous, p); append(output, p); previous = p;
                }
                Point last = fitted.points.back(); length += distance(previous, last); append(output, last);
                status = "ok";
            }
        }
    }
    py::dict result; result["xy"] = array(std::move(output), 2); result["length_m"] = length;
    result["clip_count"] = static_cast<std::int64_t>(used.size()); result["used_clip_index"] = array(std::move(used));
    result["forward_clip_count"] = forward; result["reverse_clip_count"] = reverse_count; result["status"] = status;
    return result;
}


struct ClipData {
    std::vector<double> xy, ratios;
    std::vector<std::int64_t> source, from, to;
    std::int64_t start = -1, end = -1;
    double length = 0;
};
struct InsideInterval {
    double start, end;
    std::int64_t enter_id, exit_id;
};

py::list clip_track(F64 xy, F64 centers, F64 radii, const py::dict& config) {
    matrix_shape(xy, 2, "xy"); matrix_shape(centers, 2, "centers");
    const auto n = xy.shape(0), k = centers.shape(0); vector_shape(radii, k, "radii");
    const double spacing = option(config, "cross_grid_size_m", 100.0);
    const double min_length = option(config, "min_length_m", 5.0);
    const bool boundary = option(config, "include_boundary_clips", true);
    positive(spacing, "cross_grid_size_m"); positive(min_length, "min_length_m", true);
    Limits limits(config);
    const auto *points = xy.data(), *nodes = centers.data(), *radius = radii.data();
    std::vector<ClipData> output;
    {
        py::gil_scoped_release release;
        Buckets buckets; std::int64_t memberships = 0, pair_checks = 0;
        for (py::ssize_t i = 0; i < k; ++i) {
            const Point c = point(nodes, i);
            if (!finite(c)) throw std::invalid_argument("clip centers must be finite");
            positive(radius[i], "radii");
            box_cells({c.x - radius[i], c.y - radius[i]}, {c.x + radius[i], c.y + radius[i]}, spacing,
                      limits.memberships, [&](Cell key) {
                check_limit(++memberships, limits.memberships, "max_grid_memberships (clip discs)");
                buckets[key].push_back(i);
            });
        }
        for (py::ssize_t i = 0; i < n; ++i)
            if (!finite(point(points, i))) throw std::invalid_argument("clip coordinates must be finite");
        std::vector<std::int64_t> seen(k, -1);
        ClipData current;
        bool active = false, previous_inside = false;
        std::int64_t previous_exit = -1;
        auto vertex = [&](std::size_t segment, double ratio) {
            ratio = std::clamp(ratio, 0.0, 1.0);
            Point p = point(points, segment) * (1.0 - ratio) + point(points, segment + 1) * ratio;
            if (!current.xy.empty()) {
                const Point old = point(current.xy.data(), current.xy.size() / 2 - 1);
                const double delta = distance(old, p);
                if (delta <= eps) return;
                current.length += delta;
            }
            append(current.xy, p);
            current.source.push_back(ratio <= 1e-12 ? static_cast<std::int64_t>(segment) : ratio >= 1.0 - 1e-12 ? static_cast<std::int64_t>(segment + 1) : -1);
            current.from.push_back(segment); current.to.push_back(segment + 1); current.ratios.push_back(ratio);
        };
        auto finish = [&](std::int64_t end) {
            if (!active) return;
            current.end = end;
            if (current.xy.size() >= 4 && current.length >= min_length &&
                (boundary || (current.start >= 0 && current.end >= 0))) {
                check_limit(static_cast<std::int64_t>(output.size()) + 1, limits.candidates, "max_candidates (clips)");
                output.push_back(std::move(current));
            }
            current = ClipData{}; active = false;
        };
        auto outside = [&](std::size_t segment, double begin, double end, std::int64_t left, std::int64_t right) {
            if (end - begin <= 1e-12) return;
            if (!active) { current = ClipData{}; current.start = left; active = true; vertex(segment, begin); }
            vertex(segment, end);
            if (right >= 0) finish(right);
        };
        for (py::ssize_t i = 0; i + 1 < n; ++i) {
            const Point a = point(points, i), b = point(points, i + 1), direction = b - a;
            const double denominator = dot(direction, direction);
            if (denominator <= eps * eps) continue;
            std::vector<InsideInterval> intervals;
            box_cells(a, b, spacing, limits.memberships, [&](Cell key) {
                auto found = buckets.find(key); if (found == buckets.end()) return;
                for (const auto j : found->second) {
                    if (seen[j] == i) continue;
                    seen[j] = i; check_limit(++pair_checks, limits.pairs, "max_pair_checks (clip)");
                    const Point offset = a - point(nodes, j);
                    const double projected = -dot(offset, direction) / denominator;
                    // Use perpendicular distance instead of subtracting two large quadratic coefficients.
                    const double perpendicular = cross(offset, direction);
                    const double radial = radius[j] * radius[j] - perpendicular * perpendicular / denominator;
                    if (radial <= std::max(1e-12, radius[j] * radius[j] * 1e-14)) continue; // tangent
                    const double reach = std::sqrt(radial / denominator);
                    const double begin = std::max(0.0, projected - reach), end = std::min(1.0, projected + reach);
                    if (end - begin > 1e-12) intervals.push_back({begin, end, static_cast<std::int64_t>(j), static_cast<std::int64_t>(j)});
                }
            });
            std::sort(intervals.begin(), intervals.end(), [](const auto& a, const auto& b) {
                if (a.start != b.start) return a.start < b.start;
                if (a.end != b.end) return a.end > b.end;
                return a.enter_id < b.enter_id;
            });
            std::vector<InsideInterval> merged;
            for (auto item : intervals) {
                if (merged.empty() || item.start > merged.back().end + 1e-12) merged.push_back(item);
                else {
                    auto& last = merged.back();
                    if (item.start == last.start) last.enter_id = std::min(last.enter_id, item.enter_id);
                    if (item.end > last.end + 1e-12) { last.end = item.end; last.exit_id = item.exit_id; }
                    else if (std::abs(item.end - last.end) <= 1e-12) last.exit_id = std::min(last.exit_id, item.exit_id);
                }
            }
            if (!merged.empty() && merged.front().start <= 1e-12 && active) {
                vertex(i, 0.0); finish(merged.front().enter_id);
            }
            double cursor = 0.0; std::int64_t left = previous_inside ? previous_exit : -1;
            for (const auto& interval : merged) {
                outside(i, cursor, interval.start, left, interval.enter_id);
                cursor = interval.end; left = interval.exit_id;
            }
            if (cursor < 1.0 - 1e-12) outside(i, cursor, 1.0, left, -1);
            previous_inside = !merged.empty() && merged.back().end >= 1.0 - 1e-12;
            previous_exit = previous_inside ? merged.back().exit_id : -1;
        }
        finish(-1);
    }
    py::list result;
    for (auto& item : output) {
        py::dict record;
        record["xy"] = array(std::move(item.xy), 2); record["source_index"] = array(std::move(item.source));
        record["from_index"] = array(std::move(item.from)); record["to_index"] = array(std::move(item.to));
        record["ratio_in_gap"] = array(std::move(item.ratios)); record["length_m"] = item.length;
        record["start_intersection_index"] = item.start; record["end_intersection_index"] = item.end;
        record["clip_type"] = (item.start >= 0 && item.end >= 0 ? (item.start == item.end ? "same_intersection" : "between_intersection") :
                               item.start >= 0 ? "boundary_end" : item.end >= 0 ? "boundary_start" : "full_track");
        result.append(record);
    }
    return result;
}
} // namespace

PYBIND11_MODULE(_native, module) {
    py::register_exception<NativeResourceLimitExceeded>(module, "NativeResourceLimitExceeded", PyExc_RuntimeError);
    module.doc() = "C++ numerical operators and resource telemetry for the roadnet parameter workbench";
    module.attr("kernel_version") = "workbench-1";
    module.def("refine", &refine, py::arg("xy"), py::arg("timestamp"), py::arg("heading"), py::arg("speed"), py::arg("config") = py::dict(),
               "Refine one complete, timestamp-ordered vehicle; return columns and source-index provenance.");
    module.def("turns", &turns, py::arg("xy"), py::arg("timestamp"), py::arg("heading"), py::arg("subtrack_index"), py::arg("sequence"), py::arg("config") = py::dict(),
               "Extract circular-mean heading changes from symmetric trajectory windows.");
    module.def("crossings", &crossings, py::arg("segments"), py::arg("track_ids"), py::arg("config") = py::dict(),
               py::arg("headings") = py::none(), py::arg("speeds") = py::none(),
               "Find exact cross-vehicle segment intersections with a bounded spatial index.");
    module.def("cluster", &cluster, py::arg("xy"), py::arg("eps_m"), py::arg("min_samples"), py::arg("max_pair_checks") = 100000000,
               "Exact DBSCAN with dense-cell compression and deterministic border assignment.");
    module.def("topology", &topology, py::arg("centers"), py::arg("segments"), py::arg("track_ids"), py::arg("config") = py::dict(),
               "Count supported outward arms and distinct vehicles within each intersection disk.");
    module.def("clip_track", &clip_track, py::arg("xy"), py::arg("centers"), py::arg("radii"), py::arg("config") = py::dict(),
               "Clip an ordered track outside the union of intersection discs, with interpolated lineage.");
    module.def("fit", &fit, py::arg("xy"), py::arg("offsets"), py::arg("reversed"), py::arg("config") = py::dict(),
               "Orient, resample and aggregate clips with a coordinate-wise median; resample to metre spacing.");
    bind_profiling(module);
}
