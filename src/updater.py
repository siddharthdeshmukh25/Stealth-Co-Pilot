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

import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import bsdiff4
import requests

from paths import app_data_dir
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


def settings_path() -> Path:
    return app_data_dir() / "ui.json"


def read_settings() -> dict:
    """Shared ui.json (theme, update folder); a broken file must never block startup."""
    try:
        data = json.loads(settings_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(**changes) -> dict:
    """Merge keys into ui.json instead of replacing it, so one feature cannot wipe another."""
    data = read_settings()
    data.update(changes)
    try:
        settings_path().write_text(json.dumps(data), "utf-8")
    except OSError:
        log.debug("Could not persist the app settings")
    return data


def update_dir() -> Path:
    """Folder the chosen at setup time; patches, the staged EXE and the swap script live here."""
    raw = str(read_settings().get("update_dir") or "").strip()
    folder = Path(raw) if raw else app_data_dir() / "updates"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    except OSError:
        log.warning("Update folder %s is not writable; falling back to the temp folder", folder)
        return Path(tempfile.gettempdir())


def download(update: Update, progress: Progress | None = None) -> Path:
    """Fetch the whole release EXE into the update folder."""
    target = update_dir() / f"StealthInterviewCoPilot-{update.version}.exe"
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
        patch_file = update_dir() / f"copilot-{update.version}.bsdiff"
        _stream(update.patch_url, patch_file, update.patch_size, progress)
        if status is not None:
            status("Applying the update")
        target = update_dir() / f"StealthInterviewCoPilot-{update.version}.exe"
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

    script = update_dir() / "stealth-copilot-update.bat"
    # Join with "\n" and let write_text add CRLF: pre-joining with "\r\n" mixed with
    # the text-mode translation produced "\r\r\n", which left a stray CR on every
    # line and made the whole script silently fail to replace the EXE.
    #
    # Success is judged by the staged file disappearing, never by %APP% existing:
    # the old check was true even when the first move had failed because the EXE was
    # still locked, so the script "finished" without swapping anything and simply
    # relaunched the old version.
    script.write_text(
        "\n".join([
            "@echo off",
            "setlocal EnableExtensions DisableDelayedExpansion",
            f'set "APP={exe}"',
            f'set "NEW={staging}"',
            f'set "APPDIR={exe.parent}"',
            'set "LOG=%LOCALAPPDATA%\\StealthInterviewCoPilot\\logs\\update.log"',
            'echo [%DATE% %TIME%] waiting for the app to close >> "%LOG%"',
            "set /a WAIT=0",
            ":wait",
            f'tasklist /fi "imagename eq {exe.name}" 2>nul | find /i "{exe.name}" >nul',
            "if errorlevel 1 goto closed",
            "set /a WAIT+=1",
            "if %WAIT% gtr 90 goto stillrunning",
            "ping -n 2 127.0.0.1 >nul",
            "goto wait",
            ":stillrunning",
            'echo [%DATE% %TIME%] app never exited, giving up >> "%LOG%"',
            "goto end",
            ":closed",
            'echo [%DATE% %TIME%] closed, swapping >> "%LOG%"',
            "set /a TRIES=0",
            ":swap",
            "set /a TRIES+=1",
            'if exist "%APP%.old" del /f /q "%APP%.old" >nul 2>&1',
            'if not exist "%NEW%" goto nosource',
            'move /y "%APP%" "%APP%.old" >nul 2>&1',
            'move /y "%NEW%" "%APP%" >nul 2>&1',
            'if exist "%NEW%" goto retry',
            'if not exist "%APP%" goto retry',
            "goto startapp",
            ":retry",
            "if %TRIES% lss 15 (",
            "  ping -n 2 127.0.0.1 >nul",
            "  goto swap",
            ")",
            'echo [%DATE% %TIME%] swap failed after %TRIES% tries >> "%LOG%"',
            'if not exist "%APP%" if exist "%APP%.old" move /y "%APP%.old" "%APP%" >nul 2>&1',
            "goto end",
            ":nosource",
            'echo [%DATE% %TIME%] staged update missing: %NEW% >> "%LOG%"',
            "goto end",
            ":startapp",
            'echo [%DATE% %TIME%] swapped, starting the new build >> "%LOG%"',
            'start "" /d "%APPDIR%" "%APP%"',
            "ping -n 4 127.0.0.1 >nul",
            'del /f /q "%APP%.old" >nul 2>&1',
            ":end",
            'del /f /q "%~f0" >nul 2>&1',
            "",
        ]),
        encoding="utf-8",
        newline="\r\n",
    )

    # CREATE_NO_WINDOW: DETACHED_PROCESS still let cmd.exe put a console on screen,
    # which is the pair of black terminal windows users saw next to the update.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True,
                         cwd=str(exe.parent), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        log.exception("Could not start the update script")
        return False
    log.info("Update %s staged; restarting through %s", staging.name, script.name)
    return True
