"""Google sign-in (Firebase) for the desktop app.

The EXE checks for a stored session on start: without one, the sign-in screen is
the only way into the wizard. The same Google account is used by the web app
(where the download is unlocked), so one login works on both devices.

Google OAuth cannot run inside Tk, so sign-in happens in the default browser:
the app listens on 127.0.0.1, opens ``<web>/auth/desktop`` and the page posts the
Firebase refresh token back to that loopback port. Only ``identitytoolkit`` REST
calls are used afterwards, so no service credentials ship in the EXE.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import requests

from paths import app_data_dir

FIREBASE_API_KEY = os.environ.get(
    "COPILOT_FIREBASE_API_KEY", "AIzaSyDVWKY0h8eFXuNWnAssorpenLX2UgeQnPM"
)
_REFRESH = "https://securetoken.googleapis.com/v1/token"
_TIMEOUT = 20

# Shown in the browser when the hand-off arrives as a plain navigation.
_DONE_HTML = """<!doctype html><meta charset="utf-8"><title>Stealth Co-Pilot</title>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#fafafa;font:16px/1.6 Segoe UI,system-ui,sans-serif;color:#17181a">
<div style="max-width:340px;padding:28px;border:1px solid #e5e6e8;border-radius:18px;
background:#fff;text-align:center">
<p style="margin:0 0 6px;font-size:20px;font-weight:700">Signed in</p>
<p style="margin:0;color:#5f6468;font-size:13px">You can close this tab — the desktop app is ready.</p>
</div></body>"""

_MESSAGES = {
    "OPERATION_NOT_ALLOWED": (
        "Google sign-in is disabled for this Firebase project: enable the Google provider in the console."
    ),
    "CONFIGURATION_NOT_FOUND": (
        "Firebase Authentication is not set up yet: in the Firebase console open "
        "Authentication → Sign-in method and enable Google."
    ),
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts. Wait a minute and try again.",
    "USER_DISABLED": "This account has been disabled.",
    "API_KEY_INVALID": "This build has an invalid Firebase API key.",
    "INVALID_API_KEY": "This build has an invalid Firebase API key.",
    "TOKEN_EXPIRED": "Your session expired. Sign in again.",
    "INVALID_REFRESH_TOKEN": "Your session expired. Sign in again.",
    "USER_NOT_FOUND": "Your session expired. Sign in again.",
}


class AuthError(Exception):
    """A sign-in/refresh failure with a message fit for the sign-in screen."""


def _friendly(code: str, message: str) -> str:
    if code in _MESSAGES:
        return _MESSAGES[code]
    cleaned = (message or "").replace("_", " ").strip()
    return f"Sign-in failed: {cleaned[:160]}" if cleaned else "Sign-in failed. Try again."


def _post(url: str, payload: dict) -> dict:
    try:
        response = requests.post(url, json=payload, timeout=_TIMEOUT)
    except requests.RequestException:
        raise AuthError("No internet connection. Check your network and try again.") from None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        error = data.get("error") or {}
        code = str(error.get("message") or "").split(" ")[0]
        raise AuthError(_friendly(code, str(error.get("message") or "")))
    if not isinstance(data, dict):
        raise AuthError("The sign-in service returned an unexpected answer.")
    return data


def _account_from(payload: dict, email: str, refresh_token: str = "") -> dict:
    expires_in = int(payload.get("expiresIn") or payload.get("expires_in") or 3600)
    return {
        "email": str(payload.get("email") or email).strip(),
        "local_id": str(payload.get("localId") or payload.get("user_id") or ""),
        "id_token": str(payload.get("idToken") or payload.get("id_token") or ""),
        "refresh_token": str(payload.get("refreshToken") or payload.get("refresh_token") or refresh_token),
        "expires_at": time.time() + max(60, expires_in),
        "signed_in_at": time.time(),
    }


class AccountAuth:
    """Small wrapper around Firebase Auth: sign in, sign up, refresh, storage."""

    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or FIREBASE_API_KEY).strip()

    @property
    def path(self):
        return app_data_dir() / "account.json"

    # -- local session ----------------------------------------------------
    def load(self) -> dict | None:
        if os.environ.get("COPILOT_IGNORE_ACCOUNT"):
            return None
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("refresh_token"):
            return None
        if not data.get("email"):
            return None
        return data

    def save(self, account: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(account), "utf-8")
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    # -- network ----------------------------------------------------------
    def _url(self, endpoint: str) -> str:
        return f"{endpoint}?key={self.api_key}"

    def sign_in_with_google(self, web_base: str, timeout: int = 240) -> dict:
        """Open the browser hand-off page and wait for the refresh token."""
        if not self.api_key:
            raise AuthError("This build has no Firebase API key configured.")
        state = secrets.token_urlsafe(18)
        received: dict = {}
        done = threading.Event()

        class _Handler(BaseHTTPRequestHandler):
            # The hand-off page is a different origin, so allow its POST back here.
            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                # Chrome only lets an https page call loopback when the local
                # server answers the Private Network Access preflight.
                self.send_header("Access-Control-Allow-Private-Network", "true")

            def log_message(self, *_args) -> None:  # keep the console quiet
                return

            def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
                self.send_response(status)
                self._cors()
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _accept(self, payload: dict) -> bool:
                if not isinstance(payload, dict) or payload.get("state") != state or not payload.get("refresh_token"):
                    self._reply(400, b'{"error":"invalid sign-in hand-off"}')
                    return False
                received.update(payload)
                done.set()
                return True

            def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                if self.path.split("?")[0] != "/finish":
                    self._reply(404, b'{"error":"not found"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    payload = {}
                if self._accept(payload):
                    self._reply(200, b'{"ok":true}')

            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                """Fallback for browsers that block the https -> loopback POST."""
                parsed = urlparse(self.path)
                if parsed.path != "/finish":
                    self._reply(404, b'{"error":"not found"}')
                    return
                query = parse_qs(parsed.query)
                payload = {key: query.get(key, [""])[0]
                           for key in ("state", "refresh_token", "email", "name")}
                if self._accept(payload):
                    self._reply(200, _DONE_HTML.encode("utf-8"), "text/html; charset=utf-8")

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        except OSError as exc:
            raise AuthError(f"Could not open the local sign-in port: {exc}") from None
        port = int(server.server_address[1])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            # auto=1 lets the page start Google straight away instead of asking
            # for a second click on "Continue with Google".
            url = f"{web_base.rstrip('/')}/auth/desktop?port={port}&state={quote(state)}&auto=1"
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - user can open the link manually
                pass
            if not done.wait(timeout):
                raise AuthError(
                    "Google sign-in timed out. Try again, and finish signing in in the browser window."
                )
        finally:
            server.shutdown()
            server.server_close()

        account = self.refresh(str(received.get("refresh_token") or ""))
        account["email"] = str(received.get("email") or account.get("email") or "")
        account["name"] = str(received.get("name") or "")
        return account

    def refresh(self, refresh_token: str) -> dict:
        if not self.api_key:
            raise AuthError("This build has no Firebase API key configured.")
        payload = _post(self._url(_REFRESH), {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        return _account_from(payload, "", refresh_token)

