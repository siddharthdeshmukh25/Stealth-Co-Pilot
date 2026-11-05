"""Offline speech-to-text that runs inside the desktop app.

The EXE transcribes each captured utterance on this PC with faster-whisper and
sends only the finished text to the backend. Groq is still used by the backend,
but only for AI answers — a slow link or a Groq hiccup no longer delays captions.

Set ``COPILOT_LOCAL_STT=0`` to go back to the old behaviour (upload audio and let
the backend transcribe it), or ``COPILOT_STT_MODEL=small.en`` for better accuracy
at roughly twice the CPU cost.
"""
from __future__ import annotations

import io
import logging
import os
import queue
import threading
import wave
from collections.abc import Callable

import numpy as np

log = logging.getLogger(__name__)

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")

DEFAULT_MODEL = os.environ.get("COPILOT_STT_MODEL", "base.en")
DEVICE = os.environ.get("COPILOT_STT_DEVICE", "cpu")
COMPUTE_TYPE = os.environ.get("COPILOT_STT_COMPUTE", "int8")
TARGET_RATE = 16000

# Whisper invents short filler lines on quiet audio; drop the usual suspects.
SILENCE_HALLUCINATIONS = {
    "you", "thank you.", "thank you", "thanks for watching.", "thanks for watching",
    "bye.", "bye", "okay.", "okay", ".", "...",
}


def local_stt_enabled() -> bool:
    return os.environ.get("COPILOT_LOCAL_STT", "1").strip().lower() not in {"0", "false", "off", "no"}


def _cpu_threads() -> int:
    try:
        requested = int(os.environ.get("COPILOT_STT_THREADS", "0"))
    except ValueError:
        requested = 0
    if requested > 0:
        return requested
    return max(2, min(8, (os.cpu_count() or 4) // 2 or 2))


def _wav_to_float32(wav_bytes: bytes) -> tuple[np.ndarray, float]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as recording:
        channels = recording.getnchannels()
        rate = recording.getframerate()
        frames = recording.readframes(recording.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    audio = samples.astype(np.float32) / 32768.0
    if rate != TARGET_RATE and audio.size:
        # The capture stage already emits 16 kHz mono; this only guards odd devices.
        duration = audio.size / float(rate)
        target_size = max(1, int(duration * TARGET_RATE))
        audio = np.interp(
            np.linspace(0.0, duration, target_size, endpoint=False),
            np.linspace(0.0, duration, audio.size, endpoint=False),
            audio,
        ).astype(np.float32)
    return audio, audio.size / float(TARGET_RATE)


class LocalTranscriber:
    """Loads the model once in the background and transcribes utterances in order."""

    def __init__(
        self,
        on_text: Callable[[str], None],
        on_status: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        model_name: str | None = None,
    ):
        self.on_text = on_text
        self.on_status = on_status or (lambda _message: None)
        self.on_error = on_error or (lambda _message: None)
        self.model_name = model_name or DEFAULT_MODEL
        self.model = None
        self.failed = False
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=4)
        self._stopped = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="local-stt", daemon=True)
        self._thread.start()

    def submit(self, wav_bytes: bytes) -> None:
        if self._stopped or self.failed or not wav_bytes:
            return
        while self._queue.full():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._queue.put_nowait(wav_bytes)

    def stop(self) -> None:
        self._stopped = True

    def _load_model(self):
        from faster_whisper import WhisperModel

        from paths import app_data_dir

        model_dir = app_data_dir() / "models"
        model_dir.mkdir(parents=True, exist_ok=True)
        self.on_status(f"Loading local speech model ({self.model_name})...")
        return WhisperModel(
            self.model_name,
            device=DEVICE,
            compute_type=COMPUTE_TYPE,
            download_root=str(model_dir),
            cpu_threads=_cpu_threads(),
            num_workers=1,
        )

    def _run(self) -> None:
        try:
            self.model = self._load_model()
            log.info("Local speech model %s ready", self.model_name)
            self.on_status("Local speech model ready.")
        except Exception as exc:  # noqa: BLE001 - fall back to backend transcription
            self.failed = True
            log.exception("Local speech model could not load")
            self.on_error(f"Local transcription unavailable ({exc}). Falling back to the backend.")
            return

        while not self._stopped:
            try:
                wav_bytes = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                text = self.transcribe(wav_bytes)
                if text:
                    self.on_text(text)
            except Exception as exc:  # noqa: BLE001 - one bad clip must not stop captions
                log.warning("Local transcription failed: %s", exc)

    def transcribe(self, wav_bytes: bytes) -> str:
        if self.model is None:
            return ""
        audio, duration = _wav_to_float32(wav_bytes)
        if duration < 0.15 or audio.size == 0:
            return ""
        segments, _info = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
            no_speech_threshold=0.6,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            return ""
        if duration < 1.6 and text.lower().strip() in SILENCE_HALLUCINATIONS:
            log.debug("Dropped a silence hallucination: %s", text)
            return ""
        return text
