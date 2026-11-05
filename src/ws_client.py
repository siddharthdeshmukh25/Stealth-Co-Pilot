import asyncio
import base64
import json
import logging
import socket
import ssl
import threading
from collections.abc import Callable
from urllib.parse import urlparse

import websockets

log = logging.getLogger(__name__)


def socket_base(backend_url: str) -> str:
    """http(s) backend URL -> ws(s) base URL, whatever scheme case the env var uses."""
    parsed = urlparse(backend_url.strip())
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(parsed.scheme.lower(), "ws")
    if not parsed.netloc:
        # No scheme at all: treat the whole value as host[:port] and stay on ws.
        return f"ws://{backend_url.strip().strip('/')}"
    return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"

def _friendly_error(exc: Exception) -> str:
    """Turn socket/SSL failures into something a user can act on."""
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, socket.gaierror) or "getaddrinfo" in text or "Name or service not known" in text:
        return "the backend address could not be resolved (DNS problem on this network)"
    if isinstance(exc, ssl.SSLError):
        return f"the secure connection was rejected ({text})"
    if isinstance(exc, ConnectionRefusedError) or "Connection refused" in text:
        return "the backend refused the connection"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timed out" in text.lower():
        return "the backend did not answer in time"
    return text


class AudioWebSocketClient:
    """Audio socket that reconnects on its own after a backend restart."""

    def __init__(self, backend_url: str, session_id: str, on_event: Callable[[dict], None] | None = None):
        self.url = f"{socket_base(backend_url)}/ws/{session_id}"
        log.info("Connecting to %s", self.url)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.ws = None
        self.on_event = on_event
        self.connected = threading.Event()
        self._closed = False
        self.last_error: str | None = None
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self._supervise(), self.loop)
        if not self.connected.wait(timeout=30):
            self.close()
            reason = self.last_error or "no response from the caption server"
            raise RuntimeError(f"{reason[0].upper()}{reason[1:]} - caption server {self.url}")

    async def _supervise(self) -> None:
        delay = 0.7
        while not self._closed:
            try:
                self.ws = await websockets.connect(self.url, max_size=16 * 1024 * 1024, ping_interval=20)
                delay = 0.7
                self.connected.set()
                await self._listen()
            except Exception as exc:  # noqa: BLE001 - reconnect on any socket failure
                self.last_error = _friendly_error(exc)
                log.warning("Audio WebSocket dropped: %s (%s)", self.url, self.last_error)
            self.connected.clear()
            if self._closed:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.7, 8.0)

    async def _listen(self) -> None:
        try:
            async for message in self.ws:
                data = json.loads(message)
                if data.get("event") == "error":
                    log.warning("Backend reported: %s", data.get("message", "Unknown error"))
                if self.on_event:
                    try:
                        self.on_event(data)
                    except Exception:
                        log.exception("Desktop event handler failed")
        except Exception:
            log.info("Audio WebSocket listener stopped; reconnecting")

    def send_audio(self, wav_bytes: bytes):
        self._send({"event": "audio_utterance", "audio_base64": base64.b64encode(wav_bytes).decode("ascii")})

    def send_transcript(self, text: str):
        """Caption text produced on this PC — no audio ever leaves the machine."""
        if text and text.strip():
            self._send({"event": "transcript", "text": text.strip()})

    def send_caption_state(self, value: str):
        """Tell the phone whether speaker capture is on, paused or stopped."""
        self._send({"event": "caption_state", "state": value})

    def _send(self, payload: dict):
        if self.ws and self.connected.is_set():
            future = asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(payload)), self.loop)
            future.add_done_callback(self._log_send_failure)

    @staticmethod
    def _log_send_failure(future):
        try:
            future.result()
        except Exception:
            log.debug("Audio packet could not be sent; the next reconnect will pick it up")

    def close(self):
        self._closed = True
        if self.ws:
            try:
                asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop).result(timeout=5)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
