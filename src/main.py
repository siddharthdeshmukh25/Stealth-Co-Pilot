"""Native interview console: setup wizard, live cockpit and Windows WASAPI bridge.

The window is drawn with the small canvas toolkit in ``ui_kit`` so it looks like
a modern app instead of stock Tkinter. All networking/local-service logic lives
here; every widget update from a background thread goes through
``self._ui_queue`` so Tk is only ever touched from the main thread.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
from datetime import datetime
import queue
import socket
import subprocess
import threading
import time
import tkinter as tk
import ctypes
import logging
from tkinter import filedialog, messagebox
from urllib.parse import quote
import webbrowser

import qrcode
import requests
from PIL import Image, ImageDraw, ImageTk

import ui_kit as ui
from account_auth import AccountAuth, AuthError
from audio_loopback import LoopbackCapture
from local_services import LocalServices
from local_stt import LocalTranscriber, local_stt_enabled
from paths import resource_path
from paths import app_data_dir
from tray_icon import TrayController
import updater
from updater import Update, apply_and_restart, build_update, check as check_for_update
from version import APP_VERSION
from ws_client import AudioWebSocketClient
from ui_kit import (Button, Card, Entry, LoadingOverlay, Meter, NavItem, Pill, Progress,
                    TextArea, S, label, show_toast)

log = logging.getLogger(__name__)

PRODUCTION_BACKEND_URL = "https://gtxqfihqhvihdm27ulovdm04.72.61.227.226.sslip.io"
PRODUCTION_WEB_URL = "https://stealth-co-pilot.vercel.app"
# Old public host from before the rename; it 404s, so any leftover env value or
# saved link that still names it is rewritten instead of shown to the user.
RETIRED_WEB_HOSTS = {"stealth-co-pilot-frontend.vercel.app"}


def canonical_web_base(url: str) -> str:
    """Map a retired web host onto the current one so QR/link never point at a dead page."""
    host = url.split("://", 1)[-1].split("/", 1)[0].strip().lower()
    if host in RETIRED_WEB_HOSTS:
        log.info("Rewriting retired web host %s to %s", host, PRODUCTION_WEB_URL)
        return PRODUCTION_WEB_URL
    return url
APP_TITLE = "Stealth Interview Co-Pilot"

STEPS = (
    ("Resume & role", "Your profile"),
    ("Job details", "Role context"),
    ("Review room", "Confirm details"),
    ("Phone pairing", "Go live"),
)

# Not a wizard step: the sidebar entry that opens saved interviews.
HISTORY_STEP = 5
JOB_DESCRIPTION_LIMIT = 500


def _address_preference(address: str) -> tuple[int, str]:
    """Home/office Wi-Fi ranges first; 172.16/12 last because virtual adapters live there."""
    if address.startswith("192.168."):
        return (0, address)
    if address.startswith("10."):
        return (1, address)
    return (2, address)


def lan_addresses() -> list[str]:
    """Every private IPv4 this machine can be reached on, best candidate first."""
    found: set[str] = set()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect picks the outbound interface without sending a packet.
        probe.connect(("8.8.8.8", 80))
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass

    usable: list[str] = []
    for address in found:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_link_local or not parsed.is_private:
            continue
        usable.append(address)
    return sorted(usable, key=_address_preference)


def local_ip_address() -> str:
    addresses = lan_addresses()
    return addresses[0] if addresses else "127.0.0.1"


def inbound_ports_open(ports: tuple[str, ...] = ("8000", "3000")) -> bool | None:
    """Check for a Windows Firewall inbound rule covering the app ports."""
    if os.name != "nt":
        return None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        output = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in"],
            capture_output=True, text=True, timeout=8, creationflags=flags,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not output:
        return None
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if "LocalPort:" not in line:
            continue
        if not any(port in line for port in ports):
            continue
        # Only count allow rules; block rules may sit right above.
        window = "\n".join(lines[max(0, index - 6):index])
        if "Allow" in window or "allow" in line:
            return True
    return False


def response_error(response) -> str:
    """Prefer the backend's human-readable detail over a bare HTTP status."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    return f"HTTP {response.status_code} from the local backend"


def _apply_title_bar(root: tk.Tk, dark: bool) -> None:
    """Match the Windows caption to the app theme when the build supports it."""
    if os.name != "nt":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        enabled = ctypes.c_int(1 if dark else 0)
        # Windows 11 uses 20; older supported builds use attribute 19.
        for attribute in (20, 19):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled))
            if result == 0:
                break
    except (AttributeError, OSError):
        pass


