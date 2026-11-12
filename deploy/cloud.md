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

The compose file mounts `server/models` so Whisper's download survives rebuilds.

## 4. TLS

The tablet accepts `ws://` on the LAN, but a public IP should use `wss://`.
Either option is free.

### Caddy + DuckDNS

1. Register a free subdomain at duckdns.org, e.g. `myassistant.duckdns.org`,
   pointing at the instance IP.
2. Install Caddy (`apt install caddy`) or uncomment the `caddy` service in
   `docker-compose.yml`.
3. `deploy/Caddyfile` gets you a certificate automatically:

   ```bash
   sudo ASSIST_DOMAIN=myassistant.duckdns.org caddy run --config deploy/Caddyfile
   ```

   Caddy needs ports 80/443 open. With Caddy in front, bind uvicorn to
   `127.0.0.1` instead of `0.0.0.0` (`HOST=127.0.0.1` in `.env`).

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

and the settings page is `https://myassistant.duckdns.org/config?token=<token>`
(it sets a cookie after the first visit). The token protects both the WebSocket
and the `/config` page. Keep port 8765 closed in the cloud firewall when Caddy
or Cloudflare is in front.

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
Open TCP 8765 in the Lighthouse console (**防火墙 / Firewall → Add rule**, custom TCP),
then deploy from this machine with:

```bash
./deploy/push.sh <public-ip> ubuntu ~/.ssh/oracle-assistant.key
```

The script prints the tablet relay URL (`ws://IP:8765/ws?token=...`) and the settings
page URL. The service is a system unit (`/etc/systemd/system/assistant.service`), so it
starts on boot and restarts on failure without any login.
