"""Start the local API and phone web app when they are not already running."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen


def _project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parents[1]
    return Path(__file__).resolve().parents[2]


def _available(url: str) -> bool:
    try:
        with urlopen(url, timeout=1.0) as response:
            body = response.read(2048).lower()
            return b"missing required error components" not in body
    except (OSError, URLError):
        return False


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class LocalServices:
    def __init__(self):
        self.backend_server = None
        self.backend_thread = None
        self.web_process = None
        self.web_port = 3000
        self._stopping = threading.Event()

    def start(self, progress=None, backend_url=None, frontend_backend_url=None):
        if self._stopping.is_set():
            raise RuntimeError("Application is closing.")
        if not _available("http://127.0.0.1:8000/health"):
            if progress:
                progress("Starting local backend…")
            self._start_backend()
            self._wait_for("http://127.0.0.1:8000/health", "Local backend did not start.", 25)

        # The network server mode passes its selected backend URL and needs its
        # own Next process so a stale NEXT_PUBLIC_BACKEND_URL cannot be reused.
        healthy_port = None if backend_url else next((port for port in range(3000, 3011) if _available(f"http://127.0.0.1:{port}/desktop-preview")), None)
        if healthy_port is not None:
            self.web_port = healthy_port
            return self.web_port

        free_port = next((port for port in range(3000, 3011) if not _port_open(port)), None)
        if free_port is None:
            raise RuntimeError("No free local port is available for the phone web app (tried 3000–3010).")
        if progress:
            progress(f"Starting phone web app on port {free_port}…")
        self._start_web_app(free_port, frontend_backend_url or backend_url)
        self._wait_for(f"http://127.0.0.1:{free_port}/desktop-preview", "Phone web app did not start.", 75)
        self.web_port = free_port
        return self.web_port

    def _start_backend(self):
        backend_dir = _project_root() / "backend"
        if backend_dir.is_dir():
            os.chdir(backend_dir)
            sys.path.insert(0, str(backend_dir))
        elif getattr(sys, "frozen", False):
            sys.path.insert(0, getattr(sys, "_MEIPASS", ""))
            data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "StealthInterviewCoPilot"
            data_dir.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("DATABASE_URL", f"sqlite:///{data_dir / 'local_copilot.db'}")

        import uvicorn
        from app.main import app
        from app.config import settings

        # The phone connects over the LAN, so never inherit a loopback HOST from .env.
        settings.host = os.environ.get("COPILOT_BIND_HOST", "").strip() or "0.0.0.0"
        # log_config=None keeps uvicorn from reconfiguring logging, which fails inside the frozen EXE.
        config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="warning", access_log=False, log_config=None)
        self.backend_server = uvicorn.Server(config)
        self.backend_thread = threading.Thread(target=self.backend_server.run, name="copilot-backend", daemon=True)
        self.backend_thread.start()

    def _start_web_app(self, port: int, backend_url=None):
        web_dir = _project_root() / "web-app"
        if not (web_dir / "node_modules").is_dir():
            raise RuntimeError("Web app dependencies are missing. Run npm install in the web-app folder.")
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            raise RuntimeError("Node.js is required to start the phone web app.")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = os.environ.copy()
        env["NEXT_DIST_DIR"] = f".next-copilot-{port}"
        if backend_url:
            env["NEXT_PUBLIC_BACKEND_URL"] = backend_url
        self.web_process = subprocess.Popen(
            [npm, "run", "dev", "--", "-p", str(port)],
            cwd=web_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        if self._stopping.is_set():
            self._stop_web_process()

    def _wait_for(self, url: str, message: str, timeout: int):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                raise RuntimeError("Application is closing.")
            if _available(url):
                return
            time.sleep(0.4)
        raise RuntimeError(message)

    def _stop_web_process(self):
        process = self.web_process
        if not process or process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=6,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def stop(self):
        self._stopping.set()
        if self.backend_server:
            self.backend_server.should_exit = True
        if self.backend_thread and self.backend_thread.is_alive():
            self.backend_thread.join(timeout=4)
        self._stop_web_process()

