"""One native extension; no runtime compiler or Python algorithm fallback."""
import sys

from pybind11.setup_helpers import Pybind11Extension, build_ext
from setuptools import setup

setup(
    ext_modules=[
        Pybind11Extension(
            "pipeline._native",
            ["native/core.cpp", "native/profiling.cpp"],
            cxx_std=17,
            extra_compile_args=["-O3", "-pthread"] if sys.platform != "win32" else ["/O2"],
            extra_link_args=["-pthread"] if sys.platform != "win32" else [],
            libraries=["dl"] if sys.platform.startswith("linux") else [],
        )
    ],
    cmdclass={"build_ext": build_ext},
)