class InterviewSetup:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"{APP_TITLE} · Interview Console")
        self.theme = self._load_theme()
        ui.apply_theme(self.theme)
        root.configure(bg=ui.BG)
        root.geometry(f"{S(1180)}x{S(812)}")
        root.minsize(S(1060), S(730))
        root.protocol("WM_DELETE_WINDOW", self.close_window)
        try:
            root.iconbitmap(str(resource_path("assets/stealth_copilot.ico")))
        except tk.TclError:
            log.exception("Could not load the app window icon")
        _apply_title_bar(root, ui.is_dark())

        self.backend_url = os.environ.get("COPILOT_BACKEND_URL", PRODUCTION_BACKEND_URL).rstrip("/")
        self.web_base_override = canonical_web_base(
            os.environ.get("COPILOT_WEB_URL", PRODUCTION_WEB_URL).strip())
        self.lan_ip = local_ip_address()
        self.web_base = (self.web_base_override or f"http://{self.lan_ip}:3000").rstrip("/")
        self.server_mode_var = tk.StringVar(value="VPS Server")
        self.previous_server_mode = "VPS Server"
        self.extraction_warning = ""
        self.firewall_ok: bool | None = None
        self.resume_path = ""
        self.job_title_value = ""
        self.job_description_value = ""
        self.session_id: str | None = None
        self.pairing_code = ""
        self.mobile_url = ""
        self.qr_image = None
        self.capture: LoopbackCapture | None = None
        self.ws: AudioWebSocketClient | None = None
        self.transcriber: LocalTranscriber | None = None
        self.tray: TrayController | None = None
        self.services = LocalServices()
        self.busy = False
        self._closing = False
        self.step = 1
        self.max_step = 1
        self.caption_count = 0
        self.started_at: float | None = None
        self._answer_chars = 0
        self._answer_question = ""
        self._ui_queue: queue.Queue = queue.Queue()
        self._nav_items: dict[int, NavItem] = {}
        self._loading: LoadingOverlay | None = None
        self._pending_feeds: dict | None = None
        self.history_items: list[dict] = []
        self.history_detail: dict | None = None
        self.history_error = ""
        self.history_loading = False

        # In-app updates: the release the GitHub workflow publishes for this repo.
        self.update: Update | None = None
        self.update_busy = False
        self.update_window: tk.Toplevel | None = None
        self.update_bar: Progress | None = None
        self.update_detail = None
        self.update_count = None
        self.navbar_right: tk.Frame | None = None
        self.update_button: Button | None = None

        # One Firebase account for the EXE and the web app. A stored session skips
        # the sign-in screen; it is re-validated against the auth server below.
        self.auth = AccountAuth()
        self.account: dict | None = self.auth.load()
        self.signin_busy = False

        self._build_shell()
        self._render_step(animate=False)
        self._apply_server_mode(initial=True)
        if self.account:
            self._validate_account_async()
        self._bind_shortcuts()
        self._tick()
        self.root.after(2500, self._check_updates_async)
        self._show_loading("Loading dashboard...")
        self.root.after(900, self._dismiss_loading)
        self.root.after(1100, self._maybe_first_run_setup)

    # ---------------------------------------------------------------- theme
    @staticmethod
    def _settings_path():
        return app_data_dir() / "ui.json"

    def _load_theme(self) -> str:
        override = os.environ.get("COPILOT_THEME", "").strip().lower()
        if override in {"light", "dark"}:
            return override
        try:
            saved = json.loads(self._settings_path().read_text("utf-8"))
        except (OSError, ValueError):
            return "light"
        value = str(saved.get("theme", "light")).lower()
        return "dark" if value == "dark" else "light"

    def _save_theme(self, name: str) -> None:
        # Merge, not replace: ui.json also holds the update folder picked at setup.
        updater.write_settings(theme=name)

    # ------------------------------------------------------- first-run setup
    def _maybe_first_run_setup(self) -> None:
        """Ask once where updates are stored; later launches read ui.json."""
        if str(updater.read_settings().get("update_dir") or "").strip():
            return
        log.info("First run: asking for the update folder")
        self._open_setup_storage()

    def _open_setup_storage(self) -> None:
        """Intro step: choose the drive/folder that holds downloaded updates."""
        window = tk.Toplevel(self.root)
        window.title(APP_TITLE)
        window.configure(bg=ui.BG)
        window.transient(self.root)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", lambda: finish(silent=True))

        body = tk.Frame(window, bg=ui.BG, padx=S(26), pady=S(24))
        body.pack(fill="both", expand=True)
        body.configure(width=S(470))
        label(body, "Welcome to Stealth Co-Pilot", 15, ui.TEXT, "semibold").pack(anchor="w")
        label(body, "Step 1 · Choose where updates are kept", 11, ui.MUTED).pack(anchor="w", pady=(S(3), 0))
        label(body,
              "The app downloads each new version here, swaps itself and reopens on the "
              "new build. Pick a drive with at least 1.5 GB free, and keep this folder.",
              11, ui.MUTED, wraplength=S(410), justify="left").pack(anchor="w", pady=(S(12), 0))

        chosen = tk.StringVar(value=str(updater.update_dir()))
        path_box = tk.Frame(body, bg=ui.PANEL, padx=S(12), pady=S(10))
        path_box.pack(fill="x", pady=(S(14), 0))
        path_label = label(path_box, "", 10, ui.TEXT)
        path_label.pack(anchor="w")
        free_label = label(path_box, "", 10, ui.MUTED)
        free_label.pack(anchor="w", pady=(S(3), 0))

        def free_text(path: str) -> str:
            try:
                free = shutil.disk_usage(path).free / (1024 ** 3)
            except OSError:
                return "This drive is not available right now"
            return f"{free:.1f} GB free on this drive"

        def refresh() -> None:
            path_label.configure(text=chosen.get())
            free_label.configure(text=free_text(chosen.get()))

        def pick() -> None:
            folder = filedialog.askdirectory(parent=window, title="Choose the update folder",
                                             initialdir=chosen.get() or str(app_data_dir()))
            if folder:
                chosen.set(folder)
                refresh()

        def finish(silent: bool = False) -> None:
            updater.write_settings(update_dir=chosen.get())
            log.info("Update folder set to %s", chosen.get())
            if window.winfo_exists():
                window.destroy()
            if not silent:
                show_toast(self.root, "Update folder saved", "success", 2600)

        row = tk.Frame(body, bg=ui.BG)
        row.pack(fill="x", pady=(S(16), 0))
        Button(row, text="Choose folder", command=pick, variant="secondary", height=40,
               size=12).pack(side="left")
        Button(row, text="Continue", command=finish, variant="primary", height=40,
               size=12).pack(side="right")

        refresh()
        window.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - window.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - window.winfo_height()) // 3)
        window.geometry(f"+{x}+{y}")
        window.grab_set()

    def _toggle_theme(self) -> None:
        self._switch_theme("light" if ui.is_dark() else "dark")

    def _switch_theme(self, name: str) -> None:
        """Swap tokens, rebuild the chrome and repaint the current step."""
        feeds = self._capture_feeds() if self.step == 4 else None
        ui.apply_theme(name)
        self.theme = name
        self._save_theme(name)
        _apply_title_bar(self.root, ui.is_dark())
        self.root.configure(bg=ui.BG)
        self._build_shell()
        self._pending_feeds = feeds
        self._render_step(animate=False)

    # ------------------------------------------------------------- loading
    def _show_loading(self, text: str = "Loading dashboard...") -> None:
        if self._loading is not None and self._loading.winfo_exists():
            self._loading.set_text(text)
            return
        self._loading = LoadingOverlay(self.root, text)

    def _dismiss_loading(self) -> None:
        if self._loading is None:
            return
        if self._loading.winfo_exists():
            self._loading.close()
        self._loading = None

    # ------------------------------------------------------------------ ui
    def _build_shell(self):
        """Sidebar + top bar + content: the dashboard skeleton."""
        if getattr(self, "shell", None) is not None and self.shell.winfo_exists():
            self.shell.destroy()
        self._nav_items = {}

        self.shell = tk.Frame(self.root, bg=ui.BG)
        self.shell.pack(fill="both", expand=True)
        self.shell.grid_columnconfigure(0, minsize=S(210), weight=0)
        self.shell.grid_columnconfigure(2, weight=1)
        self.shell.grid_rowconfigure(0, weight=1)

        self.sidebar = tk.Frame(self.shell, bg=ui.BG_SOFT, width=S(210), padx=S(10), pady=S(14))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar_rule = tk.Frame(self.shell, bg=ui.LINE, width=1)
        self.sidebar_rule.grid(row=0, column=1, sticky="ns")

        brand = tk.Frame(self.sidebar, bg=ui.BG_SOFT)
        brand.pack(fill="x", padx=S(6), pady=(S(2), S(22)))
        try:
            icon = Image.open(resource_path("assets/stealth_copilot.png")).convert("RGBA")
            self.brand_icon = ImageTk.PhotoImage(icon.resize((S(28), S(28)), Image.Resampling.LANCZOS))
            tk.Label(brand, image=self.brand_icon, bg=ui.BG_SOFT).pack(side="left", padx=(0, S(10)))
        except (OSError, tk.TclError):
            log.exception("Could not load the app brand icon")
            label(brand, "◉", 18, ui.ACCENT_TEXT).pack(side="left", padx=(0, S(10)))
        label(brand, "Stealth Co-Pilot", 15, ui.TEXT, "semibold").pack(side="left")

        self.ready_rows = {}
        self.history_item = None
        if self.account:
            label(self.sidebar, "SETUP FLOW", 10, ui.MUTED, "medium").pack(anchor="w", padx=S(6), pady=(0, S(6)))
            for index, (title, caption) in enumerate(STEPS, start=1):
                item = NavItem(self.sidebar, index, title, caption, height=44, command=self._nav_clicked)
                item.pack(fill="x", pady=S(1))
                self._nav_items[index] = item

            tk.Frame(self.sidebar, bg=ui.LINE, height=1).pack(fill="x", padx=S(6), pady=S(13))
            label(self.sidebar, "READINESS", 10, ui.MUTED, "medium").pack(anchor="w", padx=S(6), pady=(0, S(5)))
            for key, text in (("resume", "Resume loaded"), ("role", "Role context"), ("room", "Room created")):
                row = tk.Frame(self.sidebar, bg=ui.BG_SOFT)
                row.pack(fill="x", padx=S(6), pady=S(1))
                dot = tk.Canvas(row, width=S(8), height=S(8), bg=ui.BG_SOFT, highlightthickness=0, bd=0)
                dot.pack(side="left", padx=(0, S(8)))
                marker = dot.create_oval(S(1), S(1), S(7), S(7), fill=ui.LINE, outline="")
                label(row, text, 10, ui.MUTED).pack(side="left")
                self.ready_rows[key] = (dot, marker)

            tk.Frame(self.sidebar, bg=ui.LINE, height=1).pack(fill="x", padx=S(6), pady=S(13))
            label(self.sidebar, "RECORDS", 10, ui.MUTED, "medium").pack(anchor="w", padx=S(6), pady=(0, S(5)))
            self.history_item = NavItem(self.sidebar, HISTORY_STEP, "Interview history", "Past questions",
                                        height=44, command=self._nav_clicked, glyph="↺")
            self.history_item.pack(fill="x", pady=S(1))
            self._build_account_box()
        else:
            # Signed out: the setup flow stays locked behind the sign-in screen.
            self._build_account_box(locked=True)

        main = tk.Frame(self.shell, bg=ui.BG)
        main.grid(row=0, column=2, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        self.navbar = tk.Frame(main, bg=ui.BG, height=S(54), padx=S(24))
        self.navbar.grid(row=0, column=0, sticky="ew")
        self.navbar.grid_propagate(False)
        crumb = tk.Frame(self.navbar, bg=ui.BG)
        crumb.place(x=S(24), rely=0.5, anchor="w")
        self.crumb_label = label(crumb, "Setup", 13, ui.TEXT, "semibold")
        self.crumb_label.pack(side="left")

        right = tk.Frame(self.navbar, bg=ui.BG)
        right.place(relx=1.0, x=-S(24), rely=0.5, anchor="e")
        # Sun / moon glyph instead of a Light-Dark word switch.
        self.theme_button = Button(right, text="", icon="☾" if ui.is_dark() else "☀",
                                   command=self._toggle_theme, variant="secondary",
                                   width=S(38), height=30, size=17, radius=9, padx=0)
        self.theme_button.pack(side="right")
        self.navbar_right = right
        self._mount_update_button()
        self.server_pill = Pill(right, "Ready", ui.ACCENT_TEXT, height=24, size=10)
        self.server_pill.pack(side="right", padx=(0, S(8)))

        self.navbar_rule = tk.Frame(main, bg=ui.LINE, height=1)
        self.navbar_rule.grid(row=1, column=0, sticky="ew")

        body = tk.Frame(main, bg=ui.BG, padx=S(26), pady=S(18))
        body.grid(row=2, column=0, sticky="nsew")
        self.main_panel = body

        top = tk.Frame(body, bg=ui.BG)
        top.pack(fill="x")
        self.step_label = label(top, "", 10, ui.ACCENT_TEXT, "semibold")
        self.step_label.pack(side="left")
        self.step_counter = label(top, "", 10, ui.MUTED, mono=True)
        self.step_counter.pack(side="right")

        self.heading = label(body, "", 28, ui.TEXT, "semibold")
        self.heading.pack(anchor="w", pady=(S(7), S(5)))
        self.subheading = label(body, "", 13, ui.TEXT_DIM, wraplength=S(760), justify="left", anchor="w")
        self.subheading.pack(anchor="w", fill="x", pady=(0, S(14)))
        self.progress = Progress(body, height=3)
        self.progress.pack(fill="x", pady=(0, S(16)))

        self.content = tk.Frame(body, bg=ui.BG)
        self.content.pack(fill="both", expand=True)
        body.bind("<Configure>", self._on_panel_resize)
        if not self.account:
            self._apply_auth_layout()

    def _apply_auth_layout(self):
        """Signed out: strip the console chrome so only the sign-in card shows."""
        for widget in (self.sidebar, self.sidebar_rule, self.navbar, self.navbar_rule):
            widget.grid_remove()
        self.shell.grid_columnconfigure(0, minsize=0)
        self.shell.grid_columnconfigure(1, minsize=0)
        for widget in (self.step_label, self.step_counter, self.heading, self.subheading,
                       self.progress):
            widget.pack_forget()

    def _on_panel_resize(self, event):
        self.subheading.configure(wraplength=max(S(360), event.width - S(30)))

    # -------------------------------------------------------------- account
    def _build_account_box(self, locked: bool = False):
        """Sidebar footer: who is signed in, and the way out."""
        box = tk.Frame(self.sidebar, bg=ui.BG_SOFT)
        box.pack(side="bottom", fill="x", padx=S(6), pady=(S(10), S(2)))
        tk.Frame(box, bg=ui.LINE, height=1).pack(fill="x", pady=(0, S(9)))
        if locked:
            label(box, "ACCOUNT", 9, ui.MUTED, "medium").pack(anchor="w")
            label(box, "Sign in to unlock the setup flow", 11, ui.TEXT_DIM,
                  wraplength=S(178), justify="left", anchor="w").pack(anchor="w", pady=(S(3), 0))
            return
        email = str((self.account or {}).get("email") or "")
        label(box, "SIGNED IN", 9, ui.MUTED, "medium").pack(anchor="w")
        label(box, email, 11, ui.TEXT, "semibold", wraplength=S(178), justify="left",
              anchor="w").pack(anchor="w", fill="x", pady=(S(2), S(2)))
        label(box, f"Desktop v{APP_VERSION}", 9, ui.MUTED).pack(anchor="w", pady=(0, S(8)))
        self._button(box, "Sign out", self._sign_out, secondary=True, height=28, size=11).pack(fill="x")

    def _validate_account_async(self):
        """Refresh the saved Firebase session in the background on every launch."""
        token = str((self.account or {}).get("refresh_token") or "")
        if not token:
            return
        threading.Thread(target=self._refresh_account, args=(token,), daemon=True).start()

    def _refresh_account(self, refresh_token: str):
        try:
            account = self.auth.refresh(refresh_token)
        except AuthError as exc:
            self._post(lambda message=str(exc): self._account_invalid(message))
            return
        self._post(lambda: self._account_refreshed(account))

    def _account_refreshed(self, account: dict):
        previous_email = str((self.account or {}).get("email") or "")
        account["email"] = str(account.get("email") or previous_email)
        self.account = account
        self.auth.save(account)
        self._restore_session_async()

    def _account_invalid(self, message: str):
        log.info("Saved account could not be refreshed: %s", message)
        self.auth.clear()
        self.account = None
        self.step, self.max_step = 1, 1
        self._build_shell()
        self._render_step(animate=False)
        show_toast(self.root, message[:120], "warn", 3200)

    def _render_signin(self):
        """Gate: one small card in the middle of the window, nothing else."""
        card = Card(self.content, pad=24, radius=16)
        card.place(relx=0.5, rely=0.5, anchor="center", width=S(354))
        body = card.body

        logo = tk.Frame(body, bg=ui.PANEL)
        logo.pack(anchor="center")
        try:
            image = Image.open(resource_path("assets/stealth_copilot.png")).convert("RGBA")
            self.signin_logo = ImageTk.PhotoImage(image.resize((S(54), S(54)), Image.Resampling.LANCZOS))
            tk.Label(logo, image=self.signin_logo, bg=ui.PANEL, bd=0).pack()
        except (OSError, tk.TclError):
            log.exception("Could not load the sign-in logo")
            label(logo, "◉", 26, ui.ACCENT_TEXT).pack()

        label(body, "Stealth Co-Pilot", 15, ui.TEXT, "semibold").pack(anchor="center", pady=(S(10), 0))
        label(body, "Login / Sign up", 22, ui.TEXT, "semibold").pack(anchor="center", pady=(S(16), 0))
        label(body, "One Google account for this app, your phone and the Windows download.",
              10, ui.MUTED, wraplength=S(280), justify="center").pack(anchor="center", pady=(S(5), 0))

        self.signin_button = Button(body, text="Continue with Google", command=self._start_google_signin,
                                    variant="primary", icon="G", width=S(268), height=44, size=13)
        self.signin_button.pack(anchor="center", pady=(S(18), 0))

        self.signin_hint = label(body, "A browser window opens for Google sign-in, then comes back here.",
                                 10, ui.MUTED, wraplength=S(280), justify="center")
        self.signin_hint.pack(anchor="center", pady=(S(10), 0))

        self.signin_error = label(body, "", 10, ui.DANGER, wraplength=S(280), justify="center")

    def _start_google_signin(self):
        if self.signin_busy:
            return
        self.signin_busy = True
        self.signin_error.configure(text="")
        if self.signin_error.winfo_manager():
            self.signin_error.pack_forget()
        self.signin_hint.configure(text="Waiting for Google… finish signing in in the browser window.")
        self.signin_button.set_text("Waiting for Google…")
        self.signin_button.set_enabled(False)
        threading.Thread(target=self._google_auth_request, daemon=True).start()

    def _google_auth_request(self):
        try:
            account = self.auth.sign_in_with_google(self.web_base)
        except AuthError as exc:
            self._post(lambda message=str(exc): self._signin_failed(message))
            return
        except Exception as exc:  # noqa: BLE001 - never leave the button stuck
            log.exception("Google sign-in failed")
            self._post(lambda message=str(exc): self._signin_failed(message))
            return
        self._post(lambda: self._signed_in(account))

    def _signin_failed(self, message: str):
        self.signin_busy = False
        try:
            self.signin_button.set_text("Continue with Google")
            self.signin_button.set_enabled(True)
            self.signin_hint.configure(text="A browser window opens for Google sign-in, then comes back here.")
            self.signin_error.configure(text=message[:200])
            if not self.signin_error.winfo_manager():
                self.signin_error.pack(anchor="center", pady=(S(8), 0))
        except tk.TclError:
            pass

    def _signed_in(self, account: dict):
        self.signin_busy = False
        self.auth.save(account)
        self.account = account
        try:
            self.root.unbind("<Return>")
        except tk.TclError:
            pass
        self.step, self.max_step = 1, 1
        self._build_shell()
        self._render_step(animate=False)
        show_toast(self.root, f"Signed in as {account.get('email', '')}", "success", 2400)
        self._restore_session_async()

    def _sign_out(self):
        if self.session_id:
            self._stop_audio(keep_status=True)
            self._clear_session_state()
            self.session_id = None
            self.pairing_code = ""
            self.mobile_url = ""
        self.auth.clear()
        self.account = None
        self.step, self.max_step = 1, 1
        self._build_shell()
        self._render_step(animate=False)
        show_toast(self.root, "Signed out", "warn", 2000)

    def _bind_shortcuts(self):
        self.root.bind("<Control-l>", lambda _e: self.copy_link())
        self.root.bind("<Control-p>", lambda _e: self.toggle_capture())
        self.root.bind("<Control-o>", lambda _e: self.open_mobile_link())

    def _apply_server_mode(self, initial=False):
        """Switch the app between the local LAN server and the production VPS."""
        selected = self.server_mode_var.get()
        if not initial and selected == self.previous_server_mode:
            return
        old_services = self.services
        if not initial and self.previous_server_mode == "Network Server":
            threading.Thread(target=old_services.stop, daemon=True).start()
        self.services = LocalServices()
        self.previous_server_mode = selected

        if selected == "VPS Server":
            self.backend_url = os.environ.get("COPILOT_BACKEND_URL", "").strip().rstrip("/") or PRODUCTION_BACKEND_URL
            self.web_base_override = canonical_web_base(
                os.environ.get("COPILOT_WEB_URL", "").strip() or PRODUCTION_WEB_URL)
            self.web_base = self.web_base_override.rstrip("/")
            self.server_pill.set("Ready", ui.ACCENT, pulsing=False)
        else:
            self.backend_url = "http://127.0.0.1:8000"
            self.web_base_override = ""
            self.lan_ip = local_ip_address()
            self.web_base = f"http://{self.lan_ip}:3000"
            self.server_pill.set("Starting", ui.WARN, pulsing=True)
        threading.Thread(target=self._start_services, args=(selected,), daemon=True).start()

    def _start_services(self, selected_mode):
        if selected_mode == "VPS Server":
            # Hosted backend: nothing local to start, and no inbound LAN rule is needed.
            self.firewall_ok = True
            log.info("Using remote backend %s; local services skipped.", self.backend_url)
            self._post(lambda: self._server_mode_ready(selected_mode))
            return
        self.firewall_ok = inbound_ports_open()
        try:
            port = self.services.start(
                backend_url=self.backend_url,
                frontend_backend_url=f"http://{self.lan_ip}:8000",
            )
            self.lan_ip = local_ip_address()
            if not self.web_base_override:
                self.web_base = f"http://{self.lan_ip}:{port}"
            self._post(lambda: self._server_mode_ready(selected_mode))
        except Exception as exc:  # noqa: BLE001 - surfaced in the window
            self._post(lambda error=str(exc): self._show_service_error(error))

    def _server_mode_ready(self, selected_mode):
        if self.server_mode_var.get() != selected_mode:
            return
        self.server_pill.set("Ready", ui.ACCENT, pulsing=False)

    def _show_service_error(self, error):
        self.server_pill.set("Server issue", ui.DANGER, pulsing=False)
        show_toast(self.root, f"Local server could not start: {error[:90]}", "error")

    # --------------------------------------------------------------- steps
    def _update_chrome(self):
        if not self.account:
            # Signed out: the sidebar shows no wizard steps at all.
            if getattr(self, "progress", None) is not None and self.progress.winfo_exists():
                self.progress.set(0)
            return
        in_history = self.step == HISTORY_STEP
        for index, item in self._nav_items.items():
            if index == self.step:
                item.set_state("active")
            elif in_history or index <= self.max_step:
                item.set_state("reached")
            else:
                item.set_state("upcoming")
        if getattr(self, "history_item", None) is not None and self.history_item.winfo_exists():
            self.history_item.set_state("active" if in_history else "upcoming")
        self.progress.set(min(self.step, len(STEPS)) / len(STEPS))
        self.step_counter.configure(text="SAVED INTERVIEWS" if in_history else f"STEP {self.step} OF {len(STEPS)}")
        if getattr(self, "crumb_label", None) is not None and self.crumb_label.winfo_exists():
            self.crumb_label.configure(text="Interview history" if in_history else STEPS[self.step - 1][0])
        checks = {
            "resume": bool(self.resume_path),
            "role": bool(self.job_title_value and self.job_description_value),
            "room": bool(self.session_id),
        }
        for key, done in checks.items():
            dot, marker = self.ready_rows[key]
            dot.itemconfigure(marker, fill=ui.ACCENT if done else ui.LINE)

    def _nav_clicked(self, step: int):
        if not self.account:
            return
        if step == HISTORY_STEP:
            self._open_history()
            return
        if self.session_id or step > self.max_step or step == self.step:
            return
        self._go_to_step(step)

    def _go_to_step(self, step: int):
        """Move between setup steps (and back out of history) keeping entered values."""
        if self.session_id or not 1 <= step <= len(STEPS):
            return
        if self.step == 2 and hasattr(self, "jd_area"):
            self.job_description_value = self.jd_area.get()
        if self.step == 1 and hasattr(self, "title_entry"):
            self.job_title_value = self.title_entry.get()
        self.step = max(1, step)
        self._render_step()

    def _render_step(self, animate: bool = True):
        self._update_chrome()
        for child in self.content.winfo_children():
            child.destroy()
        if not self.account:
            self._render_signin()
            return
        if self.step == 1:
            self.step_label.configure(text="INTERVIEW PROFILE")
            self.heading.configure(text="Set up the interview")
            self.subheading.configure(text="Pick the resume and target role. The AI tailors every answer to this context.")
            self._render_profile()
        elif self.step == 2:
            self.step_label.configure(text="ROLE CONTEXT")
            self.heading.configure(text="Add the job description")
            self.subheading.configure(text="Paste the posting. Keywords here are what the answers lean on.")
            self._render_job_description()
        elif self.step == 3:
            self.step_label.configure(text="READY TO CREATE")
            self.heading.configure(text="Review your interview room")
            self.subheading.configure(text="One click creates the room, a 6-digit pairing code and the phone link.")
            self._render_review()
        elif self.step == HISTORY_STEP:
            self.step_label.configure(text="RECORDS")
            self.heading.configure(text="Interview history")
            self.subheading.configure(text="Every ended interview keeps its transcript, questions and AI answers.")
            self._render_history()
        else:
            self.step_label.configure(text="INTERVIEW ROOM")
            self.heading.configure(text="Room is live")
            self.subheading.configure(text="Pair the phone, start speaker capture, and watch captions and answers here.")
            self._render_cockpit(animate)
        if animate:
            self._slide_in()

    def _slide_in(self):
        """Small eased settle so a step change feels intentional, not abrupt."""
        frames = 6

        def step(frame: int = 0) -> None:
            try:
                if not self.content.winfo_exists():
                    return
            except tk.TclError:
                return
            progress = min(1.0, frame / frames)
            eased = 1 - (1 - progress) ** 3
            self.content.pack_configure(pady=(int(S(9) * (1 - eased)), 0))
            if progress < 1.0:
                self.content.after(14, lambda: step(frame + 1))

        step()

    def _section(self, text: str, pady=(0, 7)):
        lab = label(self.content, text, 10, ui.MUTED, "medium")
        lab.pack(anchor="w", pady=(0, S(pady[1])) if isinstance(pady, tuple) else S(pady))
        return lab

    def _icon_tile(self, parent, glyph: str, color: str | None = None, fill: str | None = None,
                   size: int = 46):
        color = color or ui.ACCENT_TEXT
        fill = fill or ui.PANEL_HIGH
        tile = tk.Canvas(parent, width=S(size), height=S(size), bg=ui.PANEL, highlightthickness=0, bd=0)
        ui.round_rect(tile, 1, 1, S(size) - 1, S(size) - 1, S(12), fill=fill, outline=ui.LINE, width=1)
        tile.create_text(S(size) / 2, S(size) / 2, text=glyph, fill=color, font=ui.font(18))
        return tile

    def _guide_card(self, parent, title: str, rows: tuple[tuple[str, str], ...]):
        """Small numbered explainer that keeps short steps from looking unfinished."""
        card = Card(parent, pad=14)
        card.pack(side="top", fill="x", pady=(S(18), 0))
        label(card.body, title, 10, ui.MUTED, "medium").pack(anchor="w", pady=(0, S(8)))
        for glyph, text in rows:
            row = tk.Frame(card.body, bg=ui.PANEL)
            row.pack(fill="x", pady=S(3))
            badge = tk.Canvas(row, width=S(20), height=S(20), bg=ui.PANEL, highlightthickness=0, bd=0)
            badge.pack(side="left", padx=(0, S(9)))
            ui.round_rect(badge, 0, 0, S(20), S(20), S(6), fill=ui.ACCENT_SOFT, outline=ui.ACCENT_SOFT, width=0)
            badge.create_text(S(10), S(10), text=glyph, fill=ui.ACCENT, font=ui.font(9, "semibold"))
            label(row, text, 12, ui.TEXT_DIM).pack(side="left")
        return card

    # ------------------------------------------------------------- step 1
    def _render_profile(self):
        footer = tk.Frame(self.content, bg=ui.BG)
        footer.pack(side="bottom", fill="x", pady=(S(12), 0))
        self._button(footer, "Continue", self._next_from_profile, primary=True, icon="→", width=S(180)).pack(side="right")

        self._section("RESUME PDF")
        card = Card(self.content, pad=14)
        card.pack(fill="x")
        row = tk.Frame(card.body, bg=ui.PANEL)
        row.pack(fill="x")
        self._icon_tile(row, "▤").pack(side="left", padx=(0, S(14)))
        info = tk.Frame(row, bg=ui.PANEL)
        info.pack(side="left", fill="x", expand=True)
        self.resume_label = label(info, os.path.basename(self.resume_path) if self.resume_path else "Choose a resume PDF", 15, ui.TEXT, "semibold")
        self.resume_label.pack(anchor="w")
        self.resume_hint = label(info, "Ready for this interview" if self.resume_path else "PDF · up to 12 MB", 12, ui.MUTED)
        self.resume_hint.pack(anchor="w", pady=(S(4), 0))
        self._button(row, "Browse", self.choose_resume, secondary=True, icon="", width=S(108)).pack(side="right", padx=(S(12), 0))

        self._section("INTERVIEW / TARGET ROLE", pady=(18, 7))
        self.title_entry = Entry(self.content, placeholder="e.g. Senior Backend Engineer · Razorpay")
        self.title_entry.configure(width=S(400))
        self.title_entry.pack(fill="x")
        if self.job_title_value:
            self.title_entry.set(self.job_title_value)

        self.error_label = label(self.content, "", 12, ui.DANGER, wraplength=S(600), justify="left", anchor="w")
        self.error_label.pack(anchor="w", fill="x", pady=(S(10), 0))

        self._guide_card(self.content, "HOW IT WORKS", (
            ("1", "Upload the resume the answers should come from"),
            ("2", "Paste the role so answers match the posting"),
            ("3", "Pair the phone, then capture the interviewer's audio"),
        ))

    # ------------------------------------------------------------- step 2
    def _render_job_description(self):
        footer = tk.Frame(self.content, bg=ui.BG)
        footer.pack(side="bottom", fill="x", pady=(S(12), 0))
        self._button(footer, "Review room", self._next_from_jd, primary=True, icon="→", width=S(180)).pack(side="right")
        self._button(footer, "← Back", lambda: self._go_to_step(1), secondary=True, width=S(100)).pack(side="left")
        self.counter_label = label(footer, "", 10, ui.MUTED, mono=True)
        self.counter_label.pack(side="left", padx=(S(14), 0), pady=(S(8), 0))

        self._section("JOB DESCRIPTION")
        self.jd_area = TextArea(self.content, height=250, max_length=JOB_DESCRIPTION_LIMIT)
        self.jd_area.pack(fill="both", expand=True)
        self.jd_area.set(self.job_description_value[:JOB_DESCRIPTION_LIMIT])
        # <<Modified>> also covers the text the widget trims at the limit.
        self.jd_area.text.bind("<<Modified>>", lambda _e: self._update_counter(), add="+")
        self._update_counter()

        self.error_label = label(self.content, "", 12, ui.DANGER, wraplength=S(600), justify="left", anchor="w")
        self.error_label.pack(anchor="w", fill="x", pady=(S(10), 0))

    def _update_counter(self):
        text = self.jd_area.get()
        count = len(text)
        if not self.counter_label.winfo_exists():
            return
        limit = JOB_DESCRIPTION_LIMIT
        if count >= limit:
            self.counter_label.configure(text=f"{count} / {limit} · limit reached", fg=ui.DANGER)
        elif count < 20:
            self.counter_label.configure(text=f"{count} / {limit} · 20 minimum", fg=ui.MUTED)
        else:
            self.counter_label.configure(text=f"{count} / {limit} characters · {len(text.split())} words",
                                         fg=ui.ACCENT_TEXT)

    # ------------------------------------------------------------- step 3
    def _render_review(self):
        footer = tk.Frame(self.content, bg=ui.BG)
        footer.pack(side="bottom", fill="x", pady=(S(12), 0))
        self.create_button = self._button(footer, "Create interview room", self.create_session, primary=True, width=S(260))
        self.create_button.pack(side="right")
        self._button(footer, "← Back", lambda: self._go_to_step(2), secondary=True, width=S(100)).pack(side="left")
        label(footer, "Codes expire after 10 minutes.", 10, ui.MUTED).pack(side="left", padx=(S(14), 0), pady=(S(8), 0))

        # Compact KPI strip: the three facts this room is built from.
        kpis = tk.Frame(self.content, bg=ui.BG)
        kpis.pack(fill="x")
        for column in range(3):
            kpis.grid_columnconfigure(column, weight=1, uniform="kpi")
        specs = (
            ("TARGET ROLE", self.job_title_value or "Not set", 1),
            ("RESUME", os.path.basename(self.resume_path) or "Not selected", 1),
            ("ROOM", "Not created yet", 0),
        )
        for column, (caption, value, target) in enumerate(specs):
            stat = Card(kpis, pad=12, radius=12, min_height=76, bg=ui.BG)
            stat.grid(row=0, column=column, sticky="nsew",
                      padx=(0 if column == 0 else S(6), 0 if column == 2 else S(6)))
            head = tk.Frame(stat.body, bg=ui.PANEL)
            head.pack(fill="x")
            label(head, caption, 10, ui.MUTED, "medium").pack(side="left")
            if target:
                self._button(head, "Edit", lambda t=target: self._go_to_step(t), variant="ghost",
                             width=S(48), height=22, size=10).pack(side="right")
            shown = value if len(value) <= 28 else value[:27] + "…"
            label(stat.body, shown, 15, ui.TEXT, "semibold", anchor="w").pack(anchor="w", fill="x", pady=(S(7), 0))

        card = Card(self.content, pad=16)
        card.pack(fill="x", pady=(S(12), 0))
        head = tk.Frame(card.body, bg=ui.PANEL)
        head.pack(fill="x", pady=(0, S(6)))
        label(head, "JOB DESCRIPTION", 10, ui.MUTED, "medium").pack(side="left")
        self._button(head, "Edit", lambda: self._go_to_step(2), variant="ghost",
                     width=S(48), height=22, size=10).pack(side="right")
        preview = self.job_description_value[:400].strip()
        label(card.body, preview + ("…" if len(self.job_description_value) > 400 else ""), 12, ui.TEXT_DIM,
              wraplength=S(590), justify="left", anchor="w").pack(anchor="w", fill="x", pady=(S(4), 0))

        self.error_label = label(self.content, "", 12, ui.DANGER, wraplength=S(600), justify="left", anchor="w")
        self.error_label.pack(anchor="w", fill="x", pady=(S(10), 0))

        self._guide_card(self.content, "AFTER YOU CREATE THE ROOM", (
            ("1", "Scan the QR with your phone — the code expires in 10 minutes"),
            ("2", "Start live captions when the interviewer joins"),
            ("3", "Ask AI from the phone — answers stream to both screens"),
        ))

    # ------------------------------------------------------------- step 4
    def _render_cockpit(self, animate: bool = True):
        grid = tk.Frame(self.content, bg=ui.BG)
        grid.pack(fill="both", expand=True)
        grid.grid_columnconfigure(0, weight=5, uniform="top")
        grid.grid_columnconfigure(1, weight=6, uniform="top")
        grid.grid_rowconfigure(1, weight=1)

        self._render_pairing_card(grid)
        self._render_live_card(grid)
        self._render_feeds(grid)
        self._refresh_cockpit_state()

    def _render_pairing_card(self, parent):
        # Autosized cards: the taller of the two defines the row, the other stretches.
        card = Card(parent, pad=16, min_height=190, stretch=True)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, S(14)), pady=(0, S(14)))
        body = card.body
        label(body, "PAIRING CODE", 10, ui.MUTED, "medium").pack(anchor="w")
        code = self.pairing_code or "000000"

        # Grid here so the QR keeps its width while the code column gives way.
        top = tk.Frame(body, bg=ui.PANEL)
        top.pack(fill="x", pady=(S(5), S(9)))
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, minsize=S(98), weight=0)
        code_box = tk.Frame(top, bg=ui.PANEL)
        code_box.grid(row=0, column=0, sticky="nw")
        label(code_box, f"{code[:3]} {code[3:]}", 30, ui.ACCENT_TEXT, "semibold", mono=True).pack(anchor="w")
        label(code_box, "expires in 10 min", 10, ui.MUTED).pack(anchor="w", pady=(S(2), 0))

        # The PhotoImage reference must live on self, or Tk drops the image.
        self.qr_image = self._make_qr(self.mobile_url)
        if self.qr_image is not None:
            holder = tk.Canvas(top, width=S(96), height=S(96), bg=ui.PANEL, highlightthickness=0, bd=0)
            holder.grid(row=0, column=1, sticky="ne")
            ui.round_rect(holder, 0, 0, S(96), S(96), S(10), fill="#FFFFFF", outline=ui.LINE)
            holder.create_image(S(48), S(48), image=self.qr_image)

        self.link_label = label(body, self.mobile_url or "Link appears once the room is created", 10,
                                ui.INFO, wraplength=S(248), justify="left", anchor="w", cursor="hand2")
        self.link_label.pack(anchor="w", fill="x")
        self.link_label.bind("<Button-1>", lambda _e: self.open_mobile_link())
        buttons = tk.Frame(body, bg=ui.PANEL)
        buttons.pack(fill="x", pady=(S(10), 0))
        self._button(buttons, "Copy link", self.copy_link, secondary=True, height=34).pack(side="left", fill="x", expand=True, padx=(0, S(8)))
        self._button(buttons, "Open", self.open_mobile_link, secondary=True, width=S(84), height=34).pack(side="left")

    def _render_live_card(self, parent):
        card = Card(parent, pad=16, min_height=190, stretch=True)
        card.grid(row=0, column=1, sticky="nsew", pady=(0, S(14)))
        body = card.body
        top = tk.Frame(body, bg=ui.PANEL)
        top.pack(fill="x")
        self.live_pill = Pill(top, "Idle", ui.MUTED, height=26)
        self.live_pill.pack(side="left")
        self.timer_label = label(top, "00:00", 22, ui.TEXT, "semibold", mono=True)
        self.timer_label.pack(side="right")

        self.live_status = label(body, "Speaker capture is off. Start it when the call begins.", 12,
                                 ui.TEXT_DIM, wraplength=S(330), justify="left")
        self.live_status.pack(anchor="w", pady=(S(10), S(6)))

        # Capture control sits right under the status: it is the first action of
        # the room. End Interview is pinned to the card floor, above the meter.
        self.action_button = self._button(body, "Start live captions", self.start_listening, primary=True, height=40)
        self.action_button.pack(fill="x", pady=(S(10), 0))
        self.end_button = self._button(body, "End Interview", self.end_interview, variant="danger", height=36)
        self.end_button.pack(side="bottom", fill="x")
        self.level_meter = Meter(body, height=34)
        self.level_meter.pack(side="bottom", fill="x", pady=(0, S(12)))
        tk.Frame(body, bg=ui.LINE, height=1).pack(side="bottom", fill="x", pady=(0, S(12)))

    def _render_feeds(self, parent):
        feed_card = Card(parent, pad=14, autosize=False, stretch=True, fill=ui.BG_SOFT, outline=ui.LINE_SOFT)
        feed_card.grid(row=1, column=0, sticky="nsew", padx=(0, S(14)))
        head = tk.Frame(feed_card.body, bg=ui.BG_SOFT)
        head.pack(fill="x", pady=(0, S(8)))
        label(head, "LIVE TRANSCRIPT", 10, ui.MUTED, "medium").pack(side="left")
        self.caption_pill = Pill(head, "0 lines", ui.MUTED, height=24, size=11)
        self.caption_pill.pack(side="right")
        self._button(head, "Copy", self.copy_transcript, secondary=True, width=S(64), height=26).pack(side="right", padx=(0, S(8)))
        self.transcript = self._feed_widget(feed_card.body, ui.BG_SOFT)
        self.transcript.pack(fill="both", expand=True)

        answer_card = Card(parent, pad=14, autosize=False, stretch=True, fill=ui.BG_SOFT, outline=ui.LINE_SOFT)
        answer_card.grid(row=1, column=1, sticky="nsew")
        head = tk.Frame(answer_card.body, bg=ui.BG_SOFT)
        head.pack(fill="x", pady=(0, S(8)))
        label(head, "AI ANSWER", 10, ui.MUTED, "medium").pack(side="left")
        self.answer_pill = Pill(head, "Standby", ui.MUTED, height=24, size=11)
        self.answer_pill.pack(side="right")
        self.answer = self._feed_widget(answer_card.body, ui.BG_SOFT)
        self.answer.pack(fill="both", expand=True)
        # A theme rebuild re-creates these widgets from scratch; put the
        # transcript back exactly as it was instead of starting from empty.
        pending, self._pending_feeds = self._pending_feeds, None
        if pending:
            self._restore_feed(self.transcript, pending.get("transcript"))
            self._restore_feed(self.answer, pending.get("answer"))
        else:
            self._answer_placeholder()

    def _feed_widget(self, parent, bg: str) -> tk.Text:
        widget = tk.Text(parent, bg=bg, fg=ui.TEXT, relief="flat", bd=0, highlightthickness=0,
                         wrap="word", font=ui.font(13), padx=S(2), pady=S(2),
                         spacing1=S(4), spacing3=S(8), insertbackground=ui.ACCENT_TEXT,
                         selectbackground=ui.SELECT, selectforeground=ui.SELECT_FG, cursor="arrow")
        widget.tag_configure("time", foreground=ui.MUTED, font=ui.font(10, mono=True))
        widget.tag_configure("body", foreground=ui.TEXT_DIM, font=ui.font(13), lmargin1=S(4), lmargin2=S(4))
        widget.tag_configure("question", foreground=ui.ACCENT_TEXT, font=ui.font(13, "semibold"), lmargin1=S(4), lmargin2=S(4))
        widget.tag_configure("answer", foreground=ui.TEXT, font=ui.font(13), lmargin1=S(2), lmargin2=S(2))
        widget.tag_configure("meta", foreground=ui.MUTED, font=ui.font(11), lmargin1=S(2), lmargin2=S(2))
        widget.tag_configure("placeholder", foreground=ui.MUTED, font=ui.font(12), lmargin1=S(2), lmargin2=S(2))
        widget.configure(state="disabled")
        return widget

    def _make_qr(self, data: str):
        if not data:
            return None
        try:
            code = qrcode.QRCode(border=2, box_size=1)
            code.add_data(data)
            code.make(fit=True)
            matrix = code.get_matrix()
            modules = len(matrix)
            # Integer upscale keeps every module a whole pixel block; a
            # fractional resize makes the QR ragged and hurts scanning.
            factor = max(1, round(S(76) / modules))
            image = Image.new("RGB", (modules * factor, modules * factor), "#FFFFFF")
            draw = ImageDraw.Draw(image)
            for row_index, row in enumerate(matrix):
                for column_index, filled in enumerate(row):
                    if filled:
                        x, y = column_index * factor, row_index * factor
                        draw.rectangle([x, y, x + factor - 1, y + factor - 1], fill="#000000")
            return ImageTk.PhotoImage(image)
        except Exception:  # noqa: BLE001 - a missing QR must never break the room
            log.exception("Could not render the pairing QR code")
            return None

    # --------------------------------------------------------- cockpit state
    def _refresh_cockpit_state(self):
        if not hasattr(self, "action_button") or not self.action_button.winfo_exists():
            return
        self.end_button.set_enabled(bool(self.session_id))
        if self.capture is None:
            self.action_button.set_text("Start live captions")
            self.action_button.set_command(self.start_listening)
            self.action_button.set_enabled(not self.busy)
            self.live_pill.set("Idle", ui.MUTED, pulsing=False)
        elif self.capture.paused:
            self.action_button.set_text("Resume capture")
            self.action_button.set_command(self.toggle_capture)
            self.action_button.set_enabled(True)
            self.live_pill.set("Paused", ui.WARN, pulsing=False)
        else:
            self.action_button.set_text("Pause capture")
            self.action_button.set_command(self.toggle_capture)
            self.action_button.set_enabled(True)
            self.live_pill.set("Live", ui.ACCENT, pulsing=True)
        self.level_meter.active = bool(self.capture and not self.capture.paused)

    def _answer_placeholder(self):
        self._write_feed(self.answer, [("Answers appear here when you or the phone ask the AI.", "placeholder")])

    def _capture_feeds(self) -> dict:
        """Snapshot both feeds (text + tags) so they survive a rebuild."""
        return {
            "transcript": self._dump_feed(self.transcript),
            "answer": self._dump_feed(self.answer),
        }

    @staticmethod
    def _dump_feed(widget: tk.Text) -> list:
        if not widget.winfo_exists():
            return []
        try:
            return list(widget.dump("1.0", "end", text=True))
        except tk.TclError:
            return []

    @staticmethod
    def _restore_feed(widget: tk.Text, dump) -> None:
        if not dump or not widget.winfo_exists():
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        active: list[str] = []
        for key, value, _index in dump:
            if key == "tagon":
                active.append(value)
            elif key == "tagoff":
                if value in active:
                    active.remove(value)
            elif key == "text":
                widget.insert("end", value, tuple(active))
        widget.see("end")
        widget.configure(state="disabled")

    @staticmethod
    def _write_feed(widget: tk.Text, chunks: list[tuple[str, str]], clear: bool = False):
        if not widget.winfo_exists():
            return
        widget.configure(state="normal")
        if clear:
            widget.delete("1.0", "end")
        for text, tag in chunks:
            widget.insert("end", text, tag)
        widget.see("end")
        widget.configure(state="disabled")

    # ------------------------------------------------------------- helpers
    def _button(self, parent, text, command, primary=False, secondary=False, variant=None,
                icon="", width=None, height=40, bg=None, size=13):
        if variant is None:
            variant = "primary" if primary else ("secondary" if secondary else "ghost")
        # bg stays None so the button inherits its parent's colour (rounded corners).
        return Button(parent, text=text, command=command, variant=variant, icon=icon,
                      width=width, height=height, bg=bg, size=size)

    def _post(self, callback):
        """Queue a UI update from any thread; the main loop drains it."""
        try:
            self._ui_queue.put_nowait(callback)
        except queue.Full:
            log.debug("Dropped a UI update: queue full")

    def _drain_queue(self):
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                callback()
            except tk.TclError:
                log.debug("UI update ran after its widget was destroyed")
            except Exception:  # noqa: BLE001 - a bad callback must not kill the loop
                log.exception("UI update failed")

    def _tick(self):
        """One 50 ms heartbeat: UI queue, session timer and the level meter."""
        if self._closing:
            return
        self._drain_queue()
        if self.session_id and self.started_at and hasattr(self, "timer_label") and self.timer_label.winfo_exists():
            elapsed = int(time.monotonic() - self.started_at)
            self.timer_label.configure(text=f"{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}")
        if hasattr(self, "level_meter") and self.level_meter.winfo_exists():
            capture = self.capture
            level = 0.0
            if capture is not None and not capture.paused:
                level = min(1.0, capture.level / 2200.0)
            self.level_meter.set_level(level, active=capture is not None and not capture.paused)
        try:
            self.root.after(50, self._tick)
        except tk.TclError:
            pass

    def _set_error(self, message: str):
        if hasattr(self, "error_label") and self.error_label.winfo_exists():
            self.error_label.configure(text=message, fg=ui.DANGER)

    # ---------------------------------------------------------- form actions
    def choose_resume(self):
        selected = filedialog.askopenfilename(title="Choose your resume",
                                              filetypes=[("PDF documents", "*.pdf")], parent=self.root)
        if not selected:
            return
        self.resume_path = selected
        if hasattr(self, "resume_label") and self.resume_label.winfo_exists():
            self.resume_label.configure(text=os.path.basename(selected), fg=ui.TEXT)
            self.resume_hint.configure(text="Ready for this interview", fg=ui.MUTED)
        self._set_error("")
        self._update_chrome()
        show_toast(self.root, f"Attached {os.path.basename(selected)}", "success", 1600)

    def _next_from_profile(self):
        self.job_title_value = self.title_entry.get() if hasattr(self, "title_entry") else self.job_title_value
        if not self.resume_path:
            self._set_error("Choose your resume PDF to continue.")
            return
        if not self.job_title_value:
            self._set_error("Enter the interview or target job title.")
            return
        self.step = 2
        self.max_step = max(self.max_step, 2)
        self._render_step()

    def _next_from_jd(self):
        self.job_description_value = self.jd_area.get() if hasattr(self, "jd_area") else self.job_description_value
        if len(self.job_description_value) < 20:
            self._set_error("Paste the job description (at least 20 characters).")
            return
        self.step = 3
        self.max_step = max(self.max_step, 3)
        self._render_step()

    # ------------------------------------------------------- room lifecycle
    def create_session(self):
        if self.busy:
            return
        self.busy = True
        self._show_loading("Creating interview room...")
        if hasattr(self, "create_button") and self.create_button.winfo_exists():
            self.create_button.set_enabled(False)
            self.create_button.set_text("Creating room…")
        if hasattr(self, "error_label") and self.error_label.winfo_exists():
            self.error_label.configure(text="Uploading and parsing the resume, then creating the room…", fg=ui.MUTED)
        threading.Thread(target=self._create_session_request, daemon=True).start()

    def _create_session_request(self):
        try:
            health = requests.get(f"{self.backend_url}/health", timeout=8)
            if health.status_code >= 400:
                raise RuntimeError(response_error(health))
            if not health.json().get("ai_configured"):
                raise RuntimeError(
                    "GROQ_API_KEY is missing. Add it to backend\\.env or %LOCALAPPDATA%\\StealthInterviewCoPilot\\.env, then restart."
                )
            with open(self.resume_path, "rb") as resume_file:
                response = requests.post(
                    f"{self.backend_url}/api/resume/upload",
                    files={"file": (os.path.basename(self.resume_path), resume_file, "application/pdf")},
                    timeout=120,
                )
            if response.status_code >= 400:
                raise RuntimeError(response_error(response))
            resume = response.json()
            extraction = resume.get("extraction") or {}
            self.extraction_warning = str(extraction.get("warning") or "")
            response = requests.post(
                f"{self.backend_url}/api/session/create",
                json={"candidate_id": resume["id"], "job_title": self.job_title_value,
                      "job_description": self.job_description_value},
                timeout=25,
            )
            if response.status_code >= 400:
                raise RuntimeError(response_error(response))
            session = response.json()
            self._post(lambda: self._session_created(session))
            if self.extraction_warning:
                log.warning("Resume extraction warning: %s", self.extraction_warning)
        except Exception as exc:  # noqa: BLE001 - surfaced in the window
            self._post(lambda error=str(exc): self._session_failed(error))

    def _session_created(self, session: dict):
        self.busy = False
        self._dismiss_loading()
        self.session_id = session["session_id"]
        self.pairing_code = session["pairing_code"]
        self.mobile_url = f"{self.web_base}/teleprompter/{quote(self.session_id)}"
        self.step = 4
        self.max_step = 4
        self._render_step(animate=False)
        self._save_session_state()
        self.progress.set(1.0)
        show_toast(self.root, "Room created · scan the QR with your phone", "success", 2600)
        if self.extraction_warning:
            show_toast(self.root, f"Resume note: {self.extraction_warning[:80]}", "warn", 3200)

    def _session_failed(self, error: str):
        self.busy = False
        self._dismiss_loading()
        if hasattr(self, "create_button") and self.create_button.winfo_exists():
            self.create_button.set_enabled(True)
            self.create_button.set_text("Create interview room")
        self._set_error(f"Could not create room: {error[:200]}")
        show_toast(self.root, f"Room creation failed: {error[:80]}", "error", 3200)

    def end_interview(self):
        """End the room on every device and keep its data for Interview History."""
        if not self.session_id:
            return
        if not messagebox.askyesno(
            "End Interview",
            "End this interview here and on your phone?\n\n"
            "The transcript and AI answers are saved to Interview History.",
            parent=self.root,
        ):
            return
        session_id, code = self.session_id, self.pairing_code
        self._stop_audio(keep_status=True)
        self._reset_setup()
        show_toast(self.root, "Interview ended · saved to Interview history", "success", 2800)
        if session_id and code:
            threading.Thread(target=self._request_interview_end, args=(session_id, code), daemon=True).start()

    def _reset_setup(self):
        """Clear everything the user filled in, ready for the next interview."""
        self._clear_session_state()
        self.session_id = None
        self.pairing_code = ""
        self.mobile_url = ""
        self.started_at = None
        self.caption_count = 0
        self._answer_chars = 0
        self._answer_question = ""
        self._pending_feeds = None
        self.resume_path = ""
        self.job_title_value = ""
        self.job_description_value = ""
        self.extraction_warning = ""
        self.step = 1
        self.max_step = 1
        self._render_step(animate=False)

    def _request_interview_end(self, session_id: str, code: str):
        """Archive the room server-side so the phone ends with the desktop."""
        try:
            response = requests.post(
                f"{self.backend_url}/api/session/{quote(session_id)}/end",
                json={"pairing_code": code},
                timeout=10,
            )
            if response.status_code >= 400:
                log.warning("Ending the room was rejected by the backend (HTTP %s).", response.status_code)
                self._post(lambda: show_toast(self.root, "Ended here, but the server did not confirm it.", "warn", 3200))
        except requests.RequestException as exc:
            log.warning("Could not reach the backend to end the room: %s", type(exc).__name__)

    # ------------------------------------------------------- live room memory
    @staticmethod
    def _session_state_path():
        return app_data_dir() / "session.json"

    def _save_session_state(self) -> None:
        """Remember the live room so closing the window never ends the interview."""
        if not self.session_id:
            return
        payload = {
            "session_id": self.session_id,
            "pairing_code": self.pairing_code,
            "job_title": self.job_title_value,
            "job_description": self.job_description_value,
            "resume_path": self.resume_path,
            "server_mode": self.server_mode_var.get(),
            "elapsed": int(time.monotonic() - self.started_at) if self.started_at else 0,
            # Rooms belong to the account that created them.
            "account": str((self.account or {}).get("email") or ""),
        }
        try:
            self._session_state_path().write_text(json.dumps(payload), "utf-8")
        except OSError:
            log.debug("Could not persist the live room")

    def _clear_session_state(self) -> None:
        try:
            self._session_state_path().unlink(missing_ok=True)
        except OSError:
            log.debug("Could not clear the saved room")

    def _restore_session_async(self) -> None:
        """Reopen the caption page for a room that is still live on the server."""
        if os.environ.get("COPILOT_IGNORE_SESSION"):
            # Design snapshots need the wizard, not whatever room is live here.
            return
        try:
            saved = json.loads(self._session_state_path().read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not str(saved.get("session_id") or ""):
            return
        owner = str(saved.get("account") or "")
        current = str((self.account or {}).get("email") or "")
        if owner and current and owner.lower() != current.lower():
            # A different account signed in: leave the other user's room alone.
            self._clear_session_state()
            return
        threading.Thread(target=self._restore_session_request, args=(saved,), daemon=True).start()

    def _restore_session_request(self, saved: dict) -> None:
        session_id = str(saved.get("session_id") or "")
        try:
            response = requests.get(f"{self.backend_url}/api/session/{quote(session_id)}", timeout=10)
        except requests.RequestException as exc:
            log.info("Saved room could not be checked yet: %s", type(exc).__name__)
            return
        if response.status_code >= 400:
            self._post(self._clear_session_state)
            return
        try:
            status = str(response.json().get("status") or "")
        except ValueError:
            return
        if status != "active":
            self._post(self._clear_session_state)
            return
        self._post(lambda: self._session_restored(saved))

    def _session_restored(self, saved: dict) -> None:
        """Land straight on the same caption page instead of the setup wizard."""
        if self._closing:
            return
        self.session_id = str(saved.get("session_id") or "")
        self.pairing_code = str(saved.get("pairing_code") or "")
        self.job_title_value = str(saved.get("job_title") or self.job_title_value)
        self.job_description_value = str(saved.get("job_description") or self.job_description_value)
        self.resume_path = str(saved.get("resume_path") or self.resume_path)
        self.mobile_url = f"{self.web_base}/teleprompter/{quote(self.session_id)}"
        elapsed = float(saved.get("elapsed") or 0)
        self.started_at = time.monotonic() - elapsed if elapsed else time.monotonic()
        self.step, self.max_step = 4, 4
        self._render_step(animate=False)
        self._set_live_status("Rejoined your live interview · waiting for the next caption.", ui.ACCENT_TEXT)
        show_toast(self.root, "Rejoined your live interview room", "success", 2600)
        threading.Thread(target=self._reconnect_socket, args=(self.session_id,), daemon=True).start()

    def _reconnect_socket(self, session_id: str) -> None:
        """Listen again for captions and answers without grabbing the microphone."""
        try:
            ws = AudioWebSocketClient(self.backend_url, session_id, on_event=self._on_backend_event)
            self.ws = ws
            # No microphone on a restored room: the phone must say captions are off.
            ws.send_caption_state("off")
        except Exception as exc:  # noqa: BLE001 - surfaced in the status line
            self._post(lambda error=str(exc): self._set_live_status(
                f"Room restored, captions reconnecting: {error[:120]}", ui.WARN))
            return
        self._post(self._refresh_cockpit_state)

    # --------------------------------------------------------------- audio
    def start_listening(self):
        if not self.account or not self.session_id or self.capture or self.busy:
            return
        self.busy = True
        self.action_button.set_enabled(False)
        self.action_button.set_text("Connecting…")
        self._set_live_status("Pairing this computer and opening speaker capture…", ui.WARN)
        threading.Thread(target=self._connect_audio, args=(self.session_id, self.pairing_code), daemon=True).start()

    def _connect_audio(self, session_id: str, pairing_code: str):
        try:
            response = requests.post(f"{self.backend_url}/api/session/pair",
                                     json={"pairing_code": pairing_code}, timeout=15)
            if response.status_code >= 400:
                raise RuntimeError(response_error(response))
            ws = AudioWebSocketClient(self.backend_url, session_id, on_event=self._on_backend_event)
            self.ws = ws
            ws.send_caption_state("on")
            self.transcriber = self._start_local_stt(ws)
            capture = LoopbackCapture(self._on_utterance)
            self.capture = capture
            threading.Thread(target=self._run_capture, daemon=True).start()
            self.tray = TrayController(capture, self._exit_from_tray, on_show=self._show_from_tray)
            self.tray.start()
            self._post(self._audio_started)
        except Exception as exc:  # noqa: BLE001 - surfaced in the window
            self._post(lambda error=str(exc): self._audio_failed(error))

    def _start_local_stt(self, ws: AudioWebSocketClient) -> LocalTranscriber | None:
        """Transcribe on this PC so only text travels to the backend."""
        if not local_stt_enabled():
            log.info("Local transcription disabled; audio will be uploaded for backend transcription")
            return None
        transcriber = LocalTranscriber(
            on_text=ws.send_transcript,
            on_status=lambda message: self._post(lambda: self._set_live_status(message[:170], ui.MUTED)),
            on_error=self._local_stt_failed,
        )
        transcriber.start()
        return transcriber

    def _local_stt_failed(self, message: str):
        log.warning("%s", message)
        self._post(lambda: self._set_live_status(message[:170], ui.WARN))

    def _on_utterance(self, wav_bytes: bytes):
        """Runs on the capture thread: local STT when ready, backend upload otherwise."""
        transcriber = self.transcriber
        if transcriber is not None and not transcriber.failed:
            transcriber.submit(wav_bytes)
        elif self.ws:
            self.ws.send_audio(wav_bytes)

    def _run_capture(self):
        try:
            if self.capture:
                self.capture.run()
        except Exception as exc:  # noqa: BLE001 - surfaced in the window
            self._post(lambda error=str(exc): self._audio_failed(error))

    def _set_live_status(self, message: str, color: str):
        try:
            if hasattr(self, "live_status") and self.live_status.winfo_exists():
                self.live_status.configure(text=message, fg=color)
        except tk.TclError:
            log.debug("Skipped a status update after the cockpit closed")

    def _audio_started(self):
        self.busy = False
        self.started_at = time.monotonic()
        self._set_live_status("Listening to laptop audio · captions stream to your phone and this window.", ui.ACCENT_TEXT)
        self._refresh_cockpit_state()
        show_toast(self.root, "Live captions are running", "success", 2000)

    def _audio_failed(self, error: str):
        """Keep the room alive so the user can retry instead of losing the session."""
        self.busy = False
        self._stop_audio(keep_status=True)
        self._set_live_status(f"Could not start audio: {error[:170]}", ui.DANGER)
        self._refresh_cockpit_state()
        show_toast(self.root, f"Audio failed: {error[:90]}", "error", 3600)

    def _stop_audio(self, keep_status: bool = False):
        capture, transcriber, ws, tray = self.capture, self.transcriber, self.ws, self.tray
        self.capture = None
        self.transcriber = None
        self.ws = None
        self.tray = None
        if capture:
            capture.running = False
        if transcriber:
            transcriber.stop()
        if ws:
            try:
                ws.send_caption_state("off")
            except Exception:  # noqa: BLE001 - a dead socket must not block stopping
                log.debug("Could not report the caption state", exc_info=True)
        if ws:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                log.debug("WebSocket close failed", exc_info=True)
        if tray:
            try:
                tray.stop()
            except Exception:  # noqa: BLE001
                log.debug("Could not stop the tray icon", exc_info=True)
        if not keep_status:
            self._set_live_status("Speaker capture stopped. Start it again whenever you are ready.", ui.TEXT_DIM)
        self._refresh_cockpit_state()

    def toggle_capture(self):
        if not self.capture:
            self.start_listening()
            return
        self.capture.paused = not self.capture.paused
        source_ws = self.ws
        if source_ws:
            try:
                source_ws.send_caption_state("paused" if self.capture.paused else "on")
            except Exception:  # noqa: BLE001 - status reporting is best effort
                log.debug("Could not report the caption state", exc_info=True)
        if self.capture.paused:
            self._set_live_status("Paused · nothing is being transcribed until you resume.", ui.WARN)
        else:
            self._set_live_status("Listening to laptop audio · captions stream to your phone and this window.", ui.ACCENT_TEXT)
        self._refresh_cockpit_state()

    def _exit_from_tray(self):
        """pystray runs on its own thread; hop back to Tk before touching widgets."""
        self._post(self._close_from_tray)

    def _show_from_tray(self):
        self._post(self.show_window)

    def _close_from_tray(self):
        self.shutdown()

    # --------------------------------------------------------------- events
    def _on_backend_event(self, event: dict):
        kind = event.get("event")
        if kind == "caption":
            self._post(lambda: self._on_caption(str(event.get("text") or "")))
        elif kind == "question_detected":
            text = str(event.get("text") or "")
            self._post(lambda: self._on_question(text))
        elif kind == "answer_started":
            text = str(event.get("text") or "")
            self._post(lambda: self._on_answer_started(text))
        elif kind == "answer_token":
            token = str(event.get("token") or "")
            self._post(lambda: self._on_answer_token(token))
        elif kind == "answer_done":
            status = str(event.get("status") or "ok")
            answer = str(event.get("answer") or "")
            self._post(lambda: self._on_answer_done(status, answer))
        elif kind == "answer_cleared":
            self._post(self._on_answer_cleared)
        elif kind == "status":
            state = str(event.get("state") or "")
            self._post(lambda: self._on_status(state))
        elif kind == "snapshot":
            captions = list(event.get("captions") or [])
            answer = str(event.get("answer") or "")
            self._post(lambda: self._on_snapshot(captions, answer))
        elif kind == "error":
            message = str(event.get("message") or "Unknown error")
            self._post(lambda: self._on_backend_error(message))
        elif kind in {"interview_ended", "room_closed"}:
            # The phone can end the interview; both screens must close together.
            self._post(self._on_interview_ended)
        elif kind == "transcription_empty":
            message = str(event.get("message") or "No clear speech was detected.")
            self._post(lambda: self._set_live_status(message[:170], ui.WARN))

    def _on_caption(self, text: str):
        if not text.strip():
            return
        self.caption_count += 1
        self._write_feed(self.transcript, [(time.strftime("%H:%M:%S  "), "time"), (text + "\n", "body")])
        self.caption_pill.set(f"{self.caption_count} line{'s' if self.caption_count != 1 else ''}", ui.ACCENT)
        self._set_live_status("Caption ready · waiting for the next question.", ui.ACCENT_TEXT)

    def _on_question(self, text: str):
        if not text.strip():
            return
        self._write_feed(self.transcript, [(time.strftime("%H:%M:%S  "), "time"), (f"❯ {text}\n", "question")])
        self.transcript.see("end")

    def _on_answer_started(self, question: str):
        self._answer_chars = 0
        self._answer_question = question.strip()
        chunks = [(f"Q  {self._answer_question}\n\n", "question")] if self._answer_question else []
        chunks.append(("…", "meta"))
        self._write_feed(self.answer, chunks, clear=True)
        self.answer_pill.set("Writing", ui.ACCENT, pulsing=True)

    def _on_answer_token(self, token: str):
        if not token:
            return
        if self._answer_chars == 0:
            chunks = [(f"Q  {self._answer_question}\n\n", "question")] if self._answer_question else []
            self._write_feed(self.answer, chunks, clear=True)
        self._answer_chars += len(token)
        self._write_feed(self.answer, [(token, "answer")])
        self.answer_pill.set("Streaming", ui.ACCENT, pulsing=True)

    def _on_answer_done(self, status: str, answer: str):
        if status == "error":
            self.answer_pill.set("Failed", ui.DANGER, pulsing=False)
            self._write_feed(self.answer, [("\nThe AI answer failed. Check the key or connection and try again.\n", "meta")])
            return
        if self._answer_chars == 0 and answer:
            self._write_feed(self.answer, [(answer, "answer")], clear=True)
        if status == "truncated":
            self._write_feed(self.answer, [("\n(answer hit the length limit)", "meta")])
            self.answer_pill.set("Truncated", ui.WARN, pulsing=False)
        else:
            self.answer_pill.set("Ready", ui.ACCENT, pulsing=False)

    def _on_answer_cleared(self):
        self._answer_chars = 0
        self.answer_pill.set("Standby", ui.MUTED, pulsing=False)
        self._write_feed(self.answer, [], clear=True)
        self._answer_placeholder()

    def _on_status(self, state: str):
        messages = {
            "listening": ("Listening for the interviewer", ui.TEXT_DIM),
            "transcribing": ("Transcribing speech on this PC…", ui.WARN),
            "thinking": ("Writing a tailored answer…", ui.ACCENT_TEXT),
            "exe_connected": ("Desktop audio bridge connected", ui.ACCENT_TEXT),
        }
        if state in messages and self.capture is not None:
            text, color = messages[state]
            if state != "exe_connected":
                self._set_live_status(text, color)

    def _on_snapshot(self, captions: list, answer: str):
        if captions and not self.caption_count:
            for text in captions:
                self.caption_count += 1
                self._write_feed(self.transcript, [(time.strftime("%H:%M:%S  "), "time"), (str(text) + "\n", "body")])
            self.caption_pill.set(f"{self.caption_count} lines", ui.ACCENT)
        if answer:
            self._write_feed(self.answer, [(answer, "answer")], clear=True)
            self._answer_chars = len(answer)
            self.answer_pill.set("Ready", ui.ACCENT)

    def _on_backend_error(self, message: str):
        log.warning("Backend reported: %s", message)
        if "unsupported event" in message.lower():
            # An older backend does not know caption_state yet; nothing is broken.
            return
        self._set_live_status(f"Backend: {message[:170]}", ui.DANGER)

    def _on_interview_ended(self):
        """Ended from the phone: stop capture and go back to an empty wizard."""
        if not self.session_id:
            return
        self._stop_audio(keep_status=True)
        self._reset_setup()
        show_toast(self.root, "Interview ended from the web app · saved to history", "success", 3000)

    # -------------------------------------------------------------- history
    def _open_history(self):
        if self.session_id:
            show_toast(self.root, "End the live interview before opening history.", "warn", 2400)
            return
        self.history_detail = None
        self.history_error = ""
        self.step = HISTORY_STEP
        self._render_step()
        if not self.history_items and not self.history_loading:
            self._fetch_history()

    def _fetch_history(self):
        self.history_loading = True
        threading.Thread(target=self._history_request, daemon=True).start()

    def _history_request(self):
        try:
            response = requests.get(f"{self.backend_url}/api/history", timeout=15)
            if response.status_code >= 400:
                raise RuntimeError(response_error(response))
            items = list((response.json() or {}).get("interviews") or [])
            self._post(lambda: self._history_loaded(items))
        except Exception as exc:  # noqa: BLE001 - shown on the page
            self._post(lambda error=str(exc): self._history_failed(error))

    def _history_loaded(self, items: list):
        self.history_loading = False
        self.history_items = items
        self.history_error = ""
        if self.step == HISTORY_STEP:
            self._render_step(animate=False)

    def _history_failed(self, error: str):
        self.history_loading = False
        self.history_error = error
        if self.step == HISTORY_STEP:
            self._render_step(animate=False)

    def _open_history_item(self, session_id: str):
        self.history_detail = None
        self.history_error = ""
        self.history_loading = True
        self._render_step(animate=False)
        threading.Thread(target=self._history_detail_request, args=(session_id,), daemon=True).start()

    def _history_detail_request(self, session_id: str):
        try:
            response = requests.get(f"{self.backend_url}/api/history/{quote(session_id)}", timeout=15)
            if response.status_code >= 400:
                raise RuntimeError(response_error(response))
            self._post(lambda: self._history_detail_loaded(response.json()))
        except Exception as exc:  # noqa: BLE001 - shown on the page
            self._post(lambda error=str(exc): self._history_failed(error))

    def _history_detail_loaded(self, data: dict):
        self.history_loading = False
        self.history_detail = data
        self.history_error = ""
        if self.step == HISTORY_STEP:
            self._render_step(animate=False)

    @staticmethod
    def _format_when(value: str) -> str:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return ""
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%d %b %Y · %H:%M")

    @staticmethod
    def _format_clock(value: str) -> str:
        """Caption stamps are seconds apart, so keep the seconds."""
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return "--:--:--"
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%H:%M:%S")

    @staticmethod
    def _format_duration(seconds) -> str:
        try:
            total = int(seconds or 0)
        except (TypeError, ValueError):
            return "—"
        if total <= 0:
            return "—"
        hours, rest = divmod(total, 3600)
        minutes, secs = divmod(rest, 60)
        if hours:
            return f"{hours}h {minutes:02d}m"
        if minutes:
            return f"{minutes} min"
        return f"{secs}s"

    def _render_history(self):
        if self.history_detail:
            self._render_history_detail(self.history_detail)
            return

        footer = tk.Frame(self.content, bg=ui.BG)
        footer.pack(side="bottom", fill="x", pady=(S(12), 0))

        def refresh():
            self._fetch_history()
            self._render_step(animate=False)

        self._button(footer, "Refresh", refresh, secondary=True, width=S(110)).pack(side="right")

        if self.history_error:
            card = Card(self.content, pad=16)
            card.pack(fill="x")
            label(card.body, "Could not load your interviews", 15, ui.TEXT, "semibold").pack(anchor="w")
            label(card.body, self.history_error[:220], 12, ui.DANGER, wraplength=S(620),
                  justify="left", anchor="w").pack(anchor="w", pady=(S(6), 0))
            return

        if self.history_loading and not self.history_items:
            card = Card(self.content, pad=16)
            card.pack(fill="x")
            label(card.body, "Loading saved interviews…", 13, ui.MUTED).pack(anchor="w")
            return

        if not self.history_items:
            card = Card(self.content, pad=18)
            card.pack(fill="x")
            label(card.body, "No saved interviews yet", 17, ui.TEXT, "semibold").pack(anchor="w")
            label(card.body, "End an interview and its questions, answers and transcript show up here.",
                  12, ui.TEXT_DIM, wraplength=S(600), justify="left", anchor="w").pack(anchor="w", pady=(S(6), 0))
            return

        card = Card(self.content, pad=8, autosize=False, stretch=True)
        card.pack(fill="both", expand=True)
        feed = self._feed_widget(card.body, ui.PANEL)
        feed.pack(fill="both", expand=True)
        feed.configure(cursor="hand2", padx=S(10), pady=S(8))
        feed.configure(state="normal")
        feed.delete("1.0", "end")
        for index, item in enumerate(self.history_items):
            title_tag, meta_tag = f"title{index}", f"meta{index}"
            feed.tag_configure(title_tag, foreground=ui.TEXT, font=ui.font(14, "semibold"),
                               spacing1=S(6), lmargin1=S(6), lmargin2=S(6))
            feed.tag_configure(meta_tag, foreground=ui.MUTED, font=ui.font(11),
                               spacing3=S(8), lmargin1=S(6), lmargin2=S(6))
            session_id = str(item.get("session_id") or "")
            for tag in (title_tag, meta_tag):
                feed.tag_bind(tag, "<Button-1>", lambda _e, sid=session_id: self._open_history_item(sid))
                feed.tag_bind(tag, "<Enter>", lambda _e, tags=(title_tag, meta_tag):
                              [feed.tag_configure(t, background=ui.PANEL_HIGH) for t in tags])
                feed.tag_bind(tag, "<Leave>", lambda _e, tags=(title_tag, meta_tag):
                              [feed.tag_configure(t, background=ui.PANEL) for t in tags])
            questions = int(item.get("questions") or 0)
            captions = int(item.get("captions") or 0)
            meta = " · ".join(part for part in (
                self._format_when(item.get("ended_at") or item.get("created_at")),
                self._format_duration(item.get("duration_seconds")),
                f"{questions} question{'s' if questions != 1 else ''}",
                f"{captions} caption{'s' if captions != 1 else ''}",
            ) if part and part != "—")
            feed.insert("end", f"{item.get('job_title') or 'Interview'}\n", (title_tag,))
            feed.insert("end", f"{meta}\n", (meta_tag,))
        feed.configure(state="disabled")

    def _render_history_detail(self, data: dict):
        footer = tk.Frame(self.content, bg=ui.BG)
        footer.pack(side="bottom", fill="x", pady=(S(12), 0))
        self._button(footer, "← All interviews", self._back_to_history, secondary=True,
                     width=S(160)).pack(side="left")

        head = Card(self.content, pad=16)
        head.pack(fill="x")
        label(head.body, str(data.get("job_title") or "Interview"), 17, ui.TEXT, "semibold").pack(anchor="w")
        answers = data.get("answers") or []
        transcript = data.get("transcript") or []
        meta = " · ".join(part for part in (
            self._format_when(data.get("ended_at") or data.get("created_at")),
            self._format_duration(data.get("duration_seconds")),
            f"{len(answers)} question{'s' if len(answers) != 1 else ''}",
            f"{len(transcript)} caption{'s' if len(transcript) != 1 else ''}",
        ) if part and part != "—")
        label(head.body, meta, 11, ui.MUTED).pack(anchor="w", pady=(S(4), 0))

        body = Card(self.content, pad=12, autosize=False, stretch=True, fill=ui.BG_SOFT, outline=ui.LINE_SOFT)
        body.pack(fill="both", expand=True, pady=(S(12), 0))
        feed = self._feed_widget(body.body, ui.BG_SOFT)
        feed.pack(fill="both", expand=True)
        chunks: list[tuple[str, str]] = [("QUESTIONS & ANSWERS\n", "meta")]
        if answers:
            for index, entry in enumerate(answers, start=1):
                chunks.append((f"Q{index}  {str(entry.get('question') or '').strip()}\n", "question"))
                chunks.append(((str(entry.get("answer") or "").strip() or "—") + "\n\n", "answer"))
        else:
            chunks.append(("No AI answers were captured in this interview.\n\n", "placeholder"))
        chunks.append(("TRANSCRIPT\n", "meta"))
        if transcript:
            for segment in transcript:
                chunks.append((f"{self._format_clock(segment.get('timestamp'))}  ", "time"))
                chunks.append((str(segment.get("text") or "").strip() + "\n", "body"))
        else:
            chunks.append(("No captions were captured in this interview.\n", "placeholder"))
        self._write_feed(feed, chunks)
        # _write_feed parks the view at the newest line; history reads top-down.
        feed.see("1.0")

    def _back_to_history(self):
        self.history_detail = None
        self.history_error = ""
        self._render_step(animate=False)

    # -------------------------------------------------------------- actions
    def copy_link(self):
        if not self.mobile_url:
            show_toast(self.root, "Create the room first — there is no link yet.", "warn", 2000)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.mobile_url)
        show_toast(self.root, "Phone link copied", "success", 1600)

    def copy_transcript(self):
        if not hasattr(self, "transcript") or not self.transcript.winfo_exists():
            return
        text = self.transcript.get("1.0", "end").strip()
        if not text:
            show_toast(self.root, "Nothing to copy yet.", "warn", 1600)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        show_toast(self.root, "Transcript copied", "success", 1600)

    def open_mobile_link(self):
        if self.mobile_url:
            webbrowser.open(self.mobile_url)
        else:
            show_toast(self.root, "Create the room first — there is no link yet.", "warn", 2000)

    def hide_to_tray(self):
        if not self.tray:
            self.shutdown()
            return
        show_toast(self.root, "Still capturing in the tray · click the tray icon to reopen.", "info", 2200)
        if self.session_id:
            self._save_session_state()
        self.root.after(700, self.root.withdraw)

    def show_window(self):
        """Tray can bring the same window (and room) back."""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            log.debug("Window could not be restored")

    # ------------------------------------------------------------- updates
    def _mount_update_button(self) -> None:
        """Offer the new build in the top bar as soon as the check finds one."""
        frame = self.navbar_right
        if self.update is None or frame is None or not frame.winfo_exists():
            return
        if self.update_button is not None and self.update_button.winfo_exists():
            return
        self.update_button = Button(frame, text=f"Update v{self.update.version}", icon="⇪",
                                    command=self._install_update, variant="secondary",
                                    height=30, size=11, radius=9)
        self.update_button.pack(side="right", padx=(0, S(8)))

    def _check_updates_async(self) -> None:
        """Ask GitHub for the latest release without blocking the UI."""
        threading.Thread(target=self._check_updates, name="copilot-update-check", daemon=True).start()

    def _check_updates(self) -> None:
        try:
            found = check_for_update()
        except Exception:  # noqa: BLE001 - an update check must never crash the app
            log.debug("Update check failed", exc_info=True)
            return
        if found is not None:
            self._post(lambda: self._update_found(found))

    def _update_found(self, update: Update) -> None:
        self.update = update
        self._mount_update_button()
        log.info("Update v%s is available (running v%s)", update.version, APP_VERSION)
        show_toast(self.root, f"Update v{update.version} is ready - install it from the top bar", "info", 4200)

    def _install_update(self) -> None:
        if self.update is None or self.update_busy:
            return
        if self.session_id:
            messagebox.showinfo(APP_TITLE, "Finish the live room before installing the update.",
                                parent=self.root)
            return
        update = self.update
        size_mb = (update.patch_size if update.has_patch else update.size) / 1048576
        kind = "only the changed data" if update.has_patch else "the full installer"
        confirmed = messagebox.askyesno(
            APP_TITLE,
            f"Install v{update.version} now?\n\nDownload: {size_mb:.1f} MB ({kind}).\n"
            "The app restarts itself when the update is ready.",
            parent=self.root)
        if not confirmed:
            return
        self.update_busy = True
        self._open_update_dialog()
        threading.Thread(target=self._download_update, name="copilot-update-download", daemon=True).start()

    def _open_update_dialog(self) -> None:
        """Small modal window with a live progress bar and byte counts."""
        update = self.update
        if update is None:
            return
        window = tk.Toplevel(self.root)
        window.title(APP_TITLE)
        window.configure(bg=ui.BG)
        window.transient(self.root)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", lambda: None)
        body = tk.Frame(window, bg=ui.BG, padx=S(24), pady=S(22))
        body.pack(fill="both", expand=True)
        label(body, f"Updating to v{update.version}", 14, ui.TEXT, "semibold").pack(anchor="w")
        detail = label(body, "Preparing the download...", 11, ui.MUTED)
        detail.pack(anchor="w", pady=(S(4), 0))
        bar = Progress(body, height=6, bg=ui.BG)
        bar.pack(fill="x", pady=(S(14), 0))
        bar.configure(width=S(360))
        count = label(body, "", 10, ui.MUTED)
        count.pack(anchor="w", pady=(S(8), 0))
        self.update_window = window
        self.update_bar = bar
        self.update_detail = detail
        self.update_count = count
        window.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - window.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - window.winfo_height()) // 3)
        window.geometry(f"+{x}+{y}")

    def _update_progress(self, done: int, total: int) -> None:
        bar = self.update_bar
        if bar is None or not bar.winfo_exists():
            return
        fraction = (done / total) if total else 0.0
        bar.set(min(1.0, fraction), animate=False)
        if self.update_count is not None and self.update_count.winfo_exists():
            done_mb = done / 1048576
            if total:
                self.update_count.configure(
                    text=f"{done_mb:.1f} MB / {total / 1048576:.1f} MB  -  {int(fraction * 100)}%")
            else:
                self.update_count.configure(text=f"{done_mb:.1f} MB")

    def _update_status(self, message: str) -> None:
        if self.update_detail is not None and self.update_detail.winfo_exists():
            self.update_detail.configure(text=message)

    def _close_update_dialog(self) -> None:
        window, self.update_window = self.update_window, None
        self.update_bar = self.update_detail = self.update_count = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def _update_failed(self, message: str) -> None:
        self.update_busy = False
        self._close_update_dialog()
        show_toast(self.root, f"Update failed: {message[:120]}", "warn", 6000)

    def _download_update(self) -> None:
        update = self.update
        if update is None:
            return
        try:
            path = build_update(
                update,
                progress=lambda done, total: self._post(lambda: self._update_progress(done, total)),
                status=lambda message: self._post(lambda: self._update_status(message)),
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            log.exception("Update download failed")
            message = str(exc)
            self._post(lambda: self._update_failed(message))
            return
        self._post(lambda: self._finish_update(path))

    def _finish_update(self, path) -> None:
        self.update_busy = False
        if not apply_and_restart(path):
            self._close_update_dialog()
            show_toast(self.root, "This build cannot update itself - grab the new EXE from the website.",
                       "warn", 5200)
            return
        self._update_status("Installing - the app restarts in a moment.")
        if self.update_bar is not None and self.update_bar.winfo_exists():
            self.update_bar.set(1.0, animate=False)
        if self.update_count is not None and self.update_count.winfo_exists():
            self.update_count.configure(text="Download complete")
        self.root.after(600, self._quit_for_update)

    def _quit_for_update(self) -> None:
        """Exit hard so the swap script can replace the EXE straight away.

        A normal shutdown stops the local services first; when that block stalled the
        window stayed open, the update script kept waiting for the process and the
        user was left on the old version. Nothing here needs a graceful teardown.
        """
        self._closing = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        try:
            logging.shutdown()
        except Exception:  # noqa: BLE001 - exiting must never be blocked
            pass
        os._exit(0)

    # ------------------------------------------------------------ shutdown
    def close_window(self):
        """Closing the window must never end the interview."""
        if self.session_id and self.tray is not None:
            # Audio is still running: keep the room alive in the tray.
            self.hide_to_tray()
            return
        self.shutdown()

    def shutdown(self):
        if self._closing:
            return
        if threading.current_thread() is not threading.main_thread():
            self._post(self.shutdown)
            return
        self._closing = True
        # Keep the room open on the server and remember it for the next launch.
        self._save_session_state()
        self._stop_audio(keep_status=True)
        self.session_id = None
        self.pairing_code = ""
        try:
            self.services.stop()
        except Exception:  # noqa: BLE001 - never block window close
            log.debug("Local services did not stop cleanly", exc_info=True)
        try:
            self.root.destroy()
        except tk.TclError:
            pass


if __name__ == "__main__":
    from app_logging import configure_logging

    ui.set_scale(ui.enable_dpi_awareness())
    configure_logging()
    root = tk.Tk()
    InterviewSetup(root)
    root.mainloop()
