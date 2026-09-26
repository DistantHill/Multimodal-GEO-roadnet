"""Single configuration boundary shared by Pipeline command-line entrypoints."""

from __future__ import annotations

import getpass
import os

from ..config import PipelineConfig, load_pipeline_config
from ..errors import ConfigError


def load_startup_config(*, ask_password: bool = False) -> PipelineConfig:
    """Load and validate the one Pipeline TOML selected by the environment."""
    config_path = os.environ.get("PIPELINE_CONFIG_PATH")
    if not config_path:
        raise ConfigError("PIPELINE_CONFIG_PATH is required")
    password = (
        getpass.getpass("PostgreSQL password: ")
        if ask_password
        else os.environ.get("POSTGRES_PASSWORD", "")
    )
    return load_pipeline_config(config_path, postgres_password=password)
