from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel


class Transcriber:
    def __init__(
        self,
        model_name: str,
        device: str,
        compute_type: str,
        download_root: Path,
    ) -> None:
        self._lock = asyncio.Lock()
        self.model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=str(download_root),
        )

    async def transcribe(self, pcm: bytes) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._transcribe_sync, pcm)

    def _transcribe_sync(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self.model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()
