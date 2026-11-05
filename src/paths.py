"""Paths shared by the portable desktop build and the source checkout."""
from pathlib import Path
import os
import sys


def resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / relative_path


def app_data_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = root / "StealthInterviewCoPilot"
    path.mkdir(parents=True, exist_ok=True)
    return path
