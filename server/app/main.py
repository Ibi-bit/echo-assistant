from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.responses import RedirectResponse

from .audio import WakeWord
from .config import SERVER_DIR, settings
from .pipeline import AssistantSession
from .stt import Transcriber
from .tts import Speaker
from .webconfig import router as web_config_router

log = logging.getLogger("assistant")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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
    if settings.auth_token and websocket.query_params.get("token") != settings.auth_token:
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
