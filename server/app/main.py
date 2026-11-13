from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.responses import RedirectResponse

from .audio import WakeWord
from .config import (
    SERVER_DIR,
    is_local_request,
    is_secure_transport,
    redact_token,
    settings,
)
from .pipeline import AssistantSession
from .stt import Transcriber
from .tts import Speaker
from .webconfig import router as web_config_router

log = logging.getLogger("assistant")


class _TokenRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_token(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_token(a) if isinstance(a, str) else a for a in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: redact_token(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redactor = _TokenRedactingFilter()
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(redactor)
    for noisy in ("httpx2", "huggingface_hub", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not settings.api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (put it in server/.env)")

    log.info("loading whisper model: %s", settings.whisper_model)
    app.state.stt = Transcriber(
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute,
        download_root=SERVER_DIR / "models" / "whisper",
    )

    log.info("loading piper voice: %s", settings.voice_path())
    app.state.tts = Speaker(settings.voice_path())

    app.state.http = httpx.AsyncClient()
    log.info("ready: ws://%s:%d/ws", settings.host, settings.port)
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="Echo Show Assistant", lifespan=lifespan)
app.include_router(web_config_router)


@app.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse("/config")


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "model": settings.model,
        "fallback_models": list(settings.fallback_models),
        "wake_words": list(settings.wake_words),
        "whisper": settings.whisper_model,
        "voice": settings.voice_path().name,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    peer = websocket.client.host if websocket.client else ""
    forwarded_for = websocket.headers.get("x-forwarded-for", "")
    if not is_secure_transport(
        peer,
        forwarded_for,
        websocket.headers.get("x-forwarded-proto", ""),
        websocket.url.scheme,
    ):
        await websocket.close(code=4403)
        return
    if settings.auth_token:
        auth = websocket.headers.get("authorization", "")
        supplied = (
            auth[7:].strip()
            if auth.lower().startswith("bearer ")
            else websocket.query_params.get("token")
            or websocket.headers.get("x-assist-token")
            or ""
        )
        allowed = bool(supplied) and hmac.compare_digest(
            supplied.encode(), settings.auth_token.encode()
        )
    else:
        allowed = is_local_request(peer, forwarded_for)
    if not allowed:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    # openWakeWord's Model keeps per-stream audio state. One shared instance would
    # interleave the audio of every connected client and break detection, so each
    # session gets its own model (cheap to create, and reconnects are rare).
    wake = WakeWord(settings.wake_words, settings.wake_threshold)
    session = AssistantSession(
        websocket,
        settings,
        wake,
        websocket.app.state.stt,
        websocket.app.state.tts,
        websocket.app.state.http,
    )
    await session.run()
