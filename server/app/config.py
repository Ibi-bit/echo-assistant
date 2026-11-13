from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent.parent
load_dotenv(SERVER_DIR / ".env")


_PRIVATE_NETS = tuple(
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def is_local_host(host: str) -> bool:
    """True for loopback/private LAN addresses (and localhost).

    Deliberately not ``ipaddress.is_private``: Python 3.14 also marks
    documentation ranges such as 203.0.113.0/24 as private.
    """
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or any(ip in net for net in _PRIVATE_NETS)


def is_local_request(peer_host: str, forwarded_for: str = "") -> bool:
    """True for loopback/LAN clients, including a local reverse proxy.

    A public client cannot spoof its way in: the direct peer must be local
    before X-Forwarded-For is considered. A private peer with a forwarded
    header is a proxy (host or Docker), so use the original client address.
    """
    if not is_local_host(peer_host):
        return False
    if forwarded_for:
        return is_local_host(forwarded_for.split(",", 1)[0].strip())
    return True


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
    # Longer window after the user asks to keep listening, so they have time
    # to think without needing the wake word again.
    continue_listen_ms: int = field(default_factory=lambda: _int("CONTINUE_LISTEN_MS", 30000))

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
