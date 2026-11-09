from __future__ import annotations

import asyncio
import re
from pathlib import Path

import numpy as np
from piper import PiperVoice

OUTPUT_RATE = 22050

_SENTENCE_RE = re.compile(r"[.!?…]+(?=\s|$)")


def _resample(samples: np.ndarray, source_rate: int) -> np.ndarray:
    if source_rate == OUTPUT_RATE or len(samples) == 0:
        return samples
    count = max(1, int(round(len(samples) * OUTPUT_RATE / source_rate)))
    x = np.linspace(0, len(samples) - 1, count)
    return np.interp(x, np.arange(len(samples)), samples.astype(np.float32)).astype(np.int16)


def _to_pcm(data: bytes, rate: int, channels: int, width: int) -> bytes:
    if width != 2:
        raise ValueError(f"unsupported sample width: {width}")
    if channels == 1 and rate == OUTPUT_RATE:
        return data
    samples = np.frombuffer(data, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return _resample(samples, rate).tobytes()


class Speaker:
    def __init__(self, voice_path: Path) -> None:
        self._lock = asyncio.Lock()
        self.voice = PiperVoice.load(str(voice_path))

    async def speak(self, text: str) -> bytes:
        async with self._lock:
            return await asyncio.to_thread(self._synth, text)

    def _synth(self, text: str) -> bytes:
        out = bytearray()
        for chunk in self.voice.synthesize(text):
            out += _to_pcm(
                chunk.audio_int16_bytes,
                chunk.sample_rate,
                chunk.sample_channels,
                chunk.sample_width,
            )
        return bytes(out)


class SentenceBuffer:
    """Turns a token stream into sentence-sized chunks for low-latency TTS."""

    def __init__(self, max_chars: int = 280) -> None:
        self.buffer = ""
        self.max_chars = max_chars

    def feed(self, text: str) -> list[str]:
        self.buffer += text
        out: list[str] = []

        while True:
            match = _SENTENCE_RE.search(self.buffer)
            if not match:
                break
            end = match.end()
            sentence = self.buffer[:end].strip()
            self.buffer = self.buffer[end:].lstrip()
            if sentence:
                out.append(sentence)

        while len(self.buffer) > self.max_chars:
            cut = self.buffer.rfind(" ", 0, self.max_chars)
            if cut <= 0:
                cut = self.max_chars
            out.append(self.buffer[:cut].strip())
            self.buffer = self.buffer[cut:].lstrip()

        return out

    def flush(self) -> list[str]:
        rest = self.buffer.strip()
        self.buffer = ""
        return [rest] if rest else []
