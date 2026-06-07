# IADSS Signal Tracker

A TradingView webhook signal sequencer that connects to Freqtrade and executes trades only when three specific indicators fire **in the correct order** within a set time window.

> ⚠️ **Disclaimer:** This software is for educational purposes only. Trading cryptocurrencies involves significant risk of loss. Never trade with money you cannot afford to lose. The authors accept no liability for any financial losses. Seek independent financial advice before trading.

---

## What it does

Most trading bots act on a single signal. IADSS requires three signals to arrive in sequence before doing anything:

1. **Mean Reversion** — the price has stretched away from its average
2. **Buy/Sell signal** — your primary entry/exit indicator confirms
3. **Trend Change** — the trend is shifting in your favour

Only when all three fire **in order** within your time window does IADSS send a trade to Freqtrade. If they arrive out of order, or the window expires, the sequence resets and nothing happens. This dramatically reduces false entries.

```
TradingView alert → IADSS (checks order + timing) → Freqtrade → Exchange
```

---

## Choose your setup path

IADSS needs to run 24/7 on a machine with a public HTTPS address so TradingView can reach it. There are two ways to achieve this:

| | Path A — Cloud VPS | Path B — Self-hosted |
|---|---|---|
| **Hardware** | Cloud server (~$5–10/month) | Your own machine (Unraid, NAS, PC, Pi) |
| **Running cost** | $5–10/month | Electricity only |
| **Domain needed** | Yes (~$10/year) | Yes (~$10/year) |
| **Reverse proxy** | Caddy (auto HTTPS) | Cloudflare Tunnel (free) |
| **Works behind home router/CGNAT** | N/A | ✅ Yes |
| **Difficulty** | Moderate | Moderate |

**Not sure which to pick?**
- Already have a home server, NAS, Unraid box, or Raspberry Pi running 24/7? → **Path B**
- Don't have a home server or want the simplest setup? → **Path A**

---

## What you'll need (both paths)

| Requirement | Cost | Notes |
|---|---|---|
| A domain name | ~$10/year | Two subdomains for webhooks + UI |
| Docker | Free | Runs the containers |
| Exchange account (Kraken, Binance, etc.) | Free | API keys — trading permissions only |
| TradingView Pro account | ~$15/month | Required for webhook alerts |
| Telegram bot | Free | Optional, for trade notifications |
| **Path A only:** Cloud VPS | ~$5–10/month | Ubuntu 22.04, 2 GB RAM |
| **Path B only:** Cloudflare account | Free | For the tunnel |

---

## Part 1A — Cloud VPS setup

*Skip to Part 1B if you're self-hosting.*

### Create your VPS

