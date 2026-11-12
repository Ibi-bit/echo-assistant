from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent.parent
load_dotenv(SERVER_DIR / ".env")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _words(name: str, default: str) -> tuple[str, ...]:
    return tuple(w.strip() for w in _env(name, default).split(",") if w.strip())


DEFAULT_SYSTEM_PROMPT = (
    "You are the voice assistant built into a wall-mounted Echo Show in the user's home. "
    "Reply in plain spoken English. Keep answers short: one to three sentences unless the "
    "user asks for detail. Never use markdown, bullet lists, code blocks, emoji, or stage "
    "directions. If you do not know something, say so in one short sentence."
)


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8765))
    auth_token: str = field(default_factory=lambda: _env("ASSIST_TOKEN", ""))

    wake_words: tuple[str, ...] = field(default_factory=lambda: _words("WAKE_WORDS", "hey_jarvis"))
    wake_threshold: float = field(default_factory=lambda: _float("WAKE_THRESHOLD", 0.5))
    vad_aggressiveness: int = field(default_factory=lambda: _int("VAD_AGGRESSIVENESS", 2))
    end_silence_ms: int = field(default_factory=lambda: _int("END_SILENCE_MS", 900))
    max_utterance_ms: int = field(default_factory=lambda: _int("MAX_UTTERANCE_MS", 15000))
    no_speech_timeout_ms: int = field(default_factory=lambda: _int("NO_SPEECH_TIMEOUT_MS", 6000))

    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "base.en"))
    whisper_device: str = field(default_factory=lambda: _env("WHISPER_DEVICE", "cpu"))
    whisper_compute: str = field(default_factory=lambda: _env("WHISPER_COMPUTE", "int8"))
    piper_voice: str = field(
        default_factory=lambda: _env("PIPER_VOICE", "models/piper/en_US-lessac-medium.onnx")
    )

    api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY", ""))
    api_base: str = field(
        default_factory=lambda: _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    model: str = field(
        default_factory=lambda: _env("OPENROUTER_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
    )
    fallback_model: str = field(
        default_factory=lambda: _env("OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3.5-lightning:free")
    )
    fallback_models: tuple[str, ...] = field(
        default_factory=lambda: _words(
            "OPENROUTER_FALLBACK_MODELS",
            _env("OPENROUTER_FALLBACK_MODEL", "nvidia/nemotron-3.5-lightning:free"),
        )
    )
    disable_reasoning: bool = field(
        default_factory=lambda: _env("DISABLE_REASONING", "true").lower()
        not in ("0", "false", "no", "off")
    )
    system_prompt: str = field(default_factory=lambda: _env("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT))
    location: str = field(default_factory=lambda: _env("ASSIST_LOCATION", ""))
    history_turns: int = field(default_factory=lambda: _int("HISTORY_TURNS", 8))
    max_reply_tokens: int = field(default_factory=lambda: _int("MAX_REPLY_TOKENS", 350))
    playback_timeout_s: float = field(default_factory=lambda: _float("PLAYBACK_TIMEOUT_S", 12.0))

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    def voice_path(self) -> Path:
        path = Path(self.piper_voice)
        return path if path.is_absolute() else (SERVER_DIR / path)


settings = Settings()
