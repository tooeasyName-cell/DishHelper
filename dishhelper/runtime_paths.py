"""Separate bundled application resources from per-user writable files."""
import os
from pathlib import Path
import sys


def data_directory():
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "DishHelper"
    return Path(__file__).resolve().parents[1] / "data"


def local_env_path():
    if getattr(sys, "frozen", False):
        return data_directory() / ".env"
    return Path(__file__).resolve().parents[1] / ".env"
