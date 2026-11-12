from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime
from enum import Enum

import httpx
from fastapi import WebSocket, WebSocketDisconnect

from .audio import WAKE_CHUNK_BYTES, Preroll, Segmenter, WakeWord
from .config import Settings
from .llm import ChatClient
from .stt import Transcriber
from .tools import TOOL_SCHEMAS
from .tts import OUTPUT_RATE, SentenceBuffer, Speaker

log = logging.getLogger("assistant.session")

_NUMBER_WORD_VALUES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

_NUMBER_ONES = "one|two|three|four|five|six|seven|eight|nine"
_NUMBER_TEENS = (
    "ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
)
_NUM = (
    r"(?:\d+|one hundred|a hundred|hundred|"
    r"twenty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"thirty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"forty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"fifty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"sixty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"seventy(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"eighty(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    r"ninety(?:[ -]?(?:" + _NUMBER_ONES + r"))?|"
    + _NUMBER_TEENS + r"|" + _NUMBER_ONES + r"|a|an)"
)


def _number_from_words(raw: str) -> int | None:
    """Parse digits or English number words (incl. 'twenty five', 'one hundred')."""
    s = raw.strip().lower().replace("-", " ")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    tokens = [tok for tok in s.split() if tok != "and"]
    if not tokens:
        return None
    current = 0
    seen = False
    for tok in tokens:
        if tok in ("a", "an"):
            value = 1
        elif tok == "hundred":
            current = (current or 1) * 100
            seen = True
            continue
        elif tok in _NUMBER_WORD_VALUES:
            value = _NUMBER_WORD_VALUES[tok]
        else:
            return None
        current += value
        seen = True
    return current if seen else None


def _regex_action(text: str) -> tuple[str, dict] | None:
    """High-precision deterministic intent routing.

    The free model routinely answers short imperatives ('Pause music.', 'mute.')
    from context WITHOUT calling its tool, then hallucinates success. These
    patterns run the action server-side first; the model then only confirms.
    Only unambiguous action phrases fire — questions fall through to the model.
    """
    core = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9: ]", " ", text.lower())).strip()
    if not core:
        return None
    interrogative = core.startswith(
        ("what ", "how ", "when ", "which ", "why ", "is ", "are ", "did ", "do ",
         "can you tell", "tell me", "how much", "how loud")
    )

    if re.search(r"\b(stop|quit|exit)\s+(listening|the conversation)\b", core) or re.search(
        r"\b(shut\s*up|be\s+quiet|that\s+s\s+all|that\s+is\s+all|never\s*mind)\b", core
    ):
        return "stop_listening", {}

    # Strip leading fillers so imperative detection sees the verb.
    imp = core
    for _ in range(4):
        stripped = re.sub(r"^(hey jarvis|ok|okay|so|and|well|please|just|now|then|um|uh)\b[ ,]*", "", imp)
        if stripped == imp:
            break
        imp = stripped
    imp = re.sub(r"^(can you|could you|would you|will you)\s+", "", imp).strip()

    def starts_with(verb_re: str) -> bool:
        return re.match(verb_re, imp) is not None

    obj = r"(?:the\s+)?(?:music|song|track|it|this|that|playback|volume)"

    def verb_obj(verb_re: str) -> bool:
        return bool(starts_with(verb_re) or re.search(verb_re + r"\s+" + obj + r"\b", imp))

    if re.search(r"\b(cancel|dismiss|stop|kill|remove)\b[^.]*\btimer\b", core):
        return "cancel_timer", {}
    if re.search(r"\b(cancel|dismiss|turn off|stop|kill|remove)\b[^.]*\balarm\b", core):
        return "cancel_alarm", {}

    if re.search(r"\btimer\b", core) and not interrogative:
        m = re.search(r"(" + _NUM + r")\s*(seconds?|minutes?|hours?)\b", core)
        if m:
            n = _number_from_words(m.group(1))
            if n is not None and n > 0:
                unit = m.group(2)
                secs = n if unit.startswith("s") else n * 60 if unit.startswith("m") else n * 3600
                return "set_timer", {"seconds": max(1, min(86400, secs))}

    if re.search(r"\balarm\b|\bwake me\b", core) and not interrogative:
        hour: int | None = None
        minute = 0
        ampm = None
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", core)
        if m and (m.group(2) or m.group(3) or re.search(r"\b(?:at|for|to)\s+\d", core)):
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = m.group(3)
        if hour is None:
            m2 = re.search(r"\b(" + _NUM + r")\s*(am|pm)\b", core)
            if m2:
                hour = _number_from_words(m2.group(1))
                ampm = m2.group(2)
        if hour is None:
            m3 = re.search(r"\balarm\s+(?:for\s+|at\s+)?(" + _NUM + r")\b", core)
            if m3 and not re.search(r"\b(minutes?|seconds?|hours?)\b", core):
                hour = _number_from_words(m3.group(1))
        if hour is not None and 0 <= hour <= 23:
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return "set_alarm", {"hour": hour, "minute": minute}

    if not interrogative:
        m = re.search(r"\bvolume\b[^.]*?\bto\s+(" + _NUM + r")\b", core)
        if m:
            level = _number_from_words(m.group(1))
            if level is not None and 0 <= level <= 100:
                return "set_volume", {"level": level}
        m = re.search(r"\b(?:set|turn|put|change)\b[^.]*?\bvolume\b[^.]*?\b(" + _NUM + r")\s*(%|percent|per\s*cent)?\b", core)
        if m:
            level = _number_from_words(m.group(1))
            if level is not None and 0 <= level <= 100 and (
                m.group(2) or re.search(r"\b(?:set|put)\b", core)
            ):
                return "set_volume", {"level": level}
        if re.search(r"\bunmute\b", core):
            return "change_volume", {"direction": "unmute"}
        if re.search(r"\bmute\b", core):
            return "change_volume", {"direction": "mute"}
        if verb_obj(r"(?:volume\s+up|louder|turn\s+(?:it\s+)?up|turn\s+(?:the\s+)?volume\s+up|crank(?:\s+it)?)"):
            return "change_volume", {"direction": "up", "steps": 3}
        if verb_obj(r"(?:volume\s+down|quieter|turn\s+(?:it\s+)?down|turn\s+(?:the\s+)?volume\s+down|lower(?:\s+it)?)"):
            return "change_volume", {"direction": "down", "steps": 3}

        stop_it = starts_with(r"stop\b") and not re.search(r"\b(timer|alarm)\b", core)
        if verb_obj(r"pause") or stop_it:
            return "media_control", {"action": "pause"}
        if verb_obj(r"play") or starts_with(r"resume\b") or re.search(r"\bkeep\s+playing\b", core):
            return "media_control", {"action": "play"}
        if verb_obj(r"(?:skip|next)") or starts_with(r"shuffle\b"):
            return "media_control", {"action": "next"}
        if starts_with(r"(?:previous|back)\b") or re.search(r"\bgo\s+back\b", core):
            return "media_control", {"action": "previous"}
        if (
            verb_obj(r"(?:restart|replay)")
            or re.search(r"\bfrom\s+the\s+(?:start|beginning)\b", core)
            or starts_with(r"start\s+(?:it|the\s+song|the\s+track|over)\b")
        ):
            return "media_control", {"action": "restart"}

    if re.search(r"\b(keep|continue|stay)\s+listening\b", core):
        return "continue_listening", {}

    return None


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class AssistantSession:
    def __init__(
        self,
        websocket: WebSocket,
        settings: Settings,
        wake: WakeWord,
        stt: Transcriber,
        tts: Speaker,
        http: httpx.AsyncClient,
    ) -> None:
        self.ws = websocket
        self.settings = settings
        self.wake = wake
        self.stt = stt
        self.tts = tts
        self.state = State.IDLE
        self.device_context = ""
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._command_seq = 0
        self._context_seen = False
        self._audio_bytes = 0
        self._audio_log_at = time.monotonic()
        self._followup = False
        self._want_continue = False
        self._want_stop = False
        # Conversation mode: opened by continue_listening, stays open across
        # turns until a follow-up window expires without speech.
        self._conversation = False
        self._followup_deadline = 0.0

        prompt = self._system_prompt()
        self.chat = ChatClient(
            http,
            api_key=settings.api_key,
            model=settings.model,
            fallback_models=list(settings.fallback_models),
            system_prompt=prompt,
            base_url=settings.api_base,
            max_tokens=settings.max_reply_tokens,
            disable_reasoning=settings.disable_reasoning,
        )

        self.history: list[dict[str, str]] = []
        self._pcm = bytearray()
        self._preroll = Preroll(160)
        self._segmenter: Segmenter | None = None
        self._playback_done = asyncio.Event()
        self._audio_sent = 0
        self._utterance_task: asyncio.Task[None] | None = None
        self._closed = False

    def _system_prompt(self) -> str:
        now = datetime.now()
        context = self.device_context.strip() or "No live device context is available."
        place = f"The user's home is in {self.settings.location}. " if self.settings.location else ""
        return (
            f"{self.settings.system_prompt} "
            f"{place}"
            f"The current date and time is {now:%A, %B %d, %Y at %I:%M %p}. "
            "Use that when asked about the date or time; do not guess. "
            "You can set alarms and timers, change media volume, and control Spotify playback "
            "with the provided tools. For any alarm, timer, volume, or media request, use the "
            "tool — never describe an action as done without its tool result, and never state "
            "a volume level, time, or timer length unless a tool result gave it to you. "
            "If you did not call a tool in your response, describe no action as done: say what "
            "you actually did, or ask what you should do. "
            "Use the device context below for questions about the device or what "
            "is playing; it may be a few seconds old. "
            "If the user asks you to keep listening, stay on, or continue without a wake word, "
            "call the continue_listening tool in the same response as any other tool. "
            "If the user tells you to stop listening, that's all, never mind, or shut up, "
            "call the stop_listening tool and say nothing more. "
            f"Your wake word is '{self.settings.wake_words[0] if self.settings.wake_words else 'Hey Jarvis'}'. "
            "For current or time-sensitive information — weather, news, sports, prices, recent "
            "events — or any fact you are unsure of, call the search tool before answering; "
            "answer from its results in plain spoken English, without citing URLs. For facts "
            "you know, answer directly. "
            "DEVICE CONTEXT:\n"
            f"{context}"
        )

    # ---------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        await self._send({"type": "ready", "tts": {"sample_rate": OUTPUT_RATE, "channels": 1, "encoding": "pcm_s16le"}, "wake_words": list(self.settings.wake_words)})
        await self._set_state(State.IDLE)
        try:
            while True:
                message = await self.ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                audio = message.get("bytes")
                text = message.get("text")
                if audio:
                    await self._on_audio(audio)
                elif text:
                    await self._on_text(text)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("session loop crashed")
        finally:
            self._closed = True
            if self._utterance_task and not self._utterance_task.done():
                self._utterance_task.cancel()
            for future in self._pending.values():
                if not future.done():
                    future.set_result({"ok": False, "message": "The tablet disconnected."})
            self._pending.clear()

    async def _on_text(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = message.get("type")

        if kind == "ping":
            await self._send({"type": "pong"})
        elif kind == "playback_done":
            self._playback_done.set()
        elif kind == "device_context":
            self.device_context = str(message.get("text") or "")[:6000]
            if not self._context_seen:
                self._context_seen = True
                log.info(
                    "device context: %s",
                    self.device_context.replace("\n", " | ")[:500],
                )
        elif kind == "command_result":
            ident = str(message.get("id") or "")
            future = self._pending.pop(ident, None)
            if future is not None and not future.done():
                future.set_result(
                    {
                        "ok": bool(message.get("ok")),
                        "message": str(message.get("message") or ""),
                    }
                )
        elif kind == "reset":
            self.history.clear()
            await self._send({"type": "reset"})
        elif kind == "text":
            # Typed input: bypasses wake word + STT for testing, then runs the
            # exact same turn (LLM, tools, TTS) as a spoken utterance.
            typed = str(message.get("text") or "").strip()
            if typed and self.state in (State.IDLE, State.LISTENING):
                # Typing while the mic is open behaves like speaking.
                self._segmenter = None
                await self._set_state(State.THINKING)
                self._utterance_task = asyncio.create_task(self._handle_text(typed))
        elif kind == "listen":
            if self.state is State.IDLE:
                self._conversation = False
                await self._start_listening(manual=True)
            elif self.state is State.SPEAKING:
                # Manual barge-in from tablet (e.g., tap-to-talk button).
                log.info("manual interrupt during SPEAKING")
                await self._interrupt()
                self._conversation = False
                await self._start_listening(manual=True)

    async def _on_audio(self, data: bytes) -> None:
        now = time.monotonic()
        self._audio_bytes += len(data)
        if now - self._audio_log_at >= 2.0:
            log.debug(
                "audio in: %.0f kB in %.1fs",
                self._audio_bytes / 1024,
                now - self._audio_log_at,
            )
            self._audio_bytes = 0
            self._audio_log_at = now

        # Barge-in: wake word during SPEAKING interrupts TTS.
        if self.state is State.SPEAKING:
            self._pcm.extend(data)
            while len(self._pcm) >= WAKE_CHUNK_BYTES:
                chunk = bytes(self._pcm[:WAKE_CHUNK_BYTES])
                del self._pcm[:WAKE_CHUNK_BYTES]
                hit = self.wake.detect(chunk)
                if hit:
                    log.info("barge-in wake word=%s score=%.3f", hit[0], hit[1])
                    await self._interrupt()
                    self._conversation = False
                    await self._start_listening(manual=False, word=hit[0])
                    return

        if self.state is State.IDLE:
            self._pcm.extend(data)
            while len(self._pcm) >= WAKE_CHUNK_BYTES:
                chunk = bytes(self._pcm[:WAKE_CHUNK_BYTES])
                del self._pcm[:WAKE_CHUNK_BYTES]
                self._preroll.push(chunk)
                hit = self.wake.detect(chunk)
                if hit:
                    log.info("wake word=%s score=%.3f", hit[0], hit[1])
                    self._conversation = False
                    await self._start_listening(manual=False, word=hit[0])
                    return

        elif self.state is State.LISTENING and self._segmenter is not None:
            result = self._segmenter.feed(data)
            if result is None:
                return
            frames = len(self._segmenter.frames) if self._segmenter else -1
            self._segmenter = None
            if not result:
                remaining = int((self._followup_deadline - time.monotonic()) * 1000)
                if remaining > 500:
                    # A cough or a door slam is not an answer: keep the window
                    # open instead of dropping back to the wake word.
                    log.info(
                        "short blip ignored (frames=%d, %dms left in window)",
                        frames,
                        remaining,
                    )
                    self._segmenter = self._make_segmenter(remaining)
                    return
                if self._followup:
                    log.info(
                        "follow-up window expired without speech (frames=%d)",
                        frames,
                    )
                    self._conversation = False
                    await self._back_to_idle()
                    return
                log.info("no speech after wake")
                await self._send({"type": "notice", "text": "I didn't catch that."})
                await self._back_to_idle()
                return
            await self._set_state(State.THINKING)
            self._utterance_task = asyncio.create_task(self._handle_utterance(result))

    # ------------------------------------------------------------------ pipeline

    def _make_segmenter(self, no_speech_ms: int) -> Segmenter:
        return Segmenter(
            aggressiveness=self.settings.vad_aggressiveness,
            end_silence_ms=self.settings.end_silence_ms,
            max_ms=self.settings.max_utterance_ms,
            no_speech_ms=no_speech_ms,
        )

    async def _start_listening(
        self,
        manual: bool,
        word: str | None = None,
        followup: bool = False,
    ) -> None:
        if self.state is not State.IDLE:
            return
        self.wake.reset()
        self._pcm.clear()
        self._audio_sent = 0
        self._playback_done.clear()
        self._followup = followup
        window = (
            self.settings.continue_listen_ms if followup else self.settings.no_speech_timeout_ms
        )
        self._followup_deadline = time.monotonic() + window / 1000
        self._segmenter = self._make_segmenter(window)
        if manual:
            await self._send({"type": "manual"})
        else:
            await self._send({"type": "wake", "word": word})
        await self._set_state(State.LISTENING)
        if not manual:
            for chunk in self._preroll.drain():
                self._segmenter.feed(chunk)

    async def _handle_utterance(self, pcm: bytes) -> None:
        try:
            text = await self.stt.transcribe(pcm)
            log.info("heard: %r", text)
            if not text:
                await self._send({"type": "notice", "text": "I didn't catch that."})
                await self._back_to_idle()
                return
            await self._run_turn(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("utterance failed")
            await self._send({"type": "error", "message": str(exc)})
            await self._back_to_idle()

    async def _handle_text(self, text: str) -> None:
        log.info("typed: %r", text)
        await self._run_turn(text)

    async def _run_turn(self, text: str) -> None:
        try:

            await self._send({"type": "transcript", "text": text})
            self.history.append({"role": "user", "content": text})
            self._trim_history()

            # Deterministic pre-routing: run the obvious action first, then let
            # the model only confirm (it routinely skips tools on short phrases).
            pre_name = ""
            pre_msg = ""
            pre = _regex_action(text)
            pre_msgs: list[dict] = []
            if pre:
                pre_name, pre_args = pre
                result = await self._execute_tool(
                    {"function": {"name": pre_name, "arguments": json.dumps(pre_args)}}
                )
                pre_msg = str(result.get("message") or "")
                log.info("pretool: %s %s -> %r", pre_name, pre_args, pre_msg)
                if pre_name != "continue_listening":
                    # Hand the result back through the normal tool channel so
                    # the model confirms it instead of parroting a note.
                    pre_msgs = [
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "pre_call",
                                    "type": "function",
                                    "function": {
                                        "name": pre_name,
                                        "arguments": json.dumps(pre_args),
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "pre_call",
                            "content": json.dumps({"ok": True, "message": pre_msg}),
                        },
                    ]

            queue: asyncio.Queue[str | None] = asyncio.Queue()
            speaker = asyncio.create_task(self._speak_worker(queue))
            sentence_buffer = SentenceBuffer()
            parts: list[str] = []
            messages: list[dict] = [
                {"role": "system", "content": self._system_prompt()},
                *self.history,
                *pre_msgs,
            ]
            last_tool_message = ""

            try:
                for _ in range(3):
                    if self._closed:
                        break
                    # Refresh the clock and device context for every round.
                    messages[0]["content"] = self._system_prompt()
                    round_text = ""
                    async for delta in self.chat.stream(messages, tools=TOOL_SCHEMAS):
                        if self._closed:
                            break
                        # Keep the round private until we know whether it ends in a
                        # tool call. Narration before a tool call ("Let me check…")
                        # is process noise, not an answer, and used to be spoken.
                        round_text += delta
                    if self._closed:
                        break

                    calls = [
                        call
                        for call in self.chat.last_tool_calls
                        if str((call.get("function") or {}).get("name") or "")
                    ]
                    if not calls:
                        text = self._clean_reply(round_text)
                        if text:
                            parts.append(text)
                            await self._send({"type": "delta", "text": text})
                            for sentence in sentence_buffer.feed(text):
                                await queue.put(sentence)
                            for sentence in sentence_buffer.flush():
                                await queue.put(sentence)
                        break
                    if round_text.strip():
                        log.info("dropped pre-tool narration: %r", round_text.strip()[:120])
                    log.info("tools: %s", [str((c.get("function") or {}).get("name")) for c in calls])
                    for call in calls:
                        if not call.get("id"):
                            self._command_seq += 1
                            call["id"] = f"call_{self._command_seq}"
                    messages.append(
                        {"role": "assistant", "content": None, "tool_calls": calls}
                    )
                    for call in calls:
                        call_name = str((call.get("function") or {}).get("name") or "")
                        if pre_name and call_name == pre_name:
                            # Already done deterministically above; don't run it twice.
                            log.info("pretool: suppressed duplicate %s", call_name)
                            result = {"ok": True, "message": pre_msg}
                        else:
                            result = await self._execute_tool(call)
                        last_tool_message = str(result.get("message") or "")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": json.dumps(result),
                            }
                        )

                if self._closed:
                    return
                # If the model performed an action but did not narrate the result,
                # speak the tool's own confirmation instead of staying silent.
                if not parts and last_tool_message:
                    parts.append(last_tool_message)
                    for sentence in sentence_buffer.feed(last_tool_message):
                        await queue.put(sentence)
                    for sentence in sentence_buffer.flush():
                        await queue.put(sentence)
            finally:
                await queue.put(None)

            await speaker

            reply = "".join(parts).strip()
            if reply:
                log.info("reply: %r", reply)
                self.history.append({"role": "assistant", "content": reply})
                self._trim_history()
                await self._send({"type": "reply", "text": reply})

            await self._send({"type": "audio_end"})
            if self._audio_sent:
                seconds = self._audio_sent / 2 / OUTPUT_RATE
                timeout = min(self.settings.playback_timeout_s, 3.0 + seconds * 1.5)
                try:
                    await asyncio.wait_for(self._playback_done.wait(), timeout)
                except asyncio.TimeoutError:
                    log.debug("playback_done not received in %.1fs", timeout)

            await self._back_to_idle()
            if self._want_stop:
                self._want_stop = False
                self._want_continue = False
                self._conversation = False
            elif self._want_continue:
                self._want_continue = False
                self._conversation = True
            if self._conversation:
                log.info("conversation mode: listening again")
                await self._start_listening(manual=True, followup=True)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("utterance failed")
            await self._send({"type": "error", "message": str(exc)})
            await self._back_to_idle()

    async def _execute_tool(self, call: dict) -> dict:
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        try:
            args = json.loads(function.get("arguments") or "{}")
        except (TypeError, json.JSONDecodeError):
            return {"ok": False, "message": "I couldn't understand that request."}
        if not isinstance(args, dict):
            return {"ok": False, "message": "I couldn't understand that request."}

        if name == "get_device_status":
            return {
                "ok": True,
                "message": self.device_context or "No live device context is available.",
            }
        if name == "get_volume":
            return await self._device_command("get_volume")

        if name == "set_alarm":
            hour = self._as_int(args.get("hour"))
            minute = self._as_int(args.get("minute"))
            if hour is None or minute is None or not (0 <= hour <= 23) or not (0 <= minute <= 59):
                return {"ok": False, "message": "I need a valid hour and minute for the alarm."}
            return await self._device_command(
                "set_alarm", hour=hour, minute=minute, label=str(args.get("label") or "")[:80]
            )

        if name == "set_timer":
            seconds = self._as_int(args.get("seconds"))
            if seconds is None or not (1 <= seconds <= 86400):
                return {"ok": False, "message": "I need a timer length between one second and one day."}
            return await self._device_command(
                "set_timer", seconds=seconds, label=str(args.get("label") or "")[:80]
            )

        if name == "cancel_alarm":
            return await self._device_command("cancel_alarm", all=bool(args.get("all")))

        if name == "cancel_timer":
            return await self._device_command("cancel_timer", all=bool(args.get("all")))

        if name == "set_volume":
            level = self._as_int(args.get("level"))
            if level is None or not (0 <= level <= 100):
                return {"ok": False, "message": "Volume must be a percentage from zero to one hundred."}
            return await self._device_command("set_volume", level=level)

        if name == "change_volume":
            direction = str(args.get("direction") or "").lower()
            if direction not in ("up", "down", "mute", "unmute"):
                return {"ok": False, "message": "I can turn the volume up, down, mute, or unmute."}
            steps = self._as_int(args.get("steps")) or 3
            return await self._device_command(
                "change_volume", direction=direction, steps=max(1, min(10, steps))
            )

        if name == "media_control":
            action = str(args.get("action") or "").lower()
            if action not in ("play", "pause", "toggle", "next", "previous", "restart"):
                return {"ok": False, "message": "I don't know that media action."}
            return await self._device_command("media_control", action=action)

        if name == "continue_listening":
            self._want_continue = True
            self._want_stop = False
            return {"ok": True, "message": "Continuing to listen."}

        if name == "stop_listening":
            self._want_stop = True
            self._want_continue = False
            return {"ok": True, "message": "Stopped listening."}

        if name == "search":
            query = str(args.get("query") or "").strip()
            if not query:
                return {"ok": False, "message": "I need something to search for."}
            return await self._web_search(query)

        return {"ok": False, "message": "I don't know how to do that yet."}

    _META_PREFIX = re.compile(
        r"^(?:based on|from|according to|looking at) (?:the )?device "
        r"(?:context|status|data|snapshot|state)[,:]?\s*",
        re.IGNORECASE,
    )
    _SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

    @classmethod
    def _clean_reply(cls, text: str) -> str:
        """Drop filler preambles and repeated sentences before speaking."""
        text = re.sub(r"[ \t]+", " ", text.replace("\r", ""))
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if not text:
            return ""
        out: list[str] = []
        seen: set[str] = set()
        for sentence in cls._SENTENCE_SPLIT.split(text):
            sentence = cls._META_PREFIX.sub("", sentence).strip()
            if not sentence:
                continue
            key = re.sub(r"[^a-z0-9 ]", "", sentence.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(sentence)
        return cls._META_PREFIX.sub("", " ".join(out)).strip()

    @staticmethod
    def _as_int(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    async def _fetch_wttr(self, location: str) -> str | None:
        """Fetch concise current weather from wttr.in (no API key, JSON)."""
        import urllib.parse
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    f"https://wttr.in/{urllib.parse.quote(location)}?format=j1"
                )
                response.raise_for_status()
                data = response.json()
                current = data.get("current_condition", [{}])[0]
                area = data.get("nearest_area", [{}])[0]
                temp_c = current.get("temp_C")
                temp_f = current.get("temp_F")
                desc = current.get("weatherDesc", [{}])[0].get("value", "").lower()
                humidity = current.get("humidity")
                wind_kph = current.get("windspeedKmph")
                wind_dir = current.get("winddir16Point")
                place = area.get("areaName", [{}])[0].get("value", location)
                if temp_c is None:
                    return None
                parts = [
                    f"{place}: {temp_c}°C ({temp_f}°F)",
                    desc,
                ]
                if humidity:
                    parts.append(f"humidity {humidity}%")
                if wind_kph:
                    parts.append(f"wind {wind_kph} km/h {wind_dir}")
                return "Right now, " + ", ".join(parts) + "."
        except Exception:
            log.debug("wttr.in failed for %r", location)
            return None

    async def _web_search(self, query: str) -> dict:
        """Server-side web lookup with a fast wttr.in path for weather queries."""
        q = query.lower()
        # Weather fast-path: if query contains weather terms and we have a location,
        # try wttr.in first (much cleaner than DDG HTML parsing).
        if any(k in q for k in ("weather", "temperature", "forecast", "rain", "sunny", "cloudy")):
            location = self.settings.location.strip() or None
            if location:
                wttr = await self._fetch_wttr(location)
                if wttr:
                    return {"ok": True, "message": wttr}
        # Fallback to DuckDuckGo.
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        }
        try:
            async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
                response = await client.get(
                    "https://html.duckduckgo.com/html/",
                    params={"q": query},
                )
                if response.status_code == 200:
                    results = self._parse_ddg_results(response.text)
                    if results:
                        return {"ok": True, "message": self._format_results(query, results)}
                response = await client.get(
                    "https://api.duckduckgo.com/",
                    params={"q": query, "format": "json", "no_html": 1},
                )
                response.raise_for_status()
                data = response.json()
                abstract = str(data.get("AbstractText") or "").strip()
                if abstract:
                    return {"ok": True, "message": f"Results for '{query}': {abstract}"}
        except Exception:
            log.exception("web search failed")
        return {"ok": False, "message": "I couldn't reach the web right now."}

    @staticmethod
    def _parse_ddg_results(page: str) -> list[tuple[str, str]]:
        def clean(raw: str) -> str:
            text = re.sub(r"<[^>]+>", "", raw)
            return html.unescape(text).strip()

        titles = re.findall(
            r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', page, flags=re.S | re.I
        )
        snippets = re.findall(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', page, flags=re.S | re.I
        ) or re.findall(
            r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', page, flags=re.S | re.I
        )
        results: list[tuple[str, str]] = []
        for index, title in enumerate(titles[:4]):
            snippet = snippets[index] if index < len(snippets) else ""
            results.append((clean(title), clean(snippet)))
        return results

    @staticmethod
    def _format_results(query: str, results: list[tuple[str, str]]) -> str:
        lines = [f"Results for '{query}':"]
        for title, snippet in results:
            line = title
            if snippet:
                line = f"{title}: {snippet}" if title else snippet
            lines.append(f"- {line}")
        return "\n".join(lines)[:1200]

    async def _device_command(self, name: str, **args: object) -> dict:
        if self._closed:
            return {"ok": False, "message": "The tablet is not connected."}
        self._command_seq += 1
        ident = f"cmd_{self._command_seq}"
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[ident] = future
        # NOTE: the envelope key must be "name", NOT "action" — media_control
        # carries its own inner "action" arg (play/pause/next/...), which would
        # silently overwrite an "action" envelope field in the dict.
        await self._send({"type": "command", "id": ident, "name": name, **args})
        if self._closed:
            self._pending.pop(ident, None)
            return {"ok": False, "message": "The tablet is not connected."}
        try:
            return await asyncio.wait_for(future, timeout=15.0)
        except asyncio.TimeoutError:
            log.warning("device command %s timed out", name)
            return {"ok": False, "message": "The tablet did not respond in time."}
        finally:
            self._pending.pop(ident, None)

    async def _speak_worker(self, queue: asyncio.Queue[str | None]) -> None:
        speaking = False
        while True:
            sentence = await queue.get()
            if sentence is None:
                break
            try:
                pcm = await self.tts.speak(sentence)
            except Exception:
                log.exception("tts failed for %r", sentence[:60])
                continue
            if not pcm:
                continue
            if not speaking:
                speaking = True
                await self._set_state(State.SPEAKING)
            self._audio_sent += len(pcm)
            await self._send_audio(pcm)

    # -------------------------------------------------------------------- helpers

    def _trim_history(self) -> None:
        limit = max(2, self.settings.history_turns * 2)
        if len(self.history) > limit:
            self.history = self.history[-limit:]

    async def _back_to_idle(self) -> None:
        self._segmenter = None
        self._followup = False
        self._pcm.clear()
        self._preroll.drain()
        self.wake.reset()
        await self._set_state(State.IDLE)

    async def _interrupt(self) -> None:
        """Cancel current TTS playback and transition to listening."""
        if self._utterance_task and not self._utterance_task.done():
            self._utterance_task.cancel()
            try:
                await self._utterance_task
            except asyncio.CancelledError:
                pass
        # Tell the tablet to stop audio playback immediately.
        await self._send({"type": "stop_audio"})
        # Drain any pending audio queue.
        self._pcm.clear()
        self._preroll.drain()
        self.wake.reset()
        await self._set_state(State.IDLE)

    async def _set_state(self, state: State) -> None:
        self.state = state
        await self._send({"type": "state", "value": state.value})

    async def _send(self, payload: dict) -> None:
        if self._closed:
            return
        try:
            await self.ws.send_json(payload)
        except Exception:
            self._closed = True

    async def _send_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        try:
            await self.ws.send_bytes(b"\x01" + pcm)
        except Exception:
            self._closed = True