**Recommended providers:** [Hetzner](https://www.hetzner.com) (cheapest, EU), [DigitalOcean](https://www.digitalocean.com), [Vultr](https://www.vultr.com)

1. Sign up and create a new server — choose **Ubuntu 22.04 LTS**, cheapest tier (1 CPU / 2 GB RAM)
2. Copy the server's **public IP address** (looks like `123.45.67.89`)

### Connect to your server

```bash
ssh root@YOUR_SERVER_IP
```

### Open firewall ports

In your provider's dashboard, open ports **80** and **443** for the server.

### Install Docker

```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
docker --version   # should print Docker version 26.x.x
```

**→ Skip to Part 2 (Domain Setup)**

---

## Part 1B — Self-hosted setup (Unraid, NAS, home server, Raspberry Pi)

This path uses **Cloudflare Tunnel** — a free service that creates a secure connection between Cloudflare's global network and your home machine. No port forwarding, no exposed ports, works even if your ISP uses carrier-grade NAT (CGNAT).

### Supported hardware

- **Unraid** (6.9+) — runs Docker natively, terminal access built in
- **Synology NAS** — via Container Manager + SSH
- **Raspberry Pi 4/5** — running Raspberry Pi OS or Ubuntu
- **Any always-on PC or server** — running Ubuntu, Debian, or similar
- **TrueNAS SCALE** — Docker support built in

The machine needs to be on and connected to the internet whenever you want the bot to be active.

### Install Docker (skip if already installed)

**Unraid:** Docker is built in. Go to **Settings → Docker** and make sure it's enabled.

**Raspberry Pi / Ubuntu / Debian:**
```bash
curl -fsSL https://get.docker.com | sh
docker --version
```

**Synology:** Install **Container Manager** from the Package Center, then enable SSH in Control Panel → Terminal & SNMP.

### Set up Cloudflare Tunnel

Cloudflare Tunnel replaces the need for Caddy and a VPS. It handles HTTPS automatically.

1. Sign up for a free [Cloudflare account](https://cloudflare.com)
2. Add your domain to Cloudflare:
   - In the Cloudflare dashboard click **Add a Site**, enter your domain
   - Follow the steps to change your domain's nameservers to Cloudflare's
   - This takes 5–30 minutes
3. Go to **Zero Trust** (in the left sidebar) → **Networks** → **Tunnels**
4. Click **Create a tunnel** → choose **Cloudflared** → give it a name (e.g. `iadss`)
5. Copy the **tunnel token** — it's a long string starting with `eyJ...`
6. Click **Next** and configure two **Public Hostnames**:

   | Subdomain | Domain | Service |
   |---|---|---|
   | `signals` | `yourdomain.com` | `http://signal-tracker:5000` |
   | `trade` | `yourdomain.com` | `http://freqtrade:8080` |

7. Save the tunnel

Your tunnel token goes in your `.env` file as `CLOUDFLARE_TUNNEL_TOKEN`. The setup script will ask for it.

> **Note:** You do NOT need to create DNS A records when using Cloudflare Tunnel — Cloudflare creates them automatically.

### Unraid-specific: running via terminal

Unraid has a built-in terminal. Click the terminal icon in the top-right of the Unraid UI, or SSH in:

```bash
ssh root@YOUR_UNRAID_IP
```

All the git and docker compose commands in the rest of this guide run in this terminal.

**→ Continue to Part 2**

---

## Part 2 — Get a domain name

Both paths need a domain. A cheap option is [Porkbun](https://porkbun.com) or [Namecheap](https://www.namecheap.com) — `.com` costs ~$10/year, `.xyz` is often under $2/year.

**Path A (VPS + Caddy):** Create two DNS **A records** pointing at your VPS IP:

| Name | Type | Value |
|---|---|---|
| `signals` | A | `YOUR_VPS_IP` |
| `trade` | A | `YOUR_VPS_IP` |

Check propagation with: `nslookup signals.yourdomain.com`

**Path B (Cloudflare Tunnel):** No DNS records needed — Cloudflare creates them when you configure the tunnel hostnames in step 6 of Part 1B.

---

## Part 3 — Get exchange API keys

IADSS needs API keys to place trades. **Only grant trading permissions — never withdrawal permissions.**

### Kraken

1. Log in → your name (top right) → **Security** → **API** → **Generate New Key**
2. Name it `IADSS`
3. Enable: **Query Funds**, **Query Open Orders**, **Create & Modify Orders**, **Cancel/Close Orders**
4. Leave **Withdraw Funds** unchecked
5. Save the **API Key** and **Private Key**

### Binance

1. Log in → profile icon → **API Management** → **Create API** → **System generated**
2. Label it `IADSS`, complete 2FA
3. Enable only **Enable Spot & Margin Trading** — leave **Enable Withdrawals** unchecked
4. Add your server/home IP to the IP restriction list
5. Save the **API Key** and **Secret Key**

---

## Part 4 — Create a Telegram bot (optional)

1. Open Telegram → search **@BotFather** → send `/newbot`
2. Choose a name and username (username must end in `bot`)
3. Save the **token** BotFather sends (e.g. `123456789:ABCdef...`)
4. Start a chat with your new bot
5. Get your Chat ID — open this URL in a browser (replace `YOUR_TOKEN`):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
   Send your bot any message, refresh the URL, find `"chat":{"id":` — that number is your Chat ID

---

## Part 5 — Install IADSS

On your VPS, home server, Unraid terminal, or Pi:

### Clone the repo

```bash
cd ~
git clone https://github.com/ballzac81/IADSS-Signal-Tracker.git
cd IADSS-Signal-Tracker
```

### Run the setup script

```bash
chmod +x setup.sh
./setup.sh
```

The script asks:

- **Setup type** — VPS (Caddy) or self-hosted (Cloudflare Tunnel)
- **Signal domain** — e.g. `signals.yourdomain.com`
- **Freqtrade domain** — e.g. `trade.yourdomain.com`
- **Cloudflare tunnel token** — (self-hosted only) from Part 1B step 5
- **Exchange name** — `kraken`, `binance`, etc.
- **API Key and Secret** — from Part 3
- **Trading pair** — e.g. `SOL/USD`
- **Window seconds** — how long all 3 signals have to arrive

**Window seconds guide:**

| Chart timeframe | Candle window | WINDOW_SECONDS |
|---|---|---|
| 1h | 10 candles | 36000 |
| 4h | 10 candles | 144000 |
| 1d | 10 candles | 864000 |

When done, the script prints your Freqtrade password and webhook URLs. Save these.

### Start everything

**Path A (VPS):**
```bash
docker compose up -d
```

**Path B (self-hosted):**
```bash
docker compose -f docker-compose.selfhosted.yml up -d
```

### Unraid tip — auto-start on boot

Unraid starts Docker containers automatically if they have the **Autostart** flag set. Since we're using docker compose, add this to make it restart on Unraid reboots:

```bash
# Add to /boot/config/go (runs on every Unraid boot)
echo "cd /root/IADSS-Signal-Tracker && docker compose -f docker-compose.selfhosted.yml up -d" >> /boot/config/go
```

### Verify it's running

```bash
docker compose ps   # or docker compose -f docker-compose.selfhosted.yml ps
```

You should see four containers running. Test the signal tracker:

```bash
curl https://signals.yourdomain.com/status?token=YOUR_SECRET_TOKEN
```

You should get a JSON response with `"step": 0` for both buy and sell.

---

## Part 6 — Set up TradingView alerts

### Prerequisites

- TradingView **Pro** plan or higher (free accounts cannot send webhooks)
- Your three indicators already on your chart

### Create the 6 alerts

Repeat these steps for each of the 6 endpoints:

1. On your chart, click the **Alert** button (clock icon, top toolbar)
2. Set **Condition** to your indicator and the signal it fires on
3. Set **Options** to **Once Per Bar Close**
4. Under **Notifications**, enable **Webhook URL** and enter the URL for this step (table below)
5. Set the **Message** to:
   ```json
   {"pair": "SOL/USD", "token": "YOUR_SECRET_TOKEN"}
   ```
   Replace `SOL/USD` with your pair and `YOUR_SECRET_TOKEN` with the value from your `.env`
6. Click **Create**

### Webhook URLs

| Alert | Webhook URL |
|---|---|
| Mean Reversion Buy | `https://signals.yourdomain.com/mr-buy` |
| Buy Signal | `https://signals.yourdomain.com/signal-buy` |
| Trend Change Buy | `https://signals.yourdomain.com/trend-buy` |
| Mean Reversion Sell | `https://signals.yourdomain.com/mr-sell` |
| Sell Signal | `https://signals.yourdomain.com/signal-sell` |
| Trend Change Sell | `https://signals.yourdomain.com/trend-sell` |

### Test an alert manually

```bash
curl -X POST https://signals.yourdomain.com/mr-buy \
  -H "Content-Type: application/json" \
  -d '{"pair": "SOL/USD", "token": "YOUR_SECRET_TOKEN"}'
```

Expected response: `{"status": "ok", "reason": "ok"}`

---

## Part 7 — Access the Freqtrade UI

Open `https://trade.yourdomain.com` in your browser.

- **Username:** `admin`
- **Password:** printed by `setup.sh` (also in `.env` as `FREQTRADE_PASS`)

IADSS starts in **dry run mode** — it simulates trades without spending real money. Run it for a few days before going live.

---

## Part 8 — Go live

When you're satisfied with dry run performance:

1. Edit `user_data/config.json`:
   ```json
   "dry_run": false
   ```
2. Restart Freqtrade:
   ```bash
   docker compose restart freqtrade
   # or: docker compose -f docker-compose.selfhosted.yml restart freqtrade
   ```

> ⚠️ Make sure your exchange account has funds in the correct currency before going live.

---

## Monitoring

### Check signal sequence state

```bash
curl "https://signals.yourdomain.com/status?token=YOUR_SECRET_TOKEN"
```

### Useful commands

```bash
# View live logs from all containers
docker compose logs -f

# View a specific container's logs
docker compose logs -f signal-tracker
docker compose logs -f freqtrade

# Restart everything
docker compose restart

# Stop everything
docker compose down

# Update to latest images
docker compose pull && docker compose up -d
```

Add `-f docker-compose.selfhosted.yml` after `docker compose` if you're on Path B.

### Telegram commands

| Command | Description |
|---|---|
| `/status` | Open trades and current profit |
| `/profit` | Profit summary |
| `/balance` | Current exchange balance |
| `/stop` | Stop the bot |
| `/start` | Start the bot |

---

## Adding more trading pairs

1. Add the pair to `user_data/config.json`:
   ```json
   "pair_whitelist": ["SOL/USD", "BTC/USD", "ETH/USD"]
   ```
2. Restart Freqtrade:
   ```bash
   docker compose restart freqtrade
   ```
3. Create 6 new TradingView alerts using the same webhook URLs but with the new pair in the message:
   ```json
   {"pair": "BTC/USD", "token": "YOUR_SECRET_TOKEN"}
   ```

---

## Position sizing

- Each buy uses **50% of available balance** (`tradable_balance_ratio: 0.5`)
- Each sell exits **50% of the current position**
- Multiple buys on the same pair are supported (pyramiding)

To change: edit `tradable_balance_ratio` in `user_data/config.json` and restart.

---

## Security

- **HTTPS everywhere** — Caddy (Path A) or Cloudflare Tunnel (Path B) handle TLS. All traffic is encrypted.
- **No direct port exposure** — only Caddy/Cloudflare is your public entry point. All other containers are internal only.
- **Token authentication** — every webhook requires your `SECRET_TOKEN`. `/status` requires it as a query parameter.
- **Rate limiting** — webhook endpoints are capped at 20 requests/minute per IP.
- **Secrets in `.env`** — never hardcoded, gitignored.
- **Redis persistence** — signal state survives container restarts.
- **Exchange API safety** — never enable withdrawal permissions. If the server is compromised, an attacker can trade but cannot withdraw funds.
- **Path B bonus** — Cloudflare Tunnel means zero open ports on your home network. Your router firewall stays completely closed.

---

## Troubleshooting

### Caddy isn't getting a TLS certificate (Path A)

- Verify DNS: `nslookup signals.yourdomain.com` should return your VPS IP
- Make sure ports 80 and 443 are open in your cloud firewall
- Check logs: `docker compose logs caddy`

### Cloudflare Tunnel isn't connecting (Path B)

- Check logs: `docker compose -f docker-compose.selfhosted.yml logs cloudflared`
- Make sure the tunnel token in `.env` is correct and the tunnel is Active in the Cloudflare dashboard
- Confirm the Public Hostnames in the Cloudflare tunnel config point to `http://signal-tracker:5000` and `http://freqtrade