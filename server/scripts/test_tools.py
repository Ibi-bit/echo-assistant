#!/usr/bin/env python3
"""Comprehensive tool-path test: exercises every tool the model can call.

Sends a wake phrase + request over the WebSocket, waits for the command/result
round trip, and reports which tools fired. The tablet side is faked — the
script answers every command itself — so this verifies the server-side loop,
tool schemas, and command/result handshake end-to-end without the tablet.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path

import numpy as np
import websockets
from piper import PiperVoice

ROOT = Path(__file__).resolve().parent.parent
RATE = 16000
OUT_RATE = 22050
CHUNK = 1280  # 80 ms


def synth(voice: PiperVoice, text: str) -> np.ndarray:
    pcm = b"".join(c.audio_int16_bytes for c in voice.synthesize(text))
    audio = np.frombuffer(pcm, np.int16).astype(np.float32)
    count = int(len(audio) * RATE / OUT_RATE)
    audio = np.interp(np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio)
    return audio.astype(np.int16)


async def run_test(phrase: str, voice: PiperVoice, url: str) -> dict:
    """Send one wake phrase, collect all events, return a summary dict."""
    speech = synth(voice, phrase)
    audio = np.concatenate([np.zeros(RATE, np.int16), speech, np.zeros(2 * RATE, np.int16)])
    audio = np.pad(audio, (0, (-len(audio)) % CHUNK))

    events: list[dict] = []
    commands: list[dict] = []
    received = bytearray()
    done = asyncio.Event()

    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "device_context",
            "text": (
                "Battery: 88%, charging.\n"
                "Device: Amzn Echo Show 8, Android 11 (SDK 30), uptime 1h.\n"
                "Audio: media 33%, alarm 85%.\n"
                "Network: Wi-Fi, IP 192.168.68.108.\n"
                "Rusty: foreground, panel lockscreen, screen on, brightness 100%.\n"
                "Spotify: paused on \"Test Track\" by Test Artist.\n"
                "Next alarm: 7:30 AM.\n"
                "Assistant-set timers: none."
            ),
        }))

        async def sender():
            for offset in range(0, len(audio), CHUNK):
                await ws.send(audio[offset:offset + CHUNK].astype(np.int16).tobytes())
                await asyncio.sleep(0.08)

        saw_thinking = False

        async def receiver():
            async for message in ws:
                if isinstance(message, bytes):
                    if message and message[0] == 0x01:
                        received.extend(message[1:])
                    continue
                event = json.loads(message)
                kind = event.get("type")
                events.append(event)
                if kind == "delta":
                    print(event.get("text", ""), end="", flush=True)
                elif kind == "command":
                    commands.append(event)
                    # Answer every command successfully.
                    await ws.send(json.dumps({
                        "type": "command_result",
                        "id": event.get("id"),
                        "ok": True,
                        "message": f"OK: {event.get('action')} {json.dumps(event.get('args', {}))}",
                    }))
                elif kind == "error":
                    print(f"\n[error] {event}", file=sys.stderr)
                    done.set()
                    return
                elif kind == "state" and event.get("value") == "thinking":
                    saw_thinking = True
                if kind == "state" and event.get("value") == "idle" and saw_thinking:
                    done.set()
                    return
                if kind == "audio_end":
                    await ws.send(json.dumps({"type": "playback_done"}))

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            await asyncio.wait_for(done.wait(), timeout=120)
        except asyncio.TimeoutError:
            print("\nTIMEOUT waiting for idle", file=sys.stderr)
        for task in tasks:
            task.cancel()

    return {
        "phrase": phrase,
        "commands": commands,
        "events": events,
        "reply_audio_bytes": len(received),
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--voice", default=str(ROOT / "models/piper/en_US-lessac-medium.onnx"))
    args = parser.parse_args()

    voice = PiperVoice.load(args.voice)

    tests = [
        # 1. Volume — absolute set
        "Hey Jarvis, set the volume to 42.",
        # 2. Volume — relative change
        "Hey Jarvis, turn the volume up.",
        # 3. Volume — mute
        "Hey Jarvis, mute the volume.",
        # 4. Timer
        "Hey Jarvis, set a timer for five minutes.",
        # 5. Alarm
        "Hey Jarvis, set an alarm for 7:30 AM.",
        # 6. Media control — pause
        "Hey Jarvis, pause Spotify.",
        # 7. Media control — next
        "Hey Jarvis, skip to the next track.",
        # 8. Device status query
        "Hey Jarvis, what is the battery level?",
    ]

    results = []
    for phrase in tests:
        print(f"\n{'='*70}\nTEST: {phrase}\n{'='*70}", flush=True)
        result = await run_test(phrase, voice, args.url)
        results.append(result)

    print(f"\n\n{'='*70}\nSUMMARY\n{'='*70}")
    for r in results:
        actions = [c.get("action") for c in r["commands"]]
        print(f"  {r['phrase']!r}\n    commands: {actions or 'NONE'}\n")

    failures = [r for r in results if not r["commands"]]
    if failures:
        print(f"FAILURES ({len(failures)}/{len(results)}):")
        for r in failures:
            print(f"  - {r['phrase']!r}")
        return 1
    print(f"ALL {len(results)} TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))