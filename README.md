# Echo Show 8 voice assistant

A wake-word voice assistant for an Echo Show 8 (1st gen) running LineageOS, with all the AI on an always-on Linux desktop. The tablet is a thin audio terminal:
it streams 16 kHz microphone audio over the LAN and plays back whatever the desktop sends. Wake word, speech-to-text, the LLM, and text-to-speech all run server-side, so you can change any of them without rebuilding the APK.

Rusty (`dev.rusty.app`, the Spotify Connect / Home Assistant appliance) keeps the screen. The assistant runs as a background foreground-service and draws only a small floating card on top, so Rusty stays fullscreen, interactive and playing underneath.

```
  tablet (LineageOS)                         desktop
  ┌──────────────────┐   ws://LAN:8765    ┌──────────────────────────────┐
  │ Rusty (fullscreen)│                   │ openWakeWord  (hey_jarvis)   │
  │  └ overlay card   │                  │ faster-whisper (base.en)     │
  │ mic → PCM 16 kHz │ ─────────────────► │ OpenRouter (Nemotron 3.5)    │
  │ speaker ← PCM    │ ◄───────────────── │ Piper TTS                    │
  └──────────────────┘                    └──────────────────────────────┘
```

The model is `nvidia/nemotron-3.5-lightning:free` — text only, 1M context,
tool calling, free tier. Speech in and speech out are handled locally, so the free model only ever sees text.

## Server

Already installed in this checkout (`.venv` and models are present). To run:

```bash
cd echo-assistant/server
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Check it:

```bash
curl http://127.0.0.1:8765/health
```

`OPENROUTER_API_KEY` is read from the environment or from `server/.env` (gitignored, mode 600). If the key is missing the server refuses to start.

### Run it permanently

```bash
mkdir -p ~/.config/systemd/user
cp deploy/assistant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now assistant
systemctl --user status assistant
```

The desktop must stay awake. `loginctl enable-linger "$USER"` lets the user service run without an active login session.

### Rebuild models from scratch

```bash
server/scripts/download_models.sh
```

### Configuration

Copy `server/.env.example` to `server/.env` and edit. The useful knobs:

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3.5-lightning:free` | Any OpenRouter model id |
| `WAKE_WORDS` | `hey_jarvis` | Any bundled openWakeWord name: `hey_jarvis`, `alexa`, `hey_mycroft`, `hey_rhasspy`, `timer`, `weather` |
| `WAKE_THRESHOLD` | `0.5` | Raise to 0.6–0.7 if it wakes on the TV or loud music |
| `WHISPER_MODEL` | `base.en` | `small.en` is more accurate, ~2× slower |
| `PIPER_VOICE` | `models/piper/en_US-lessac-medium.onnx` | Download any voice from `rhasspy/piper-voices` |
| `END_SILENCE_MS` | `900` | How long a pause ends the question |
| `HISTORY_TURNS` | `8` | Conversation memory per tablet |
| `DISABLE_REASONING` | `true` | Keep `true` for Nemotron; otherwise it writes a long reasoning preamble before answering |
| `ASSIST_TOKEN` | empty | If set, clients must use `ws://host:8765/ws?token=...` |

## Tablet

The Show 8 (1st gen, model C7H6N3) is already on LineageOS. Build the APK:

```bash
# once: install JDK 17, Android SDK cmdline tools, then
sdkmanager "platform-tools" "platforms;android-35" "build-tools;35.0.0"
export ANDROID_HOME="$HOME/Android/Sdk"

cd echo-assistant/android
./build.sh assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Or open `android/` in Android Studio and press Run.

On first launch the app shows a small **setup dialog over Rusty** and asks for:

- **Microphone** access.
- **Display over other apps** — this is the floating-card permission.
- **Android permissions** (via adb):
  - `android.permission.RECORD_AUDIO`
  - `android.permission.SYSTEM_ALERT_WINDOW`
  - `com.android.alarm.permission.SET_ALARM`

Set the relay address (`ws://<desktop-lan-ip>:8765/ws`) and press **Start assistant**. Give the desktop a DHCP reservation so its LAN IP does not change; the tablet stores the address. The dialog closes and a compact card floats at the top of the screen. Rusty stays running, keeps the screen, and still receives every touch outside the card. The assistant never takes over the display.

