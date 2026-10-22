#pragma once

#include <pybind11/pybind11.h>

// Native process/cgroup telemetry and optional gperftools heap profiling.
void bind_profiling(pybind11::module_& module);
