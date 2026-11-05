"""In-app updates for the packaged Windows build.

The desktop repo publishes a GitHub release for every ``vX.Y.Z`` tag: the
workflow on GitHub attaches ``StealthInterviewCoPilot.exe`` to it. This module
asks GitHub for ``releases/latest``, compares the tag with :data:`version.APP_VERSION`
and can download the asset and swap the running EXE (renaming a running EXE is
allowed on Windows, so a tiny batch script finishes the job after we exit).
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from version import APP_VERSION, GITHUB_REPO

log = logging.getLogger(__name__)

RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "StealthInterviewCoPilot.exe"
TIMEOUT = 12


@dataclass
class Update:
    """A published release that is newer than this build."""

    version: str
    notes: str
    url: str
    size: int


def version_key(value: str) -> tuple[int, ...]:
    """Turn ``v1.2.3`` (or ``1.2.3-beta``) into ``(1, 2, 3)`` for comparisons."""
    parts: list[int] = []
    for chunk in str(value).strip().lstrip("vV").replace("-", ".").split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    return version_key(candidate) > version_key(current)


def check() -> Update | None:
    """Return the newest published release when it beats the running build."""
    try:
        response = requests.get(
            RELEASE_API,
            timeout=TIMEOUT,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"StealthCoPilot/{APP_VERSION}"},
        )
        if response.status_code != 200:
            log.info("Update check returned HTTP %s", response.status_code)
            return None
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        log.info("Update check failed: %s", exc)
        return None

    tag = str(data.get("tag_name") or "")
    if not tag or not is_newer(tag):
        return None
    for asset in data.get("assets") or []:
        if str(asset.get("name") or "").lower() == ASSET_NAME.lower():
            return Update(
                version=tag.lstrip("vV"),
                notes=str(data.get("body") or "").strip(),
                url=str(asset.get("browser_download_url") or ""),
                size=int(asset.get("size") or 0),
            )
    log.info("Release %s has no %s asset", tag, ASSET_NAME)
    return None


def download(update: Update, progress: Callable[[float], None] | None = None) -> Path:
    """Fetch the release EXE into the temp folder and return its path."""
    target = Path(tempfile.gettempdir()) / f"StealthInterviewCoPilot-{update.version}.exe"
    with requests.get(update.url, stream=True, timeout=120) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or update.size or 0)
        done = 0
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                handle.write(chunk)
                done += len(chunk)
                if progress is not None and total:
                    progress(min(1.0, done / total))
    return target


def is_frozen() -> bool:
    """True inside the PyInstaller build; a source checkout cannot swap itself."""
    return bool(getattr(sys, "frozen", False))


def apply_and_restart(new_file: Path) -> bool:
    """Hand the swap to a detached batch script; the caller should quit next."""
    if not is_frozen():
        log.info("Running from source, so the update is left for the next release")
        return False
    exe = Path(sys.executable).resolve()
    staging = Path(new_file).resolve()
    if not staging.is_file():
        log.warning("Downloaded update is missing: %s", staging)
        return False

    script = Path(tempfile.gettempdir()) / "stealth-copilot-update.bat"
    script.write_text(
        "\r\n".join([
            "@echo off",
            f'set "APP={exe}"',
            f'set "NEW={staging}"',
            ":wait",
            f'tasklist /fi "imagename eq {exe.name}" 2>nul | find /i "{exe.name}" >nul',
            "if not errorlevel 1 (",
            "  timeout /t 1 /nobreak >nul",
            "  goto wait",
            ")",
            'if exist "%APP%.old" del /f /q "%APP%.old" >nul 2>&1',
            'move /y "%APP%" "%APP%.old" >nul 2>&1',
            'move /y "%NEW%" "%APP%" >nul 2>&1',
            'if exist "%APP%" start "" "%APP%"',
            'del /f /q "%~f0" >nul 2>&1',
            "",
        ]),
        encoding="utf-8",
    )

    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True,
                         cwd=str(exe.parent))
    except OSError:
        log.exception("Could not start the update script")
        return False
    log.info("Update %s staged; restarting through %s", staging.name, script.name)
    return True
