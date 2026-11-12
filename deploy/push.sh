#!/usr/bin/env bash
# Push the server to a fresh VPS and run it as a system service.
# Usage: ./deploy/push.sh <host> [user] [keyfile]
set -euo pipefail

HOST="${1:?usage: push.sh <host> [user] [key]}"
TARGET_USER="${2:-root}"
KEY="${3:-$HOME/.ssh/oracle-assistant.key}"
SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -i "$KEY" "$TARGET_USER@$HOST")

cd "$(dirname "$0")/.."

echo "== copying server to $TARGET_USER@$HOST"
"${SSH[@]}" 'mkdir -p ~/echo-assistant/server'
rsync -az --delete \
    --exclude '.venv' --exclude 'models' --exclude '__pycache__' --exclude '*.pyc' \
    -e "ssh -o StrictHostKeyChecking=accept-new -i $KEY" \
    server/ "$TARGET_USER@$HOST:echo-assistant/server/"

echo "== installing packages, venv, models (a few minutes)"
"${SSH[@]}" "TARGET_USER=$TARGET_USER bash -s" <<'REMOTE'
set -euo pipefail
if [ "$(id -u)" = 0 ]; then SUDO=""; else SUDO="sudo"; fi
$SUDO apt-get update -qq
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-venv python3-pip curl rsync ca-certificates >/dev/null

cd "$HOME/echo-assistant/server"
[ -f .env ] || cp .env.example .env
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
# openwakeword pins tflite-runtime (no py3.12+ wheels); we use its ONNX backend
.venv/bin/pip install -q --no-deps openwakeword==0.6.0
[ -f models/piper/en_US-lessac-medium.onnx ] || bash scripts/download_models.sh

# keep the relay token stable across re-deploys
current=$(sed -n 's/^ASSIST_TOKEN="\{0,1\}\([^"#]*\)"\{0,1\}$/\1/p' .env 2>/dev/null | head -1 || true)
if [ -z "${current:-}" ] && [ -f "$HOME/.assist_token" ]; then
    current=$(cat "$HOME/.assist_token")
fi
if [ -z "${current:-}" ]; then
    current=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
    if grep -q '^ASSIST_TOKEN=' .env 2>/dev/null; then
        sed -i "s|^ASSIST_TOKEN=.*|ASSIST_TOKEN=$current|" .env
    else
        printf '\nASSIST_TOKEN=%s\n' "$current" >> .env
    fi
elif ! grep -q "^ASSIST_TOKEN=$current$" .env 2>/dev/null; then
    if grep -q '^ASSIST_TOKEN=' .env 2>/dev/null; then
        sed -i "s|^ASSIST_TOKEN=.*|ASSIST_TOKEN=$current|" .env
    else
        printf '\nASSIST_TOKEN=%s\n' "$current" >> .env
    fi
fi
printf '%s' "$current" > "$HOME/.assist_token"

$SUDO tee /etc/systemd/system/assistant.service >/dev/null <<UNIT
[Unit]
Description=Echo Show voice assistant relay
After=network-online.target
Wants=network-online.target

[Service]
User=$TARGET_USER
WorkingDirectory=$HOME/echo-assistant/server
Environment=HOME=$HOME
ExecStart=$HOME/echo-assistant/server/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now assistant
$SUDO systemctl restart assistant

if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q 'Status: active'; then
    $SUDO ufw allow 8765/tcp >/dev/null
fi
REMOTE

sleep 5
echo "== health"
"${SSH[@]}" 'curl -s --max-time 8 http://127.0.0.1:8765/health; echo; systemctl is-active assistant'

TOKEN=$("${SSH[@]}" 'cat "$HOME/.assist_token"')
echo
echo "tablet relay:  ws://$HOST:8765/ws?token=$TOKEN"
echo "settings page: http://$HOST:8765/config?token=$TOKEN"
