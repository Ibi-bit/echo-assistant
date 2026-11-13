"""Server-rendered settings page at ``/config``.

There is no build step and no JavaScript framework: one HTML document that
writes the fields you change into ``server/.env`` (comments and unknown keys
preserved, file kept mode 600) and then can restart the systemd user service so
the new values load. The tablet reconnects on its own.
"""
from __future__ import annotations

import hmac
import html
import os
import signal
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .config import SERVER_DIR, is_local_request, settings

router = APIRouter()
ENV_PATH = SERVER_DIR / ".env"
COOKIE = "assist_token"

# (env key, label, input kind, hint). Kind "password" keeps the stored value
# when submitted blank so the page never has to echo a secret back.
SECTIONS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    (
        "Model",
        [
            ("OPENROUTER_MODEL", "Model", "text", "OpenRouter model id used for replies."),
            (
                "OPENROUTER_FALLBACK_MODELS",
                "Fallback models",
                "text",
                "Comma-separated, tried in order if the primary fails before replying.",
            ),
            ("OPENROUTER_API_KEY", "OpenRouter API key", "password", "Leave blank to keep the current key."),
            ("DISABLE_REASONING", "Disable reasoning", "checkbox", "Turn off for models that require reasoning."),
        ],
    ),
    (
        "Voice",
        [
            (
                "WAKE_WORDS",
                "Wake words",
                "text",
                "openWakeWord names: hey_jarvis, alexa, hey_mycroft, hey_rhasspy.",
            ),
            ("WAKE_THRESHOLD", "Wake threshold", "number", "0 to 1. Raise it if music triggers the wake word."),
            ("WHISPER_MODEL", "Whisper model", "text", "tiny.en, base.en, small.en and so on."),
            ("PIPER_VOICE", "Piper voice", "text", "Path under server/models/piper."),
            ("END_SILENCE_MS", "End silence (ms)", "number", "Silence that ends your sentence."),
            ("NO_SPEECH_TIMEOUT_MS", "No-speech timeout (ms)", "number", "How long to wait before giving up."),
        ],
    ),
    (
        "Assistant",
        [
            ("ASSIST_LOCATION", "Home location", "text", "City used when you ask about the weather."),
            ("SYSTEM_PROMPT", "System prompt", "textarea", "The assistant's standing instructions."),
            ("HISTORY_TURNS", "History turns", "number", "How many past messages to remember."),
            ("MAX_REPLY_TOKENS", "Max reply tokens", "number", "Caps how long a reply can be."),
        ],
    ),
    (
        "Remote access",
        [
            (
                "ASSIST_TOKEN",
                "Relay token",
                "password",
                "Optional. The tablet's relay URL then needs ?token=... Leave blank to keep.",
            ),
        ],
    ),
]

TRUTHY = {"1", "true", "yes", "on"}

# Effective defaults shown when a key is not present in .env yet.
SETTING_ATTRS: dict[str, str] = {
    "OPENROUTER_MODEL": "model",
    "OPENROUTER_FALLBACK_MODELS": "fallback_models",
    "OPENROUTER_API_KEY": "api_key",
    "DISABLE_REASONING": "disable_reasoning",
    "WAKE_WORDS": "wake_words",
    "WAKE_THRESHOLD": "wake_threshold",
    "WHISPER_MODEL": "whisper_model",
    "PIPER_VOICE": "piper_voice",
    "END_SILENCE_MS": "end_silence_ms",
    "NO_SPEECH_TIMEOUT_MS": "no_speech_timeout_ms",
    "ASSIST_LOCATION": "location",
    "SYSTEM_PROMPT": "system_prompt",
    "HISTORY_TURNS": "history_turns",
    "MAX_REPLY_TOKENS": "max_reply_tokens",
    "ASSIST_TOKEN": "auth_token",
}
MODEL_SUGGESTIONS = [
    "deepseek/deepseek-v4-flash",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "qwen/qwen3.7-flash",
    "inclusionai/ling-3.1-flash",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3.5-lightning:free",
]


# ------------------------------------------------------------------ .env I/O


