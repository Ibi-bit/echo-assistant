from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx


class ChatClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        model: str,
        system_prompt: str,
        base_url: str,
        max_tokens: int,
        disable_reasoning: bool = True,
        fallback_model: str = "",
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.model = model
        self.fallback_model = (fallback_model or "").strip()
        self.system_prompt = system_prompt
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.disable_reasoning = disable_reasoning
        # Populated while streaming. Each entry is an OpenAI-shaped tool call.
        self.last_tool_calls: list[dict] = []

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[str]:
        """Stream content deltas, collecting any tool calls into last_tool_calls.

        Tries ``model`` first. If that call errors before any token is yielded
        (HTTP error, network, empty network stream), retry once with
        ``fallback_model`` when configured so a backup model keeps the turn alive.
        """
        yielded = False
        try:
            async for chunk in self._stream_with(self.model, messages, tools):
                yielded = True
                yield chunk
        except Exception as primary_exc:
            if yielded:
                raise
            if not self.fallback_model or self.fallback_model == self.model:
                raise
            async for chunk in self._stream_with(self.fallback_model, messages, tools):
                yield chunk

    async def _stream_with(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": 0.6,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        self.last_tool_calls = []
        if self.disable_reasoning:
            # Nemotron 3.5 Lightning streams a long reasoning preamble by default.
            # For a voice assistant that is pure latency (and it eats the token
            # budget before any answer is produced), so ask for it to be disabled.
            payload["reasoning"] = {"enabled": False}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost",
            "X-Title": "Echo Show Assistant",
        }
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)

        async with self.client.stream(
            "POST", f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=timeout
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(f"OpenRouter {response.status_code}: {body}")

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if event.get("error"):
                    raise RuntimeError(str(event["error"]))
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                for call in delta.get("tool_calls") or []:
                    try:
                        index = int(call.get("index") or 0)
                    except (TypeError, ValueError):
                        index = 0
                    while len(self.last_tool_calls) <= index:
                        self.last_tool_calls.append(
                            {
                                "type": "function",
                                "id": "",
                                "function": {"name": "", "arguments": ""},
                            }
                        )
                    slot = self.last_tool_calls[index]
                    if call.get("id"):
                        slot["id"] = str(call["id"])
                    function = call.get("function") or {}
                    if function.get("name"):
                        slot["function"]["name"] = str(function["name"])
                    if function.get("arguments"):
                        slot["function"]["arguments"] += str(function["arguments"])
                content = delta.get("content")
                if content:
                    yield content
