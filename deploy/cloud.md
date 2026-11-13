# Running the assistant in the cloud

The relay keeps one WebSocket open per tablet, so it must stay awake. That rules
out free PaaS tiers that sleep when idle (Render, Railway, Cloud Run) and points
at a small always-on VM.

| Option | Cost | Fit |
| --- | --- | --- |
| **Oracle Cloud Always Free — Ampere A1** | $0 | 4 OCPU / 24 GB RAM / 200 GB, ARM64. Best choice. |
| GCP e2-micro always free | $0 | x86, 1 GB RAM. The relay peaks around 1.2 GB, so it swaps. |
| Oracle AMD micro | $0 | 1 GB RAM, same problem. |
| Hetzner CX22 | ~€3.79/mo | 4 GB RAM, x86, zero setup friction. |
| Fly.io shared-cpu-1x | ~$3–5/mo | 512 MB is too small for Whisper. |

Everything below is for the Oracle Ampere shape. On Hetzner or any x86 host the
same steps work unchanged.

## 1. Create the instance

1. Oracle Cloud → Compute → Instances → Create.
2. Image **Ubuntu 24.04** (aarch64), shape **VM.Standard.A1.Flex**, 2–4 OCPU and
   6–12 GB RAM (all within Always Free).
3. Upload your SSH key, create, and note the public IP.
4. In the VCN security list allow ingress TCP **22**, **80**, **443** (and
   **8765** only if you skip TLS).
5. `ssh ubuntu@<public-ip>`

The ARM64 Python wheels all exist: `onnxruntime` 1.31.x (manylinux_2_28
aarch64), `ctranslate2` 4.6.x, `piper-tts`, `openwakeword` (pure Python).

## 2. Install

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git curl
git clone https://github.com/Ibi-bit/echo-assistant.git
cd echo-assistant/server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./scripts/download_models.sh
cp .env.example .env
chmod 600 .env
nano .env          # OpenRouter key, ASSIST_TOKEN, model
```

Start it by hand once to confirm:

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8765
curl http://127.0.0.1:8765/health
```

## 3. Keep it running

systemd — same unit as the desktop:

```bash
mkdir -p ~/.config/systemd/user
cp ../deploy/assistant.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now assistant
```

The user unit uses `%h`, so it works for any username. The cloud instance needs
no desktop session because of `enable-linger`.

Or Docker:

```bash
sudo apt install -y docker.io docker-compose-v2
cd ~/echo-assistant
sudo docker compose up -d --build
```

The compose file mounts only `server/models/whisper` so Whisper's download
survives rebuilds. Piper and wake-word models come from the image.

## 4. TLS

The tablet accepts `ws://` on the LAN, but a public host must use `wss://`;
the app rejects public `ws://`. Either option is free.

### Caddy + DuckDNS

1. Register a free subdomain at duckdns.org, e.g. `myassistant.duckdns.org`,
   pointing at the instance IP.
2. Install Caddy (`apt install caddy`) or uncomment the `caddy` service in
   `docker-compose.yml`.
3. `deploy/Caddyfile` gets you a certificate automatically:

   ```bash
   sudo ASSIST_DOMAIN=myassistant.duckdns.org caddy run --config deploy/Caddyfile
   ```

   Caddy needs ports 80/443 open. Keep 8765 closed at the cloud firewall;
   Caddy reaches the assistant over localhost or the compose network.

### Cloudflare Tunnel

No open ports at all:

```bash
cloudflared tunnel create assistant
cloudflared tunnel route dns assistant assistant.example.com
cloudflared tunnel run --url http://127.0.0.1:8765 assistant
```

Run it as a service with `sudo cloudflared service install`.

## 5. Lock it down

Set a long random `ASSIST_TOKEN` in `.env`:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Then the tablet relay URL becomes:

- `wss://myassistant.duckdns.org/ws?token=<token>`

and the settings page is `https://myassistant.duckdns.org/config`. It asks for
the token and sets a cookie after the first visit. The app moves a token in the
relay URL into an `Authorization` header, so it does not stay in access logs.
The token protects both the WebSocket and the `/config` page. Public cleartext
`ws://`/`http://` is rejected by the app and by the server; use `wss://` behind
a local Caddy or Cloudflare proxy. `push.sh` closes port 8765 in `ufw` and
opens only 80/443.

## 6. Point the tablet at it

Long-press the overlay card (or open the app) and enter the new relay URL.
Everything else stays server-side: no APK rebuild when you change model, wake
word, or voice from `/config`.

## Costs

With `deepseek/deepseek-v4-flash` as the primary model, a typical day of voice
use (a few dozen short turns) costs cents per month at $0.022/M input and
$0.40/M output. The free Nemotron fallbacks cover outages. The VM itself is $0
on Oracle Always Free.

## Tencent Cloud Lighthouse (what we actually use)

A 2 vCPU / 2 GB Lighthouse instance in Ubuntu 24.04 runs the whole stack in ~800 MB.
The app rejects public `ws://`, so put Caddy or Cloudflare in front (section 4)
and open ports 80/443 instead of exposing 8765. Then deploy from this machine
with:

```bash
ASSIST_DOMAIN=myassistant.duckdns.org \
  ./deploy/push.sh <public-ip> ubuntu ~/.ssh/oracle-assistant.key
```

The script prints the tablet `wss://` relay URL and the settings page URL. The
service is a system unit (`/etc/systemd/system/assistant.service`), so it starts
on boot and restarts on failure without any login.

### Deploy from GitHub Actions

`.github/workflows/deploy.yml` runs CI, then calls `deploy/push.sh` on pushes to
`main` that touch `server/` or `deploy/`. It can also be started manually from
the Actions tab.

Add these repository secrets:

- `DEPLOY_HOST` — public IP or hostname
- `DEPLOY_USER` — SSH user (defaults to `ubuntu` when unset)
- `DEPLOY_SSH_KEY` — private key for that user

Optional repository variable: `ASSIST_DOMAIN` (for example
`myassistant.duckdns.org`). The key needs SSH access and passwordless `sudo`,
because the script installs packages, restarts the system service, and edits
firewall rules. Use a dedicated deploy key, not your personal key. The job uses
the `production` environment, so you can add required reviewers there if you
want manual approval before each deploy.

The workflow does not copy your local `.env`; it preserves the server's
`~/echo-assistant/server/.env`. On a fresh host, create that file once with
`OPENROUTER_API_KEY` set (or run `push.sh` manually first). The deploy log does
not print the relay token; read it from `~/.assist_token` on the server.
`push.sh` does not install Caddy, so set up TLS first. Until the deploy secrets
are present, the deploy job skips with a warning instead of failing the build.