- **Tap the card** to talk without the wake word (testing and follow-ups).
- **Long-press the card** to reopen the relay-address dialog.
- The service is a foreground service (`microphone`) with a low-importance notification, so Android keeps the mic alive while Rusty is the focused app.
- It starts on boot once all permissions are already granted. A transparent `BootActivity` does the start: Android 11 refuses microphone access to a foreground service started directly from `BOOT_COMPLETED`, but allows it when the service is started from a brief visible activity. Expect a few seconds of delay while the system finishes booting.

The app is deliberately **not a launcher** and does not request the HOME role. Rusty remains the appliance UI; install and update the assistant alongside it.

You can provision everything over adb, without touching the screen:

```bash
adb shell pm grant com.leonp.assistant android.permission.RECORD_AUDIO
adb shell appops set com.leonp.assistant SYSTEM_ALERT_WINDOW allow
adb shell pm grant com.leonp.assistant com.android.alarm.permission.SET_ALARM
adb shell am start -n com.leonp.assistant/.MainActivity \
    --es relay_url ws://192.168.1.50:8765/ws --ez start true
```

Use the desktop's LAN IP, not `127.0.0.1`. For a USB-only test, tunnel the port and point the app at localhost instead. This is only for testing: `adb reverse` does not survive a reboot or unplugging the cable. If 8765 is already in use on the device, map a different local port:

```bash
adb reverse tcp:8766 tcp:8765
adb shell am start -n com.leonp.assistant/.MainActivity \
    --es relay_url ws://127.0.0.1:8766/ws
```

### Audio while Rusty plays

Before speaking, the service asks Android for transient audio focus. Apps that honour focus pause or duck; Rusty's native Spotify receiver does not currently, so music may keep playing under the voice. Pause Spotify or use the volume keys if you need the reply louder. Rusty's own `/api/announce/text` endpoint does pause and resume Spotify, but it speaks with Rusty's TTS; switching to it is an option if the overlap becomes annoying.

## How the turn works

1. The tablet streams 80 ms PCM chunks continuously while the relay is connected.
2. `openWakeWord` scores each chunk (~1.2 ms on this desktop). On a hit the server sends `wake` + `listening` and starts a VAD segmenter.
3. When the user pauses for `END_SILENCE_MS`, the utterance goes to faster-whisper, then to OpenRouter.
4. Reply tokens stream to the overlay card and are cut into sentences; each sentence is synthesized by Piper and pushed as raw PCM 22.05 kHz. First audio usually arrives while the model is still writing the end of the reply.
5. The tablet reports `playback_done` when its speaker queue drains, and only then does the server go back to `idle` — this stops the assistant from waking itself on its own voice.

Reasoning is disabled in the request (`reasoning: {enabled: false}`). Without that, Nemotron spends the whole token budget on a visible reasoning preamble and never gets to the answer.

## Known limits

- **Free tier:** OpenRouter's free models are rate limited (about 20 requests/min, 50/day without credits, 1,000/day after a one-time $10 top-up). NVIDIA's free endpoint also logs requests to improve its products — don't feed it secrets.
- **One speaker at a time:** the server serialises Whisper and Piper across sessions, so a second tablet queues behind the first.
- **No barge-in yet:** saying the wake word while the assistant is speaking is ignored until playback drains. This is deliberate; adding echo cancellation and a cancel path is the next step.
- **No tools:** the model can't check live weather or control lights yet. Add OpenRouter tool definitions in `server/app/llm.py` and handle them in `pipeline.py` if you want that.