def _read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _quote(value: str) -> str:
    if value == "" or any(c in value for c in ' "\'#\n\t'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    return value


def _write_env(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        export = key.startswith("export ")
        if export:
            key = key[7:].strip()
        if key in remaining:
            out.append(f"{'export ' if export else ''}{key}={_quote(remaining.pop(key))}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={_quote(value)}")
    tmp = ENV_PATH.with_name(".env.tmp")
    tmp.write_text("\n".join(out).rstrip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_PATH)


# ------------------------------------------------------------------- helpers


def _current_values() -> dict[str, str]:
    """Keys as stored in .env, filled with the live settings for anything absent."""
    values = _read_env()
    for key, attr in SETTING_ATTRS.items():
        if key in values:
            continue
        value = getattr(settings, attr)
        if isinstance(value, bool):
            values[key] = "true" if value else "false"
        elif isinstance(value, (tuple, list)):
            values[key] = ", ".join(value)
        else:
            values[key] = str(value)
    return values


def _service_active() -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "assistant"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _supplied_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (
        request.query_params.get("token")
        or request.headers.get("x-assist-token")
        or request.cookies.get(COOKIE)
        or ""
    )


def _authorized(request: Request) -> bool:
    if not settings.auth_token:
        peer = request.client.host if request.client else ""
        return is_local_request(peer, request.headers.get("x-forwarded-for", ""))
    supplied = _supplied_token(request)
    return bool(supplied) and hmac.compare_digest(
        supplied.encode(), settings.auth_token.encode()
    )


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc == request.headers.get("host", "")


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    return request.url.scheme == "https" or forwarded.lower() == "https"


def _schedule_restart() -> None:
    def restart() -> None:
        # systemd (Restart=always) and Docker's restart policy revive us. This
        # works for user units, system units and containers without needing
        # permission to call systemctl from the service account.
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Timer(0.5, restart).start()


def _validated(form: dict[str, list[str]]) -> tuple[dict[str, str], list[str]]:
    updates: dict[str, str] = {}
    errors: list[str] = []
    for _, fields in SECTIONS:
        for key, label, kind, _hint in fields:
            if key not in form:
                # A missing checkbox means unchecked; missing text means "keep".
                if kind == "checkbox":
                    updates[key] = "false"
                continue
            value = (form.get(key) or [""])[0].strip()
            if kind == "checkbox":
                updates[key] = "true" if value.lower() in TRUTHY else "false"
                continue
            if kind == "password":
                if value:
                    updates[key] = value
                continue
            if kind == "number":
                if key in ("WAKE_THRESHOLD",):
                    try:
                        number = float(value)
                    except ValueError:
                        errors.append(f"{label}: must be a number.")
                        continue
                    if not 0.0 <= number <= 1.0:
                        errors.append(f"{label}: must be between 0 and 1.")
                        continue
                    updates[key] = f"{number:g}"
                else:
                    try:
                        number = int(value)
                    except ValueError:
                        errors.append(f"{label}: must be a whole number.")
                        continue
                    if number < 0:
                        errors.append(f"{label}: must not be negative.")
                        continue
                    updates[key] = str(number)
                continue
            if kind == "textarea":
                value = " ".join(value.split())
            if key == "OPENROUTER_MODEL" and not value:
                errors.append("Model: cannot be empty.")
                continue
            updates[key] = value
    return updates, errors


# ---------------------------------------------------------------------- HTML


def _field_html(key: str, label: str, kind: str, hint: str, values: dict[str, str]) -> str:
    value = values.get(key, "")
    safe_hint = html.escape(hint)
    safe_label = html.escape(label)
    if kind == "checkbox":
        checked = " checked" if value.strip().lower() in TRUTHY else ""
        control = (
            f'<input type="hidden" name="{key}" value="false">'
            f'<input type="checkbox" id="{key}" name="{key}" value="true"{checked}>'
        )
        return (
            f'<div class="row check"><label for="{key}">{control}'
            f"<span>{safe_label}</span></label>"
            f'<p class="hint">{safe_hint}</p></div>'
        )
    if kind == "textarea":
        control = f'<textarea id="{key}" name="{key}" rows="5">{html.escape(value)}</textarea>'
    else:
        shown = "" if kind == "password" else html.escape(value)
        placeholder = ""
        if kind == "password" and value:
            placeholder = f' placeholder="saved ···{html.escape(value[-4:])}"'
        list_attr = ""
        if key == "OPENROUTER_MODEL" or key == "OPENROUTER_FALLBACK_MODELS":
            list_attr = ' list="models"'
        control = (
            f'<input type="{kind}" id="{key}" name="{key}" value="{shown}"'
            f'{placeholder}{list_attr} autocomplete="off" spellcheck="false">'
        )
    return (
        f'<div class="row"><label for="{key}">{safe_label}</label>{control}'
        f'<p class="hint">{safe_hint}</p></div>'
    )


def _page(values: dict[str, str], message: str = "", error: bool = False) -> str:
    sections = []
    for title, fields in SECTIONS:
        rows = "".join(_field_html(key, label, kind, hint, values) for key, label, kind, hint in fields)
        sections.append(f'<section><h2>{html.escape(title)}</h2>{rows}</section>')

    notice = ""
    if message:
        notice = f'<div class="notice{" bad" if error else ""}">{html.escape(message)}</div>'

    suggestions = "".join(f'<option value="{html.escape(m)}">' for m in MODEL_SUGGESTIONS)
    active = html.escape(_service_active())
    status = (
        f'<div class="status"><span class="dot {active}"></span> service {active}'
        f' · model <code>{html.escape(values.get("OPENROUTER_MODEL", ""))}</code>'
        f' · wake <code>{html.escape(values.get("WAKE_WORDS", ""))}</code>'
        f' · threshold <code>{html.escape(values.get("WAKE_THRESHOLD", ""))}</code></div>'
    )
    host = html.escape(f"{settings.host}:{settings.port}")
    token_state = "on" if settings.auth_token else "off (local network only)"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Echo Show Assistant · settings</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 20px 64px;
    background: radial-gradient(1200px 600px at 20% -10%, #1b2b45 0%, #0b1220 55%, #070b13 100%);
    color: #e6edf7; font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  main {{ max-width: 780px; margin: 0 auto; }}
  header h1 {{ margin: 0 0 4px; font-size: 24px; letter-spacing: -0.01em; }}
  header p {{ margin: 0 0 18px; color: #8fa3bd; }}
  .status {{
    margin: 0 0 20px; padding: 10px 14px; border-radius: 10px;
    background: #0f1a2b; border: 1px solid #1e2d45; color: #a9bcd4; font-size: 13px;
  }}
  .status code {{ color: #cfe3ff; }}
  .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #64748b; margin-right: 6px; }}
  .dot.active {{ background: #34d399; box-shadow: 0 0 8px #34d399; }}
  .dot.failed, .dot.inactive {{ background: #f87171; }}
  section {{
    margin: 0 0 16px; padding: 18px 20px; border-radius: 14px;
    background: rgba(15, 26, 43, 0.85); border: 1px solid #1e2d45;
  }}
  h2 {{ margin: 0 0 14px; font-size: 13px; text-transform: uppercase; letter-spacing: 0.12em; color: #7dd3fc; }}
  .row {{ margin: 0 0 14px; }}
  .row:last-child {{ margin-bottom: 0; }}
  label {{ display: block; font-weight: 600; font-size: 14px; margin-bottom: 6px; }}
  .row.check label {{ display: flex; align-items: center; gap: 8px; margin: 0 0 4px; }}
  .row.check input[type=checkbox] {{ width: 18px; height: 18px; accent-color: #38bdf8; }}
  input[type=text], input[type=password], input[type=number], textarea {{
    width: 100%; padding: 10px 12px; border-radius: 9px;
    background: #0a1422; color: #e6edf7; border: 1px solid #26374f;
    font: inherit; outline: none;
  }}
  input:focus, textarea:focus {{ border-color: #38bdf8; box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.16); }}
  textarea {{ resize: vertical; }}
  .hint {{ margin: 6px 0 0; font-size: 12.5px; color: #7b8fa9; }}
  .notice {{ margin: 0 0 16px; padding: 12px 14px; border-radius: 10px; background: #0f2a22; border: 1px solid #1d5c45; color: #a7f3d0; }}
  .notice.bad {{ background: #2a1414; border-color: #6b2323; color: #fecaca; }}
  .actions {{ display: flex; gap: 10px; align-items: center; margin-top: 4px; }}
  button {{
    padding: 11px 18px; border-radius: 10px; border: 0; cursor: pointer;
    font: inherit; font-weight: 600; color: #04121f;
    background: linear-gradient(180deg, #7dd3fc, #38bdf8);
  }}
  button.ghost {{ background: transparent; color: #a9bcd4; border: 1px solid #2a3b55; }}
  button:hover {{ filter: brightness(1.08); }}
  footer {{ margin-top: 22px; color: #64748b; font-size: 12.5px; }}
  footer code {{ color: #93b4d8; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>Echo Show Assistant</h1>
    <p>Settings for the relay at <code>{host}</code>. Changes are written to <code>server/.env</code>.</p>
  </header>
  {notice}
  {status}
  <form method="post" action="/config/save">
    {''.join(sections)}
    <div class="actions">
      <button type="submit" name="action" value="save_restart">Save &amp; restart</button>
      <button class="ghost" type="submit" name="action" value="save">Save only</button>
    </div>
    <footer>
      Save &amp; restart reloads the models and voice in a few seconds; the tablet reconnects on its own.
      Page auth: {html.escape(token_state)}. Secrets are never shown back to the browser.
    </footer>
  </form>
  <datalist id="models">{suggestions}</datalist>
</main>
</body>
</html>"""


# --------------------------------------------------------------------- routes


def _respond(request: Request, body: str, status: int = 200) -> HTMLResponse:
    response = HTMLResponse(body, status_code=status)
    token = request.query_params.get("token")
    if settings.auth_token and token and hmac.compare_digest(
        token.encode(), settings.auth_token.encode()
    ):
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
        )
    return response


@router.post("/config/login", response_model=None)
async def config_login(request: Request) -> Response:
    form = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    supplied = (form.get("token") or [""])[0]
    if settings.auth_token and hmac.compare_digest(
        supplied.encode(), settings.auth_token.encode()
    ):
        response = RedirectResponse("/config", status_code=303)
        response.set_cookie(
            COOKIE,
            supplied,
            httponly=True,
            samesite="lax",
            secure=_is_https(request),
        )
        return response
    return HTMLResponse(_login_page("Token did not match."), status_code=401)


@router.get("/config", response_model=None)
async def config_page(request: Request) -> Response:
    if not _authorized(request):
        return HTMLResponse(_login_page(), status_code=401)
    saved = request.query_params.get("saved")
    if saved == "restart":
        return _respond(request, _restarting_page())
    return _respond(request, _page(_current_values(), "Saved." if saved else ""))


@router.post("/config/save", response_model=None)
async def config_save(request: Request) -> Response:
    if not _authorized(request):
        return HTMLResponse(_login_page(), status_code=401)
    if not _same_origin(request):
        return HTMLResponse("Cross-origin request rejected.", status_code=403)
    form = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    updates, errors = _validated(form)
    if errors:
        values = _current_values()
        values.update(
            {k: v for k, v in updates.items() if k not in ("OPENROUTER_API_KEY", "ASSIST_TOKEN")}
        )
        return _respond(request, _page(values, " ".join(errors), error=True), status=400)
    _write_env(updates)
    if (form.get("action") or [""])[0] == "save_restart":
        _schedule_restart()
        return _respond(request, _restarting_page())
    return RedirectResponse("/config?saved=1", status_code=303)


@router.post("/config/restart", response_model=None)
async def config_restart(request: Request) -> Response:
    if not _authorized(request):
        return HTMLResponse(_login_page(), status_code=401)
    if not _same_origin(request):
        return HTMLResponse("Cross-origin request rejected.", status_code=403)
    _schedule_restart()
    return _respond(request, _restarting_page())


def _restarting_page() -> str:
    return """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="4;url=/config">
<title>Restarting…</title>
<style>body{background:#070b13;color:#e6edf7;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0}
p{color:#8fa3bd}</style></head><body><div style="text-align:center">
<h1>Restarting the assistant…</h1><p>This page returns in a few seconds.</p></div></body></html>"""


def _login_page(message: str = "") -> str:
    notice = f'<p class="bad">{html.escape(message)}</p>' if message else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Token required</title>
<style>body{{background:#070b13;color:#e6edf7;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#0f1a2b;border:1px solid #1e2d45;border-radius:14px;padding:24px;width:320px}}
input{{width:100%;padding:10px 12px;border-radius:9px;background:#0a1422;color:#e6edf7;border:1px solid #26374f;font:inherit;margin:10px 0}}
button{{width:100%;padding:11px;border-radius:10px;border:0;background:#38bdf8;color:#04121f;font:inherit;font-weight:600;cursor:pointer}}
p{{color:#8fa3bd;margin:0}}.bad{{color:#fecaca;margin:8px 0 0}}</style></head><body>
<form method="post" action="/config/login"><h2>Relay token</h2>
<p>This assistant is protected by ASSIST_TOKEN.</p>{notice}
<input type="password" name="token" autofocus autocomplete="off">
<button type="submit">Open settings</button></form></body></html>"""
