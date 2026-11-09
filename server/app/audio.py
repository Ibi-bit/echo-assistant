from __future__ import annotations

from collections import deque

import numpy as np
import webrtcvad
from openwakeword.model import Model

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2  # 640 bytes = 320 int16 samples
WAKE_CHUNK_BYTES = 2560  # 80 ms, required by openWakeWord


class WakeWord:
    """openWakeWord ONNX detector. This is the only path that needs no tflite."""

    def __init__(self, words: tuple[str, ...], threshold: float) -> None:
        self.threshold = threshold
        self.words = words
        self.model = Model(wakeword_models=list(words), inference_framework="onnx")

    def reset(self) -> None:
        self.model.reset()

    def detect(self, chunk: bytes) -> tuple[str, float] | None:
        audio = np.frombuffer(chunk, dtype=np.int16)
        scores = self.model.predict(audio)
        if not scores:
            return None
        name, score = max(scores.items(), key=lambda item: item[1])
        if score >= self.threshold:
            return name, float(score)
        return None


class Segmenter:
    """Collects one utterance after a wake word until enough trailing silence."""

    def __init__(
        self,
        aggressiveness: int,
        end_silence_ms: int,
        max_ms: int,
        no_speech_ms: int,
        min_speech_ms: int = 250,
        preroll_ms: int = 160,
    ) -> None:
        self.vad = webrtcvad.Vad(aggressiveness)
        self.end_silence_ms = end_silence_ms
        self.max_ms = max_ms
        self.no_speech_ms = no_speech_ms
        self.min_speech_ms = min_speech_ms
        self.preroll_frames = max(0, preroll_ms // FRAME_MS)

        self.frames: list[bytes] = []
        self.first_speech: int | None = None
        self.speech_ms = 0
        self.silence_ms = 0

    @property
    def total_ms(self) -> int:
        return len(self.frames) * FRAME_MS

    def feed(self, chunk: bytes) -> bytes | None:
        """Return None while listening, b'' to abort, or the utterance PCM."""
        for offset in range(0, len(chunk) - FRAME_BYTES + 1, FRAME_BYTES):
            frame = chunk[offset : offset + FRAME_BYTES]
            self.frames.append(frame)
            if self.vad.is_speech(frame, SAMPLE_RATE):
                self.speech_ms += FRAME_MS
                self.silence_ms = 0
                if self.first_speech is None:
                    self.first_speech = len(self.frames) - 1
            elif self.first_speech is not None:
                self.silence_ms += FRAME_MS

        if self.first_speech is None:
            return b"" if self.total_ms >= self.no_speech_ms else None

        if self.total_ms >= self.max_ms:
            return self.audio() if self.speech_ms >= self.min_speech_ms else b""

        if self.silence_ms >= self.end_silence_ms:
            return self.audio() if self.speech_ms >= self.min_speech_ms else b""

        return None

    def audio(self) -> bytes:
        if self.first_speech is None:
            return b""
        start = max(0, self.first_speech - self.preroll_frames)
        return b"".join(self.frames[start:])


class Preroll:
    """Tiny ring buffer so the start of the query is not clipped by detection lag."""

    def __init__(self, ms: int = 160) -> None:
        self.chunks: deque[bytes] = deque(maxlen=max(1, ms // 80))

    def push(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    def drain(self) -> list[bytes]:
        out = list(self.chunks)
        self.chunks.clear()
        return out
