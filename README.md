# IADSS Signal Tracker

A TradingView webhook sequencer for [Freqtrade](https://www.freqtrade.io).

Listens for **three independent TradingView signals** that must fire in the correct order within a time window before a trade is placed. If they fire out of order or the window expires, the sequence resets — no trade is made.

Signal state is persisted in Redis so a container restart never loses progress mid-sequence.

---

## How it works

```
Step 1 → Mean Reversion signal fires     (/mr-buy or /mr-sell)
Step 2 → Confirmation signal fires       (/confirm-buy or /confirm-sell)
Step 3 → Trend/breakout signal fires     (/lb-buy or /lb-sell)  ← trade executes
```

All three must fire **in order** within `WINDOW_SECONDS` (default: 144000 s = 40 hours on a 4h chart).
If they arrive out of order or the window expires, the counter resets.

---

## Webhook endpoints

| Endpoint | Step | Direction |
|----------|------|-----------|
| `/mr-buy` | 1 | Buy — mean reversion long signal |
| `/confirm-buy` | 2 | Buy — confluence/confirmation |
| `/lb-buy` | 3 | Buy — trend/breakout (triggers trade) |
| `/mr-sell` | 1 | Sell — mean reversion short signal |
| `/confirm-sell` | 2 | Sell — confluence/confirmation |
| `/lb-sell` | 3 | Sell — trend/breakout (triggers 50% exit) |
| `/status` | — | Check current sequence state (token required) |
| `/health` | — | Health check (no auth, used by Docker) |

---

## TradingView alert message body

Use this exact JSON for **all six alerts** (just change the endpoint URL per alert):

```json
{"pair": "SOL/USD", "token": "YOUR_SECRET_TOKEN"}
```

> Your `SECRET_TOKEN` is in your `.env` file. Never share it.

---

## Setting up TradingView alerts

Create one alert per endpoint. The alert conditions are entirely up to you — use whichever indicators you trade with. The signal tracker only cares that the webhook fires; it doesn't inspect the indicator.

**For each alert:**
- Set **Trigger** to `Once per bar close`
- Set **Expiration** to `Open-ended`
- Enable **Webhook URL** and paste the endpoint
- Paste the JSON message body above (with your pair and token)

**Example mapping (use your own indicators):**

| What fires it | Endpoint |
|---------------|----------|
| Your mean reversion indicator → long condition | `https://signals.yourdomain.com/mr-buy` |
| Your confluence/confirmation indicator → long condition | `https://signals.yourdomain.com/confirm-buy` |
| Your trend/breakout indicator → crossing up | `https://signals.yourdomain.com/lb-buy` |
| Your mean reversion indicator → short condition | `https://signals.yourdomain.com/mr-sell` |
| Your confluence/confirmation indicator → short condition | `https://signals.yourdomain.com/confirm-sell` |
| Your trend/breakout indicator → crossing down | `https://signals.yourdomain.com/lb-sell` |

> The order matters. Step 1 must fire before Step 2, and Step 2 before Step 3.
> If Step 2 arrives before Step 1, nothing happens and the sequence stays at 0.

---

## Signal window

The window controls how long the sequence stays alive after Step 1.

| Chart timeframe | 10 candles | `WINDOW_SECONDS` |
|-----------------|------------|------------------|
| 4h | 40 hours | `144000` |
| 1h | 10 hours | `36000` |
| 15m | 2.5 hours | `9000` |

Set this in your `.env` file or when running `setup.sh`.

---

## Installation

### Prerequisites

- Docker and Docker Compose
- A domain name (free with Cloudflare)
- A Cloudflare account (free tier is fine)
- Exchange API keys (read + trade, never withdrawal)
- TradingView account with webhook alerts

---

### Choose your setup path

#### Path 1A — VPS (cloud server)
Best if you have a VPS. Caddy handles HTTPS automatically. Requires ports 80 and 443 open.

#### Path 1B — Self-hosted (Unraid / NAS / home server / Raspberry Pi)
No VPS needed. Uses Cloudflare Tunnel — a free outbound-only connection from your home network to Cloudflare's edge. No open ports, no port forwarding, works behind CGNAT.

---

### Part 1 — Get a domain and set up Cloudflare

1. Register a domain (or use one you already own)
2. Add it to Cloudflare (free account at cloudflare.com)
3. Point your domain's nameservers to Cloudflare's

You'll create two subdomains — one for the signal tracker, one for the Freqtrade UI.
Example: `signals.yourdomain.com` and `trade.yourdomain.com`

---

### Part 2A — VPS setup (skip if self-hosted)

1. Spin up a VPS (Ubuntu 22.04+, 1 CPU / 1 GB RAM minimum)
2. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
3. Open ports 80 and 443 in your firewall
4. In Cloudflare DNS, create two **A records** pointing to your VPS public IP:
   - `signals.yourdomain.com` → your VPS IP
   - `trade.yourdomain.com` → your VPS IP
5. Skip to **Part 3 — Install**

---

### Part 2B — Self-hosted / Cloudflare Tunnel setup (skip if VPS)

**Option A — You already have a Cloudflare Tunnel container running** (e.g. on Unraid via the Community App)

1. Note the Docker network your tunnel container runs on (on Unraid: open the container template → Network Type field)
2. In Cloudflare Zero Trust → Networks → Tunnels → your tunnel → Public Hostnames, add:
   - `signals.yourdomain.com` → `http://signal-tracker:5000`
   - `trade.yourdomain.com` → `http://freqtrade:8080`
3. You'll enter this network name when running `setup.sh`

**Option B — No cloudflared running yet**

1. Go to [Cloudflare Zero Trust](https://one.dash.cloudflare.com) → Networks → Tunnels → Create a tunnel
2. Give it a name, copy the tunnel token
3. Under Public Hostnames, add:
   - `signals.yourdomain.com` → `http://signal-tracker:5000`
   - `trade.yourdomain.com` → `http://freqtrade:8080`
4. Paste the token when `setup.sh` asks for it
5. In `docker-compose.selfhosted.yml`, uncomment the `cloudflared` service and remove the `networks` sections

---

### Part 3 — Get exchange API keys

**Kraken:**
1. Log in → Security → API → Create API key
2. Permissions: Query Funds, Query Orders, Create & Modify Orders, Cancel/Close Orders
3. **Never enable Withdraw**

**Binance:**
1. Log in → Profile → API Management → Create API
2. Enable: Enable Reading, Enable Spot & Margin Trading
3. **Never enable withdrawals**

---

### Part 4 — Create a Telegram bot (optional)

1. Open Telegram → search `@BotFather` → `/newbot`
2. Follow prompts, copy the bot token
3. Start a chat with your bot, then visit:
   `https://api.telegram.org/botYOUR_TOKEN/getUpdates`
4. Copy your `chat_id` from the response

---

### Part 5 — Install

```bash
# Clone the repo
git clone https://github.com/ballzac81/IADSS-Signal-Tracker.git
cd IADSS-Signal-Tracker

# Run setup (generates secrets, collects config, writes .env)
chmod +x setup.sh
./setup.sh
```

`setup.sh` will ask you:
- VPS or self-hosted?
- Your two domain names
- Cloudflare tunnel token (self-hosted only)
- Docker network name (self-hosted Option A only)
- Exchange name, API key, API secret
- Trading pair (default: SOL/USD)
- Signal window in seconds (default: 144000)
- Telegram token and chat ID (optional)

It will generate three random secrets automatically and write your `.env` file.

---

### Part 6 — Start

**VPS:**
```bash
docker compose up -d
```

**Self-hosted:**
```bash
docker compose -f docker-compose.selfhosted.yml up -d
```

**Unraid auto-start on boot** — add to `/boot/config/go`:
```bash
cd /mnt/user/appdata/IADSS-Signal-Tracker && docker compose -f docker-compose.selfhosted.yml up -d
```

---

### Part 7 — Verify

```bash
curl https://signals.yourdomain.com/health
# → {"status":"ok","persistence":"redis"}
```

Check signal state (replace with your token):
```
https://signals.yourdomain.com/status?token=YOUR_SECRET_TOKEN
```

---

### Part 8 — Freqtrade UI

Open `https://trade.yourdomain.com` in your browser.

- Username: `admin`
- Password: the `FREQTRADE_PASS` value shown at the end of `setup.sh`

---

### Part 9 — Set up TradingView alerts

See the [TradingView alerts section](#setting-up-tradingview-alerts) above.

After your first alert fires, check the status endpoint — you should see Step 1 registered.

---

## Going live

By default everything runs in **dry run mode** — no real money moves.

When you're satisfied with dry run performance:

1. Edit `user_data/config.json` → set `"dry_run": false`
2. Restart:
   ```bash
   # VPS
   docker compose restart freqtrade

   # Self-hosted
   docker compose -f docker-compose.selfhosted.yml restart freqtrade
   ```

---

## Monitoring

```bash
# All logs
docker compose logs -f

# Signal tracker only
docker compose logs -f signal-tracker

# Freqtrade only
docker compose logs -f freqtrade
```

---

## Adding more trading pairs

1. Add the pair to `user_data/config.json`:
   ```json
   "pair_whitelist": ["SOL/USD", "BTC/USD", "ETH/USD"]
   ```

2. Restart Freqtrade

3. Create new TradingView alerts for the new pair — same 6 endpoints, just change the pair in the message body:
   ```json
   {"pair": "BTC/USD", "token": "YOUR_SECRET_TOKEN"}
   ```

---

## Security

- Webhook endpoints require `SECRET_TOKEN` in every request body
- `/status` requires `?token=SECRET_TOKEN` in the URL query string
- `/health` is unauthenticated (used by Docker healthcheck only)
- Rate limiting: 20 requests/minute on webhooks, 60/hour on status
- No ports are exposed publicly — all traffic goes through Caddy (VPS) or Cloudflare Tunnel (self-hosted)
- `.env` is gitignored and never committed
- `user_data/` is gitignored — contains your live config and API keys
- **Never enable withdrawal permissions on your exchange API keys**

---

## Telegram commands

Once your bot is connected, send these in your Telegram chat:

| Command | Action |
|---------|--------|
| `/status` | Open trades |
| `/profit` | Profit summary |
| `/balance` | Current balance |
| `/stop` | Stop the bot |
| `/start` | Start the bot |

---

## Troubleshooting

**Health check fails:**
```bash
docker compose ps          # check all containers are Up
docker compose logs signal-tracker
```

**Signals not registering:**
- Check the webhook URL in TradingView matches exactly (including `/mr-buy` not `/mrbuy`)
- Check the JSON body has `"pair"` and `"token"` keys
- Verify your token matches `SECRET_TOKEN` in `.env`

**Freqtrade not connecting:**
- Check `docker compose logs freqtrade`
- Verify `user_data/config.json` was generated (run `ls user_data/`)

**Cloudflare Tunnel shows as inactive:**
- On Unraid: check your cloudflared Community App container is running
- Verify the tunnel hostname matches the container name exactly (`signal-tracker`, `freqtrade`)

**Redis not connecting:**
```bash
docker compose exec signal-tracker curl http://localhost:5000/health
# "persistence":"file" means Redis is down — check IADSS_redis container
```

---

## File structure

```
IADSS-Signal-Tracker/
├── signal_tracker.py              # Flask webhook server
├── Dockerfile                     # Container build
├── requirements.txt               # Python dependencies
├── docker-compose.yml             # VPS path (Caddy + all services)
├── docker-compose.selfhosted.yml  # Self-hosted path (Cloudflare Tunnel)
├── Caddyfile                      # Auto-HTTPS config (VPS only)
├── config.json                    # Freqtrade config template (safe, no secrets)
├── .env.example                   # Config template — copy to .env
├── setup.sh                       # Interactive setup script
└── strategies/
    └── WebhookStrategy.py         # Freqtrade strategy file
```

`user_data/` is created by `setup.sh` and is gitignored. It contains your live config, logs, and trade database.

---

## Disclaimer

This software is for educational and informational purposes only. It is not financial advice.

Trading cryptocurrencies involves significant risk of loss. Past performance is not indicative of future results. You may lose some or all of your capital.

By using this software you acknowledge that:
- You are solely responsible for your trading decisions
- The authors accept no liability for financial losses
- Never trade with money you cannot afford to lose
- Seek independent financial advice before trading

**Use at your own risk.**
