#!/usr/bin/env python3
"""End-to-end test client: pretends to be the Echo Show tablet.

Synthesizes a wake phrase plus a question with Piper, streams it over the
WebSocket as 80 ms PCM chunks, prints every event, and saves the returned
speech to scripts/out.wav.
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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--text", default="Tell me a very short joke.")
    parser.add_argument("--manual", action="store_true", help="skip the wake phrase")
    parser.add_argument("--no-playback-done", action="store_true")
    parser.add_argument("--voice", default=str(ROOT / "models/piper/en_US-lessac-medium.onnx"))
    args = parser.parse_args()

    voice = PiperVoice.load(args.voice)
    phrase = args.text if args.manual else f"Hey Jarvis, {args.text}"
    speech = synth(voice, phrase)
    audio = np.concatenate([np.zeros(RATE, np.int16), speech, np.zeros(2 * RATE, np.int16)])
    audio = np.pad(audio, (0, (-len(audio)) % CHUNK))

    print(f"sending {len(audio) / RATE:.1f}s of audio: {phrase!r}")
    received = bytearray()
    saw_thinking = False
    done = asyncio.Event()
    reply = ""

    async with websockets.connect(args.url, max_size=None) as ws:

        async def sender() -> None:
            for offset in range(0, len(audio), CHUNK):
                chunk = audio[offset : offset + CHUNK].astype(np.int16).tobytes()
                await ws.send(chunk)
                await asyncio.sleep(0.08)

        async def receiver() -> None:
            nonlocal saw_thinking, reply
            async for message in ws:
                if isinstance(message, bytes):
                    if message and message[0] == 0x01:
                        received.extend(message[1:])
                    continue
                event = json.loads(message)
                kind = event.get("type")
                if kind == "delta":
                    print(event.get("text", ""), end="", flush=True)
                elif kind == "reply":
                    reply = event.get("text", "")
                else:
                    print(f"\n[{kind}] {json.dumps({k: v for k, v in event.items() if k != 'type'})}")
                if kind == "state" and event.get("value") == "thinking":
                    saw_thinking = True
                if kind == "state" and event.get("value") == "idle" and saw_thinking:
                    done.set()
                    return
                if kind == "audio_end" and not args.no_playback_done:
                    await ws.send(json.dumps({"type": "playback_done"}))
                if kind == "error":
                    done.set()
                    return

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        try:
            await asyncio.wait_for(done.wait(), timeout=90)
        except asyncio.TimeoutError:
            print("\nTIMEOUT waiting for idle", file=sys.stderr)
        for task in tasks:
            task.cancel()

    if received:
        out = Path(__file__).with_name("out.wav")
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(OUT_RATE)
            wav.writeframes(bytes(received))
        print(f"\nsaved {len(received) / 2 / OUT_RATE:.2f}s of reply audio -> {out}")
    print(f"reply: {reply!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
