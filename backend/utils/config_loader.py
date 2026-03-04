"""Configuration loading utilities."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


def load_yaml_config(path: str | Path) -> dict:
    """Load a YAML configuration file."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_project_root() -> Path:
    """Return the project root directory."""
    return Path(__file__).resolve().parent.parent.parent


def get_config_path() -> Path:
    """Get the pipeline configuration file path."""
    env_path = os.environ.get("PIPELINE_CONFIG")
    if env_path:
        return Path(env_path)
    return get_project_root() / "config" / "pipeline_config.yaml"


def get_correction_dict_path() -> Path:
    """Get the correction dictionary file path."""
    return get_project_root() / "config" / "correction_dict.json"
