"""In-app updates for the packaged Windows build.

Every ``vX.Y.Z`` tag produces a GitHub release from the desktop repo. Alongside
the full ``StealthInterviewCoPilot.exe`` the workflow also publishes a bsdiff
patch that turns the previous build into the new one - a one-file EXE is ~108 MB
but a real code change usually patches in under a megabyte.

The updater downloads the patch when it exists (falling back to the full EXE),
rebuilds the new binary locally, verifies its size and hands the swap to a small
batch script that waits for the app to exit, replaces the EXE and starts it.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import bsdiff4
import requests

from version import APP_VERSION, GITHUB_REPO

log = logging.getLogger(__name__)

RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "StealthInterviewCoPilot.exe"
VERSIONED_ASSET = "StealthInterviewCoPilot-v{version}.exe"
PATCH_TEMPLATE = "StealthInterviewCoPilot-v{old}-to-v{new}.bsdiff"
TIMEOUT = 20
CHUNK = 1 << 18

# (bytes downloaded, total bytes when the server tells us, else 0)
Progress = Callable[[int, int], None]
Status = Callable[[str], None]


@dataclass
class Update:
    """A published release that is newer than this build."""

    version: str
    notes: str
    url: str
    size: int
    patch_url: str = ""
    patch_size: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def has_patch(self) -> bool:
        return bool(self.patch_url)


def version_key(value: str) -> tuple[int, ...]:
    """Turn ``v1.2.3`` (or ``1.2.3-beta``) into ``(1, 2, 3)`` for comparisons."""
    parts: list[int] = []
    for chunk in str(value).strip().lstrip("vV").replace("-", ".").split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    return version_key(candidate) > version_key(current)


def _installer_asset(assets: list, version: str) -> dict | None:
    """Prefer the versioned installer so the saved file says which build it is."""
    wanted = VERSIONED_ASSET.format(version=version).lower()
    for item in assets:
        if str(item.get("name") or "").lower() == wanted:
            return item
    for item in assets:
        if str(item.get("name") or "").lower() == ASSET_NAME.lower():
            return item
    return None


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
    new_version = tag.lstrip("vV")
    assets = data.get("assets") or []
    full = _installer_asset(assets, new_version)
    if full is None:
        log.info("Release %s has no %s asset", tag, ASSET_NAME)
        return None
    patch_name = PATCH_TEMPLATE.format(old=APP_VERSION, new=new_version).lower()
    patch = next((item for item in assets
                  if str(item.get("name") or "").lower() == patch_name), None)
    update = Update(
        version=new_version,
        notes=str(data.get("body") or "").strip(),
        url=str(full.get("browser_download_url") or ""),
        size=int(full.get("size") or 0),
        patch_url=str((patch or {}).get("browser_download_url") or ""),
        patch_size=int((patch or {}).get("size") or 0),
    )
    log.info("Update %s found (delta %s)", new_version,
             f"{update.patch_size / 1048576:.2f} MB" if update.has_patch else "unavailable")
    return update


def _stream(url: str, target: Path, expected: int, progress: Progress | None) -> int:
    """Download ``url`` into ``target``, reporting byte counts as they arrive."""
    with requests.get(url, stream=True, timeout=180) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or expected or 0)
        done = 0
        if progress is not None:
            progress(0, total)
        with target.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                handle.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    return done


def download(update: Update, progress: Progress | None = None) -> Path:
    """Fetch the whole release EXE into the temp folder."""
    target = Path(tempfile.gettempdir()) / f"StealthInterviewCoPilot-{update.version}.exe"
    _stream(update.url, target, update.size, progress)
    return target


def is_frozen() -> bool:
    """True inside the PyInstaller build; a source checkout cannot patch itself."""
    return bool(getattr(sys, "frozen", False))


def build_update(update: Update, progress: Progress | None = None,
                 status: Status | None = None) -> Path:
    """Download the delta patch (or the full EXE) and return the new binary."""
    if update.has_patch and is_frozen():
        if status is not None:
            status(f"Downloading the {update.patch_size / 1048576:.1f} MB update")
        patch_file = Path(tempfile.gettempdir()) / f"copilot-{update.version}.bsdiff"
        _stream(update.patch_url, patch_file, update.patch_size, progress)
        if status is not None:
            status("Applying the update")
        target = Path(tempfile.gettempdir()) / f"StealthInterviewCoPilot-{update.version}.exe"
        current = Path(sys.executable).resolve().read_bytes()
        rebuilt = bsdiff4.patch(current, patch_file.read_bytes())
        target.write_bytes(rebuilt)
        patch_file.unlink(missing_ok=True)
        log.info("Applied delta patch for v%s (%s bytes)", update.version, len(rebuilt))
    else:
        if status is not None:
            status(f"Downloading v{update.version}")
        target = download(update, progress)

    if update.size and target.stat().st_size != update.size:
        target.unlink(missing_ok=True)
        raise RuntimeError("the downloaded update has an unexpected size")
    return target


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
    # Join with "\n" and let write_text add CRLF: pre-joining with "\r\n" mixed with
    # the text-mode translation produced "\r\r\n", which left a stray CR on every
    # line and made the whole script silently fail to replace the EXE.
    script.write_text(
        "\n".join([
            "@echo off",
            "setlocal EnableExtensions",
            f'set "APP={exe}"',
            f'set "NEW={staging}"',
            'set "LOG=%LOCALAPPDATA%\\StealthInterviewCoPilot\\logs\\update.log"',
            'echo [%DATE% %TIME%] waiting for the app to close >> "%LOG%"',
            ":wait",
            f'tasklist /fi "imagename eq {exe.name}" 2>nul | find /i "{exe.name}" >nul',
            "if not errorlevel 1 (",
            "  ping -n 2 127.0.0.1 >nul",
            "  goto wait",
            ")",
            'echo [%DATE% %TIME%] closed, swapping >> "%LOG%"',
            "set /a TRIES=0",
            ":swap",
            "set /a TRIES+=1",
            'if exist "%APP%.old" del /f /q "%APP%.old" >nul 2>&1',
            'move /y "%APP%" "%APP%.old" >nul 2>&1',
            'move /y "%NEW%" "%APP%" >nul 2>&1',
            'if exist "%APP%" goto :startapp',
            "if %TRIES% lss 10 (",
            "  ping -n 2 127.0.0.1 >nul",
            "  goto swap",
            ")",
            'echo [%DATE% %TIME%] swap failed >> "%LOG%"',
            "goto :end",
            ":startapp",
            'echo [%DATE% %TIME%] swapped, starting the new build >> "%LOG%"',
            'start "" "%APP%"',
            ":end",
            'del /f /q "%~f0" >nul 2>&1',
            "",
        ]),
        encoding="utf-8",
        newline="\r\n",
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
