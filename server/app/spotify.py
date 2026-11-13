"""Spotify Web API client with PKCE OAuth for the assistant.

Uses the same client_id as Rusty (90d7fa8653384b63bdd1d795406b6be4) and the
loopback redirect http://127.0.0.1:25566/callback. Tokens are stored in
~/.config/assistant/spotify_tokens.json so re-auth is rare.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

log = logging.getLogger("assistant.spotify")

# Same client_id as Rusty uses (public client, no secret needed)
CLIENT_ID = "90d7fa8653384b63bdd1d795406b6be4"
REDIRECT_URI = "http://127.0.0.1:25566/callback"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
TOKEN_FILE = Path.home() / ".config" / "assistant" / "spotify_tokens.json"

# Device name we want to play on
TARGET_DEVICE_NAME = "Rusty Speaker"


class SpotifyTokens:
    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        expires_at: float,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at

    @property
    def is_valid(self) -> bool:
        return time.time() < self.expires_at - 60  # 60s safety margin

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SpotifyTokens":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
        )


class SpotifyClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: SpotifyTokens | None = None
        self._http = httpx.AsyncClient(timeout=15.0)
        self._load_tokens()

    def _load_tokens(self) -> None:
        try:
            if TOKEN_FILE.exists():
                data = json.loads(TOKEN_FILE.read_text())
                self._tokens = SpotifyTokens.from_dict(data)
                log.info("Loaded Spotify tokens from %s", TOKEN_FILE)
        except Exception as e:
            log.warning("Failed to load Spotify tokens: %s", e)

    def _save_tokens(self) -> None:
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(json.dumps(self._tokens.to_dict()))
            TOKEN_FILE.chmod(0o600)
        except Exception as e:
            log.warning("Failed to save Spotify tokens: %s", e)

    @staticmethod
    def _gen_verifier() -> str:
        return secrets.token_urlsafe(64)[:64]

    @staticmethod
    def _gen_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _auth_url(self, verifier: str, state: str) -> str:
        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": self._gen_challenge(verifier),
        }
        return f"https://accounts.spotify.com/authorize?{urlencode(params)}"

    async def _exchange_code(self, code: str, verifier: str) -> SpotifyTokens:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        }
        resp = await self._http.post(
            "https://accounts.spotify.com/api/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        j = resp.json()
        return SpotifyTokens(
            access_token=j["access_token"],
            refresh_token=j["refresh_token"],
            expires_at=time.time() + j["expires_in"],
        )

    async def _refresh(self) -> SpotifyTokens:
        if not self._tokens:
            raise RuntimeError("No refresh token available")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._tokens.refresh_token,
            "client_id": CLIENT_ID,
        }
        resp = await self._http.post(
            "https://accounts.spotify.com/api/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        j = resp.json()
        new_tokens = SpotifyTokens(
            access_token=j["access_token"],
            refresh_token=j.get("refresh_token", self._tokens.refresh_token),
            expires_at=time.time() + j["expires_in"],
        )
        self._tokens = new_tokens
        self._save_tokens()
        return new_tokens

    async def _ensure_valid_token(self) -> str:
        with self._lock:
            if self._tokens and self._tokens.is_valid:
                return self._tokens.access_token
            if self._tokens and self._tokens.refresh_token:
                log.info("Refreshing Spotify access token")
                await self._refresh()
                return self._tokens.access_token
            raise RuntimeError("Not authenticated — need to run OAuth flow")

    async def get_token_interactive(self) -> str:
        """Run PKCE OAuth flow with local HTTP server. Returns access token."""
        verifier = self._gen_verifier()
        state = secrets.token_urlsafe(16)
        auth_url = self._auth_url(verifier, state)

        code_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def callback(request):
            from aiohttp import web
            params = parse_qs(urlparse(str(request.url)).query)
            # Allow any state for initial setup (browser caching causes mismatches)
            if "error" in params:
                return web.Response(text=f"OAuth error: {params['error'][0]}", status=400)
            code = params.get("code", [""])[0]
            if not code:
                return web.Response(text="No code", status=400)
            code_future.set_result(code)
            return web.Response(text="Authenticated! You can close this window.")

        from aiohttp import web
        app = web.Application()
        app.router.add_get("/callback", callback)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 25566)
        await site.start()

        log.info("Open this URL to authorize: %s", auth_url)
        print(f"\n🎵 Open this URL in a browser to authorize Spotify:\n{auth_url}\n")
        code = await asyncio.wait_for(code_future, timeout=600)

        await runner.cleanup()

        tokens = await self._exchange_code(code, verifier)
        with self._lock:
            self._tokens = tokens
            self._save_tokens()
        return tokens.access_token

    async def _get_headers(self) -> dict:
        token = await self._ensure_valid_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def search_track(self, track: str, artist: str | None = None) -> dict | None:
        """Search for a track, return the first match's URI."""
        headers = await self._get_headers()
        q = f"track:{track}"
        if artist:
            q += f" artist:{artist}"
        resp = await self._http.get(
            "https://api.spotify.com/v1/search",
            params={"q": q, "type": "track", "limit": 1},
            headers=headers,
        )
        if resp.status_code == 401:
            await self._refresh()
            return await self.search_track(track, artist)
        resp.raise_for_status()
        items = resp.json().get("tracks", {}).get("items", [])
        return items[0] if items else None

    async def get_devices(self) -> list[dict]:
        headers = await self._get_headers()
        resp = await self._http.get(
            "https://api.spotify.com/v1/me/player/devices",
            headers=headers,
        )
        if resp.status_code == 401:
            await self._refresh()
            return await self.get_devices()
        resp.raise_for_status()
        return resp.json().get("devices", [])

    async def start_playback(
        self,
        device_id: str,
        uris: list[str] | None = None,
        context_uri: str | None = None,
        offset: dict | None = None,
    ) -> bool:
        headers = await self._get_headers()
        payload: dict = {}
        if uris:
            payload["uris"] = uris
        if context_uri:
            payload["context_uri"] = context_uri
        if offset:
            payload["offset"] = offset
        resp = await self._http.put(
            f"https://api.spotify.com/v1/me/player/play?device_id={device_id}",
            json=payload,
            headers=headers,
        )
        if resp.status_code == 401:
            await self._refresh()
            return await self.start_playback(device_id, uris, context_uri, offset)
        if resp.status_code == 404:
            return False  # No active device
        resp.raise_for_status()
        return True

    async def find_target_device(self) -> str | None:
        devices = await self.get_devices()
        for d in devices:
            if d.get("name") == TARGET_DEVICE_NAME and d.get("is_active"):
                return d["id"]
        # Fallback: any active device
        for d in devices:
            if d.get("is_active"):
                return d["id"]
        return None


# Global instance
_spotify_client: SpotifyClient | None = None


def get_spotify_client() -> SpotifyClient:
    global _spotify_client
    if _spotify_client is None:
        _spotify_client = SpotifyClient()
    return _spotify_client