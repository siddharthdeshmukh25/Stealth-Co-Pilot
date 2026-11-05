"""Create the delta patch between the previous release EXE and this build.

Runs inside the release workflow after PyInstaller finished. It looks up the
newest published release that still carries ``StealthInterviewCoPilot.exe``,
downloads it, diffs it against ``dist/StealthInterviewCoPilot.exe`` and leaves
``dist/StealthInterviewCoPilot-vOLD-to-vNEW.bsdiff`` for the release step.

The name matters: ``src/updater.py`` asks for exactly that asset when the running
build is ``vOLD``, and silently falls back to the full EXE when it is missing.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import bsdiff4

REPO = os.environ.get("GITHUB_REPOSITORY", "siddharthdeshmukh25/Stealth-Co-Pilot")
ASSET = "StealthInterviewCoPilot.exe"
DIST = Path("dist")


def api(path: str) -> object:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "stealth-copilot-release",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def main() -> int:
    new_exe = DIST / ASSET
    if not new_exe.is_file():
        print(f"{new_exe} is missing; nothing to patch")
        return 0
    tag = (os.environ.get("RELEASE_TAG") or "").strip()
    new_version = tag.lstrip("vV")
    if not new_version:
        print("RELEASE_TAG is not set; skipping the delta patch")
        return 0

    releases = api("/releases?per_page=20")
    previous = None
    for release in releases if isinstance(releases, list) else []:
        if str(release.get("tag_name", "")).lstrip("vV") == new_version:
            continue
        if any(str(asset.get("name")) == ASSET for asset in release.get("assets") or []):
            previous = release
            break
    if previous is None:
        print("No earlier release with an EXE yet; the first build ships the full installer only")
        return 0

    old_version = str(previous["tag_name"]).lstrip("vV")
    asset = next(item for item in previous["assets"] if str(item.get("name")) == ASSET)
    old_exe = DIST / f"previous-{old_version}.exe"
    print(f"Downloading v{old_version} to diff against v{new_version}...")
    urllib.request.urlretrieve(str(asset["browser_download_url"]), old_exe)

    print(f"Diffing {old_exe.stat().st_size / 1048576:.1f} MB -> {new_exe.stat().st_size / 1048576:.1f} MB")
    patch = bsdiff4.diff(old_exe.read_bytes(), new_exe.read_bytes())
    out = DIST / f"StealthInterviewCoPilot-v{old_version}-to-v{new_version}.bsdiff"
    out.write_bytes(patch)
    old_exe.unlink(missing_ok=True)
    print(f"{out.name}: {len(patch) / 1048576:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
