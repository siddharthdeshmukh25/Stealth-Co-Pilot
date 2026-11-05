"""WASAPI loopback capture with a simple RMS pause detector."""
import asyncio
import io
import wave
from collections.abc import Callable

import numpy as np
import pyaudiowpatch as pyaudio

CHUNK = 1024
TARGET_RATE = 16000
SPEECH_THRESHOLD = 300
# Short pauses already end an utterance: waiting almost a second made captions feel late.
SILENCE_SECONDS = 0.45
MIN_SPEECH_SECONDS = 0.3
# Never hold audio longer than this: continuous speech still produces live captions.
MAX_UTTERANCE_SECONDS = 5.0

class LoopbackCapture:
    def __init__(self, on_utterance: Callable[[bytes], None], threshold: int = SPEECH_THRESHOLD):
        self.on_utterance = on_utterance
        self.threshold = threshold
        self.paused = False
        self.running = False
        # Plain float published for the UI level meter; written on the capture
        # thread and only ever read, so no lock is needed.
        self.level = 0.0

    def _flush(self, buffer: list[bytes], rate: int, channels: int) -> None:
        if buffer:
            self.on_utterance(self._to_wav(buffer, rate, channels))

    def run(self) -> None:
        audio = pyaudio.PyAudio()
        try:
            device = audio.get_default_wasapi_loopback()
            rate = int(device["defaultSampleRate"])
            channels = min(int(device["maxInputChannels"]), 2)
            stream = audio.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                                input_device_index=device["index"], frames_per_buffer=CHUNK)
            self.running = True
            buffer: list[bytes] = []
            silence_chunks = 0
            speech_chunks = 0
            while self.running:
                frame = stream.read(CHUNK, exception_on_overflow=False)
                rms = int(np.sqrt(np.mean(np.square(np.frombuffer(frame, dtype=np.int16).astype(np.float32)))))
                # Fast attack, gentle release keeps the meter readable.
                self.level = max(float(rms), self.level * 0.86)
                if self.paused:
                    buffer.clear(); silence_chunks = speech_chunks = 0
                    continue
                if rms >= self.threshold:
                    buffer.append(frame); speech_chunks += 1; silence_chunks = 0
                    if speech_chunks * CHUNK / rate >= MAX_UTTERANCE_SECONDS:
                        self._flush(buffer, rate, channels)
                        buffer.clear(); silence_chunks = speech_chunks = 0
                elif buffer:
                    buffer.append(frame); silence_chunks += 1
                    if silence_chunks * CHUNK / rate >= SILENCE_SECONDS:
                        if speech_chunks * CHUNK / rate >= MIN_SPEECH_SECONDS:
                            self._flush(buffer, rate, channels)
                        buffer.clear(); silence_chunks = speech_chunks = 0
            stream.stop_stream(); stream.close()
        finally:
            audio.terminate()

    @staticmethod
    def _to_wav(frames: list[bytes], source_rate: int, channels: int) -> bytes:
        samples = np.frombuffer(b"".join(frames), dtype=np.int16).reshape(-1, channels)
        if channels > 1:
            samples = samples.mean(axis=1).astype(np.int16)
        if source_rate != TARGET_RATE:
            # Linear interpolation keeps the client dependency-light; speech is sent as mono 16 kHz PCM.
            old_x = np.arange(len(samples), dtype=np.float64)
            new_length = round(len(samples) * TARGET_RATE / source_rate)
            samples = np.interp(np.linspace(0, len(samples) - 1, new_length), old_x, samples).astype(np.int16)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(TARGET_RATE); wav.writeframes(samples.tobytes())
        return output.getvalue()
