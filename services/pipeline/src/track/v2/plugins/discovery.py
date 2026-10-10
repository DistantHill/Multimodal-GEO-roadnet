"""Discover explicit descriptions in the dedicated internal plugin packages."""
from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from types import ModuleType

from .core import CandidatePlugin, ClusterPlugin
from ..errors import ConfigError


def discover(package: str | ModuleType, description_type: type) -> dict:
    root = importlib.import_module(package) if isinstance(package, str) else package
    result = {}
    for info in sorted(pkgutil.iter_modules(root.__path__, root.__name__ + "."), key=lambda item: item.name):
        module = importlib.import_module(info.name)
        description = getattr(module, "PLUGIN", None)
        if description is None:
            continue
        if not isinstance(description, description_type):
            raise ConfigError(f"{info.name}.PLUGIN: invalid plugin description")
        identity = description.algorithm_id
        if not isinstance(identity, str) or not identity.strip():
            raise ConfigError(f"{info.name}.PLUGIN: algorithm ID must be non-empty")
        if identity in result:
            raise ConfigError(f"duplicate plugin ID: {identity}")
        if not isinstance(description.params_type, type) or not dataclasses.is_dataclass(description.params_type):
            raise ConfigError(f"{identity}: params_type must be a dataclass type")
        for item in dataclasses.fields(description.params_type):
            if item.default is not dataclasses.MISSING or item.default_factory is not dataclasses.MISSING:
                raise ConfigError(f"{identity}.{item.name}: parameter defaults are prohibited")
        if not callable(description.run):
            raise ConfigError(f"{identity}: run must be callable")
        declared = description.output_type if isinstance(description, CandidatePlugin) else description.input_type
        if declared != "candidate_points":
            raise ConfigError(f"{identity}: unsupported output/input type {declared!r}")
        result[identity] = description
    return result


def candidate_plugins() -> dict[str, CandidatePlugin]:
    return discover("track.v2.candidate_plugins", CandidatePlugin)


def cluster_plugins() -> dict[str, ClusterPlugin]:
    return discover("track.v2.cluster_plugins", ClusterPlugin)
