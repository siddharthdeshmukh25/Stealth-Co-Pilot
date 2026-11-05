"""End-to-end check of the self-update swap, without a real release.

Run from desktop-client/:  python scripts/test_update_swap.py
It fakes sys.frozen / sys.executable so apply_and_restart() runs exactly as it does
inside the packaged EXE, then asserts the staged build replaced the old one.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import updater  # noqa: E402

OLD, NEW = b"OLD BUILD", b"NEW BUILD"


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="copilot-swap-"))
    try:
        appdir = root / "Stealth Co-Pilot"
        appdir.mkdir()
        app = appdir / "StealthInterviewCoPilot.exe"
        app.write_bytes(OLD)
        stage = root / "update"
        stage.mkdir()
        # A leftover from an earlier attempt must not confuse the retry loop.
        (appdir / "StealthInterviewCoPilot.exe.old").write_bytes(b"ANCIENT")

        staged = stage / "StealthInterviewCoPilot-9.9.9.exe"
        staged.write_bytes(NEW)

        updater.update_dir = lambda: stage
        sys.frozen = True
        sys.executable = str(app)

        if not updater.apply_and_restart(staged):
            print("FAIL: apply_and_restart said the update could not be started")
            return 1

        deadline = time.time() + 30
        while time.time() < deadline and app.read_bytes() != NEW:
            time.sleep(0.5)

        # The script deletes its backup and itself a few seconds after the swap.
        cleanup_deadline = time.time() + 20
        while time.time() < cleanup_deadline and (stage / "stealth-copilot-update.bat").exists():
            time.sleep(0.5)

        checks = {
            "app replaced": app.read_bytes() == NEW,
            "staged file consumed": not staged.exists(),
            "old backup removed": not (appdir / "StealthInterviewCoPilot.exe.old").exists(),
            "script cleaned up": not (stage / "stealth-copilot-update.bat").exists(),
        }
        for name, ok in checks.items():
            print(f"{'OK  ' if ok else 'FAIL'} {name}")
        log = stage.parent / "StealthInterviewCoPilot" / "logs" / "update.log"
        if Path.home().joinpath("AppData/Local/StealthInterviewCoPilot/logs/update.log").exists():
            log = Path.home() / "AppData/Local/StealthInterviewCoPilot/logs/update.log"
        if log.exists():
            print("--- update.log tail ---")
            print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]))
        return 0 if all(checks.values()) else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
